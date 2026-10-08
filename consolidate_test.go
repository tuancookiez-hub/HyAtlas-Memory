package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// The slow path is what makes ultra more than pro: it reasons across memories
// rather than within one turn. These tests pin both the capability and the
// safety guards, because consolidation DELETES stored memories — a bug here
// loses data rather than merely returning a wrong answer.

// mockConsolidationServer replies with a fixed consolidation payload and counts
// calls, so a test can assert both the effect and that exactly one call happened.
func mockConsolidationServer(t *testing.T, calls *int, payload any) *httptest.Server {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"choices":[{"message":{"content":%s}}]}`, string(mustJSON(body)))
	}))
}

func mustJSON(b []byte) []byte {
	out, _ := json.Marshal(string(b))
	return out
}

// seed writes n L3 facts and returns their IDs in order.
func seed(t *testing.T, store *MemoryStore, n int, prefix string) []string {
	t.Helper()
	ids := make([]string, 0, n)
	for i := 0; i < n; i++ {
		id := newID()
		if err := store.Add("l3_fact", id, fmt.Sprintf("%s fact %d", prefix, i), map[string]string{
			"user_id": "u", "agent_id": "a", "ts": time.Now().UTC().Format(time.RFC3339),
		}); err != nil {
			t.Fatalf("seed: %v", err)
		}
		ids = append(ids, id)
	}
	return ids
}

func factCount(t *testing.T, store *MemoryStore) int {
	t.Helper()
	_, total := store.List("l3_fact", "", "", 0, 0, false)
	return total
}

// A merge must write the replacement BEFORE pruning what it absorbed, so the
// count drops by len(supersedes)-1 and the consolidated text is retrievable.
func TestConsolidateMergesAndPrunes(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ids := seed(t, srv.store, 4, "contradictory")

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{
		Merges: []Merge{{Text: "the port is 4471, not 4472", Supersedes: []string{ids[0], ids[1]}}},
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}

	if rep.Merged != 1 {
		t.Errorf("Merged = %d, want 1", rep.Merged)
	}
	if rep.Dropped != 2 {
		t.Errorf("Dropped = %d, want 2 (both superseded facts)", rep.Dropped)
	}
	if calls != 1 {
		t.Errorf("LLM calls = %d, want exactly 1", calls)
	}
	// 4 seeded - 2 pruned + 1 replacement = 3
	if got := factCount(t, srv.store); got != 3 {
		t.Errorf("fact count = %d, want 3", got)
	}

	// The replacement must be stored and marked. Checked by listing rather
	// than by search rank: the test embedder is a byte-hash stub, so vector
	// ordering carries no semantic meaning and hits[0] is not reliably the
	// row just written.
	_, total := srv.store.List("l3_fact", "", "", 1, 0, false)
	rows, _ := srv.store.List("l3_fact", "", "", total, 0, false)
	var found bool
	for _, d := range rows {
		if strings.Contains(d.Content, "the port is 4471") {
			found = true
			if d.Meta["consolidated"] != "true" {
				t.Errorf("consolidated flag = %q, want \"true\"", d.Meta["consolidated"])
			}
		}
	}
	if !found {
		t.Error("the consolidated fact was not stored")
	}
	// And the originals must be gone, not merely shadowed.
	for _, id := range ids[:2] {
		for _, d := range rows {
			if d.ID == id {
				t.Errorf("superseded fact %s survived the merge", id)
			}
		}
	}
}

// A hallucinated ID in the model's reply must delete nothing. This is the main
// data-loss guard: the LLM never gets to name an arbitrary memory to remove.
func TestConsolidateIgnoresUnknownIDs(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ids := seed(t, srv.store, 3, "real")
	before := factCount(t, srv.store)

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{
		Merges: []Merge{{Text: "merged", Supersedes: []string{"m-fabricated-1", "m-fabricated-2"}}},
		Drops:  []string{"m-fabricated-3", ids[0], "m-fabricated-4"},
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}

	// The merge named only fabricated IDs, so it must be skipped entirely.
	if rep.Merged != 0 {
		t.Errorf("Merged = %d, want 0 (all superseded IDs were fabricated)", rep.Merged)
	}
	// Only ids[0] was real, so exactly one drop may apply.
	if rep.Dropped != 1 {
		t.Errorf("Dropped = %d, want 1 (only the real ID)", rep.Dropped)
	}
	if got := factCount(t, srv.store); got != before-1 {
		t.Errorf("fact count = %d, want %d", got, before-1)
	}
}

// A "merge" naming fewer than two real facts is a rewrite, not a consolidation.
// Applying it would delete the original for no dedup gain.
func TestConsolidateRejectsSingleFactMerge(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ids := seed(t, srv.store, 3, "solo")
	before := factCount(t, srv.store)

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{
		Merges: []Merge{{Text: "rewritten", Supersedes: []string{ids[0]}}},
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Merged != 0 {
		t.Errorf("Merged = %d, want 0 (a single fact cannot be a merge)", rep.Merged)
	}
	if got := factCount(t, srv.store); got != before {
		t.Errorf("fact count changed to %d, want %d (nothing should be touched)", got, before)
	}
}

// Raw decay must never delete an L2 memory that a live L5 edge cites, or the
// knowledge graph ends up pointing at a memory that no longer exists.
func TestDecayRawProtectsCitedMemories(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	old := time.Now().UTC().Add(-40 * 24 * time.Hour).Format(time.RFC3339)

	cited, uncited := newID(), newID()
	for _, id := range []string{cited, uncited} {
		if err := srv.store.Add("l2_raw", id, "old raw "+id, map[string]string{
			"user_id": "u", "agent_id": "a", "ts": old,
		}); err != nil {
			t.Fatal(err)
		}
	}
	// A live edge citing one of them.
	if err := srv.store.Graph().AddEdgeWithSource("skyhook", "listens_on", "4471", cited); err != nil {
		t.Fatalf("edge: %v", err)
	}

	c := NewConsolidator(srv.store, nil, time.Hour, 7*24*time.Hour, 200)
	pruned, protected := c.decayRaw()

	if pruned != 1 {
		t.Errorf("pruned = %d, want 1 (only the uncited old raw)", pruned)
	}
	if protected != 1 {
		t.Errorf("protected = %d, want 1 (the cited raw)", protected)
	}

	// limit is a page size, not "all": 0 returns no rows. Size the page from
	// the reported total or this check silently sees an empty list.
	_, n := srv.store.List("l2_raw", "", "", 1, 0, false)
	rows, _ := srv.store.List("l2_raw", "", "", n, 0, false)
	remaining := map[string]bool{}
	for _, d := range rows {
		remaining[d.ID] = true
	}
	if !remaining[cited] {
		t.Error("the L5-cited raw memory was deleted; the graph edge is now a dangling citation")
	}
	if remaining[uncited] {
		t.Error("the old uncited raw memory was not decayed")
	}
}

// With no retention configured, nothing is ever deleted. Decay is opt-in, so a
// default install cannot lose history.
func TestDecayRawDisabledWithoutRetention(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	old := time.Now().UTC().Add(-400 * 24 * time.Hour).Format(time.RFC3339)
	if err := srv.store.Add("l2_raw", newID(), "ancient", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": old,
	}); err != nil {
		t.Fatal(err)
	}
	c := NewConsolidator(srv.store, nil, time.Hour, 0, 200)
	if pruned, protected := c.decayRaw(); pruned != 0 || protected != 0 {
		t.Errorf("decayRaw() = (%d,%d), want (0,0) when retention is unset", pruned, protected)
	}
	if _, total := srv.store.List("l2_raw", "", "", 0, 0, false); total != 1 {
		t.Errorf("raw count = %d, want 1 (nothing may be deleted)", total)
	}
}

// Ultra writes generalised schemas and a cross-session arc; these are the two
// outputs no single-turn extraction can produce.
func TestConsolidateWritesSchemasAndArc(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seed(t, srv.store, 3, "context")

	arc := "over three weeks the work moved from scaffolding to release hardening"
	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{
		Schemas: []Schema{{Pattern: "ports are always declared explicitly", Context: "service config"}},
		Arc:     &arc,
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Schemas != 1 {
		t.Errorf("Schemas = %d, want 1", rep.Schemas)
	}
	if !rep.Arc {
		t.Error("Arc = false, want the cross-session summary written")
	}
	if _, n := srv.store.List("l6_schema", "", "", 0, 0, false); n != 1 {
		t.Errorf("L6 schema count = %d, want 1", n)
	}
	if _, n := srv.store.List("l4_summary", "", "", 0, 0, false); n != 1 {
		t.Errorf("L4 summary count = %d, want 1", n)
	}
	// A null arc must not write an empty summary.
	runs, last, at := c.Stats()
	if runs != 1 || last == nil || at.IsZero() {
		t.Errorf("Stats() = (%d,%v,%v), want one recorded run", runs, last, at)
	}
}

// A null arc must not create a blank L4 row.
func TestConsolidateSkipsNullArc(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seed(t, srv.store, 2, "thin")

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{Arc: nil})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Arc {
		t.Error("Arc = true, want false for a null arc")
	}
	if _, n := srv.store.List("l4_summary", "", "", 0, 0, false); n != 0 {
		t.Errorf("L4 summary count = %d, want 0", n)
	}
}

// Fewer than two facts means there is nothing to reconcile; the pass must
// report as run with zero changes rather than burning an LLM call.
func TestConsolidateSkipsWhenNothingToReconcile(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seed(t, srv.store, 1, "lonely")

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if calls != 0 {
		t.Errorf("made %d LLM call(s) with only one fact; nothing to reconcile", calls)
	}
	if rep.Merged != 0 || rep.Dropped != 0 {
		t.Errorf("changes reported on a no-op pass: %+v", rep)
	}
	if runs, _, _ := c.Stats(); runs != 1 {
		t.Errorf("runs = %d, want 1 (a no-op pass still ran)", runs)
	}
}

// The pass must not wedge when the LLM fails; it records the error and reports
// zero changes rather than propagating a fatal error to the ticker.
func TestConsolidateSurvivesLLMFailure(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seed(t, srv.store, 3, "keep")
	before := factCount(t, srv.store)

	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatalf("Once() returned a fatal error: %v", err)
	}
	if len(rep.Errors) == 0 {
		t.Error("the LLM failure was not recorded")
	}
	if got := factCount(t, srv.store); got != before {
		t.Errorf("fact count changed to %d after a failed pass, want %d", got, before)
	}
}

// Unparsable model output must be tolerated the same way extraction tolerates
// prose: no deletion, error recorded.
func TestConsolidateToleratesGarbageReply(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seed(t, srv.store, 3, "safe")
	before := factCount(t, srv.store)

	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"Sure! Here are your memories."}}]}`))
	}))
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, _ := c.Once(context.Background())
	if len(rep.Errors) == 0 {
		t.Error("a prose reply was silently accepted")
	}
	if got := factCount(t, srv.store); got != before {
		t.Errorf("fact count changed to %d after an unparsable reply, want %d", got, before)
	}
}

