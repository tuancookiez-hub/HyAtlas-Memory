package main

import (
	"context"
	"fmt"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// seedAt writes one L3 fact for alice/a1 with an explicit timestamp, so the
// newest-first order that windows are cut from is known rather than a tie broken
// by random IDs.
func seedAt(t *testing.T, store *MemoryStore, ts, text string) string {
	t.Helper()
	id := newID()
	if err := store.Add(memory.L3Fact, id, text, map[string]string{
		"user_id": "alice", "agent_id": "a1", "source_id": "src-" + id, "ts": ts,
	}); err != nil {
		t.Fatalf("seed %q: %v", text, err)
	}
	return id
}

// A new fact before every pass used to reset the owner to its newest window, so the
// oldest window was never sent. With 3 windows (batch 10, 21 facts) the oldest
// window holds the oldest fact, and it must be reached within 6 changed passes.
func TestChangingOwnerReachesOldestWindow(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	oldest := seedAt(t, srv.store, "2026-01-01T00:00:00Z", "alice old 0")
	for i := 1; i < 21; i++ {
		seedAt(t, srv.store, fmt.Sprintf("2026-01-01T00:%02d:00Z", i), fmt.Sprintf("alice old %d", i))
	}

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 10)

	reached := false
	for pass := 1; pass <= 6 && !reached; pass++ {
		seedAt(t, srv.store, fmt.Sprintf("2026-02-01T00:%02d:00Z", pass), fmt.Sprintf("alice new %d", pass))
		before := len(log.all())
		if _, err := c.Once(context.Background()); err != nil {
			t.Fatal(err)
		}
		prompts := log.all()[before:]
		if len(prompts) != 1 {
			t.Fatalf("pass %d made %d calls, want 1", pass, len(prompts))
		}
		for _, id := range factIDs(prompts[0]) {
			if id == oldest {
				reached = true
			}
		}
	}
	if !reached {
		t.Fatal("owner that changed before every pass never reached its oldest window in 6 passes")
	}
}

// A window that fails every pass is given up on after maxWindowFails passes, so
// the walk moves on and the rest of the owner's facts still get consolidated.
func TestPoisonedWindowIsSkippedAfterRepeatedFailures(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	// Newest first with batch 2 and 5 facts: window 0 = [0,1], window 1 = [2,3],
	// window 2 = [3,4]. The poison fact (index 2) is only in window 1.
	for i := 0; i < 5; i++ {
		text := fmt.Sprintf("alice fact %d", i)
		if i == 2 {
			text = "alice poison fact"
		}
		seedAt(t, srv.store, fmt.Sprintf("2026-01-01T00:%02d:00Z", 5-i), text)
	}

	var log promptLog
	mock := scopeMock(t, &log, func(prompt string) (int, string) {
		if strings.Contains(prompt, "alice poison fact") {
			return http.StatusInternalServerError, `{"error":"poisoned"}`
		}
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 2)

	var reports []*Report
	for pass := 1; pass <= 6; pass++ {
		rep, err := c.Once(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		reports = append(reports, rep)
	}

	poisoned := 0
	for _, p := range log.all() {
		if strings.Contains(p, "alice poison fact") {
			poisoned++
		}
	}
	if poisoned != maxWindowFails {
		t.Errorf("poisoned window was sent %d times, want exactly %d", poisoned, maxWindowFails)
	}
	// The first pass is clean and the poisoned window is then sent on each later
	// pass, so the skip is reported on the pass that makes the last failure.
	reported := false
	for _, rep := range reports {
		if strings.Contains(strings.Join(rep.Errors, " "), "skipped") {
			reported = true
		}
	}
	if !reported {
		t.Error("the skipped window was not reported")
	}
	// After the skip the walk reaches window 2, which does not contain the poison.
	final := log.all()
	last := final[len(final)-1]
	if strings.Contains(last, "alice poison fact") {
		t.Fatal("last pass still sent the poisoned window")
	}
	if got := len(factIDs(last)); got != 2 {
		t.Errorf("window after the skip sent %d facts, want 2", got)
	}
}

// consolidateScope fails a pass only for fatal errors. Here the arc and the schema
// write fail (both soft), so the owner's watermark advances and the next pass makes
// no call. The failures are still reported.
func TestSoftWriteFailureDoesNotHoldOwnerBack(t *testing.T) {
	dir := t.TempDir()
	em := &flakyEmbedder{inner: NewLocalEmbedder(384)}
	store, err := NewMemoryStore(context.Background(), dir, em, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(store.Close)
	seedSourced(t, store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, store, "alice", "a1", "ra-2", "alice two")

	arc := "alice arc"
	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{
			Arc:     &arc,
			Schemas: []ConsolidatedSchema{{Pattern: "alice pattern"}},
		})
	})
	defer mock.Close()
	c := NewConsolidator(store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)

	em.fail.Store(true)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(rep.Errors, " ")
	if !strings.Contains(joined, "arc:") || !strings.Contains(joined, "schema:") {
		t.Fatalf("Errors = %v, want the arc and schema failures reported", rep.Errors)
	}
	em.fail.Store(false)

	before := len(log.all())
	if _, err := c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()) - before; n != 0 {
		t.Fatalf("calls after soft-only failures = %d, want 0: the owner must not be retried", n)
	}
}

