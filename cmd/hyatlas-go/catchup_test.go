package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// catchupLLM is a fake chat endpoint that counts its calls and can be made to fail.
type catchupLLM struct {
	calls atomic.Int32
	fail  atomic.Bool
}

// newCatchupServer builds a pro-mode test server whose LLM client talks to a fake
// endpoint. Every call returns one fact, or an HTTP 500 while fail is set.
func newCatchupServer(t *testing.T) (*Server, *catchupLLM) {
	t.Helper()
	stub := &catchupLLM{}
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := stub.calls.Add(1)
		if stub.fail.Load() {
			http.Error(w, "upstream down", http.StatusInternalServerError)
			return
		}
		reply := `{"facts":[{"data":"Catch-up fact number ` + string(rune('a'+n)) + `","layer":"project_state"}]}`
		out, _ := json.Marshal(map[string]any{"choices": []map[string]any{
			{"message": map[string]string{"content": reply}}}})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	}))
	t.Cleanup(mock.Close)
	srv := newTestServer(t, "test-model", mock.URL)
	srv.mode = ModePro
	srv.llm = NewLLMClient(mock.URL, "k", "test-model")
	return srv, stub
}

// catchupRaw stores an unextracted raw row written age ago, marked as meant to be
// extracted like a row written in pro or ultra mode.
func catchupRaw(t *testing.T, s *MemoryStore, id, text string, age time.Duration) {
	t.Helper()
	if err := s.Add(memory.L2Raw, id, text, map[string]string{
		"user_id": "u", "agent_id": "a", "ts": time.Now().Add(-age).UTC().Format(time.RFC3339),
		metaExtractWanted: "true",
	}); err != nil {
		t.Fatal(err)
	}
}

func extractedOf(t *testing.T, s *MemoryStore, id string) bool {
	t.Helper()
	d := s.GetMany([]string{id})
	if len(d) == 0 {
		t.Fatalf("row %s missing", id)
	}
	return d[0].Extracted
}

// A run extracts every old unextracted row, flags it, and leaves no backlog.
func TestCatchupExtractsUnextractedRows(t *testing.T) {
	srv, stub := newCatchupServer(t)
	for i, id := range []string{"r1", "r2", "r3"} {
		catchupRaw(t, srv.store, id, "raw note "+id, time.Duration(i+1)*time.Hour)
	}
	if got := srv.unextractedCount(); got != 3 {
		t.Fatalf("unextractedCount before: want 3, got %d", got)
	}
	c := NewCatchup(srv, time.Hour)
	res, ran := c.Once(context.Background())
	if !ran || res.Extracted != 3 || res.Failed != 0 || stub.calls.Load() != 3 {
		t.Fatalf("run: ran %v, %+v, %d calls; want 3 extracted in 3 calls", ran, res, stub.calls.Load())
	}
	for _, id := range []string{"r1", "r2", "r3"} {
		if !extractedOf(t, srv.store, id) {
			t.Errorf("%s not flagged extracted", id)
		}
	}
	if got := srv.unextractedCount(); got != 0 {
		t.Errorf("unextractedCount after: want 0, got %d", got)
	}
	if facts, _ := srv.store.List(memory.L3Fact, "u", "a", 10, 0, false); len(facts) != 3 {
		t.Errorf("want 3 facts stored, got %d", len(facts))
	}
	// Nothing left, so a second run makes no call.
	if _, ran := c.Once(context.Background()); !ran || stub.calls.Load() != 3 {
		t.Errorf("second run called the model again: %d calls", stub.calls.Load())
	}
}

// With the endpoint failing, a run makes one call and stops, leaving every row for
// the next run.
func TestCatchupStopsAtFirstError(t *testing.T) {
	srv, stub := newCatchupServer(t)
	stub.fail.Store(true)
	for i, id := range []string{"r1", "r2", "r3"} {
		catchupRaw(t, srv.store, id, "raw note "+id, time.Duration(i+1)*time.Hour)
	}
	res, ran := NewCatchup(srv, time.Hour).Once(context.Background())
	if !ran || res.Failed != 1 || res.Extracted != 0 {
		t.Fatalf("run: ran %v, %+v; want 1 failure", ran, res)
	}
	if got := stub.calls.Load(); got != 1 {
		t.Errorf("want 1 model call, got %d", got)
	}
	if got := srv.unextractedCount(); got != 3 {
		t.Errorf("rows must stay unextracted: want 3, got %d", got)
	}
}

// A row under two minutes old may still have its own extraction running, so it waits.
func TestCatchupSkipsNewRows(t *testing.T) {
	srv, stub := newCatchupServer(t)
	catchupRaw(t, srv.store, "old", "an old note", time.Hour)
	catchupRaw(t, srv.store, "new", "a fresh note", 30*time.Second)
	res, _ := NewCatchup(srv, time.Hour).Once(context.Background())
	if res.Extracted != 1 || res.Skipped != 1 || stub.calls.Load() != 1 {
		t.Fatalf("%+v with %d calls; want one extracted and one skipped", res, stub.calls.Load())
	}
	if !extractedOf(t, srv.store, "old") || extractedOf(t, srv.store, "new") {
		t.Error("the old row must be extracted and the new one left alone")
	}
}

