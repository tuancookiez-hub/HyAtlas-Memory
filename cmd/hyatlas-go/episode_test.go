package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// episodeLLMStub is a fake chat endpoint for episode tests. It counts calls and
// keeps the request bodies, so a test can check what the model was sent.
type episodeLLMStub struct {
	calls  atomic.Int32
	mu     sync.Mutex
	bodies []string
}

// newEpisodeLLMStub starts a fake chat endpoint that answers every call with the
// given HTTP status and, on success, the given reply text. It returns the stub and
// an LLM client that calls it.
func newEpisodeLLMStub(t *testing.T, status int, reply string) (*episodeLLMStub, *LLMClient) {
	t.Helper()
	stub := &episodeLLMStub{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		stub.calls.Add(1)
		body, _ := io.ReadAll(r.Body)
		stub.mu.Lock()
		stub.bodies = append(stub.bodies, string(body))
		stub.mu.Unlock()
		if status != http.StatusOK {
			w.WriteHeader(status)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]string{"content": reply}}},
		})
	}))
	t.Cleanup(srv.Close)
	return stub, NewLLMClient(srv.URL, "k", "m")
}

// episodeTestTime is a fixed UTC start time so every test works from the same clock.
var episodeTestTime = time.Date(2026, 10, 17, 9, 0, 0, 0, time.UTC)

// TestEpisodeNoteCutsOnGap checks that writes inside the gap share one episode and
// that a write after the gap closes it and starts a new one.
func TestEpisodeNoteCutsOnGap(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	llm := NewLLMClient("http://unused.invalid", "k", "m")
	e := NewEpisodes(t.TempDir(), st, llm, 30*time.Minute)

	id1 := addRow(t, st, memory.L3Fact, "first fact", episodeTestTime.Format(time.RFC3339))
	id2 := addRow(t, st, memory.L3Fact, "second fact", episodeTestTime.Add(10*time.Minute).Format(time.RFC3339))
	id3 := addRow(t, st, memory.L3Fact, "third fact", episodeTestTime.Add(50*time.Minute).Format(time.RFC3339))

	e.Note("u", "a", []string{id1}, episodeTestTime)
	e.Note("u", "a", []string{id2}, episodeTestTime.Add(10*time.Minute))
	if open, pending := e.Counts(); open != 1 || pending != 0 {
		t.Fatalf("after two close writes: open=%d pending=%d, want 1 and 0", open, pending)
	}
	if got := e.open["u|a"].FactIDs; len(got) != 2 || got[0] != id1 || got[1] != id2 {
		t.Fatalf("open episode facts = %v, want [%s %s]", got, id1, id2)
	}

	e.Note("u", "a", []string{id3}, episodeTestTime.Add(50*time.Minute))
	if open, pending := e.Counts(); open != 1 || pending != 1 {
		t.Fatalf("after a write past the gap: open=%d pending=%d, want 1 and 1", open, pending)
	}
	if got := e.open["u|a"].FactIDs; len(got) != 1 || got[0] != id3 {
		t.Fatalf("new episode facts = %v, want [%s]", got, id3)
	}
}

// TestEpisodeTickWritesSummary checks that a pending episode with live facts is
// summarised into one L4 row, that the episode leaves pending, and that the model
// was sent each fact.
func TestEpisodeTickWritesSummary(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	stub, llm := newEpisodeLLMStub(t, http.StatusOK, "Worked on the parser and fixed the bug.")
	e := NewEpisodes(t.TempDir(), st, llm, 30*time.Minute)

	id1 := addRow(t, st, memory.L3Fact, "alpha parser change", episodeTestTime.Format(time.RFC3339))
	id2 := addRow(t, st, memory.L3Fact, "beta bug fixed", episodeTestTime.Add(5*time.Minute).Format(time.RFC3339))
	e.Note("u", "a", []string{id1}, episodeTestTime)
	e.Note("u", "a", []string{id2}, episodeTestTime.Add(5*time.Minute))

	// One hour later the open episode is past the gap, so the tick closes and summarises it.
	e.tick(context.Background(), episodeTestTime.Add(time.Hour))

	if _, pending := e.Counts(); pending != 0 {
		t.Fatalf("pending = %d after a successful summary, want 0", pending)
	}
	if n := stub.calls.Load(); n != 1 {
		t.Fatalf("model calls = %d, want 1", n)
	}
	stub.mu.Lock()
	body := stub.bodies[0]
	stub.mu.Unlock()
	for _, text := range []string{"alpha parser change", "beta bug fixed"} {
		if !strings.Contains(body, text) {
			t.Errorf("request body does not contain fact %q", text)
		}
	}

	rows, _ := st.List(memory.L4Summary, "u", "a", 10, 0, false)
	if len(rows) != 1 {
		t.Fatalf("L4 rows = %d, want 1", len(rows))
	}
	d := rows[0]
	if d.Meta["kind"] != kindEpisode {
		t.Errorf("kind = %q, want %q", d.Meta["kind"], kindEpisode)
	}
	if want := strings.Join([]string{id1, id2}, ","); d.Meta["source_ids"] != want {
		t.Errorf("source_ids = %q, want %q", d.Meta["source_ids"], want)
	}
	if want := episodeTestTime.Add(5 * time.Minute).Format(time.RFC3339); d.Meta["ts"] != want {
		t.Errorf("ts = %q, want episode last write %q", d.Meta["ts"], want)
	}
}

