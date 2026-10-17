package main

import (
	"context"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// A fact that arrives while a pass is in flight was never shown to the model, so
// its owner must stay due for the next pass.
func TestFactArrivingMidPassKeepsOwnerDue(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice two")

	var log promptLog
	injected := false
	mock := scopeMock(t, &log, func(string) (int, string) {
		if !injected {
			injected = true
			seedSourced(t, srv.store, "alice", "a1", "ra-late", "alice late")
		}
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	if _, err := c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 1 {
		t.Fatalf("first pass calls = %d, want 1", n)
	}
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 2 {
		t.Fatalf("second pass calls = %d, want 2 (owner must run again)", n)
	}
	if rep.OwnersRun != 1 {
		t.Errorf("second pass OwnersRun = %d, want 1", rep.OwnersRun)
	}
	if !strings.Contains(log.all()[1], "alice late") {
		t.Errorf("second pass did not see the fact that arrived mid-pass")
	}
}

// An owner with more facts than the batch cap is cut into clusters. Every fact is
// sent at least once in the first pass, and the owner is unchanged after it. A new
// fact then dirties exactly one cluster.
func TestClustersCoverEveryFactBeforeOwnerIsUnchanged(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for _, s := range []string{"r1", "r2", "r3", "r4", "r5"} {
		seedSourced(t, srv.store, "alice", "a1", s, "alice "+s)
	}

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 2)

	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, p := range log.all() {
		ids := factIDs(p)
		if len(ids) != 2 {
			t.Errorf("a call sent %d facts, want the batch of 2", len(ids))
		}
		for _, id := range ids {
			seen[id] = true
		}
	}
	if len(seen) != 5 || rep.ClustersRun != 3 {
		t.Errorf("first pass covered %d distinct facts in %d clusters, want all 5 in 3", len(seen), rep.ClustersRun)
	}

	before := len(log.all())
	rep, err = c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()) - before; n != 0 {
		t.Errorf("second pass made %d calls, want 0 (every cluster done)", n)
	}
	if rep.OwnersUnchanged != 1 {
		t.Errorf("OwnersUnchanged = %d, want 1", rep.OwnersUnchanged)
	}

	// A new fact dirties one cluster.
	seedSourced(t, srv.store, "alice", "a1", "r6", "alice r6")
	before = len(log.all())
	if rep, err = c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()) - before; n != 1 || rep.OwnersRun != 1 || rep.ClustersRun != 1 {
		t.Errorf("after a change: calls=%d owners_run=%d clusters_run=%d, want 1/1/1", n, rep.OwnersRun, rep.ClustersRun)
	}
}

// flakyEmbedder fails on demand, which makes the store's writes fail: Add embeds
// the row before writing it.
type flakyEmbedder struct {
	inner Embedder
	fail  atomic.Bool
}

// Semantic passes on whether the inner embedder's vectors carry meaning.
func (f *flakyEmbedder) Semantic() bool { return isSemantic(f.inner) }

func (f *flakyEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	if f.fail.Load() {
		return nil, errors.New("embedder down")
	}
	return f.inner.Embed(ctx, text)
}

// An owner whose merge write failed is not marked done, so the next pass sends it
// again. Without this the owner stays stuck until its facts change.
func TestOwnerWithFailedWriteIsRetried(t *testing.T) {
	dir := t.TempDir()
	em := &flakyEmbedder{inner: NewLocalEmbedder(384)}
	store, err := NewMemoryStore(context.Background(), dir, em, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(store.Close)
	seedSourced(t, store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, store, "alice", "a1", "ra-2", "alice two")

	var log promptLog
	mock := scopeMock(t, &log, mergeFirstTwo(t))
	defer mock.Close()
	c := NewConsolidator(store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)

	em.fail.Store(true)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.Errors) == 0 || !strings.Contains(strings.Join(rep.Errors, " "), "merge add") {
		t.Fatalf("Errors = %v, want the failed merge write reported", rep.Errors)
	}
	em.fail.Store(false)

	rep, err = c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 2 {
		t.Fatalf("calls = %d, want 2: the failed owner must be retried", n)
	}
	if rep.Merged != 1 {
		t.Errorf("retry Merged = %d, want 1", rep.Merged)
	}
}