// parseConsolidation must accept a fenced or prose-wrapped object, matching the
// extraction parser's tolerance.
func TestParseConsolidationTolerance(t *testing.T) {
	good := `{"merges":[{"text":"x","supersedes":["a","b"]}],"drops":[],"schemas":[],"arc":null}`
	cases := []string{
		good,
		"```json\n" + good + "\n```",
		"Here you go: " + good + " hope that helps",
	}
	for i, in := range cases {
		c, err := parseConsolidation(in)
		if err != nil {
			t.Errorf("case %d: unexpected error %v", i, err)
			continue
		}
		if len(c.Merges) != 1 || c.Merges[0].Text != "x" {
			t.Errorf("case %d: merges not parsed: %+v", i, c.Merges)
		}
		if c.Arc != nil {
			t.Errorf("case %d: arc should be nil, got %q", i, *c.Arc)
		}
	}
	for _, bad := range []string{"", "not json at all", "{"} {
		if _, err := parseConsolidation(bad); err == nil {
			t.Errorf("parseConsolidation(%q) should fail", bad)
		}
	}
}

// Pro and lite must never start the slow path: pro extracts per write and stops,
// lite never calls an LLM at all.
func TestOnlyUltraConsolidates(t *testing.T) {
	for _, m := range []Mode{ModeLite, ModePro} {
		if m.Consolidates() {
			t.Errorf("%s runs the slow path; it should not", m)
		}
	}
	if !ModeUltra.Consolidates() {
		t.Error("ultra does not run the slow path; it is then identical to pro")
	}
}

