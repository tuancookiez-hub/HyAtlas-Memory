package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Consolidation rewrites memory, so two guarantees are pinned here. First, one
// owner's facts are never reasoned over with another's. Second, a merge, drop or
// L5 edge no longer deletes its inputs; it supersedes them, and the provenance
// survives.

// promptLog records each consolidation prompt, so a test can check what the
// model was shown. The mutex keeps the record race-free across handler goroutines.
type promptLog struct {
	mu      sync.Mutex
	prompts []string
}

func (p *promptLog) add(s string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.prompts = append(p.prompts, s)
}

func (p *promptLog) all() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.prompts...)
}

// scopeMock answers each consolidation call with respond(prompt), which returns
// the HTTP status and body. It records every prompt it was sent.
func scopeMock(t *testing.T, log *promptLog, respond func(prompt string) (int, string)) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Messages []struct {
				Content string `json:"content"`
			} `json:"messages"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || len(req.Messages) == 0 {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		prompt := req.Messages[0].Content
		log.add(prompt)
		status, body := respond(prompt)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = fmt.Fprint(w, body)
	}))
}

// chatBody wraps content the way an OpenAI-style reply carries it. A struct is
// sent as JSON; a plain string is sent as-is, which a test uses for garbage.
func chatBody(t *testing.T, content any) string {
	t.Helper()
	inner, err := json.Marshal(content)
	if err != nil {
		t.Fatal(err)
	}
	out, err := json.Marshal(map[string]any{"choices": []any{
		map[string]any{"message": map[string]any{"content": string(inner)}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	return string(out)
}

// factLine matches one fact in the consolidation prompt: "- id=<id> | turn=...".
var factLine = regexp.MustCompile(`(?m)^- id=(\S+) \|`)

func factIDs(prompt string) []string {
	var ids []string
	for _, m := range factLine.FindAllStringSubmatch(prompt, -1) {
		ids = append(ids, m[1])
	}
	return ids
}

// seedSourced writes one L3 fact owned by user/agent and extracted from the L2
// row source, the way promoteExtraction does.
func seedSourced(t *testing.T, store *MemoryStore, user, agent, source, text string) string {
	t.Helper()
	id := newID()
	if err := store.Add(memory.L3Fact, id, text, map[string]string{
		"user_id": user, "agent_id": agent, "source_id": source,
		"ts": time.Now().UTC().Format(time.RFC3339),
	}); err != nil {
		t.Fatalf("seed %q: %v", text, err)
	}
	return id
}

// mergeFirstTwo is a model stand-in that merges the first two facts it is shown,
// so each owner's pass produces a merge from its own IDs.
func mergeFirstTwo(t *testing.T) func(string) (int, string) {
	return func(prompt string) (int, string) {
		ids := factIDs(prompt)
		var cons Consolidation
		if len(ids) >= 2 {
			cons.Merges = []Merge{{Text: "merged " + ids[0], Supersedes: ids[:2]}}
		}
		return http.StatusOK, chatBody(t, cons)
	}
}

// ---- Fix 1: consolidate per owner ----

// Two owners' facts must never share a prompt. A third owner with one fact has
// nothing to reconcile, so it gets no call at all.
func TestConsolidateNeverShowsTwoOwnersInOneCall(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	alice := map[string]bool{
		seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice likes tea"): true,
		seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice uses vim"):  true,
	}
	bob := map[string]bool{
		seedSourced(t, srv.store, "bob", "b1", "rb-1", "bob likes coffee"): true,
		seedSourced(t, srv.store, "bob", "b1", "rb-2", "bob uses emacs"):   true,
	}
	seedSourced(t, srv.store, "carol", "c1", "rc-1", "carol is alone")

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.Errors) != 0 {
		t.Errorf("unexpected errors: %v", rep.Errors)
	}

	prompts := log.all()
	if len(prompts) != 2 {
		t.Fatalf("LLM calls = %d, want 2 (one per owner with >=2 facts)", len(prompts))
	}
	sawAlice, sawBob := false, false
	for _, p := range prompts {
		ids := factIDs(p)
		if len(ids) != 2 {
			t.Errorf("prompt shows %d facts, want 2: %v", len(ids), ids)
		}
		switch {
		case alice[ids[0]] && alice[ids[1]]:
			sawAlice = true
		case bob[ids[0]] && bob[ids[1]]:
			sawBob = true
		default:
			t.Errorf("prompt mixes owners or shows an unexpected fact: %v", ids)
		}
	}
	if !sawAlice || !sawBob {
		t.Errorf("alice call seen=%v, bob call seen=%v; want both", sawAlice, sawBob)
	}
}

// A reply that names another owner's fact must not merge or corroborate across
// owners. The IDs are outside each call's batch, so the live-set and batch guards
// drop them, and each owner has only one of the two facts it cites.
func TestConsolidateCannotMergeOrCorroborateAcrossOwners(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	a0 := seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice fact zero")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice fact one")
	b0 := seedSourced(t, srv.store, "bob", "b1", "rb-1", "bob fact zero")
	seedSourced(t, srv.store, "bob", "b1", "rb-2", "bob fact one")

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{
			Merges: []Merge{{Text: "cross-owner merge", Supersedes: []string{a0, b0}}},
			Knowledge: []CitedRelation{{
				From: "x", Relation: "y", To: "z", Evidence: []string{a0, b0},
			}},
		})
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Merged != 0 || rep.Dropped != 0 {
		t.Errorf("Merged=%d Dropped=%d, want 0/0: a cross-owner merge must not apply", rep.Merged, rep.Dropped)
	}
	if rep.Edges != 0 {
		t.Errorf("Edges = %d, want 0: one owner's evidence must not corroborate an edge", rep.Edges)
	}
	if _, total := srv.store.List(memory.L3Fact, "", "", 1, 0, false); total != 4 {
		t.Errorf("live facts = %d, want 4 untouched", total)
	}
	if _, edges := srv.store.Graph().Snapshot(0); len(edges) != 0 {
		t.Errorf("%d edge(s) written from cross-owner evidence", len(edges))
	}
}

// Each owner's merge is written under that owner's scope, and the Report sums
// across owners. Two agents under one user count as two scopes.
func TestConsolidateWritesUnderOwnerScopeAndSumsReport(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice two")
	seedSourced(t, srv.store, "alice", "a2", "ra-3", "alice agent-two one")
	seedSourced(t, srv.store, "alice", "a2", "ra-4", "alice agent-two two")
	seedSourced(t, srv.store, "bob", "b1", "rb-1", "bob one")
	seedSourced(t, srv.store, "bob", "b1", "rb-2", "bob two")

	var log promptLog
	mock := scopeMock(t, &log, mergeFirstTwo(t))
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Merged != 3 || rep.Dropped != 6 {
		t.Errorf("Merged=%d Dropped=%d, want 3 and 6 summed across three scopes", rep.Merged, rep.Dropped)
	}
	if len(rep.Errors) != 0 {
		t.Errorf("unexpected errors: %v", rep.Errors)
	}
	if n := len(log.all()); n != 3 {
		t.Errorf("LLM calls = %d, want 3", n)
	}

	all, _ := srv.store.ListAll(memory.L3Fact, "", "", 100, 0, false)
	merged := 0
	for _, m := range all {
		if !strings.HasPrefix(m.Content, "merged ") {
			continue
		}
		merged++
		absorbed := 0
		for _, d := range all {
			if d.Meta["superseded_by"] != m.ID {
				continue
			}
			absorbed++
			if d.UserID != m.UserID || d.AgentID != m.AgentID {
				t.Errorf("merged %s is %s/%s but absorbed %s is %s/%s",
					m.ID, m.UserID, m.AgentID, d.ID, d.UserID, d.AgentID)
			}
		}
		if absorbed != 2 {
			t.Errorf("merged %s absorbed %d facts, want 2", m.ID, absorbed)
		}
	}
	if merged != 3 {
		t.Errorf("merged facts = %d, want 3", merged)
	}
}

// A failure in one owner's pass is reported under that owner, and the other
// owners' passes still run and apply.
func TestConsolidateScopedErrorDoesNotStopOtherOwners(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice two")
	bob1 := seedSourced(t, srv.store, "bob", "b1", "rb-1", "bob one")
	seedSourced(t, srv.store, "bob", "b1", "rb-2", "bob two")

	var log promptLog
	mock := scopeMock(t, &log, func(prompt string) (int, string) {
		if strings.Contains(prompt, bob1) {
			return http.StatusOK, chatBody(t, "not json at all")
		}
		return mergeFirstTwo(t)(prompt)
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Merged != 1 {
		t.Errorf("Merged = %d, want 1 from alice's pass", rep.Merged)
	}
	if len(rep.Errors) != 1 || !strings.HasPrefix(rep.Errors[0], `user_id="bob" agent_id="b1": `) {
		t.Errorf("Errors = %v, want one error prefixed with bob's scope", rep.Errors)
	}
}

// ---- Fix 3: supersede instead of delete ----

// mergeAndDrop runs one pass over a single owner with four facts. It merges the
// first two and drops the third, then returns the IDs.
type consolidated struct {
	srv                *Server
	a, b, d, e, merged string
}

func mergeAndDrop(t *testing.T) consolidated {
	t.Helper()
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	var c consolidated
	c.srv = srv
	c.a = seedSourced(t, srv.store, "u", "a", "raw-1", "port is 4472")
	c.b = seedSourced(t, srv.store, "u", "a", "raw-2", "port is 4471")
	c.d = seedSourced(t, srv.store, "u", "a", "raw-3", "uses tabs")
	c.e = seedSourced(t, srv.store, "u", "a", "raw-4", "likes go")

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{
			Merges: []Merge{{Text: "the port is 4471", Supersedes: []string{c.a, c.b}}},
			Drops:  []string{c.d},
		})
	})
	t.Cleanup(mock.Close)

	cons := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := cons.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Merged != 1 || rep.Dropped != 3 {
		t.Fatalf("Merged=%d Dropped=%d, want 1 and 3", rep.Merged, rep.Dropped)
	}
	all, _ := srv.store.ListAll(memory.L3Fact, "", "", 100, 0, false)
	for _, d := range all {
		if d.Content == "the port is 4471" {
			c.merged = d.ID
		}
	}
	if c.merged == "" {
		t.Fatal("merged fact not found")
	}
	return c
}

// Superseded facts disappear from every live read but stay in the history view
// with their markers. The merged fact keeps the conversations it absorbed.
func TestMergeSupersedesInsteadOfDeleting(t *testing.T) {
	c := mergeAndDrop(t)
	srv := c.srv

	live, liveTotal := srv.store.List(memory.L3Fact, "", "", 10, 0, false)
	if liveTotal != 2 {
		t.Errorf("live facts = %d, want 2 (merged + untouched)", liveTotal)
	}
	for _, d := range live {
		if d.ID != c.merged && d.ID != c.e {
			t.Errorf("superseded fact %s is still listed as live", d.ID)
		}
	}

	all, allTotal := srv.store.ListAll(memory.L3Fact, "", "", 10, 0, false)
	if allTotal != 5 {
		t.Fatalf("history rows = %d, want 5 (4 seeded + merged)", allTotal)
	}
	byID := byIDOf(all)
	for _, id := range []string{c.a, c.b} {
		d := byID[id]
		if d.Meta["invalid_at"] == "" || d.Meta["superseded_by"] != c.merged {
			t.Errorf("absorbed %s: invalid_at=%q superseded_by=%q, want a timestamp and %s",
				id, d.Meta["invalid_at"], d.Meta["superseded_by"], c.merged)
		}
		if d.Content == "" {
			t.Errorf("absorbed %s lost its content", id)
		}
	}
	if d := byID[c.d]; d.Meta["invalid_at"] == "" || d.Meta["superseded_by"] != "" {
		t.Errorf("dropped %s: invalid_at=%q superseded_by=%q, want a timestamp and empty",
			c.d, d.Meta["invalid_at"], d.Meta["superseded_by"])
	}
	if d := byID[c.e]; d.Meta["invalid_at"] != "" {
		t.Errorf("untouched %s was marked superseded", c.e)
	}

	m := byID[c.merged]
	if m.Meta["source_id"] != "raw-1" || m.Meta["source_ids"] != "raw-1,raw-2" {
		t.Errorf("merged provenance source_id=%q source_ids=%q, want raw-1 and raw-1,raw-2",
			m.Meta["source_id"], m.Meta["source_ids"])
	}

	hits, err := srv.store.Search("port", 10, memory.L3Fact, "", "")
	if err != nil {
		t.Fatal(err)
	}
	for _, h := range hits {
		if h.ID == c.a || h.ID == c.b || h.ID == c.d {
			t.Errorf("Search returned superseded fact %s", h.ID)
		}
	}
	if len(hits) != 2 {
		t.Errorf("Search returned %d hits, want 2 live facts", len(hits))
	}
	if n := srv.store.LayerCounts()["l3_fact"]; n != 2 {
		t.Errorf("LayerCounts l3_fact = %d, want 2", n)
	}
	if n := srv.store.TotalMemories(); n != 2 {
		t.Errorf("TotalMemories = %d, want 2", n)
	}
}

// The next pass reasons only over live facts: superseded ones are not in its ask().
// The owner must have changed since the previous pass, or that pass is skipped,
// so a new live fact is added first.
func TestSupersededFactsAreNotFedToNextPass(t *testing.T) {
	c := mergeAndDrop(t)
	fresh := seedSourced(t, c.srv.store, "u", "a", "raw-new", "a new live fact")

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()

	cons := NewConsolidator(c.srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	if _, err := cons.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	prompts := log.all()
	if len(prompts) != 1 {
		t.Fatalf("LLM calls = %d, want 1", len(prompts))
	}
	got := map[string]bool{}
	for _, id := range factIDs(prompts[0]) {
		got[id] = true
	}
	want := map[string]bool{c.merged: true, c.e: true, fresh: true}
	if len(got) != len(want) || !got[c.merged] || !got[c.e] || !got[fresh] {
		t.Errorf("next pass saw %v, want exactly the live merged, untouched and new facts", got)
	}
}

// /api/v1/list hides superseded rows unless include_superseded is set, on both
// the GET query string and the POST body. Superseded rows report what replaced them.
func TestListIncludeSupersededOptIn(t *testing.T) {
	c := mergeAndDrop(t)
	srv := c.srv

	listFor := func(req *http.Request) (int, []map[string]any) {
		t.Helper()
		rec := httptest.NewRecorder()
		srv.handleList(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("list status %d: %s", rec.Code, rec.Body.String())
		}
		var out struct {
			Total    int              `json:"total"`
			Memories []map[string]any `json:"memories"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatal(err)
		}
		return out.Total, out.Memories
	}
	post := func(body string) *http.Request {
		return httptest.NewRequest(http.MethodPost, "/api/v1/list", strings.NewReader(body))
	}

	if total, _ := listFor(httptest.NewRequest(http.MethodGet, "/api/v1/list?layer=l3_fact", nil)); total != 2 {
		t.Errorf("GET default total = %d, want 2", total)
	}
	total, rows := listFor(httptest.NewRequest(http.MethodGet, "/api/v1/list?layer=l3_fact&include_superseded=true", nil))
	if total != 5 {
		t.Errorf("GET include_superseded total = %d, want 5", total)
	}
	var sawMarker bool
	for _, r := range rows {
		if r["memory_id"] == c.a && r["superseded_by"] == c.merged && r["invalid_at"] != "" {
			sawMarker = true
		}
	}
	if !sawMarker {
		t.Error("superseded row does not report invalid_at and superseded_by")
	}
	if total, _ := listFor(post(`{"layer":"l3_fact"}`)); total != 2 {
		t.Errorf("POST default total = %d, want 2", total)
	}
	if total, _ := listFor(post(`{"layer":"l3_fact","include_superseded":true}`)); total != 5 {
		t.Errorf("POST include_superseded total = %d, want 5", total)
	}
}