// TestEpisodeSkipsWithOneLiveFact checks that an episode whose other fact was
// superseded is dropped without any model call and writes no summary.
func TestEpisodeSkipsWithOneLiveFact(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	stub, llm := newEpisodeLLMStub(t, http.StatusOK, "unused")
	e := NewEpisodes(t.TempDir(), st, llm, 30*time.Minute)

	id1 := addRow(t, st, memory.L3Fact, "kept fact", episodeTestTime.Format(time.RFC3339))
	id2 := addRow(t, st, memory.L3Fact, "retired fact", episodeTestTime.Add(time.Minute).Format(time.RFC3339))
	e.Note("u", "a", []string{id1}, episodeTestTime)
	e.Note("u", "a", []string{id2}, episodeTestTime.Add(time.Minute))
	if _, err := st.Supersede([]string{id2}, ""); err != nil {
		t.Fatal(err)
	}

	e.tick(context.Background(), episodeTestTime.Add(time.Hour))

	if n := stub.calls.Load(); n != 0 {
		t.Fatalf("model calls = %d, want 0", n)
	}
	if _, pending := e.Counts(); pending != 0 {
		t.Fatalf("pending = %d, want 0", pending)
	}
	if rows, _ := st.List(memory.L4Summary, "u", "a", 10, 0, false); len(rows) != 0 {
		t.Fatalf("L4 rows = %d, want 0", len(rows))
	}
}

// TestEpisodeDroppedAfterMaxTries checks that an episode whose model keeps failing
// gets a retry time after each failure below the max, and is dropped on the
// episodeMaxTries-th failure with no L4 row. Each loop advances the clock past the
// longest backoff, so every tick is due.
func TestEpisodeDroppedAfterMaxTries(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	stub, llm := newEpisodeLLMStub(t, http.StatusInternalServerError, "")
	e := NewEpisodes(t.TempDir(), st, llm, 30*time.Minute)

	id1 := addRow(t, st, memory.L3Fact, "one fact", episodeTestTime.Format(time.RFC3339))
	id2 := addRow(t, st, memory.L3Fact, "another fact", episodeTestTime.Add(time.Minute).Format(time.RFC3339))
	e.Note("u", "a", []string{id1}, episodeTestTime)
	e.Note("u", "a", []string{id2}, episodeTestTime.Add(time.Minute))

	ctx := context.Background()
	now := episodeTestTime.Add(time.Hour)
	for want := 1; want <= episodeMaxTries; want++ {
		now = now.Add(episodeRetryMax + time.Minute)
		e.tick(ctx, now)
		e.mu.Lock()
		if want < episodeMaxTries {
			if len(e.pending) != 1 || e.pending[0].Tries != want {
				t.Fatalf("after tick %d: pending=%d, want 1 episode with Tries=%d", want, len(e.pending), want)
			}
			if e.pending[0].NextTry == "" {
				t.Fatalf("after tick %d: NextTry is empty, want a retry time", want)
			}
		} else if len(e.pending) != 0 {
			t.Fatalf("after tick %d: pending=%d, want 0 (dropped)", want, len(e.pending))
		}
		e.mu.Unlock()
	}
	if n := stub.calls.Load(); n != int32(episodeMaxTries) {
		t.Fatalf("model calls = %d, want %d", n, episodeMaxTries)
	}
	if rows, _ := st.List(memory.L4Summary, "u", "a", 10, 0, false); len(rows) != 0 {
		t.Fatalf("L4 rows = %d, want 0", len(rows))
	}
}

