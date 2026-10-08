package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/graph"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// These tests pin the owner-scoped slow path: L5 edges per owner, every citation
// kept, watermarks that survive a restart, bounded arcs and schemas, and the L1
// mirror of a superseded preference.

// liveRows returns the live docs of a layer that belong to exactly one owner.
func liveRows(t *testing.T, store *MemoryStore, layer memory.Layer, user, agent string) []DocIndex {
	t.Helper()
	all, _ := store.List(layer, "", "", 1000, 0, false)
	var out []DocIndex
	for _, d := range all {
		if d.UserID == user && d.AgentID == agent {
			out = append(out, d)
		}
	}
	return out
}

// Two owners who mention the same entity get two nodes and one edge each, and
// the edge cites only its own owner's conversations.
func TestConsolidatedEdgesAreOwnerScoped(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice runs skyhook")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice tunes skyhook port")
	seedSourced(t, srv.store, "bob", "b1", "rb-1", "bob runs skyhook")
	seedSourced(t, srv.store, "bob", "b1", "rb-2", "bob tunes skyhook port")

	var log promptLog
	mock := scopeMock(t, &log, func(prompt string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{Knowledge: []CitedRelation{{
			From: "skyhook", Relation: "listens_on", To: "4471", Evidence: factIDs(prompt),
		}}})
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Edges != 2 {
		t.Errorf("Edges = %d, want 2: one new edge per owner", rep.Edges)
	}
	g := srv.store.Graph()
	if n, e := g.CountsScoped(graphScopeFor("alice", "")); n != 2 || e != 1 {
		t.Errorf("alice nodes/edges = %d/%d, want 2/1", n, e)
	}
	if n, e := g.CountsScoped(graphScopeFor("bob", "")); n != 2 || e != 1 {
		t.Errorf("bob nodes/edges = %d/%d, want 2/1", n, e)
	}
	_, edges := g.SnapshotScoped(graphScopeFor("alice", ""), 0)
	if len(edges) != 1 {
		t.Fatalf("alice edges = %d, want 1", len(edges))
	}
	for _, src := range edges[0].Sources {
		if src != "ra-1" && src != "ra-2" {
			t.Errorf("alice's edge cites %q, which belongs to another owner", src)
		}
	}
	if len(edges[0].Sources) != 2 {
		t.Errorf("alice's edge sources = %v, want both of her conversations", edges[0].Sources)
	}

	// A later pass that cites the same triple from a new conversation extends
	// alice's edge: it is corroborated, not created, so Edges stays at 0.
	seedSourced(t, srv.store, "alice", "a1", "ra-3", "alice skyhook again")
	rep, err = c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Edges != 0 {
		t.Errorf("second pass Edges = %d, want 0: the edge already existed", rep.Edges)
	}
	_, edges = g.SnapshotScoped(graphScopeFor("alice", ""), 0)
	if len(edges) != 1 || len(edges[0].Sources) != 3 {
		t.Errorf("after re-citation alice's edge = %+v, want one edge with three sources", edges)
	}
}

