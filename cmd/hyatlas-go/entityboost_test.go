package main

import (
	"fmt"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// TestQueryEntitiesMatchesNameAndAlias checks that a page is found from its name and
// from one of its aliases, and that an alias query returns the alias as the match.
func TestQueryEntitiesMatchesNameAndAlias(t *testing.T) {
	store, _ := newPagesStore(t)
	if _, _, err := store.pages.Apply("u", "a", "s1", []PageUpdate{{Name: "HyAtlas", Kind: "project"}}, nil); err != nil {
		t.Fatal(err)
	}
	id := store.pages.List("u", "a")[0].ID
	if _, _, err := store.pages.Apply("u", "a", "s2", []PageUpdate{{Page: id, Name: "Atlas Memory"}}, map[string]bool{id: true}); err != nil {
		t.Fatal(err)
	}

	got := store.queryEntities("how is the hyatlas server doing", []string{"u"}, "a")
	if len(got) != 1 || normalizeName(got[0]) != "hyatlas" {
		t.Errorf("name query: got %v, want the page HyAtlas", got)
	}
	got = store.queryEntities("notes on atlas memory", []string{"u"}, "a")
	if len(got) != 1 || normalizeName(got[0]) != "atlas memory" {
		t.Errorf("alias query: got %v, want the alias Atlas Memory", got)
	}
}

// TestQueryEntitiesIgnoresOtherOwner checks that a page owned by someone outside the
// given users is never returned.
func TestQueryEntitiesIgnoresOtherOwner(t *testing.T) {
	store, _ := newPagesStore(t)
	if _, _, err := store.pages.Apply("x", "a", "s1", []PageUpdate{{Name: "HyAtlas", Kind: "project"}}, nil); err != nil {
		t.Fatal(err)
	}
	if got := store.queryEntities("hyatlas status", []string{"u"}, "a"); len(got) != 0 {
		t.Errorf("another owner's page leaked: %v", got)
	}
}

// TestQueryEntitiesVersionLabel checks that a version label in the query does not stop
// the page "HyAtlas" from matching.
func TestQueryEntitiesVersionLabel(t *testing.T) {
	store, _ := newPagesStore(t)
	if _, _, err := store.pages.Apply("u", "a", "s1", []PageUpdate{{Name: "HyAtlas", Kind: "project"}}, nil); err != nil {
		t.Fatal(err)
	}
	got := store.queryEntities("hyatlas v4 release notes", []string{"u"}, "a")
	if len(got) != 1 {
		t.Errorf("hyatlas v4: got %v, want the page HyAtlas", got)
	}
}

// TestRerankEntitiesMovesUpWithinThreePlaces checks that a hit naming the entity rises
// when it sits within three places of the top, and that a hit at the tenth place does
// not jump to the first place.
func TestRerankEntitiesMovesUpWithinThreePlaces(t *testing.T) {
	mk := func(n int, content string) []SearchHit {
		hits := make([]SearchHit, n)
		for i := range hits {
			hits[i] = SearchHit{ID: fmt.Sprintf("h%d", i), Content: "other text", Layer: memory.L3Fact}
		}
		hits[n-1].Content = content
		return hits
	}
	position := func(hits []SearchHit, id string) int {
		for i, h := range hits {
			if h.ID == id {
				return i
			}
		}
		return -1
	}

	near := make([]SearchHit, 6)
	for i := range near {
		near[i] = SearchHit{ID: fmt.Sprintf("h%d", i), Content: "other text", Layer: memory.L3Fact}
	}
	near[3].Content = "HyAtlas server notes"
	out := rerankEntities(near, []string{"HyAtlas"})
	if p := position(out, "h3"); p < 0 || p >= 3 {
		t.Errorf("hit at fourth place: now at %d, want it moved up to within the first three", p)
	}

	tenth := mk(10, "")
	tenth[9] = SearchHit{ID: "h9", Content: "HyAtlas server notes", Layer: memory.L3Fact}
	out = rerankEntities(tenth, []string{"HyAtlas"})
	if p := position(out, "h9"); p != 6 {
		t.Errorf("hit at tenth place: now at %d, want 6 (three places up, not first)", p)
	}
}

// TestRerankEntitiesNoNamesUnchanged checks that without names the input order is kept,
// and that the scores are never changed by the rerank.
func TestRerankEntitiesNoNamesUnchanged(t *testing.T) {
	hits := []SearchHit{
		{ID: "a", Content: "HyAtlas notes", Score: 0.4},
		{ID: "b", Content: "other", Score: 0.3},
	}
	out := rerankEntities(hits, nil)
	if out[0].ID != "a" || out[1].ID != "b" {
		t.Errorf("order changed without names: %v", out)
	}
	out = rerankEntities(hits, []string{"HyAtlas"})
	if out[0].Score != 0.4 || out[1].Score != 0.3 {
		t.Errorf("scores changed by rerank: %v", out)
	}
}