// TestEpisodeRetryWaitsForBackoff checks that a failed summary waits its backoff
// before the next model call. A tick inside the wait makes no call, and a tick after
// the wait makes exactly one.
func TestEpisodeRetryWaitsForBackoff(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	stub, llm := newEpisodeLLMStub(t, http.StatusInternalServerError, "")
	e := NewEpisodes(t.TempDir(), st, llm, 30*time.Minute)

	id1 := addRow(t, st, memory.L3Fact, "one fact", episodeTestTime.Format(time.RFC3339))
	id2 := addRow(t, st, memory.L3Fact, "another fact", episodeTestTime.Add(time.Minute).Format(time.RFC3339))
	e.Note("u", "a", []string{id1}, episodeTestTime)
	e.Note("u", "a", []string{id2}, episodeTestTime.Add(time.Minute))

	ctx := context.Background()
	now := episodeTestTime.Add(time.Hour)
	e.tick(ctx, now)
	if n := stub.calls.Load(); n != 1 {
		t.Fatalf("calls after first tick = %d, want 1", n)
	}
	e.mu.Lock()
	if len(e.pending) != 1 || e.pending[0].Tries != 1 {
		e.mu.Unlock()
		t.Fatalf("after first failure: pending=%d, want 1 episode with Tries=1", len(e.pending))
	}
	wantNext := now.Add(5 * time.Minute).UTC().Format(time.RFC3339)
	if got := e.pending[0].NextTry; got != wantNext {
		e.mu.Unlock()
		t.Fatalf("NextTry = %q, want %q", got, wantNext)
	}
	e.mu.Unlock()

	e.tick(ctx, now.Add(4*time.Minute))
	if n := stub.calls.Load(); n != 1 {
		t.Fatalf("calls after tick inside backoff = %d, want still 1", n)
	}
	e.mu.Lock()
	if e.pending[0].Tries != 1 {
		e.mu.Unlock()
		t.Fatalf("Tries after tick inside backoff = %d, want 1", e.pending[0].Tries)
	}
	e.mu.Unlock()

	e.tick(ctx, now.Add(6*time.Minute))
	if n := stub.calls.Load(); n != 2 {
		t.Fatalf("calls after tick past backoff = %d, want 2", n)
	}
}

// TestEpisodeRetryDelay checks the backoff for each failure count: doubling from
// episodeRetryBase, capped at episodeRetryMax, and a count below 1 treated as 1.
func TestEpisodeRetryDelay(t *testing.T) {
	cases := []struct {
		tries int
		want  time.Duration
	}{
		{tries: 0, want: 5 * time.Minute},
		{tries: 1, want: 5 * time.Minute},
		{tries: 2, want: 10 * time.Minute},
		{tries: 5, want: 80 * time.Minute},
		{tries: 6, want: 2 * time.Hour},
		{tries: 10, want: 2 * time.Hour},
	}
	for _, c := range cases {
		if got := episodeRetryDelay(c.tries); got != c.want {
			t.Errorf("episodeRetryDelay(%d) = %v, want %v", c.tries, got, c.want)
		}
	}
}

// TestEpisodeLoadsOldStateWithoutNextTry checks that an episodes.json written before
// next_try existed still loads, and that its pending episode is due at once and gets
// summarised on the next tick.
func TestEpisodeLoadsOldStateWithoutNextTry(t *testing.T) {
	dir := t.TempDir()
	st := openHistoryStore(t, t.TempDir())
	stub, llm := newEpisodeLLMStub(t, http.StatusOK, "Two facts were recorded.")

	id1 := addRow(t, st, memory.L3Fact, "old fact one", episodeTestTime.Format(time.RFC3339))
	id2 := addRow(t, st, memory.L3Fact, "old fact two", episodeTestTime.Add(time.Minute).Format(time.RFC3339))

	old := `{"open":{},"pending":[{"user":"u","agent":"a",` +
		`"start":"` + episodeTestTime.Format(time.RFC3339) + `",` +
		`"last":"` + episodeTestTime.Add(time.Minute).Format(time.RFC3339) + `",` +
		`"fact_ids":["` + id1 + `","` + id2 + `"],"tries":1}]}`
	if err := os.WriteFile(filepath.Join(dir, episodesFile), []byte(old), 0o644); err != nil {
		t.Fatal(err)
	}

	e := NewEpisodes(dir, st, llm, 30*time.Minute)
	if _, pending := e.Counts(); pending != 1 {
		t.Fatalf("loaded pending = %d, want 1", pending)
	}

	e.tick(context.Background(), episodeTestTime.Add(time.Hour))
	if n := stub.calls.Load(); n != 1 {
		t.Fatalf("model calls = %d, want 1", n)
	}
	if rows, _ := st.List(memory.L4Summary, "u", "a", 10, 0, false); len(rows) != 1 {
		t.Fatalf("L4 rows = %d, want 1", len(rows))
	}
}