// The superseded rows keep their stored vectors, and the markers survive a
// reopen whether the exact index loads from doc_index.json or is rebuilt from
// chromem.
func TestSupersededMarkersSurviveReopen(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	em := NewLocalEmbedder(384)
	graphPath := filepath.Join(dir, "graph.json")

	s1, err := NewMemoryStore(ctx, dir, em, graphPath)
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	old := seedSourced(t, s1, "u", "a", "raw-1", "old claim")
	merged := newID()
	if err := s1.Add(memory.L3Fact, merged, "new claim", map[string]string{
		"user_id": "u", "agent_id": "a", "source_id": "raw-1",
		"ts": time.Now().UTC().Format(time.RFC3339),
	}); err != nil {
		t.Fatal(err)
	}
	vecBefore, err := s1.cols[memory.L3Fact].GetByID(ctx, old)
	if err != nil {
		t.Fatal(err)
	}
	if n, err := s1.Supersede([]string{old}, merged); err != nil || n != 1 {
		t.Fatalf("Supersede = %d, %v; want 1, nil", n, err)
	}
	vecAfter, err := s1.cols[memory.L3Fact].GetByID(ctx, old)
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(vecBefore.Embedding) != fmt.Sprint(vecAfter.Embedding) {
		t.Error("Supersede changed the stored vector; it must reuse it without re-embedding")
	}
	if vecAfter.Metadata["superseded_by"] != merged || vecAfter.Metadata["invalid_at"] == "" {
		t.Errorf("chromem row lacks markers: %v", vecAfter.Metadata)
	}
	s1.Close()

	check := func(label string) {
		t.Helper()
		s, err := NewMemoryStore(ctx, dir, em, graphPath)
		if err != nil {
			t.Fatalf("%s: reopen: %v", label, err)
		}
		defer s.Close()
		if _, total := s.List(memory.L3Fact, "", "", 10, 0, false); total != 1 {
			t.Errorf("%s: live facts = %d, want 1", label, total)
		}
		rows, total := s.ListAll(memory.L3Fact, "", "", 10, 0, false)
		if total != 2 {
			t.Fatalf("%s: history rows = %d, want 2", label, total)
		}
		for _, d := range rows {
			if d.ID == old && (d.Meta["superseded_by"] != merged || d.Meta["invalid_at"] == "") {
				t.Errorf("%s: markers lost on reopen: %v", label, d.Meta)
			}
		}
	}
	check("index file")

	// Force the rebuild path: with no index file, the markers must come back out
	// of chromem's own metadata.
	if err := os.Remove(filepath.Join(dir, "doc_index.json")); err != nil {
		t.Fatal(err)
	}
	check("rebuilt from chromem")
}

