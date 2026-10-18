package main

import (
	"context"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// countingEmbedder wraps an Embedder and counts the calls made to it.
type countingEmbedder struct {
	inner Embedder
	calls atomic.Int64
}

// Embed counts the call and embeds text with the wrapped embedder.
func (c *countingEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	c.calls.Add(1)
	return c.inner.Embed(ctx, text)
}

// TestHybridSearchEmbedsQueryOnce checks that one hybrid search with a time phrase
// and an entity name embeds the query a single time, not once per step.
func TestHybridSearchEmbedsQueryOnce(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ce := &countingEmbedder{inner: NewLocalEmbedder(384)}
	dir := t.TempDir()
	st, err := NewMemoryStore(context.Background(), dir, ce, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	t.Cleanup(st.Close)
	srv.store = st
	if _, _, err := st.pages.Apply("u", "a", "s1", []PageUpdate{{Name: "HyAtlas", Kind: "project"}}, nil); err != nil {
		t.Fatal(err)
	}
	addRow(t, st, memory.L3Fact, "HyAtlas shipped the search refactor last week", nowTS())

	ce.calls.Store(0)
	body := `{"query":"what did HyAtlas ship last week","user_id":"u","agent_id":"a","min_score":0}`
	w := httptest.NewRecorder()
	srv.handleSearch(w, httptest.NewRequest("POST", "/api/v1/search", strings.NewReader(body)))
	if w.Code != 200 {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if n := ce.calls.Load(); n != 1 {
		t.Errorf("query embedded %d times, want 1", n)
	}
}

// TestQueryEntitiesSeesNewAliasOnNextSearch checks that the per-owner name cache is
// dropped when a page gains an alias, so the alias is found by the next query.
func TestQueryEntitiesSeesNewAliasOnNextSearch(t *testing.T) {
	store, _ := newPagesStore(t)
	if _, _, err := store.pages.Apply("u", "a", "s1", []PageUpdate{{Name: "HyAtlas", Kind: "project"}}, nil); err != nil {
		t.Fatal(err)
	}
	if got := store.queryEntities("notes on atlas memory", []string{"u"}, "a"); len(got) != 0 {
		t.Fatalf("before the alias: got %v, want none", got)
	}
	id := store.pages.List("u", "a")[0].ID
	if _, _, err := store.pages.Apply("u", "a", "s2", []PageUpdate{{Page: id, Name: "Atlas Memory"}}, map[string]bool{id: true}); err != nil {
		t.Fatal(err)
	}
	got := store.queryEntities("notes on atlas memory", []string{"u"}, "a")
	if len(got) != 1 || normalizeName(got[0]) != "atlas memory" {
		t.Errorf("after the alias: got %v, want Atlas Memory", got)
	}
	// A page created later is found too.
	if _, _, err := store.pages.Apply("u", "a", "s3", []PageUpdate{{Name: "Zephyr", Kind: "tool"}}, nil); err != nil {
		t.Fatal(err)
	}
	if got := store.queryEntities("zephyr status", []string{"u"}, "a"); len(got) != 1 {
		t.Errorf("new page: got %v, want Zephyr", got)
	}
}

// TestRerankByFactorKeepsOrderWithUnitFactor checks that a factor of 1 leaves the
// order alone and a large factor lifts a lower hit.
func TestRerankByFactorKeepsOrderWithUnitFactor(t *testing.T) {
	hits := []SearchHit{{ID: "a"}, {ID: "b"}, {ID: "c"}}
	same := rerankByFactor(hits, func(int, SearchHit) float64 { return 1 })
	for i, h := range same {
		if h.ID != hits[i].ID {
			t.Fatalf("unit factor reordered: %v", same)
		}
	}
	lifted := rerankByFactor(hits, func(_ int, h SearchHit) float64 {
		if h.ID == "c" {
			return 10
		}
		return 1
	})
	if lifted[0].ID != "c" {
		t.Errorf("boosted hit not first: %v", lifted)
	}
}