// A fatal write failure (the merge replacement) holds the owner back, even when a
// soft failure happens in the same pass. The owner is sent again and the merge lands.
func TestFatalWriteFailureRetriesOwnerDespiteSoftFailures(t *testing.T) {
	dir := t.TempDir()
	em := &flakyEmbedder{inner: NewLocalEmbedder(384)}
	store, err := NewMemoryStore(context.Background(), dir, em, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(store.Close)
	seedSourced(t, store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, store, "alice", "a1", "ra-2", "alice two")

	arc := "alice arc"
	var log promptLog
	mock := scopeMock(t, &log, func(prompt string) (int, string) {
		ids := factIDs(prompt)
		cons := Consolidation{Arc: &arc}
		if len(ids) >= 2 {
			cons.Merges = []Merge{{Text: "merged", Supersedes: ids[:2]}}
		}
		return http.StatusOK, chatBody(t, cons)
	})
	defer mock.Close()
	c := NewConsolidator(store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)

	em.fail.Store(true)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(rep.Errors, " ")
	if !strings.Contains(joined, "merge add") || !strings.Contains(joined, "arc:") {
		t.Fatalf("Errors = %v, want both the fatal merge add and the soft arc failure", rep.Errors)
	}
	em.fail.Store(false)

	before := len(log.all())
	rep, err = c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()) - before; n != 1 {
		t.Fatalf("calls after a fatal failure = %d, want 1: the owner must be retried", n)
	}
	if rep.Merged != 1 {
		t.Errorf("retry Merged = %d, want 1", rep.Merged)
	}
}

// After an owner's walk has covered every window, one new fact must send the new
// facts against every older window again, not just the newest: a contradiction
// between a new fact and an old one is only visible if both are in one prompt.
func TestChangeAfterFullWalkRewalksOlderWindows(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	oldest := seedAt(t, srv.store, "2026-01-01T00:00:00Z", "alice old 0")
	for i := 1; i < 21; i++ {
		seedAt(t, srv.store, fmt.Sprintf("2026-01-01T00:%02d:00Z", i), fmt.Sprintf("alice old %d", i))
	}

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 10)

	// Quiet owner: walk until it stops being due.
	for pass := 0; pass < 10; pass++ {
		before := len(log.all())
		if _, err := c.Once(context.Background()); err != nil {
			t.Fatal(err)
		}
		if len(log.all()) == before {
			break
		}
	}
	before := len(log.all())
	if _, err := c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(log.all()) != before {
		t.Fatal("a fully walked, unchanged owner was sent again")
	}

	// One new fact, then no more changes: the walk must reach the oldest window again.
	seedAt(t, srv.store, "2026-03-01T00:00:00Z", "alice new")
	start := len(log.all())
	for pass := 0; pass < 6; pass++ {
		if _, err := c.Once(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	reached := false
	for _, p := range log.all()[start:] {
		for _, id := range factIDs(p) {
			if id == oldest {
				reached = true
			}
		}
	}
	if !reached {
		t.Fatal("after a change, the owner was never re-walked back to its oldest window")
	}
}