// A raw row that other rows already name as their source is extracted already, even
// when its flag was lost; re-extracting it would store its facts twice.
func TestCatchupDoesNotReextractDerivedRows(t *testing.T) {
	srv, stub := newCatchupServer(t)
	catchupRaw(t, srv.store, "live", "note with a live fact", time.Hour)
	catchupRaw(t, srv.store, "gone", "note whose fact was superseded", time.Hour)
	catchupRaw(t, srv.store, "merged", "note absorbed into a consolidated fact", time.Hour)
	catchupRaw(t, srv.store, "bare", "note with nothing derived", time.Hour)
	add := func(id, text string, meta map[string]string) {
		t.Helper()
		meta["user_id"], meta["agent_id"], meta["ts"] = "u", "a", time.Now().UTC().Format(time.RFC3339)
		if err := srv.store.Add(memory.L3Fact, id, text, meta); err != nil {
			t.Fatal(err)
		}
	}
	add("f-live", "a live fact", map[string]string{"source_id": "live"})
	add("f-gone", "an old fact", map[string]string{"source_id": "gone"})
	add("f-new", "a newer fact", map[string]string{})
	if _, err := srv.store.Supersede([]string{"f-gone"}, "f-new"); err != nil {
		t.Fatal(err)
	}
	add("f-merged", "a consolidated fact", map[string]string{"source_ids": "x,merged"})

	res, _ := NewCatchup(srv, time.Hour).Once(context.Background())
	if res.Marked != 3 || res.Extracted != 1 || stub.calls.Load() != 1 {
		t.Fatalf("%+v with %d calls; want 3 marked and 1 extracted by 1 call", res, stub.calls.Load())
	}
	for _, id := range []string{"live", "gone", "merged", "bare"} {
		if !extractedOf(t, srv.store, id) {
			t.Errorf("%s not flagged extracted", id)
		}
	}
	// The one model call was for the row with nothing derived.
	for _, f := range srv.store.GetMany(catchupFactIDs(t, srv)) {
		if strings.HasPrefix(f.Content, "Catch-up fact") && f.Meta["source_id"] != "bare" {
			t.Errorf("the model was asked about %s, which already had facts", f.Meta["source_id"])
		}
	}
}

// factIDs lists the ids of every live L3 fact of the test owner.
func catchupFactIDs(t *testing.T, srv *Server) []string {
	t.Helper()
	facts, _ := srv.store.List(memory.L3Fact, "u", "a", 50, 0, false)
	out := make([]string, len(facts))
	for i, f := range facts {
		out[i] = f.ID
	}
	return out
}