// Raw decay still protects a raw row that a live fact was extracted from. A row
// behind only a superseded fact is no longer protected.
func TestDecayRawKeepsRowsBehindLiveFacts(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	old := time.Now().UTC().Add(-40 * 24 * time.Hour).Format(time.RFC3339)
	liveRaw, supersededRaw, uncited := newID(), newID(), newID()
	for _, id := range []string{liveRaw, supersededRaw, uncited} {
		if err := srv.store.Add(memory.L2Raw, id, "raw "+id, map[string]string{
			"user_id": "u", "agent_id": "a", "ts": old,
		}); err != nil {
			t.Fatal(err)
		}
	}
	seedSourced(t, srv.store, "u", "a", liveRaw, "still believed")
	stale := seedSourced(t, srv.store, "u", "a", supersededRaw, "no longer believed")
	if n, err := srv.store.Supersede([]string{stale}, ""); err != nil || n != 1 {
		t.Fatalf("Supersede = %d, %v", n, err)
	}

	c := NewConsolidator(srv.store, nil, time.Hour, 7*24*time.Hour, 200)
	pruned, protected := c.decayRaw()
	if pruned != 2 || protected != 1 {
		t.Errorf("decayRaw() = (%d,%d), want (2,1): the superseded and uncited rows go, the live-fact row stays",
			pruned, protected)
	}
	_, n := srv.store.List(memory.L2Raw, "", "", 1, 0, false)
	rows, _ := srv.store.List(memory.L2Raw, "", "", n, 0, false)
	remaining := map[string]bool{}
	for _, d := range rows {
		remaining[d.ID] = true
	}
	if !remaining[liveRaw] {
		t.Error("raw row behind a live fact was decayed")
	}
	if remaining[supersededRaw] || remaining[uncited] {
		t.Error("a raw row with no live fact or edge was not decayed")
	}
}

