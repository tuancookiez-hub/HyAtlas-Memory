package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// pickyLLM is a fake chat endpoint that answers prose, never JSON, to any input
// containing "BADROW", and one fact to every other input.
type pickyLLM struct {
	calls atomic.Int32
	bad   atomic.Int32
}

// newPickyServer builds a pro-mode test server whose model cannot handle one kind of row.
func newPickyServer(t *testing.T) (*Server, *pickyLLM) {
	t.Helper()
	stub := &pickyLLM{}
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		stub.calls.Add(1)
		body, _ := io.ReadAll(r.Body)
		reply := `{"facts":[{"data":"A picky fact","layer":"project_state"}]}`
		if strings.Contains(string(body), "BADROW") {
			stub.bad.Add(1)
			reply = "I am sorry, I would rather chat about this than extract anything."
		}
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

// A row whose reply never parses is counted and passed over: the rows after it are
// still extracted, and after catchupMaxTries passes the loop leaves it alone.
func TestCatchupPassesOverUnparsableRow(t *testing.T) {
	srv, stub := newPickyServer(t)
	catchupRaw(t, srv.store, "bad", "BADROW the model only chats about this", 5*time.Hour)
	catchupRaw(t, srv.store, "good", "an ordinary note", time.Hour)
	c := NewCatchup(srv, time.Hour)

	res, ran := c.Once(context.Background())
	if !ran || res.Failed != 1 || res.Extracted != 1 {
		t.Fatalf("first pass: ran %v, %+v; want the bad row failed and the good one extracted", ran, res)
	}
	if !extractedOf(t, srv.store, "good") || extractedOf(t, srv.store, "bad") {
		t.Fatal("the good row must be extracted and the bad one not")
	}
	if got := srv.store.GetMany([]string{"bad"})[0].Meta[metaCatchupTries]; got != "1" {
		t.Errorf("catchup_tries after one pass: want 1, got %q", got)
	}
	for i := 2; i <= catchupMaxTries; i++ {
		if res, _ = c.Once(context.Background()); res.Failed != 1 {
			t.Fatalf("pass %d: %+v; want the bad row to fail again", i, res)
		}
	}
	// The row has used its tries: no further model call, and the pass reports it.
	before := stub.bad.Load()
	res, ran = c.Once(context.Background())
	if !ran || res.Skipped != 1 || res.Failed != 0 || stub.bad.Load() != before {
		t.Fatalf("after %d tries: ran %v, %+v, %d more bad calls; want the row skipped with no call",
			catchupMaxTries, ran, res, stub.bad.Load()-before)
	}
	// It still counts as waiting, and an explicit reprocess may try it again.
	if got := srv.unextractedCount(); got != 1 {
		t.Errorf("unextractedCount: want 1, got %d", got)
	}
}

// Many exhausted rows must not fill the batch and hide newer rows behind them.
func TestCatchupExhaustedRowsDoNotStarveOthers(t *testing.T) {
	srv, _ := newPickyServer(t)
	for i := 0; i < catchupBatch+2; i++ {
		id := "bad" + string(rune('a'+i))
		catchupRaw(t, srv.store, id, "BADROW "+id, 10*time.Hour)
		if err := srv.store.updateMeta(id, map[string]string{metaCatchupTries: "3"}); err != nil {
			t.Fatal(err)
		}
	}
	catchupRaw(t, srv.store, "good", "an ordinary note", time.Hour)
	res, _ := NewCatchup(srv, time.Hour).Once(context.Background())
	if res.Extracted != 1 || res.Skipped != catchupBatch+2 {
		t.Fatalf("%+v; want the good row extracted past %d exhausted rows", res, catchupBatch+2)
	}
}

// Only rows written to be extracted are caught up, and only while they are young.
func TestCatchupScope(t *testing.T) {
	srv, stub := newCatchupServer(t)
	// A lite-era row (or one from before 5.0): raw, unextracted, no marker.
	if err := srv.store.Add(memory.L2Raw, "lite", "written in lite mode", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": time.Now().Add(-time.Hour).UTC().Format(time.RFC3339),
	}); err != nil {
		t.Fatal(err)
	}
	catchupRaw(t, srv.store, "pro", "written in pro mode", time.Hour)
	catchupRaw(t, srv.store, "ancient", "written long ago", 30*24*time.Hour)

	if got := srv.unextractedCount(); got != 2 {
		t.Errorf("unextractedCount: want 2 (the lite row is not waiting), got %d", got)
	}
	c := NewCatchup(srv, time.Hour)
	c.maxAge = 7 * 24 * time.Hour
	res, _ := c.Once(context.Background())
	if res.Extracted != 1 || stub.calls.Load() != 1 {
		t.Fatalf("%+v with %d calls; want only the young pro row extracted", res, stub.calls.Load())
	}
	if !extractedOf(t, srv.store, "pro") || extractedOf(t, srv.store, "lite") || extractedOf(t, srv.store, "ancient") {
		t.Error("only the pro row may be extracted")
	}

	// No age limit takes the old row, but the unmarked one is never taken.
	c.maxAge = 0
	res, _ = c.Once(context.Background())
	if res.Extracted != 1 || extractedOf(t, srv.store, "lite") || !extractedOf(t, srv.store, "ancient") {
		t.Fatalf("with no age limit: %+v; want the old row extracted and the lite row left alone", res)
	}

	// Reprocess keeps taking unmarked rows.
	if rows := srv.unextractedRaw(10); len(rows) != 1 || rows[0].ID != "lite" {
		t.Errorf("unextractedRaw: want only the lite row, got %v", matchRowIDs(rows))
	}
}

