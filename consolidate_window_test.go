package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
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

// A new fact before every pass dirties one cluster, and only that cluster is sent.
// The first pass covers the whole owner, oldest fact included; after it each pass
// makes exactly one call however many passes follow, because stable clusters keep
// a new fact from reshuffling the rest.
func TestChangingOwnerRunsOnlyItsDirtyCluster(t *testing.T) {
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

	if _, err := c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	reached := false
	for _, p := range log.all() {
		for _, id := range factIDs(p) {
			if id == oldest {
				reached = true
			}
		}
	}
	if !reached {
		t.Fatal("the first pass never sent the oldest fact")
	}

	for pass := 1; pass <= 4; pass++ {
		seedAt(t, srv.store, fmt.Sprintf("2026-02-01T00:%02d:00Z", pass), fmt.Sprintf("alice new %d", pass))
		before := len(log.all())
		rep, err := c.Once(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if n := len(log.all()) - before; n != 1 || rep.ClustersRun != 1 {
			t.Fatalf("pass %d after one new fact: calls=%d clusters_run=%d, want 1/1", pass, n, rep.ClustersRun)
		}
	}
}

// A cluster that fails every pass is left alone after maxWindowFails passes, so it
// stops spending calls, and the owner's other clusters are still consolidated.
func TestPoisonedClusterIsLeftAfterRepeatedFailures(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	// Newest first with batch 2 and 5 facts the clusters are [0,1], [2,3] and [3,4].
	// The poison fact (index 2) is only in the second.
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
	var callsPerPass []int
	for pass := 1; pass <= 6; pass++ {
		before := len(log.all())
		rep, err := c.Once(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		reports = append(reports, rep)
		callsPerPass = append(callsPerPass, len(log.all())-before)
	}

	poisoned := 0
	for _, p := range log.all() {
		if strings.Contains(p, "alice poison fact") {
			poisoned++
		}
	}
	if poisoned != maxWindowFails {
		t.Errorf("poisoned cluster was sent %d times, want exactly %d", poisoned, maxWindowFails)
	}
	// Pass 1 sends all three clusters, passes 2 and 3 only the failing one, and
	// nothing is sent after that.
	if fmt.Sprint(callsPerPass) != "[3 1 1 0 0 0]" {
		t.Errorf("calls per pass = %v, want [3 1 1 0 0 0]", callsPerPass)
	}
	reported := false
	for _, rep := range reports {
		if strings.Contains(strings.Join(rep.Errors, " "), "skipped") {
			reported = true
		}
	}
	if !reported {
		t.Error("giving up on the cluster was not reported")
	}
}

// A failure count belongs to one membership: a cluster that gave up becomes due
// again as soon as its facts change.
func TestFailedClusterIsDueAgainWhenMembershipChanges(t *testing.T) {
	cs := &clusterState{ID: "c1", FactIDs: []string{"a", "b"}}
	cs.Fails, cs.FailFP = maxWindowFails, cs.fp()
	if cs.due() {
		t.Fatal("a cluster that failed maxWindowFails times on its current members is due")
	}
	cs.FactIDs = append(cs.FactIDs, "c")
	if !cs.due() {
		t.Fatal("a cluster whose members changed is not due")
	}
	if one := (&clusterState{FactIDs: []string{"a"}}); one.due() {
		t.Fatal("a cluster of one fact is due: it has nothing to be reconciled against")
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

// Once makes a call for each due cluster and for none else: after a full pass an
// unchanged owner reports no clusters run and makes no call.
func TestOnceRunsDueClustersOnly(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for i := 0; i < 21; i++ {
		seedAt(t, srv.store, fmt.Sprintf("2026-01-01T00:%02d:00Z", i), fmt.Sprintf("alice old %d", i))
	}

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 10)

	first, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if first.ClustersRun < 2 || first.ClustersPending != 0 || len(log.all()) != first.ClustersRun {
		t.Fatalf("first pass: clusters_run=%d pending=%d calls=%d, want every cluster run once",
			first.ClustersRun, first.ClustersPending, len(log.all()))
	}
	before := len(log.all())
	second, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if second.ClustersRun != 0 || len(log.all()) != before || second.OwnersUnchanged != 1 {
		t.Errorf("unchanged pass: clusters_run=%d calls=%d unchanged=%d, want 0/0/1",
			second.ClustersRun, len(log.all())-before, second.OwnersUnchanged)
	}
}

// A pass whose context leaves less than minCallBudget starts no call, and says how
// many clusters it left for the next pass.
func TestPassWithoutBudgetStartsNoCall(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for i := 0; i < 5; i++ {
		seedAt(t, srv.store, fmt.Sprintf("2026-01-01T00:%02d:00Z", i), fmt.Sprintf("alice fact %d", i))
	}
	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 2)

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	rep, err := c.Once(ctx) // a second is far under the default minCallBudget
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 0 {
		t.Fatalf("calls with no budget = %d, want 0", n)
	}
	if rep.ClustersPending != 3 || rep.ClustersRun != 0 || len(rep.SkippedOwners) != 1 {
		t.Errorf("pending=%d run=%d skipped=%v, want 3/0/one owner", rep.ClustersPending, rep.ClustersRun, rep.SkippedOwners)
	}
	// The next pass, with budget, does all of it.
	rep, err = c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.ClustersRun != 3 || rep.ClustersPending != 0 {
		t.Errorf("next pass run=%d pending=%d, want 3/0", rep.ClustersRun, rep.ClustersPending)
	}
}

// With parallel 2, two calls are in flight at once, never more, and never two
// clusters that share a fact. Five facts in batches of 2 give the clusters [0,1],
// [2,3] and [3,4], the last two overlapping on fact 3.
func TestClustersRunInParallelWithoutSharingFacts(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for i := 0; i < 5; i++ {
		seedAt(t, srv.store, fmt.Sprintf("2026-01-01T00:%02d:00Z", 5-i), fmt.Sprintf("alice fact %d", i))
	}
	var (
		mu       sync.Mutex
		inflight = map[string]int{}
		cur, max int
		overlap  bool
	)
	var log promptLog
	mock := scopeMock(t, &log, func(prompt string) (int, string) {
		ids := factIDs(prompt)
		mu.Lock()
		for _, id := range ids {
			if inflight[id] > 0 {
				overlap = true
			}
			inflight[id]++
		}
		cur++
		if cur > max {
			max = cur
		}
		mu.Unlock()
		// Hold until a second call arrives, so a serial scheduler is caught.
		for end := time.Now().Add(time.Second); time.Now().Before(end); time.Sleep(5 * time.Millisecond) {
			mu.Lock()
			both := cur >= 2
			mu.Unlock()
			if both {
				break
			}
		}
		time.Sleep(40 * time.Millisecond)
		mu.Lock()
		for _, id := range ids {
			inflight[id]--
		}
		cur--
		mu.Unlock()
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 2)
	c.parallel = 2

	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.ClustersRun != 3 {
		t.Fatalf("ClustersRun = %d, want 3", rep.ClustersRun)
	}
	mu.Lock()
	defer mu.Unlock()
	if max != 2 {
		t.Errorf("most calls in flight at once = %d, want 2", max)
	}
	if overlap {
		t.Error("two clusters that share a fact ran at the same time")
	}
}

// Run starts the next pass after the catch-up delay, not the full interval, while a
// pass has left clusters pending. Each pass here has time for one call; at every=1.5s
// three passes on the full interval take over 4s, and with catch-up under 3s.
func TestRunCatchesUpWhilePending(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for i := 0; i < 5; i++ {
		seedAt(t, srv.store, fmt.Sprintf("2026-01-01T00:%02d:00Z", i), fmt.Sprintf("alice fact %d", i))
	}
	old := minCallBudget
	minCallBudget = 400 * time.Millisecond
	t.Cleanup(func() { minCallBudget = old })

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		time.Sleep(300 * time.Millisecond)
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), 1500*time.Millisecond, 0, 2)
	c.parallel = 1
	c.catchup = 10 * time.Millisecond
	c.timeout = 600 * time.Millisecond

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { c.Run(ctx); close(done) }()
	deadline := time.Now().Add(4 * time.Second)
	for len(log.all()) < 3 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	cancel()
	<-done
	if n := len(log.all()); n != 3 {
		t.Fatalf("calls = %d within 4s, want 3: the catch-up pass did not follow early", n)
	}
	if runs, _, _ := c.Stats(); runs < 3 {
		t.Errorf("passes = %d, want at least 3 (one call fits each pass)", runs)
	}
}

// A state file from before stable clusters (owners and windows, no clusters) loads,
// and its owner has its clusters built and run.
func TestOldWindowStateFileBuildsClusters(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "alice", "a1", "ra-1", "alice one")
	seedSourced(t, srv.store, "alice", "a1", "ra-2", "alice two")
	key := scopeKey{"alice", "a1"}.String()
	old := fmt.Sprintf(`{"cursor":1,"owners":{%q:"abc"},"windows":{%q:{"next":2,"covered":3,"count":4}}}`, key, key)
	if err := os.WriteFile(filepath.Join(filepath.Dir(srv.store.indexPath), consolidateStateFile), []byte(old), 0o644); err != nil {
		t.Fatal(err)
	}

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	if len(c.clusters) != 0 {
		t.Fatalf("an old state file produced %d owners of clusters, want none", len(c.clusters))
	}
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 1 || rep.ClustersRun != 1 {
		t.Errorf("calls=%d clusters_run=%d, want 1/1", n, rep.ClustersRun)
	}
	if oc := c.clusters[key]; oc == nil || len(oc.Clusters) != 1 {
		t.Errorf("clusters were not built and stored: %+v", oc)
	}
}

