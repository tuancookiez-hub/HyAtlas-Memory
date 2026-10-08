package main

import (
	"math"
	"reflect"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// TestKeywordTokens checks that identifiers split on punctuation and stopwords drop out.
func TestKeywordTokens(t *testing.T) {
	got := keywordTokens("What is HYATLAS_SYNC_EXTRACT on port 19528? RSI(2) Reg-T")
	want := []string{"hyatlas", "sync", "extract", "port", "19528", "rsi", "reg"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("keywordTokens: want %v, got %v", want, got)
	}
}

// TestKeywordRequired checks how many query terms a document must match.
func TestKeywordRequired(t *testing.T) {
	cases := map[int]int{1: 1, 3: 3, 4: 3, 5: 4, 8: 6}
	for n, want := range cases {
		if got := keywordRequired(n); got != want {
			t.Errorf("keywordRequired(%d): want %d, got %d", n, want, got)
		}
	}
}

// TestBM25RankNeedsAllTermsOnShortQuery checks that a short query needs every term.
func TestBM25RankNeedsAllTermsOnShortQuery(t *testing.T) {
	docs := []DocIndex{
		{ID: "d1", Content: "HYATLAS_SYNC_EXTRACT=on makes the write block"},
		{ID: "d2", Content: "sync your watch daily"},
		{ID: "d3", Content: "nothing relevant"},
	}
	got := bm25Rank(keywordTokens("HYATLAS_SYNC_EXTRACT"), docs)
	if len(got) != 1 {
		t.Fatalf("want 1 result, got %d", len(got))
	}
	if got[0].Doc.ID != "d1" {
		t.Errorf("want ID d1, got %s", got[0].Doc.ID)
	}
	if got[0].Matched != 3 {
		t.Errorf("want Matched 3, got %d", got[0].Matched)
	}
}

// TestBM25RankPrefersShorterDoc checks that the shorter of two equal matches ranks first.
func TestBM25RankPrefersShorterDoc(t *testing.T) {
	docs := []DocIndex{
		{ID: "d2", Content: "nvda dip strategy plus a long tail of unrelated filler words about many other separate topics entirely"},
		{ID: "d1", Content: "nvda dip strategy"},
	}
	got := bm25Rank(keywordTokens("NVDA dip"), docs)
	if len(got) != 2 {
		t.Fatalf("want 2 results, got %d", len(got))
	}
	if got[0].Doc.ID != "d1" {
		t.Errorf("want d1 first, got %s", got[0].Doc.ID)
	}
}

// TestCosineSim checks cosine similarity on identical, orthogonal, empty and mismatched vectors.
func TestCosineSim(t *testing.T) {
	if got := cosineSim([]float32{1, 2, 3}, []float32{1, 2, 3}); math.Abs(got-1) > 1e-6 {
		t.Errorf("identical: want 1, got %v", got)
	}
	if got := cosineSim([]float32{1, 0}, []float32{0, 1}); math.Abs(got) > 1e-6 {
		t.Errorf("orthogonal: want 0, got %v", got)
	}
	if got := cosineSim(nil, []float32{1}); got != 0 {
		t.Errorf("empty: want 0, got %v", got)
	}
	if got := cosineSim([]float32{1, 2}, []float32{1, 2, 3}); got != 0 {
		t.Errorf("length mismatch: want 0, got %v", got)
	}
}

// TestKeywordSearchScopesAndSkipsRawAndSuperseded checks owner, agent, layer and
// superseded filtering on a real store.
func TestKeywordSearchScopesAndSkipsRawAndSuperseded(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	a, b, c, d, e := newID(), newID(), newID(), newID(), newID()
	mustAdd := func(layer memory.Layer, id, text, user string) {
		t.Helper()
		meta := map[string]string{"user_id": user, "agent_id": "a1"}
		if err := srv.store.Add(layer, id, text, meta); err != nil {
			t.Fatal(err)
		}
	}
	mustAdd(memory.L3Fact, a, "HYATLAS_SYNC_EXTRACT=on makes the write block", "alice")
	mustAdd(memory.L3Fact, b, "the sync button is blue", "alice")
	mustAdd(memory.L3Fact, c, "HYATLAS_SYNC_EXTRACT is a server knob", "bob")
	mustAdd(memory.L2Raw, d, "raw turn mentioning HYATLAS_SYNC_EXTRACT", "alice")
	mustAdd(memory.L3Fact, e, "old note: HYATLAS_SYNC_EXTRACT defaulted to on", "alice")
	if _, err := srv.store.Supersede([]string{e}, a); err != nil {
		t.Fatal(err)
	}

	hits, err := srv.store.KeywordSearch("HYATLAS_SYNC_EXTRACT", 5, "", []string{"alice"}, "a1")
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 1 {
		t.Fatalf("alice scoped: want 1 hit, got %d", len(hits))
	}
	if hits[0].ID != a {
		t.Errorf("alice scoped: want ID %s, got %s", a, hits[0].ID)
	}
	if hits[0].Layer != memory.L3Fact {
		t.Errorf("layer: want %s, got %s", memory.L3Fact, hits[0].Layer)
	}
	if hits[0].Meta["user_id"] != "alice" {
		t.Errorf("meta user_id: want alice, got %q", hits[0].Meta["user_id"])
	}
	if hits[0].Score <= 0 {
		t.Errorf("score: want > 0, got %v", hits[0].Score)
	}

	raw, err := srv.store.KeywordSearch("HYATLAS_SYNC_EXTRACT", 5, memory.L2Raw, []string{"alice"}, "a1")
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) != 1 || raw[0].ID != d {
		t.Errorf("L2Raw: want exactly hit %s, got %d hits", d, len(raw))
	}

	all, err := srv.store.KeywordSearch("HYATLAS_SYNC_EXTRACT", 5, "", nil, "a1")
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != 2 {
		t.Fatalf("no owner filter: want 2 hits, got %d", len(all))
	}
	ids := map[string]bool{all[0].ID: true, all[1].ID: true}
	if !ids[a] || !ids[c] {
		t.Errorf("no owner filter: want hits %s and %s, got %v", a, c, ids)
	}
}

func TestSameNumbers(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{"open PR #101722", "open PR #96783", false},
		{"Tuna prefers Vue 3 Composition API", "Tuna prefers Vue 3 with Composition API", true},
		{"runs on 127.0.0.1:19528", "server at 127.0.0.1:19528", true},
		{"version 4.4.0", "version 4.5.0", false},
		{"no numbers here", "none here either", true},
		{"port 8080", "the port", false},
	}
	for _, c := range cases {
		if got := sameNumbers(c.a, c.b); got != c.want {
			t.Errorf("sameNumbers(%q, %q) = %v, want %v", c.a, c.b, got, c.want)
		}
	}
}