// The digest endpoint must distinguish "no slow path in this mode" from "slow
// path exists but has not run yet".
func TestDigestReportsSlowPathState(t *testing.T) {
	// pro: no consolidator at all
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	srv.mode = ModePro
	w := httptest.NewRecorder()
	srv.handleDigest(w, httptest.NewRequest("GET", "/api/v1/digest", nil))
	var got map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got["digest_ok"] != false {
		t.Errorf("pro digest_ok = %v, want false", got["digest_ok"])
	}
	if s, _ := got["reason"].(string); !strings.Contains(s, "no slow path") {
		t.Errorf("pro reason = %q, want it to explain the mode has no slow path", s)
	}

	// ultra with a consolidator that has not run
	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{})
	defer mock.Close()
	srv2 := newTestServer(t, "m", mock.URL)
	srv2.mode = ModeUltra
	srv2.cons = NewConsolidator(srv2.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)

	w2 := httptest.NewRecorder()
	srv2.handleDigest(w2, httptest.NewRequest("GET", "/api/v1/digest", nil))
	var got2 map[string]any
	if err := json.Unmarshal(w2.Body.Bytes(), &got2); err != nil {
		t.Fatal(err)
	}
	if got2["digest_ok"] != true {
		t.Errorf("ultra digest_ok = %v, want true", got2["digest_ok"])
	}
	if got2["runs"].(float64) != 0 {
		t.Errorf("ultra runs = %v, want 0 (never ran yet)", got2["runs"])
	}

	// POST must actually run a pass and report it
	w3 := httptest.NewRecorder()
	srv2.handleDigest(w3, httptest.NewRequest("POST", "/api/v1/digest", nil))
	var got3 map[string]any
	if err := json.Unmarshal(w3.Body.Bytes(), &got3); err != nil {
		t.Fatal(err)
	}
	if got3["report"] == nil {
		t.Error("POST /digest returned no report")
	}
}