// TestEpisodePersistsAcrossRestart checks that an open episode written to disk is
// loaded again by a new Episodes built from the same directory.
func TestEpisodePersistsAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	st := openHistoryStore(t, t.TempDir())
	llm := NewLLMClient("http://unused.invalid", "k", "m")
	e := NewEpisodes(dir, st, llm, 30*time.Minute)

	id1 := addRow(t, st, memory.L3Fact, "persisted fact", episodeTestTime.Format(time.RFC3339))
	e.Note("u", "a", []string{id1}, episodeTestTime)

	again := NewEpisodes(dir, st, llm, 30*time.Minute)
	if open, pending := again.Counts(); open != 1 || pending != 0 {
		t.Fatalf("reloaded open=%d pending=%d, want 1 and 0", open, pending)
	}
	if got := again.open["u|a"].FactIDs; len(got) != 1 || got[0] != id1 {
		t.Fatalf("reloaded facts = %v, want [%s]", got, id1)
	}
}

// TestNewEpisodesNilSafe checks that NewEpisodes returns nil when the gap is zero
// or there is no model, and that every method is a safe no-op on nil.
func TestNewEpisodesNilSafe(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	llm := NewLLMClient("http://unused.invalid", "k", "m")
	if e := NewEpisodes(t.TempDir(), st, llm, 0); e != nil {
		t.Fatal("gap 0 must return nil")
	}
	if e := NewEpisodes(t.TempDir(), st, nil, 30*time.Minute); e != nil {
		t.Fatal("nil llm must return nil")
	}

	var e *Episodes
	e.Note("u", "a", []string{"x"}, episodeTestTime)
	e.tick(context.Background(), episodeTestTime)
	if open, pending := e.Counts(); open != 0 || pending != 0 {
		t.Fatalf("nil Counts = %d, %d; want 0, 0", open, pending)
	}
}

// A Note with no facts and no open episode has nothing to record, so it must not
// write episodes.json.
func TestEpisodeNoteWithoutFactsSkipsWrite(t *testing.T) {
	dir := t.TempDir()
	llm := NewLLMClient("http://unused.invalid", "k", "m")
	e := NewEpisodes(dir, nil, llm, 30*time.Minute)
	e.Note("u", "a", nil, episodeTestTime)
	if _, err := os.Stat(filepath.Join(dir, episodesFile)); !os.IsNotExist(err) {
		t.Fatalf("episodes.json written for a Note with no facts: stat err = %v", err)
	}
	if open, pending := e.Counts(); open != 0 || pending != 0 {
		t.Fatalf("Counts = %d, %d; want 0, 0", open, pending)
	}
}

// TestResolveEpisodeGap checks the environment variable rules: unset gives the
// default, "off" disables, a duration is used as given, and junk falls back.
func TestResolveEpisodeGap(t *testing.T) {
	cases := []struct {
		env  string
		set  bool
		want time.Duration
	}{
		{set: false, want: 30 * time.Minute},
		{env: "off", set: true, want: 0},
		{env: "10m", set: true, want: 10 * time.Minute},
		{env: "x", set: true, want: 30 * time.Minute},
		{env: "no", set: true, want: 0},
		{env: "90", set: true, want: 90 * time.Second},
		{env: "-10m", set: true, want: 30 * time.Minute},
	}
	for _, c := range cases {
		if c.set {
			t.Setenv("HYATLAS_EPISODE_GAP", c.env)
		} else {
			t.Setenv("HYATLAS_EPISODE_GAP", "")
		}
		if got := resolveEpisodeGap(); got != c.want {
			t.Errorf("HYATLAS_EPISODE_GAP=%q: got %v, want %v", c.env, got, c.want)
		}
	}
}