// Every conversation an edge cites is protected from raw decay, not only the
// first one that created it.
func TestDecayRawProtectsEverySourceOfEdge(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	old := time.Now().UTC().Add(-40 * 24 * time.Hour).Format(time.RFC3339)
	r1, r2, uncited := newID(), newID(), newID()
	for _, id := range []string{r1, r2, uncited} {
		if err := srv.store.Add(memory.L2Raw, id, "raw "+id, map[string]string{
			"user_id": "u", "agent_id": "a", "ts": old,
		}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := srv.store.Graph().AddEdgeWithSources("u", "a", "x", "r", "y", []string{r1, r2}); err != nil {
		t.Fatal(err)
	}

	c := NewConsolidator(srv.store, nil, time.Hour, 7*24*time.Hour, 200)
	pruned, protected := c.decayRaw()
	if pruned != 1 || protected != 2 {
		t.Errorf("decayRaw() = (%d,%d), want (1,2): both cited conversations kept", pruned, protected)
	}
	_, n := srv.store.List(memory.L2Raw, "", "", 1, 0, false)
	rows, _ := srv.store.List(memory.L2Raw, "", "", n, 0, false)
	remaining := map[string]bool{}
	for _, d := range rows {
		remaining[d.ID] = true
	}
	if !remaining[r1] || !remaining[r2] {
		t.Error("a conversation cited by a live edge was decayed")
	}
	if remaining[uncited] {
		t.Error("the uncited raw row was not decayed")
	}
}

// A pass whose context has already ended makes no LLM call, and reports the
// owners it did not reach instead of pretending it finished.
func TestConsolidationStopsOnCancelledContext(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice two")
	seedSourced(t, srv.store, "bob", "b1", "rb-1", "bob one")
	seedSourced(t, srv.store, "bob", "b1", "rb-2", "bob two")

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 0 {
		t.Errorf("LLM calls = %d after cancellation, want 0", n)
	}
	if len(rep.SkippedOwners) != 2 {
		t.Errorf("SkippedOwners = %v, want both owners named", rep.SkippedOwners)
	}
	if len(rep.Errors) == 0 || !strings.Contains(strings.Join(rep.Errors, " "), "stopped") {
		t.Errorf("Errors = %v, want the stop reported", rep.Errors)
	}
}

// The watermark is persisted: a restarted consolidator does not re-run owners
// whose facts are unchanged, and does run the one that gained a fact.
func TestWatermarkSurvivesRestart(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice two")
	seedSourced(t, srv.store, "bob", "b1", "rb-1", "bob one")
	seedSourced(t, srv.store, "bob", "b1", "rb-2", "bob two")

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	llm := NewLLMClient(mock.URL, "k", "m")

	first := NewConsolidator(srv.store, llm, time.Hour, 0, 200)
	if _, err := first.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 2 {
		t.Fatalf("first pass LLM calls = %d, want 2", n)
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(srv.store.indexPath), consolidateStateFile)); err != nil {
		t.Fatalf("watermark file not written: %v", err)
	}

	restarted := NewConsolidator(srv.store, llm, time.Hour, 0, 200)
	rep, err := restarted.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 2 {
		t.Errorf("restarted pass made %d new calls, want 0", n-2)
	}
	if rep.OwnersUnchanged != 2 || rep.OwnersRun != 0 {
		t.Errorf("unchanged=%d run=%d, want 2/0", rep.OwnersUnchanged, rep.OwnersRun)
	}

	seedSourced(t, srv.store, "alice", "a1", "ra-3", "alice three")
	again := NewConsolidator(srv.store, llm, time.Hour, 0, 200)
	rep, err = again.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 3 {
		t.Errorf("LLM calls after alice changed = %d, want 3", n)
	}
	if rep.OwnersRun != 1 || rep.OwnersUnchanged != 1 {
		t.Errorf("run=%d unchanged=%d, want 1/1", rep.OwnersRun, rep.OwnersUnchanged)
	}
}

// A pass cut short leaves the next pass to begin at the owner it did not reach,
// and a full pass moves the starting owner on by one. The cut is made inside the
// first LLM call, so alice's call fails and bob is the first owner not reached.
func TestCursorResumesAndRotatesOwners(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for _, who := range []string{"alice", "bob", "carol"} {
		seedSourced(t, srv.store, who, "x1", who+"-1", who+" one")
		seedSourced(t, srv.store, who, "x1", who+"-2", who+" two")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cut := true
	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		if cut {
			cut = false
			cancel()
		}
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.SkippedOwners) != 2 {
		t.Fatalf("SkippedOwners = %v, want bob and carol", rep.SkippedOwners)
	}
	if c.cursor != 1 {
		t.Errorf("cursor after cut pass = %d, want 1 (bob, the first owner not reached)", c.cursor)
	}

	// The resumed pass starts at bob. Alice's call failed, so she is still owed a
	// pass and comes last after the wrap-around.
	before := len(log.all())
	if _, err := c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	prompts := log.all()[before:]
	if len(prompts) != 3 || !strings.Contains(prompts[0], "bob one") {
		t.Fatalf("resumed pass made %d calls, first prompt does not name bob", len(prompts))
	}
	if !strings.Contains(prompts[2], "alice one") {
		t.Errorf("alice, whose pass failed, was not retried last")
	}
	if c.cursor != 2 {
		t.Errorf("cursor after full pass = %d, want 2", c.cursor)
	}
}

// A new arc replaces the owner's previous consolidation arc, so arcs do not pile
// up one per pass. The other owners' arcs are untouched.
func TestArcIsReplacedNotAccumulated(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice two")
	seedSourced(t, srv.store, "bob", "b1", "rb-1", "bob one")
	seedSourced(t, srv.store, "bob", "b1", "rb-2", "bob two")

	arc := func(text string) *httptest.Server {
		var log promptLog
		return scopeMock(t, &log, func(prompt string) (int, string) {
			cons := Consolidation{Arc: &text}
			if strings.Contains(prompt, "bob") {
				cons.Arc = nil
			}
			return http.StatusOK, chatBody(t, cons)
		})
	}
	m1 := arc("alice arc one")
	defer m1.Close()
	c1 := NewConsolidator(srv.store, NewLLMClient(m1.URL, "k", "m"), time.Hour, 0, 200)
	if _, err := c1.Once(context.Background()); err != nil {
		t.Fatal(err)
	}

	seedSourced(t, srv.store, "alice", "a1", "ra-3", "alice three")
	m2 := arc("alice arc two")
	defer m2.Close()
	c2 := NewConsolidator(srv.store, NewLLMClient(m2.URL, "k", "m"), time.Hour, 0, 200)
	if _, err := c2.Once(context.Background()); err != nil {
		t.Fatal(err)
	}

	arcs := 0
	for _, d := range liveRows(t, srv.store, memory.L4Summary, "alice", "a1") {
		if d.Meta["kind"] != "arc" {
			continue
		}
		arcs++
		if d.Content != "alice arc two" {
			t.Errorf("live arc = %q, want the newest one", d.Content)
		}
	}
	if arcs != 1 {
		t.Errorf("live consolidation arcs for alice = %d, want 1", arcs)
	}
}

// A schema whose normalised text matches a stored one is not written again. A
// refinement is written and retires the schema it names, and an ID the owner
// does not have is ignored.
func TestSchemaDedupeAndRefinement(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice two")

	m1 := scopeMock(t, &promptLog{}, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{Schemas: []ConsolidatedSchema{
			{Pattern: "Ports are declared explicitly."},
		}})
	})
	defer m1.Close()
	c1 := NewConsolidator(srv.store, NewLLMClient(m1.URL, "k", "m"), time.Hour, 0, 200)
	if _, err := c1.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	schemas := liveRows(t, srv.store, memory.L6Schema, "alice", "a1")
	if len(schemas) != 1 {
		t.Fatalf("schemas after first pass = %d, want 1", len(schemas))
	}
	oldID := schemas[0].ID

	seedSourced(t, srv.store, "alice", "a1", "ra-3", "alice three")
	var log promptLog
	m2 := scopeMock(t, &log, func(prompt string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{Schemas: []ConsolidatedSchema{
			{Pattern: "  ports are DECLARED explicitly "},
			{Pattern: "Ports are always declared explicitly in service config", Supersedes: []string{oldID, "not-mine"}},
		}})
	})
	defer m2.Close()
	c2 := NewConsolidator(srv.store, NewLLMClient(m2.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c2.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if prompts := log.all(); len(prompts) != 1 || !strings.Contains(prompts[0], "schema_id="+oldID) {
		t.Errorf("the existing schema was not shown to the model")
	}
	if rep.Schemas != 1 {
		t.Errorf("Schemas = %d, want 1: the normalised duplicate is not written", rep.Schemas)
	}
	live := liveRows(t, srv.store, memory.L6Schema, "alice", "a1")
	if len(live) != 1 || live[0].Content != "Ports are always declared explicitly in service config" {
		t.Errorf("live schemas = %+v, want only the refinement", live)
	}
	if n := len(rep.Errors); n != 0 {
		t.Errorf("unexpected errors: %v", rep.Errors)
	}
}

// A preference fact mirrored to L1 is superseded in L1 when a merge absorbs it,
// and the merged preference is mirrored under its new text.
func TestMergedPreferenceReplacesItsL1Mirror(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	promoteExtraction(srv.store, &Extraction{
		Facts: []Fact{{Data: "prefers tabs", Layer: "user_preferences"}},
	}, "alice", "a1", "raw-p1")
	promoteExtraction(srv.store, &Extraction{
		Facts: []Fact{{Data: "prefers tabs in go", Layer: "user_preferences"}},
	}, "alice", "a1", "raw-p2")

	facts := liveRows(t, srv.store, memory.L3Fact, "alice", "a1")
	if len(facts) != 2 {
		t.Fatalf("seeded facts = %d, want 2", len(facts))
	}
	mock := scopeMock(t, &promptLog{}, func(prompt string) (int, string) {
		ids := factIDs(prompt)
		return http.StatusOK, chatBody(t, Consolidation{Merges: []Merge{{
			Text: "prefers tabs everywhere", Layer: "user_preferences", Supersedes: ids[:2],
		}}})
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Merged != 1 || rep.Dropped != 2 {
		t.Fatalf("Merged=%d Dropped=%d, want 1/2", rep.Merged, rep.Dropped)
	}
	live := liveRows(t, srv.store, memory.L1Profile, "alice", "a1")
	if len(live) != 1 || live[0].Content != "prefers tabs everywhere" {
		t.Errorf("live L1 profile = %+v, want only the merged preference", live)
	}
	all, _ := srv.store.ListAll(memory.L1Profile, "alice", "a1", 100, 0, false)
	old := 0
	for _, d := range all {
		if d.Content == "prefers tabs" || d.Content == "prefers tabs in go" {
			old++
			if d.Meta["superseded_by"] == "" {
				t.Errorf("old L1 mirror %q was not superseded", d.Content)
			}
		}
	}
	if old != 2 {
		t.Errorf("old L1 mirrors in history = %d, want 2", old)
	}
}

// graphScopeFor builds the owner filter the graph tests read with.
func graphScopeFor(user, agent string) graph.Scope {
	return graph.Scope{UserID: user, AgentID: agent}
}