// Status must expose the slow path so a caller can tell pro from ultra without
// guessing from latency.
func TestStatusReportsConsolidation(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for _, c := range []struct {
		mode Mode
		cons bool
		want float64
	}{
		{ModeLite, false, -1},
		{ModePro, false, -1},
		{ModeUltra, true, 0},
	} {
		srv.mode = c.mode
		srv.cons = nil
		if c.cons {
			srv.cons = NewConsolidator(srv.store, nil, time.Hour, 0, 200)
		}
		w := httptest.NewRecorder()
		srv.handleStatus(w, httptest.NewRequest("GET", "/api/v1/status", nil))
		var st map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &st); err != nil {
			t.Fatal(err)
		}
		if got := st["consolidations"].(float64); got != c.want {
			t.Errorf("%s consolidations = %v, want %v", c.mode, got, c.want)
		}
	}
}

// The sync knob must reach the wire: blocking versus background is user-visible
// behaviour and has to be reported, not inferred.
func TestStatusReportsExtractSync(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for _, c := range []struct {
		mode Mode
		sync Sync
		want string
	}{
		{ModeLite, SyncAuto, "none"},
		{ModePro, SyncAuto, "blocking"},
		{ModePro, SyncOff, "background"},
		{ModeUltra, SyncAuto, "background"},
		{ModeUltra, SyncOn, "blocking"},
	} {
		srv.mode, srv.sync = c.mode, c.sync
		w := httptest.NewRecorder()
		srv.handleStatus(w, httptest.NewRequest("GET", "/api/v1/status", nil))
		var st map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &st); err != nil {
			t.Fatal(err)
		}
		if got, _ := st["extract_sync"].(string); !strings.Contains(got, c.want) {
			t.Errorf("mode=%s sync=%q extract_sync = %q, want it to say %q", c.mode, c.sync, got, c.want)
		}
	}
}