// When only some absorbed originals are marked, the replacement stays live, so
// the marked originals point at a fact that exists. The failure is still reported.
func TestPartialMergeKeepsReplacementLive(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	a := seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	c := NewConsolidator(srv.store, nil, time.Hour, 0, 200)

	var errs []string
	fail := func(m string) { errs = append(errs, m) }
	rep := &Report{}
	absorbed := append(srv.store.GetMany([]string{a}), DocIndex{ID: "ghost-not-stored"})
	live := map[string]bool{a: true, "ghost-not-stored": true}
	id := c.applyMerge(scopeKey{user: "alice", agent: "a1"}, "merged text", nil, "consolidated",
		absorbed, time.Now().UTC().Format(time.RFC3339), live, rep, fail, fail)
	if id == "" {
		t.Fatal("replacement retracted although one original was marked")
	}
	if got := liveRows(t, srv.store, memory.L3Fact, "alice", "a1"); len(got) != 1 || got[0].ID != id {
		t.Errorf("live facts = %+v, want only the replacement %s", got, id)
	}
	orig := srv.store.GetMany([]string{a})
	if len(orig) != 1 || orig[0].Meta["superseded_by"] != id {
		t.Errorf("marked original does not point at the replacement: %+v", orig)
	}
	if len(errs) != 1 || !strings.Contains(errs[0], "1 of 2") {
		t.Errorf("errors = %v, want one partial-merge report", errs)
	}
	if rep.Merged != 1 {
		t.Errorf("Merged = %d, want 1", rep.Merged)
	}
}

// When no original is marked, the replacement has nothing pointing at it and is
// retracted.
func TestMergeWithNothingMarkedIsRetracted(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	c := NewConsolidator(srv.store, nil, time.Hour, 0, 200)
	var errs []string
	id := c.applyMerge(scopeKey{user: "alice", agent: "a1"}, "merged text", nil, "consolidated",
		[]DocIndex{{ID: "ghost-1"}, {ID: "ghost-2"}}, time.Now().UTC().Format(time.RFC3339),
		map[string]bool{}, &Report{}, func(m string) { errs = append(errs, m) }, func(m string) { errs = append(errs, m) })
	if id != "" {
		t.Errorf("returned id %q, want none", id)
	}
	if got := liveRows(t, srv.store, memory.L3Fact, "alice", "a1"); len(got) != 0 {
		t.Errorf("replacement left live with nothing absorbed: %+v", got)
	}
	if len(errs) != 1 {
		t.Errorf("errors = %v, want one report", errs)
	}
}

// An edge is in memory even when persisting it fails, so the report counts it
// and still names the failure.
func TestEdgeCountedWhenPersistFails(t *testing.T) {
	blocker := filepath.Join(t.TempDir(), "blocker")
	if err := os.WriteFile(blocker, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	store, err := NewMemoryStore(context.Background(), dir, NewLocalEmbedder(384),
		filepath.Join(blocker, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(store.Close)
	seedSourced(t, store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, store, "alice", "a1", "ra-2", "alice two")

	mock := scopeMock(t, &promptLog{}, func(prompt string) (int, string) {
		cons := Consolidation{Knowledge: []CitedRelation{{From: "x", Relation: "r", To: "y", Evidence: factIDs(prompt)}}}
		return http.StatusOK, chatBody(t, cons)
	})
	defer mock.Close()
	c := NewConsolidator(store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Edges != 1 {
		t.Errorf("Edges = %d, want 1 (the edge exists in memory)", rep.Edges)
	}
	if !strings.Contains(strings.Join(rep.Errors, " "), "edge:") {
		t.Errorf("Errors = %v, want the persist failure reported", rep.Errors)
	}
}

// The fingerprint depends on the IDs, not on a count and a timestamp, so a
// swapped fact with the same count and newest time gives a different value.
func TestFactsFingerprintTracksIDs(t *testing.T) {
	a := idsFingerprint([]string{"m1", "m2", "m3"})
	if a != idsFingerprint([]string{"m3", "m1", "m2"}) {
		t.Error("fingerprint depends on order")
	}
	if a == idsFingerprint([]string{"m1", "m2", "m4"}) {
		t.Error("swapping one ID left the fingerprint unchanged")
	}
}

// A watermark in the old count@timestamp format never matches, so its owner runs
// once rather than being skipped on a stale value.
func TestOldFormatWatermarkRunsOwnerOnce(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice two")
	state := `{"cursor":0,"owners":{"user_id=\"alice\" agent_id=\"a1\"":"2@2026-10-06T00:00:00Z"}}`
	if err := os.WriteFile(filepath.Join(filepath.Dir(srv.store.indexPath), consolidateStateFile), []byte(state), 0o644); err != nil {
		t.Fatal(err)
	}

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	if _, err := c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 1 {
		t.Errorf("calls = %d, want 1: an old-format watermark must not skip the owner", n)
	}
}