// Handing the server a key it was waiting for runs a catch-up at once, long before
// the timer.
func TestCatchupRunsWhenKeyArrives(t *testing.T) {
	srv, stub := newCatchupServer(t)
	srv.llm = NewLLMClient(srv.llm.BaseURL, "", "test-model") // endpoint and model, no key yet
	if srv.llmState() != "awaiting_key" {
		t.Fatalf("setup: want awaiting_key, got %s", srv.llmState())
	}
	catchupRaw(t, srv.store, "r1", "written before the key", time.Hour)
	srv.catchup = NewCatchup(srv, time.Hour)
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); srv.catchup.Run(ctx) }()
	defer func() { cancel(); wg.Wait() }()

	// Without a key nothing may be tried.
	srv.catchup.Kick()
	time.Sleep(100 * time.Millisecond)
	if stub.calls.Load() != 0 {
		t.Fatalf("a run without a key made %d call(s)", stub.calls.Load())
	}

	w := httptest.NewRecorder()
	keyReq := httptest.NewRequest("POST", "/api/v1/llm/key", strings.NewReader(`{"key":"abc"}`))
	keyReq.RemoteAddr = "127.0.0.1:50000"
	srv.handleLLMKey(w, keyReq)
	if w.Code != http.StatusOK {
		t.Fatalf("key push: want 200, got %d", w.Code)
	}
	deadline := time.Now().Add(5 * time.Second)
	for !extractedOf(t, srv.store, "r1") {
		if time.Now().After(deadline) {
			t.Fatal("the key arrived but the raw row was never extracted")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// An empty key clears the credential and must not start a run, and an interval of
// zero switches the loop off without breaking its callers.
func TestCatchupOffAndNilAreSafe(t *testing.T) {
	if c := NewCatchup(&Server{}, 0); c != nil {
		t.Fatal("an interval of 0 must give no loop")
	}
	var c *Catchup
	c.Kick()
	c.Run(context.Background())
	if _, ran := c.Once(context.Background()); ran {
		t.Error("a nil Catchup ran")
	}
	var s Server
	s.catchup.Kick()
}

// /api/v1/status reports how many raw rows are waiting for extraction.
func TestStatusReportsUnextracted(t *testing.T) {
	srv, _ := newCatchupServer(t)
	catchupRaw(t, srv.store, "r1", "one", time.Hour)
	catchupRaw(t, srv.store, "r2", "two", time.Hour)
	if err := srv.store.SetExtracted("r2", true); err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	srv.handleStatus(w, httptest.NewRequest("GET", "/api/v1/status", nil))
	var st Status
	if err := json.Unmarshal(w.Body.Bytes(), &st); err != nil {
		t.Fatal(err)
	}
	if st.Unextracted != 1 {
		t.Errorf("unextracted: want 1, got %d", st.Unextracted)
	}
	if !strings.Contains(w.Body.String(), `"unextracted":1`) {
		t.Errorf("status JSON lacks unextracted: %s", w.Body.String())
	}
}

// unextractedRaw gives the oldest unextracted rows first and stops at max.
func TestUnextractedRawOldestFirstAndCapped(t *testing.T) {
	srv, _ := newCatchupServer(t)
	for i, id := range []string{"c", "b", "a", "d"} {
		catchupRaw(t, srv.store, id, "note "+id, time.Duration(i+1)*time.Hour) // c oldest-ish: 1h, d: 4h
	}
	if err := srv.store.SetExtracted("d", true); err != nil { // the oldest, but done
		t.Fatal(err)
	}
	rows := srv.unextractedRaw(2)
	if len(rows) != 2 || rows[0].ID != "a" || rows[1].ID != "b" {
		t.Fatalf("want [a b] (oldest unextracted first), got %v", matchRowIDs(rows))
	}
}

func matchRowIDs(rows []DocIndex) []string {
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = r.ID
	}
	return out
}

// textCountingEmbedder wraps an embedder and counts how often each text is embedded.
type textCountingEmbedder struct {
	inner Embedder
	mu    sync.Mutex
	texts map[string]int
}

func (c *textCountingEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	c.mu.Lock()
	c.texts[text]++
	c.mu.Unlock()
	return c.inner.Embed(ctx, text)
}

func (c *textCountingEmbedder) count(text string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.texts[text]
}

func (c *textCountingEmbedder) total() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := 0
	for _, v := range c.texts {
		n += v
	}
	return n
}

// One write that yields two facts (one a preference, so it has an L1 mirror) embeds
// the raw text once, the context once and each fact once.
func TestWritePathEmbedsEachTextOnce(t *testing.T) {
	const raw = "I like dark roast coffee and HyAtlas listens on port 19528"
	const pref, state = "The user likes dark roast coffee", "HyAtlas listens on port 19528"
	reply := `{"facts":[{"data":"` + pref + `","layer":"user_preferences"},{"data":"` + state + `","layer":"project_state"}]}`
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		out, _ := json.Marshal(map[string]any{"choices": []map[string]any{
			{"message": map[string]string{"content": reply}}}})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	}))
	defer mock.Close()

	dir := t.TempDir()
	ce := &textCountingEmbedder{inner: NewLocalEmbedder(384), texts: map[string]int{}}
	store, err := NewMemoryStore(ctxForTest(), dir, ce, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	defer store.Close()
	srv := &Server{store: store, mode: ModePro, sync: SyncOn, start: timeForTest(), dedupeScore: 0.92,
		llm: NewLLMClient(mock.URL, "k", "test-model"), llmModel: "test-model", llmBase: mock.URL}

	w := httptest.NewRecorder()
	srv.handleAdd(w, httptest.NewRequest("POST", "/api/v1/add",
		strings.NewReader(`{"text":"`+raw+`","user_id":"u","agent_id":"a"}`)))
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"extraction_status":"done"`) {
		t.Fatalf("add: %d %s", w.Code, w.Body.String())
	}
	// The raw text is embedded by the write itself and once more as the extraction
	// context (the same text), so it appears twice; each fact appears once, though it
	// is stored in L3 and, for the preference, mirrored to L1.
	if got := ce.count(raw); got != 2 {
		t.Errorf("raw text embedded %d times; want 2 (the write and the context)", got)
	}
	if got := ce.count(pref); got != 1 {
		t.Errorf("preference embedded %d times; want 1", got)
	}
	if got := ce.count(state); got != 1 {
		t.Errorf("project fact embedded %d times; want 1", got)
	}
	if got := ce.total(); got != 4 {
		t.Errorf("%d embeddings in all; want 4 (%v)", got, ce.texts)
	}
	if mirrors, _ := store.List(memory.L1Profile, "u", "a", 10, 0, false); len(mirrors) != 1 {
		t.Errorf("want 1 L1 mirror, got %d", len(mirrors))
	}
}