// The three scheduling variables are read when the consolidator is made, with the
// bounds the spec gives: parallel 1 to 4, catch-up positive.
func TestConsolidatorReadsSchedulingEnv(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	c := NewConsolidator(srv.store, nil, time.Hour, 0, 0)
	if c.parallel != 2 || c.catchup != 20*time.Minute || c.recluster != 0 {
		t.Errorf("defaults parallel=%d catchup=%s recluster=%s, want 2/20m/0", c.parallel, c.catchup, c.recluster)
	}
	t.Setenv("HYATLAS_CONSOLIDATE_PARALLEL", "9")
	t.Setenv("HYATLAS_CONSOLIDATE_CATCHUP", "0s")
	t.Setenv("HYATLAS_CONSOLIDATE_RECLUSTER", "48h")
	c = NewConsolidator(srv.store, nil, time.Hour, 0, 0)
	if c.parallel != 4 || c.catchup != 20*time.Minute || c.recluster != 48*time.Hour {
		t.Errorf("parallel=%d catchup=%s recluster=%s, want 4/20m/48h", c.parallel, c.catchup, c.recluster)
	}
	t.Setenv("HYATLAS_CONSOLIDATE_CATCHUP", "5m")
	t.Setenv("HYATLAS_CONSOLIDATE_PARALLEL", "1")
	c = NewConsolidator(srv.store, nil, time.Hour, 0, 0)
	if c.parallel != 1 || c.catchup != 5*time.Minute {
		t.Errorf("parallel=%d catchup=%s, want 1/5m", c.parallel, c.catchup)
	}
}