// A merged fact corroborates an edge with every conversation it absorbed, not just
// the first. Here the merged fact carries raw-1 and raw-2, and the third fact is
// also from raw-1, so the only second turn is raw-2.
func TestMergedFactKeepsCorroboration(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	f0 := seedSourced(t, srv.store, "u", "a", "raw-1", "fact zero")
	f1 := seedSourced(t, srv.store, "u", "a", "raw-2", "fact one")
	f2 := seedSourced(t, srv.store, "u", "a", "raw-1", "fact two")

	var log1 promptLog
	mock1 := scopeMock(t, &log1, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{
			Merges: []Merge{{Text: "merged zero and one", Supersedes: []string{f0, f1}}},
		})
	})
	defer mock1.Close()
	c1 := NewConsolidator(srv.store, NewLLMClient(mock1.URL, "k", "m"), time.Hour, 0, 200)
	if _, err := c1.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	all, _ := srv.store.ListAll(memory.L3Fact, "", "", 10, 0, false)
	var merged string
	for _, d := range all {
		if d.Content == "merged zero and one" {
			merged = d.ID
		}
	}
	if merged == "" {
		t.Fatal("merged fact not found")
	}

	var log2 promptLog
	mock2 := scopeMock(t, &log2, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{
			Knowledge: []CitedRelation{{From: "p", Relation: "q", To: "r", Evidence: []string{merged, f2}}},
		})
	})
	defer mock2.Close()
	// The owner must have changed since the first pass, or that pass's watermark
	// skips it. A new fact is that change; it is not part of the evidence.
	seedSourced(t, srv.store, "u", "a", "raw-3", "fact three")
	c2 := NewConsolidator(srv.store, NewLLMClient(mock2.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c2.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Edges != 1 {
		t.Fatalf("Edges = %d, want 1: one relation, corroborated by the merged fact's raw-1 and raw-2", rep.Edges)
	}
	// The edge must cite L2 conversations. Citing the merged fact's own ID would
	// leave decayRaw with nothing to protect. Both conversations must be kept.
	_, edges := srv.store.Graph().Snapshot(0)
	if len(edges) != 1 {
		t.Fatalf("edges = %d, want 1", len(edges))
	}
	got := map[string]bool{}
	for _, src := range edges[0].Sources {
		if src != "raw-1" && src != "raw-2" {
			t.Errorf("edge cites %q, want an L2 conversation (raw-1 or raw-2)", src)
		}
		got[src] = true
	}
	if !got["raw-1"] || !got["raw-2"] {
		t.Errorf("edge sources = %v, want both raw-1 and raw-2", edges[0].Sources)
	}
}
