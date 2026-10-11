package main

import (
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

func fusedIDs(hits []SearchHit) []string {
	ids := make([]string, len(hits))
	for i, h := range hits {
		ids[i] = h.ID
	}
	return ids
}

// A keyword hit that the vector ranking scored below the floor still comes back:
// identifiers are what embeddings score low.
func TestFuseKeepsKeywordHitBelowFloor(t *testing.T) {
	vec := []SearchHit{
		{ID: "v1", Content: "about port numbers", Score: 0.70, Layer: memory.L3Fact},
		{ID: "v2", Content: "about servers", Score: 0.65, Layer: memory.L3Fact},
		{ID: "k1", Content: "HyAtlas v4 listens on 19528", Score: 0.40, Layer: memory.L3Fact},
	}
	kw := []SearchHit{{ID: "k1", Content: "HyAtlas v4 listens on 19528", Score: 0.40, Layer: memory.L3Fact}}

	// k1 is first in the keyword ranking, so it ties v1 (first by vector) and
	// beats v2 (second by vector). The tie goes to the higher cosine.
	got := fusedIDs(fuseHits(vec, kw, 0.60, 5))
	if len(got) != 3 || got[0] != "v1" || got[1] != "k1" || got[2] != "v2" {
		t.Fatalf("fused = %v, want [v1 k1 v2]", got)
	}
}

// A vector hit below the floor with no keyword support is still dropped.
func TestFuseDropsWeakVectorOnlyHit(t *testing.T) {
	vec := []SearchHit{
		{ID: "good", Content: "relevant", Score: 0.80, Layer: memory.L3Fact},
		{ID: "weak", Content: "unrelated", Score: 0.52, Layer: memory.L3Fact},
	}
	got := fusedIDs(fuseHits(vec, nil, 0.60, 5))
	if len(got) != 1 || got[0] != "good" {
		t.Fatalf("fused = %v, want [good]", got)
	}
}

// With no keyword hits, fusion keeps the vector order, so a query with no usable
// terms behaves exactly as vector search did.
func TestFuseWithoutKeywordKeepsVectorOrder(t *testing.T) {
	vec := []SearchHit{
		{ID: "a", Content: "a", Score: 0.9, Layer: memory.L3Fact},
		{ID: "b", Content: "b", Score: 0.8, Layer: memory.L3Fact},
		{ID: "c", Content: "c", Score: 0.7, Layer: memory.L3Fact},
	}
	got := fusedIDs(fuseHits(vec, nil, 0, 2))
	if len(got) != 2 || got[0] != "a" || got[1] != "b" {
		t.Fatalf("fused = %v, want [a b]", got)
	}
}

// Repeated text is still collapsed after fusion, keeping the L1 Profile copy.
func TestFuseCollapsesMirrorText(t *testing.T) {
	vec := []SearchHit{
		{ID: "l3", Content: "Tuna starts the server by hand", Score: 0.8, Layer: memory.L3Fact},
		{ID: "l1", Content: "tuna starts the server by hand", Score: 0.8, Layer: memory.L1Profile},
	}
	got := fuseHits(vec, nil, 0, 5)
	if len(got) != 1 || got[0].Layer != memory.L1Profile {
		t.Fatalf("fused = %v, want one L1 Profile hit", fusedIDs(got))
	}
}

// Through the handler: an identifier the embedding scores below the floor is still
// found by the default (hybrid) search, and is not found by vector-only search.
func TestSearchHybridFindsIdentifierBelowFloor(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	srv.minScore = 0.999
	if err := srv.store.Add(memory.L3Fact, newID(), "HyAtlas v4 listens on port 19528",
		map[string]string{"user_id": "alice", "agent_id": "a1"}); err != nil {
		t.Fatal(err)
	}
	count := func(reader string) int {
		body := `{"query":"19528","user_id":"alice","agent_id":"a1","reader":"` + reader + `"}`
		w := httptest.NewRecorder()
		srv.handleSearch(w, httptest.NewRequest("POST", "/api/v1/search", strings.NewReader(body)))
		if w.Code != 200 {
			t.Fatalf("reader %q: status %d: %s", reader, w.Code, w.Body.String())
		}
		var resp struct {
			Memories map[string][]map[string]any `json:"memories"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatal(err)
		}
		n := 0
		for _, ch := range resp.Memories {
			n += len(ch)
		}
		return n
	}
	if n := count(""); n != 1 {
		t.Errorf("hybrid search found %d hits, want 1", n)
	}
	if n := count("hybrid_tag"); n != 1 {
		t.Errorf("keyword search found %d hits, want 1", n)
	}
	if n := count("legacy"); n != 0 {
		t.Errorf("vector-only search found %d hits, want 0 (below the floor)", n)
	}
}

func TestParseReader(t *testing.T) {
	cases := map[string]searchReader{
		"": readHybrid, "hybrid_v2": readHybrid, "anything": readHybrid,
		"legacy": readVector, "semantic": readVector,
		"hybrid_tag": readKeyword, "keyword": readKeyword,
	}
	for in, want := range cases {
		if got := parseReader(in); got != want {
			t.Errorf("parseReader(%q) = %v, want %v", in, got, want)
		}
	}
}

// L5 may take at most 40% of the slots of an all-layer search, so near-identical
// graph relations cannot crowd out the facts they summarise.
func TestCapLayerKeepsOrderAndLimitsL5(t *testing.T) {
	hits := []SearchHit{
		{ID: "k1", Layer: memory.L5Knowledge}, {ID: "k2", Layer: memory.L5Knowledge},
		{ID: "k3", Layer: memory.L5Knowledge}, {ID: "f1", Layer: memory.L3Fact},
		{ID: "k4", Layer: memory.L5Knowledge}, {ID: "p1", Layer: memory.L1Profile},
	}
	got := fusedIDs(capLayer(hits, memory.L5Knowledge, l5Share(5)))
	want := []string{"k1", "k2", "f1", "p1"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("capLayer = %v, want %v", got, want)
	}
	for limit, want := range map[int]int{1: 1, 2: 1, 5: 2, 10: 4} {
		if got := l5Share(limit); got != want {
			t.Errorf("l5Share(%d) = %d, want %d", limit, got, want)
		}
	}
}

func TestIdentifierTerms(t *testing.T) {
	cases := map[string][]string{
		"HYATLAS_SYNC_EXTRACT":              {"hyatlas", "sync", "extract"},
		"RSI(2)":                            {"rsi"},
		"19528":                             {"19528"},
		"Reg-T margin":                      {"reg"},
		"discord-tps plugin":                {"discord", "tps"},
		"how do I start the hyatlas server": nil, // a sentence, not an identifier
		"don't forget":                      nil, // an apostrophe is not identifier punctuation
		"trading strategy":                  nil, // plain words
		"which strategy makes money on NVDA 2026": nil, // too long to be an identifier query
	}
	for q, want := range cases {
		if got := identifierTerms(q); strings.Join(got, ",") != strings.Join(want, ",") {
			t.Errorf("identifierTerms(%q) = %v, want %v", q, got, want)
		}
	}
}

// For an identifier query, only hits that mention the identifier survive.
func TestRequireTermsDropsLookAlikes(t *testing.T) {
	hits := []SearchHit{
		{ID: "noise", Content: "hy_memory renamed to hyatlas"},
		{ID: "hit", Content: "HYATLAS_SYNC_EXTRACT=on makes the write block"},
	}
	got := fusedIDs(requireTerms(hits, identifierTerms("HYATLAS_SYNC_EXTRACT")))
	if len(got) != 1 || got[0] != "hit" {
		t.Errorf("requireTerms = %v, want [hit]", got)
	}
}

// Through the handler: when an identifier's best keyword matches are all L5
// relations, the L5 cap must not leave the result short; facts fill the rest.
func TestIdentifierSearchFillsPastL5Cap(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	g := srv.store.Graph()
	for i := 0; i < 15; i++ {
		if _, err := g.AddEdgeWithSources("alice", "a1", fmt.Sprintf("svc%d", i), "listens_on", "port 19528", []string{"r"}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := srv.store.BackfillL5(); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 4; i++ {
		text := fmt.Sprintf("fact %d: the HyAtlas server listens on port 19528 by default on this machine", i)
		if err := srv.store.Add(memory.L3Fact, newID(), text, map[string]string{"user_id": "alice", "agent_id": "a1"}); err != nil {
			t.Fatal(err)
		}
	}
	w := httptest.NewRecorder()
	srv.handleSearch(w, httptest.NewRequest("POST", "/api/v1/search",
		strings.NewReader(`{"query":"19528","user_id":"alice","agent_id":"a1","limit":5}`)))
	var resp struct {
		Memories map[string][]map[string]any `json:"memories"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	l5, facts := 0, 0
	for _, ch := range resp.Memories {
		for _, m := range ch {
			if m["layer"] == "l5_knowledge" {
				l5++
			} else {
				facts++
			}
		}
	}
	if l5+facts != 5 || l5 > 2 {
		t.Errorf("got %d L5 + %d other hits, want 5 in total with at most 2 L5", l5, facts)
	}
}
