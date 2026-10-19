package main

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

const storeTestTs = "2026-10-06T00:00:00Z"

func addFact(t *testing.T, st *MemoryStore, layer memory.Layer, id, text string, meta map[string]string) {
	t.Helper()
	m := map[string]string{"user_id": "u", "agent_id": "a", "ts": storeTestTs}
	for k, v := range meta {
		m[k] = v
	}
	if err := st.Add(layer, id, text, m); err != nil {
		t.Fatalf("add %s: %v", id, err)
	}
}

// Search over-fetches each layer by that layer's superseded count. The test puts
// the superseded rows nearest the query, with more live rows than the limit, so a
// fixed over-fetch would return no live hits. The count must also survive a
// reopen, which rebuilds it from the index file.
func TestSearchFillsLimitWhenNearestAreSuperseded(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	em := NewLocalEmbedder(384)
	graphPath := filepath.Join(dir, "graph.json")

	st, err := NewMemoryStore(ctx, dir, em, graphPath)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	const query = "the release notes say port is 4471 for the staging cluster"
	var dead []string
	for i, text := range []string{query, query + " again", query + " noted", query + " twice"} {
		id := fmt.Sprintf("dead-%d", i)
		addFact(t, st, memory.L3Fact, id, text, nil)
		dead = append(dead, id)
	}
	for i, text := range []string{"ok", "no", "yes"} {
		addFact(t, st, memory.L3Fact, fmt.Sprintf("live-%d", i), text, nil)
	}
	if n, err := st.Supersede(dead, "merged"); err != nil || n != len(dead) {
		t.Fatalf("Supersede = %d, %v; want %d, nil", n, err, len(dead))
	}

	// Precondition: the superseded rows really are the nearest neighbours, so an
	// un-widened query would have been all dead.
	raw, err := st.cols[memory.L3Fact].Query(ctx, query, 2, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range raw {
		if _, ok := st.superseded[r.ID]; !ok {
			t.Fatalf("precondition: nearest neighbour %s is live; test does not exercise over-fetch", r.ID)
		}
	}

	check := func(label string, st *MemoryStore) {
		t.Helper()
		hits, err := st.Search(query, 2, memory.L3Fact, "", "")
		if err != nil {
			t.Fatal(err)
		}
		if len(hits) != 2 {
			t.Fatalf("%s: Search returned %d hits, want 2 live results", label, len(hits))
		}
		for _, h := range hits {
			if len(h.ID) < 5 || h.ID[:5] != "live-" {
				t.Errorf("%s: Search returned superseded or unexpected row %s", label, h.ID)
			}
		}
	}
	check("before reopen", st)
	st.Close()

	st2, err := NewMemoryStore(ctx, dir, em, graphPath)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer st2.Close()
	st2.mu.RLock()
	if got := st2.hidden[memory.L3Fact]; got != len(dead) {
		t.Errorf("hidden[l3_fact] after reopen = %d, want %d", got, len(dead))
	}
	st2.mu.RUnlock()
	check("after reopen", st2)
}

// MirrorsOf finds the live L1 rows that mirror an L3 preference: same source_id
// and same content. A row with another source, another content, or already
// superseded is not a mirror.
func TestMirrorsOfFindsLiveMirrorsOnly(t *testing.T) {
	st := newTestServer(t, "m", "x").store
	pref := newID()
	addFact(t, st, memory.L3Fact, pref, "likes tea", map[string]string{"source_id": "raw-1"})

	mirror := newID()
	addFact(t, st, memory.L1Profile, mirror, "likes tea", map[string]string{"source_id": "raw-1"})
	addFact(t, st, memory.L1Profile, newID(), "likes tea", map[string]string{"source_id": "raw-9"})
	addFact(t, st, memory.L1Profile, newID(), "likes coffee", map[string]string{"source_id": "raw-1"})
	gone := newID()
	addFact(t, st, memory.L1Profile, gone, "likes tea", map[string]string{"source_id": "raw-1"})
	if _, err := st.Supersede([]string{gone}, pref); err != nil {
		t.Fatal(err)
	}

	got := st.MirrorsOf(memory.L1Profile, "raw-1", "likes tea")
	if len(got) != 1 || got[0] != mirror {
		t.Errorf("MirrorsOf = %v, want [%s]", got, mirror)
	}
	if got := st.MirrorsOf(memory.L1Profile, "", "likes tea"); len(got) != 0 {
		t.Errorf("empty source_id matched %v; it must match nothing", got)
	}

	// Once the mirror itself is superseded, no live mirror remains.
	if _, err := st.Supersede([]string{mirror}, pref); err != nil {
		t.Fatal(err)
	}
	if got := st.MirrorsOf(memory.L1Profile, "raw-1", "likes tea"); len(got) != 0 {
		t.Errorf("after superseding the mirror, MirrorsOf = %v, want none", got)
	}
}

// Supersede no longer holds the global lock across its chromem writes, so Adds
// run alongside it. Every target must end up superseded, every new row live, and
// the per-layer count must agree with the index.
func TestSupersedeRunsAlongsideConcurrentAdds(t *testing.T) {
	st := newTestServer(t, "m", "x").store
	var old []string
	for i := 0; i < 30; i++ {
		id := fmt.Sprintf("old-%d", i)
		addFact(t, st, memory.L3Fact, id, fmt.Sprintf("fact %d", i), nil)
		old = append(old, id)
	}

	var wg sync.WaitGroup
	errs := make(chan error, 64)
	wg.Add(1)
	go func() {
		defer wg.Done()
		if n, err := st.Supersede(old, "merged"); err != nil || n != len(old) {
			errs <- fmt.Errorf("Supersede = %d, %v; want %d, nil", n, err, len(old))
		}
	}()
	for g := 0; g < 3; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 10; i++ {
				if err := st.Add(memory.L3Fact, fmt.Sprintf("new-%d-%d", g, i), "fresh fact",
					map[string]string{"user_id": "u", "ts": storeTestTs}); err != nil {
					errs <- err
				}
			}
		}(g)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}

	if _, live := st.List(memory.L3Fact, "", "", 100, 0, false); live != 30 {
		t.Errorf("live facts = %d, want 30 new rows", live)
	}
	if _, all := st.ListAll(memory.L3Fact, "", "", 100, 0, false); all != 60 {
		t.Errorf("history rows = %d, want 60", all)
	}
	st.mu.RLock()
	hidden, supersededIDs := st.hidden[memory.L3Fact], len(st.superseded)
	st.mu.RUnlock()
	if hidden != len(old) || supersededIDs != len(old) {
		t.Errorf("superseded bookkeeping: hidden=%d set=%d, want %d", hidden, supersededIDs, len(old))
	}
	hits, err := st.Search("fresh fact", 5, memory.L3Fact, "", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 5 {
		t.Errorf("Search returned %d hits, want 5 live", len(hits))
	}
}
