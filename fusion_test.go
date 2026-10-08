package main

import (
	"encoding/json"
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
