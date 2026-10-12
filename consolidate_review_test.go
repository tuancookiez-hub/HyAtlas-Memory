package main

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"
)

// When every call of a pass fails (two or more), the provider was down, not the
// clusters, so no cluster is charged a failure and the pass says so.
func TestAllCallsFailingIsNotChargedToClusters(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for _, who := range []string{"alice", "bob"} {
		seedSourced(t, srv.store, who, "x1", who+"-1", who+" one")
		seedSourced(t, srv.store, who, "x1", who+"-2", who+" two")
	}
	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusServiceUnavailable, `{"error":"down"}`
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)

	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n := len(log.all()); n != 2 {
		t.Fatalf("calls = %d, want 2 (one per owner)", n)
	}
	for key, oc := range c.clusters {
		for _, cs := range oc.Clusters {
			if cs.Fails != 0 {
				t.Errorf("%s cluster %s Fails = %d after a provider-wide failure, want 0", key, cs.ID, cs.Fails)
			}
		}
	}
	if !strings.Contains(strings.Join(rep.Errors, " "), "not counted") {
		t.Errorf("errors = %v, want a note that the failures were not counted", rep.Errors)
	}
}

// A cluster left alone after maxWindowFails is due again once failCooldown has
// passed, even though its members never changed.
func TestSkippedClusterIsRetriedAfterCooldown(t *testing.T) {
	cs := &clusterState{ID: "c1", FactIDs: []string{"a", "b"}}
	cs.Fails, cs.FailFP = maxWindowFails, cs.fp()
	cs.FailedAt = time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	if cs.due() {
		t.Error("a cluster that just used up its failures is due; want it left alone")
	}
	cs.FailedAt = time.Now().Add(-failCooldown - time.Minute).UTC().Format(time.RFC3339)
	if !cs.due() {
		t.Error("a skipped cluster past failCooldown is not due; want it retried")
	}
}

// One person's facts under two alias IDs are consolidated as one owner: both reach
// the model in the same call, and the cluster keeps both after the call.
func TestAliasIDsAreConsolidatedTogether(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedSourced(t, srv.store, "discord-1", "x1", "d-1", "extraction model is poolside")
	seedSourced(t, srv.store, "default", "x1", "c-1", "extraction model is now stepfun")
	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	c.SetAliases(aliasMap([][]string{{"discord-1", "default"}}))

	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	prompts := log.all()
	if len(prompts) != 1 || !strings.Contains(prompts[0], "poolside") || !strings.Contains(prompts[0], "stepfun") {
		t.Fatalf("calls = %d; want one call holding both alias IDs' facts", len(prompts))
	}
	if rep.OwnersRun != 1 {
		t.Errorf("OwnersRun = %d, want 1 (one person)", rep.OwnersRun)
	}
	oc := c.clusters[scopeKey{user: "discord-1", agent: "x1"}.String()]
	if oc == nil || len(oc.Clusters) != 1 || len(oc.Clusters[0].FactIDs) != 2 || oc.Clusters[0].DoneFP == "" {
		t.Fatalf("clusters = %+v; want one done cluster keeping both facts under the canonical ID", oc)
	}
}

// Run starts with a catch-up pass when the stored state has work, and waits the
// full interval when every cluster is done.
func TestPendingAtStart(t *testing.T) {
	c := &Consolidator{clusters: map[string]*ownerClusters{}}
	if !c.pendingAtStart() {
		t.Error("no stored clusters: want pending (they still have to be built)")
	}
	done := &clusterState{ID: "c1", FactIDs: []string{"a", "b"}}
	done.DoneFP = done.fp()
	c.clusters["o"] = &ownerClusters{Clusters: []*clusterState{done}}
	if c.pendingAtStart() {
		t.Error("every cluster done: want no pending work")
	}
	c.clusters["o"].Clusters = append(c.clusters["o"].Clusters, &clusterState{ID: "c2", FactIDs: []string{"c", "d"}})
	if !c.pendingAtStart() {
		t.Error("a due cluster: want pending")
	}
}

// callBudget follows measured call times: never below minCallBudget, 1.25 times
// the median, and capped at 80% of a pass so the first calls still start.
func TestCallBudgetFollowsMeasuredCalls(t *testing.T) {
	c := &Consolidator{}
	if got := c.callBudget(); got != minCallBudget {
		t.Errorf("no measurements: budget = %s, want minCallBudget %s", got, minCallBudget)
	}
	c.durations = []time.Duration{4 * time.Minute, 5 * time.Minute, 6 * time.Minute}
	if got, want := c.callBudget(), 5*time.Minute*5/4; got != want {
		t.Errorf("budget = %s, want %s (1.25 × median)", got, want)
	}
	c.durations = []time.Duration{20 * time.Minute}
	if got, want := c.callBudget(), consolidateTimeout*4/5; got != want {
		t.Errorf("budget = %s, want the cap %s", got, want)
	}
}