// parseDuration must accept both "15m" and a bare second count, and fall back
// rather than silently disabling the slow path on a typo.
func TestParseDuration(t *testing.T) {
	t.Setenv("HYATLAS_TEST_DUR", "90s")
	if got := parseDuration("HYATLAS_TEST_DUR", time.Hour); got != 90*time.Second {
		t.Errorf("parseDuration = %v, want 90s", got)
	}
	t.Setenv("HYATLAS_TEST_DUR", "nonsense")
	if got := parseDuration("HYATLAS_TEST_DUR", time.Hour); got != time.Hour {
		t.Errorf("a bad value must fall back to the default, got %v", got)
	}
	t.Setenv("HYATLAS_TEST_DUR", "")
	if got := parseDuration("HYATLAS_TEST_DUR", 42*time.Minute); got != 42*time.Minute {
		t.Errorf("unset must use the default, got %v", got)
	}
}

// The batch cap must actually limit what the LLM is shown, so the prompt cannot
// grow without bound as memory accumulates.
func TestConsolidateRespectsBatchCap(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seed(t, srv.store, 12, "many")

	var seen int
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b := make([]byte, r.ContentLength)
		_, _ = r.Body.Read(b)
		seen = strings.Count(string(b), "id=m")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"merges\":[],\"drops\":[],\"schemas\":[],\"arc\":null}"}}]}`))
	}))
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 5)
	if _, err := c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if seen > 5 {
		t.Errorf("prompt contained %d facts, want at most the batch cap of 5", seen)
	}
	if seen == 0 {
		t.Error("prompt contained no facts; the cap swallowed everything")
	}
}

// main() wires the slow path through attachSlowPath. Every other test sets
// srv.cons directly, so without this a construction site that forgot the call
// would leave ultra silently identical to pro and no test would notice.
func TestAttachSlowPathOnlyInUltra(t *testing.T) {
	rt := runtimeCfg{Consolidate: time.Hour, Retention: 0, Batch: 50}
	for _, m := range validModes {
		srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
		srv.mode = m
		ctx, cancel := context.WithCancel(context.Background())
		attached := srv.attachSlowPath(ctx, rt)
		cancel()
		if attached != m.Consolidates() {
			t.Errorf("%s attachSlowPath() = %v, want %v", m, attached, m.Consolidates())
		}
		if (srv.cons != nil) != m.Consolidates() {
			t.Errorf("%s cons set = %v, want %v", m, srv.cons != nil, m.Consolidates())
		}
		if srv.cons != nil {
			if srv.cons.every != rt.Consolidate || srv.cons.batch != rt.Batch {
				t.Errorf("%s tuning not propagated: every=%v batch=%d", m, srv.cons.every, srv.cons.batch)
			}
			if srv.cons.retention != rt.Retention {
				t.Errorf("%s retention = %v, want %v", m, srv.cons.retention, rt.Retention)
			}
		}
	}
}

// The ticker must actually fire: a worker that is constructed but never runs
// makes ultra indistinguishable from pro in practice.
func TestSlowPathTickerFires(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seed(t, srv.store, 3, "tick")

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{})
	defer mock.Close()

	srv.mode = ModeUltra
	srv.cons = NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), 40*time.Millisecond, 0, 200)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go srv.cons.Run(ctx)

	// Wait for at least one tick rather than sleeping a fixed guess.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if n, _, _ := srv.cons.Stats(); n >= 2 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	runs, _, _ := srv.cons.Stats()
	if runs < 2 {
		t.Errorf("slow path ran %d time(s) in 5s with a 40ms ticker; the worker is not firing", runs)
	}
	if calls == 0 {
		t.Error("the ticker fired but no consolidation call was made")
	}

	// Cancelling must stop it, or the goroutine outlives the server.
	cancel()
	before, _, _ := srv.cons.Stats()
	time.Sleep(150 * time.Millisecond)
	after, _, _ := srv.cons.Stats()
	if after != before {
		t.Errorf("worker kept running after cancel: %d -> %d", before, after)
	}
}

// A zero interval must not spin: Run returns immediately rather than hammering
// the LLM in a tight loop.
func TestSlowPathDisabledByZeroInterval(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), 0, 0, 200)
	done := make(chan struct{})
	go func() { c.Run(context.Background()); close(done) }()
	select {
	case <-done:
	case <-time.After(500 * time.Millisecond):
		t.Fatal("Run() did not return for a zero interval")
	}
	if calls != 0 {
		t.Errorf("zero interval made %d call(s)", calls)
	}
}

// L5 is a System2 product: the slow path is the only thing that may write graph
// edges, and only when a triple is corroborated by more than one fact.
func TestConsolidateSynthesisesCorroboratedEdges(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	// Two facts with real L2 provenance, so the edge can cite conversations.
	raw := newID()
	if err := srv.store.Add("l2_raw", raw, "skyhook discussion", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": time.Now().UTC().Format(time.RFC3339),
	}); err != nil {
		t.Fatal(err)
	}
	ids := make([]string, 2)
	for i := range ids {
		ids[i] = newID()
		if err := srv.store.Add("l3_fact", ids[i], fmt.Sprintf("skyhook fact %d", i), map[string]string{
			"user_id": "u", "agent_id": "a", "source_id": raw,
			"ts": time.Now().UTC().Format(time.RFC3339),
		}); err != nil {
			t.Fatal(err)
		}
	}

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{
		Knowledge: []CitedRelation{{
			From: "skyhook", Relation: "listens_on", To: "4471",
			Evidence: []string{ids[0], ids[1]},
		}},
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Edges != 2 {
		t.Errorf("Edges = %d, want 2 (one per evidence citation)", rep.Edges)
	}
	_, edges := srv.store.Graph().Snapshot(0)
	if len(edges) == 0 {
		t.Fatal("no L5 edge was written")
	}
	// The citation must point at the L2 raw row, not the fact row, or decayRaw
	// cannot protect the conversation the graph depends on.
	for _, e := range edges {
		if e.Source != raw {
			t.Errorf("edge cites %q, want the L2 raw %q", e.Source, raw)
		}
	}
}

// A triple resting on a single fact is that fact restated. The per-turn pass
// already declined to write it, so the slow path must too.
func TestConsolidateRejectsUncorroboratedEdge(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ids := seed(t, srv.store, 2, "single")

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{
		Knowledge: []CitedRelation{{
			From: "a", Relation: "r", To: "b", Evidence: []string{ids[0]},
		}},
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Edges != 0 {
		t.Errorf("Edges = %d, want 0 for a single-fact triple", rep.Edges)
	}
	if _, edges := srv.store.Graph().Snapshot(0); len(edges) != 0 {
		t.Errorf("%d edge(s) written from uncorroborated evidence", len(edges))
	}
}

// Two facts merged into one must still corroborate an edge citing them. Merges
// delete IDs from the live set, so evidence resolved against that set instead of
// the original batch would silently drop the edge.
func TestConsolidateEdgeSurvivesMergingItsEvidence(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ids := seed(t, srv.store, 2, "merged")

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{
		Merges: []Merge{{Text: "the combined fact", Supersedes: []string{ids[0], ids[1]}}},
		Knowledge: []CitedRelation{{
			From: "skyhook", Relation: "listens_on", To: "4471",
			Evidence: []string{ids[0], ids[1]},
		}},
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Merged != 1 {
		t.Errorf("Merged = %d, want 1", rep.Merged)
	}
	if rep.Edges == 0 {
		t.Error("the edge was dropped because its evidence was consumed by the merge")
	}
}

// Fabricated evidence IDs must not create an edge, mirroring the merge guard.
func TestConsolidateIgnoresFabricatedEvidence(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seed(t, srv.store, 2, "real")

	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{
		Knowledge: []CitedRelation{{
			From: "x", Relation: "y", To: "z",
			Evidence: []string{"m-fake-1", "m-fake-2"},
		}},
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Edges != 0 {
		t.Errorf("Edges = %d from fabricated evidence, want 0", rep.Edges)
	}
}

// The slow path is now the ONLY writer of L5 and L6. If the per-turn pass ever
// regains them, the two systems stop being a partition.
func TestSlowPathOwnsL5AndL6(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seed(t, srv.store, 2, "own")

	// Per-turn pass writing every field it used to own.
	promoteExtraction(srv.store, &Extraction{
		Facts:     []Fact{{Data: "a fact", Layer: "project_state"}},
		Knowledge: []Relation{{From: "p", Relation: "q", To: "r"}},
		Schemas:   []Schema{{Pattern: "a pattern", Context: "ctx"}},
	}, "u", "a", "mem-src")

	if _, edges := srv.store.Graph().Snapshot(0); len(edges) != 0 {
		t.Errorf("per-turn pass wrote %d L5 edge(s); L5 belongs to the slow path", len(edges))
	}
	if _, n := srv.store.List("l6_schema", "", "", 1, 0, false); n != 0 {
		t.Errorf("per-turn pass wrote %d L6 row(s); L6 belongs to the slow path", n)
	}

	// The slow path does write both.
	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{
		Schemas: []Schema{{Pattern: "generalised", Context: "ctx"}},
		Knowledge: []CitedRelation{{From: "p", Relation: "q", To: "r",
			Evidence: []string{"will-be-ignored"}}},
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	if _, err := c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, n := srv.store.List("l6_schema", "", "", 1, 0, false); n != 1 {
		t.Errorf("slow path wrote %d L6 row(s), want 1", n)
	}
}

// Only one pass may be in flight. The ticker and a manual POST /digest can both
// ask for one, and two passes over the same batch would duplicate the LLM spend
// and race each other's merges and edge writes.
func TestOnceIsSingleFlight(t *testing.T) {
	calls := 0
	mock := mockConsolidationServer(t, &calls, Consolidation{})
	defer mock.Close()
	srv := newTestServer(t, "m", mock.URL)
	seed(t, srv.store, 3, "single")
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)

	c.gate.Lock()
	if _, err := c.Once(ctxForTest()); !errors.Is(err, errBusy) {
		t.Errorf("while a pass is in flight: err = %v, want errBusy", err)
	}
	c.gate.Unlock()

	rep, err := c.Once(ctxForTest())
	if err != nil {
		t.Fatalf("after the in-flight pass released, a new pass must run: %v", err)
	}
	if rep == nil {
		t.Fatal("no report from the pass that ran")
	}

	// The endpoint reports the same condition instead of a 500, because a cron
	// overlapping a tick is normal operation, not a failure.
	srv.mode = ModeUltra
	srv.cons = c
	c.gate.Lock()
	w := httptest.NewRecorder()
	srv.handleDigest(w, httptest.NewRequest(http.MethodPost, "/api/v1/digest", nil))
	c.gate.Unlock()
	if w.Code != 200 {
		t.Errorf("status = %d, want 200 (busy is not an error)", w.Code)
	}
	var got map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got["digest_ok"] != false {
		t.Errorf("digest_ok = %v, want false while busy", got["digest_ok"])
	}
	if s, _ := got["reason"].(string); !strings.Contains(s, "already running") {
		t.Errorf("reason = %q, want it to say a pass is already running", s)
	}
}

// The pass rewrites facts and graph edges, so a caller that stops waiting — a
// cron with a shorter timeout, a dropped connection — must not cancel work
// already in flight. The endpoint detaches the pass from the request for this
// reason; this test cancels the request mid-pass and requires the pass to still
// finish and record no error.
func TestDigestPassSurvivesClientDisconnect(t *testing.T) {
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	var hits int32
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		select {
		case entered <- struct{}{}:
		default:
		}
		<-release
		payload, _ := json.Marshal(Consolidation{})
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"choices":[{"message":{"content":%s}}]}`, string(mustJSON(payload)))
	}))
	defer mock.Close()

	srv := newTestServer(t, "m", mock.URL)
	seed(t, srv.store, 3, "disconnect")
	srv.mode = ModeUltra
	srv.cons = NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)

	ctx, cancel := context.WithCancel(context.Background())
	req := httptest.NewRequest(http.MethodPost, "/api/v1/digest", nil).WithContext(ctx)
	w := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		srv.handleDigest(w, req)
		close(done)
	}()

	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		cancel()
		close(release)
		<-done
		t.Fatal("the pass never reached the LLM")
	}

	cancel()       // the caller gives up waiting
	close(release) // the LLM answers anyway
	<-done

	if atomic.LoadInt32(&hits) == 0 {
		t.Fatal("the pass never called the LLM")
	}
	runs, last, _ := srv.cons.Stats()
	if runs != 1 {
		t.Fatalf("runs = %d, want 1: the pass did not complete after the disconnect", runs)
	}
	if last != nil && len(last.Errors) > 0 {
		t.Errorf("the disconnect leaked into the pass: errors = %v", last.Errors)
	}
	if w.Code != 200 {
		t.Errorf("status = %d, want 200", w.Code)
	}
}