// HYATLAS_CATCHUP_MAX_AGE sets the limit; off removes it.
func TestCatchupMaxAgeSetting(t *testing.T) {
	srv, _ := newCatchupServer(t)
	if got := NewCatchup(srv, time.Hour).maxAge; got != catchupMaxAgeDefault {
		t.Errorf("default max age: want %s, got %s", catchupMaxAgeDefault, got)
	}
	t.Setenv(catchupMaxAgeKey, "36h")
	if got := NewCatchup(srv, time.Hour).maxAge; got != 36*time.Hour {
		t.Errorf("max age 36h: got %s", got)
	}
	t.Setenv(catchupMaxAgeKey, "off")
	if got := NewCatchup(srv, time.Hour).maxAge; got != 0 {
		t.Errorf("max age off: got %s", got)
	}
}

// handleAdd marks a row it will extract, and leaves a lite row unmarked.
func TestAddMarksRowsMeantForExtraction(t *testing.T) {
	srv, _ := newCatchupServer(t)
	srv.sync = SyncOn
	add := func(text string) string {
		w := httptest.NewRecorder()
		srv.handleAdd(w, httptest.NewRequest("POST", "/api/v1/add",
			strings.NewReader(`{"text":"`+text+`","user_id":"u","agent_id":"a","metadata":{"extract_wanted":"true"}}`)))
		var out struct {
			ID string `json:"memory_id"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil || out.ID == "" {
			t.Fatalf("add: %d %s", w.Code, w.Body.String())
		}
		return out.ID
	}
	pro := add("pro write")
	if d := srv.store.GetMany([]string{pro}); d[0].Meta[metaExtractWanted] != "true" {
		t.Errorf("pro row lacks the marker: %v", d[0].Meta)
	}
	srv.mode = ModeLite
	lite := add("lite write")
	if d := srv.store.GetMany([]string{lite}); d[0].Meta[metaExtractWanted] != "" {
		t.Errorf("lite row carries the marker: %v", d[0].Meta)
	}
}

// An extraction whose raw row was forgotten while the model was answering stores nothing.
func TestExtractDoesNotReviveForgottenRow(t *testing.T) {
	arrived := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		once.Do(func() { close(arrived) })
		<-release
		out, _ := json.Marshal(map[string]any{"choices": []map[string]any{
			{"message": map[string]string{"content": `{"facts":[{"data":"A fact that must not appear","layer":"project_state"}]}`}}}})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	}))
	defer mock.Close()
	srv := newTestServer(t, "test-model", mock.URL)
	srv.mode = ModePro
	srv.llm = NewLLMClient(mock.URL, "k", "test-model")
	catchupRaw(t, srv.store, "r1", "a note about to be forgotten", time.Hour)

	done := make(chan error, 1)
	go func() { done <- srv.extract("a note about to be forgotten", "u", "a", "r1") }()
	select {
	case <-arrived:
	case <-time.After(5 * time.Second):
		t.Fatal("the model was never called")
	}
	if _, err := srv.store.retire([]string{"r1"}, Retire{}); err != nil {
		t.Fatal(err)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatalf("extract: %v", err)
	}
	if facts, _ := srv.store.List(memory.L3Fact, "u", "a", 10, 0, false); len(facts) != 0 {
		t.Errorf("a forgotten row's extraction stored %d fact(s)", len(facts))
	}
}

// waitTimed returns true once the work is done and false when it hangs past the timeout.
func TestWaitTimed(t *testing.T) {
	var wg sync.WaitGroup
	if !waitTimed(&wg, time.Second) {
		t.Fatal("an empty group must be done at once")
	}
	wg.Add(1)
	go func() { time.Sleep(50 * time.Millisecond); wg.Done() }()
	if !waitTimed(&wg, 5*time.Second) {
		t.Fatal("work that finishes must be waited for")
	}
	wg.Add(1)
	start := time.Now()
	if waitTimed(&wg, 60*time.Millisecond) {
		t.Fatal("hung work must time the wait out")
	}
	if time.Since(start) > 2*time.Second {
		t.Errorf("the timed wait took %s", time.Since(start))
	}
	wg.Done()
}

// A catch-up pass is counted in the server's background group, so the shutdown wait
// covers it.
func TestShutdownWaitCoversCatchupPass(t *testing.T) {
	arrived := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		once.Do(func() { close(arrived) })
		<-release
		out, _ := json.Marshal(map[string]any{"choices": []map[string]any{
			{"message": map[string]string{"content": `{"facts":[{"data":"A slow fact","layer":"project_state"}]}`}}}})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	}))
	defer mock.Close()
	srv := newTestServer(t, "test-model", mock.URL)
	srv.mode = ModePro
	srv.llm = NewLLMClient(mock.URL, "k", "test-model")
	catchupRaw(t, srv.store, "r1", "a slow note", time.Hour)

	c := NewCatchup(srv, time.Hour)
	done := make(chan struct{})
	go func() { c.Once(context.Background()); close(done) }()
	<-arrived
	if left := srv.waitExtractions(50 * time.Millisecond); left != 1 {
		t.Fatalf("a pass in progress: want 1 still running, got %d", left)
	}
	close(release)
	if left := srv.waitExtractions(5 * time.Second); left != 0 {
		t.Fatalf("after the pass: want 0 running, got %d", left)
	}
	<-done
	if !extractedOf(t, srv.store, "r1") {
		t.Error("the pass was waited for but its row is not extracted")
	}
}

// Wait on the consolidation loop covers the loop itself, and Start tolerates nil.
func TestConsolidatorWaitCoversLoop(t *testing.T) {
	var nilCons *Consolidator
	nilCons.Start(context.Background())
	if !nilCons.Wait(time.Millisecond) {
		t.Error("a nil Consolidator has nothing to wait for")
	}
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	c := NewConsolidator(srv.store, NewLLMClient("http://127.0.0.1:1/v1", "k", "m"), time.Hour, 0, 200)
	ctx, cancel := context.WithCancel(context.Background())
	c.Start(ctx)
	if c.Wait(40 * time.Millisecond) {
		t.Fatal("the loop is still running, so the wait must time out")
	}
	cancel()
	if !c.Wait(5 * time.Second) {
		t.Fatal("the loop did not stop after its context was cancelled")
	}
}

// Wait on the episode loop works the same way.
func TestEpisodesWaitCoversLoop(t *testing.T) {
	var nilEp *Episodes
	nilEp.Start(context.Background())
	if !nilEp.Wait(time.Millisecond) {
		t.Error("a nil Episodes has nothing to wait for")
	}
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	e := NewEpisodes("", srv.store, NewLLMClient("http://127.0.0.1:1/v1", "k", "m"), time.Hour)
	ctx, cancel := context.WithCancel(context.Background())
	e.Start(ctx)
	if e.Wait(40 * time.Millisecond) {
		t.Fatal("the loop is still running, so the wait must time out")
	}
	cancel()
	if !e.Wait(5 * time.Second) {
		t.Fatal("the loop did not stop after its context was cancelled")
	}
}

// After Shutdown a Now page refresh no longer starts, and a nil NowPages is fine.
func TestNowShutdownStopsRefreshes(t *testing.T) {
	var nilNow *NowPages
	if !nilNow.Shutdown(time.Millisecond) {
		t.Error("a nil NowPages has nothing to wait for")
	}
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	n := NewNowPages(t.TempDir(), srv.store, NewLLMClient("http://127.0.0.1:1/v1", "k", "m"), time.Hour)
	if !n.Shutdown(time.Second) {
		t.Fatal("nothing was running, so the wait must finish")
	}
	n.Touch("u", "a")
	n.mu.Lock()
	started := n.inflight[nowOwnerKey("u", "a")]
	n.mu.Unlock()
	if started {
		t.Error("a refresh started after Shutdown")
	}
}

// The backlog job's wait is safe on nil and on an idle job.
func TestBacklogWaitIdle(t *testing.T) {
	var nilJob *BacklogJob
	if !nilJob.Wait(time.Millisecond) {
		t.Error("a nil BacklogJob has nothing to wait for")
	}
	if !(&BacklogJob{}).Wait(time.Millisecond) {
		t.Error("an idle job has nothing to wait for")
	}
}

// The store's waiting counter follows adds, extraction and retirement, and ignores raw
// rows that were not written to be extracted.
func TestAwaitingExtractionCounter(t *testing.T) {
	srv, _ := newCatchupServer(t)
	s := srv.store
	catchupRaw(t, s, "a", "one", time.Hour)
	catchupRaw(t, s, "b", "two", time.Hour)
	if err := s.Add(memory.L2Raw, "plain", "no marker", map[string]string{"user_id": "u", "agent_id": "a"}); err != nil {
		t.Fatal(err)
	}
	if got := s.awaitingExtraction(); got != 2 {
		t.Fatalf("after adds: want 2, got %d", got)
	}
	if err := s.SetExtracted("a", true); err != nil {
		t.Fatal(err)
	}
	if got := s.awaitingExtraction(); got != 1 {
		t.Fatalf("after extracting one: want 1, got %d", got)
	}
	if _, err := s.retire([]string{"b"}, Retire{}); err != nil {
		t.Fatal(err)
	}
	if got := s.awaitingExtraction(); got != 0 {
		t.Fatalf("after retiring the other: want 0, got %d", got)
	}
	// A rebuild from the index recounts to the same number.
	s.mu.Lock()
	s.rebuildSupersededLocked()
	s.mu.Unlock()
	if got := s.awaitingExtraction(); got != 0 {
		t.Fatalf("after a rebuild: want 0, got %d", got)
	}
}

// A store that does not keep raw rows in the keyword index still finds them when a
// search asks for the raw layer, and SetKeywordRaw puts them in the index.
func TestKeywordRawIndexOnlyWhenAsked(t *testing.T) {
	srv, _ := newCatchupServer(t)
	s := srv.store
	if err := s.Add(memory.L2Raw, "raw1", "the zebrafish tank pump is noisy", map[string]string{"user_id": "u", "agent_id": "a"}); err != nil {
		t.Fatal(err)
	}
	if err := s.Add(memory.L3Fact, "fact1", "the zebrafish tank needs a new filter", map[string]string{"user_id": "u", "agent_id": "a"}); err != nil {
		t.Fatal(err)
	}
	indexed := func(id string) bool {
		s.mu.RLock()
		defer s.mu.RUnlock()
		_, ok := s.kw.docs[id]
		return ok
	}
	if indexed("raw1") || !indexed("fact1") {
		t.Fatalf("pro-style store: raw indexed %v, fact indexed %v; want only the fact", indexed("raw1"), indexed("fact1"))
	}
	hits, err := s.KeywordSearch("zebrafish pump", 5, memory.L2Raw, []string{"u"}, "a")
	if err != nil || len(hits) != 1 || hits[0].ID != "raw1" {
		t.Fatalf("raw layer search: %+v, %v; want raw1 ranked on the fly", hits, err)
	}
	if hits, _ := s.KeywordSearch("zebrafish", 5, "", []string{"u"}, "a"); len(hits) != 1 || hits[0].ID != "fact1" {
		t.Fatalf("all-layer search: %+v; want only the fact", hits)
	}
	s.SetKeywordRaw(true)
	if !indexed("raw1") {
		t.Fatal("SetKeywordRaw(true) did not index the raw row")
	}
	s.SetKeywordRaw(false)
	if indexed("raw1") {
		t.Fatal("SetKeywordRaw(false) left the raw row indexed")
	}
}
