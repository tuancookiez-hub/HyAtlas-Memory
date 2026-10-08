package main

import (
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

func TestL5DocIDIsStable(t *testing.T) {
	a := l5DocID("alice", "a1", "HyAtlas", "runs_on", "Port 19528")
	if b := l5DocID("alice", "a1", " hyatlas ", "RUNS_ON", "port 19528"); a != b {
		t.Errorf("same edge, different case/space: %s != %s", a, b)
	}
	if c := l5DocID("bob", "a1", "HyAtlas", "runs_on", "Port 19528"); a == c {
		t.Error("two owners' edges share an L5 document ID")
	}
	if got := l5Text("HyAtlas", "runs_on", "port 19528"); got != "HyAtlas runs on port 19528" {
		t.Errorf("l5Text = %q", got)
	}
}

// A graph edge with no L5 document is indexed once, can then be found by search
// in the L5 layer, and a second backfill adds nothing.
func TestBackfillL5IndexesEachEdgeOnce(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	if _, err := srv.store.Graph().AddEdgeWithSources("alice", "a1", "HyAtlas", "runs_on", "port 19528",
		[]string{"r1", "r2"}); err != nil {
		t.Fatal(err)
	}
	n, err := srv.store.BackfillL5()
	if err != nil || n != 1 {
		t.Fatalf("first backfill = %d, %v; want 1, nil", n, err)
	}
	if n, _ := srv.store.BackfillL5(); n != 0 {
		t.Errorf("second backfill indexed %d, want 0", n)
	}

	hits, err := srv.store.Search("HyAtlas runs on port 19528", 5, memory.L5Knowledge, "alice", "a1")
	if err != nil || len(hits) != 1 {
		t.Fatalf("L5 search = %d hits, %v; want 1", len(hits), err)
	}
	h := hits[0]
	if h.Content != "HyAtlas runs on port 19528" || h.Meta["relation"] != "runs_on" || h.Meta["source_id"] != "r1" {
		t.Errorf("L5 hit = %q meta %v", h.Content, h.Meta)
	}
	if other, _ := srv.store.Search("HyAtlas runs on port 19528", 5, memory.L5Knowledge, "bob", "a1"); len(other) != 0 {
		t.Error("another owner's search found alice's L5 document")
	}
}

// An edge written before owners were recorded has no owner. Under an owner filter it
// is visible, as the graph endpoints show it, while another owner's edge is not.
func TestOwnerlessL5IsVisibleUnderAnyOwner(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	g := srv.store.Graph()
	if _, err := g.AddEdgeWithSources("", "", "Foxtrot", "uses", "Postgres 16", []string{"r1"}); err != nil {
		t.Fatal(err)
	}
	if _, err := g.AddEdgeWithSources("bob", "a1", "Foxtrot", "uses", "MySQL 8", []string{"r2"}); err != nil {
		t.Fatal(err)
	}
	if n, err := srv.store.BackfillL5(); err != nil || n != 2 {
		t.Fatalf("backfill = %d, %v; want 2", n, err)
	}
	contents := func(hits []SearchHit) map[string]bool {
		m := map[string]bool{}
		for _, h := range hits {
			m[h.Content] = true
		}
		return m
	}

	vec, err := srv.store.SearchOwners("Foxtrot uses", 10, memory.L5Knowledge, []string{"alice"}, "a1")
	if err != nil {
		t.Fatal(err)
	}
	if got := contents(vec); !got["Foxtrot uses Postgres 16"] || got["Foxtrot uses MySQL 8"] {
		t.Errorf("vector search for alice = %v; want the ownerless edge and not bob's", got)
	}
	kw, err := srv.store.KeywordSearch("Foxtrot uses", 10, memory.L5Knowledge, []string{"alice"}, "a1")
	if err != nil {
		t.Fatal(err)
	}
	if got := contents(kw); !got["Foxtrot uses Postgres 16"] || got["Foxtrot uses MySQL 8"] {
		t.Errorf("keyword search for alice = %v; want the ownerless edge and not bob's", got)
	}
}

// With the dedupe threshold set, a relation whose text matches one already indexed
// for the same owner is not indexed again (the graph keeps both edges).
func TestIndexL5SkipsNearDuplicate(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	srv.store.l5Dedupe = 0.999
	g := srv.store.Graph()
	// "runs_on" and "runs on" are two edges with two IDs but the same text.
	for _, rel := range []string{"runs_on", "runs on"} {
		if _, err := g.AddEdgeWithSources("", "", "HyAtlas", rel, "port 19528", []string{"r-" + rel}); err != nil {
			t.Fatal(err)
		}
	}
	if n, err := srv.store.BackfillL5(); err != nil || n != 1 {
		t.Fatalf("backfill = %d, %v; want 1 (the second restates the first)", n, err)
	}
	if got := srv.store.Graph().EdgeCount(); got != 2 {
		t.Errorf("graph edges = %d, want 2 (the graph is not changed)", got)
	}
}
