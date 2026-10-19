package main

import (
	"reflect"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// mmrHits builds fact hits with the given ids, in order.
func mmrHits(ids ...string) []SearchHit {
	out := make([]SearchHit, 0, len(ids))
	for _, id := range ids {
		out = append(out, SearchHit{ID: id, Content: id, Layer: memory.L3Fact})
	}
	return out
}

// mmrIDs lists the ids of hits in order.
func mmrIDs(hits []SearchHit) []string {
	out := make([]string, 0, len(hits))
	for _, h := range hits {
		out = append(out, h.ID)
	}
	return out
}

// TestMMRDistinctReachTopLimit checks that three near-identical facts do not crowd out
// two distinct ones.
func TestMMRDistinctReachTopLimit(t *testing.T) {
	hits := mmrHits("a1", "a2", "a3", "b", "c")
	vecs := map[string][]float32{
		"a1": {1, 0, 0},
		"a2": {0.999, 0.04, 0},
		"a3": {0.998, 0, 0.05},
		"b":  {0, 1, 0},
		"c":  {0, 0, 1},
	}
	got := mmrIDs(mmrRerank(hits, vecs, 3, 0.5))
	top := map[string]bool{got[0]: true, got[1]: true, got[2]: true}
	if got[0] != "a1" || !top["b"] || !top["c"] {
		t.Fatalf("distinct facts should reach the top 3, got %v", got)
	}
}

// TestMMRLambdaOneUnchanged checks that lambda 1 and short windows leave the order alone.
func TestMMRLambdaOneUnchanged(t *testing.T) {
	hits := mmrHits("a1", "a2", "b", "c")
	vecs := map[string][]float32{"a1": {1, 0}, "a2": {1, 0}, "b": {0, 1}, "c": {0, 1}}
	if got := mmrRerank(hits, vecs, 2, 1); !reflect.DeepEqual(mmrIDs(got), mmrIDs(hits)) {
		t.Fatalf("lambda 1 changed the order: %v", mmrIDs(got))
	}
	if got := mmrRerank(hits[:2], vecs, 2, 0.5); !reflect.DeepEqual(mmrIDs(got), []string{"a1", "a2"}) {
		t.Fatalf("a window under 3 changed the order: %v", mmrIDs(got))
	}
}

// TestMMRMissingVectors checks that hits without vectors keep their slot and nothing panics.
func TestMMRMissingVectors(t *testing.T) {
	hits := mmrHits("a1", "x", "a2", "y", "b")
	vecs := map[string][]float32{"a1": {1, 0}, "a2": {1, 0}, "b": {0, 1}}
	got := mmrIDs(mmrRerank(hits, vecs, 3, 0.5))
	if got[1] != "x" || got[3] != "y" {
		t.Fatalf("vectorless hits moved: %v", got)
	}
	if got := mmrRerank(hits, nil, 3, 0.5); !reflect.DeepEqual(mmrIDs(got), mmrIDs(hits)) {
		t.Fatalf("no vectors changed the order: %v", mmrIDs(got))
	}
}

// TestMMRKeepsOtherLayersAndTail checks that pages keep their slot and hits after the
// window keep their order.
func TestMMRKeepsOtherLayersAndTail(t *testing.T) {
	hits := mmrHits("a1", "pg", "a2", "b", "t1", "t2")
	hits[1].Layer = memory.L8Entity
	vecs := map[string][]float32{
		"a1": {1, 0}, "pg": {1, 0}, "a2": {1, 0}, "b": {0, 1}, "t1": {1, 0}, "t2": {1, 0},
	}
	got := mmrIDs(mmrRerank(hits, vecs, 2, 0.5))
	if got[1] != "pg" || got[4] != "t1" || got[5] != "t2" {
		t.Fatalf("page or tail moved: %v", got)
	}
	if got[2] != "b" {
		t.Fatalf("distinct fact should come before the repeat: %v", got)
	}
}

// TestMMRDeterministic checks that the same input always gives the same output and
// leaves the input untouched.
func TestMMRDeterministic(t *testing.T) {
	hits := mmrHits("a1", "a2", "a3", "b", "c")
	before := mmrIDs(hits)
	vecs := map[string][]float32{
		"a1": {1, 0, 0}, "a2": {1, 0, 0}, "a3": {1, 0, 0}, "b": {0, 1, 0}, "c": {0, 0, 1},
	}
	first := mmrIDs(mmrRerank(hits, vecs, 3, 0.6))
	for i := 0; i < 20; i++ {
		if got := mmrIDs(mmrRerank(hits, vecs, 3, 0.6)); !reflect.DeepEqual(got, first) {
			t.Fatalf("run %d differs: %v vs %v", i, got, first)
		}
	}
	if !reflect.DeepEqual(mmrIDs(hits), before) {
		t.Fatalf("input changed: %v", mmrIDs(hits))
	}
}

// TestParseMMRLambda checks the default, valid and invalid settings.
func TestParseMMRLambda(t *testing.T) {
	cases := map[string]float64{"": 0.75, "0.5": 0.5, "1": 1, "0": 0.75, "1.5": 0.75, "x": 0.75, "-1": 0.75}
	for in, want := range cases {
		t.Setenv("HYATLAS_MMR_LAMBDA", in)
		if got := parseMMRLambda(); got != want {
			t.Errorf("HYATLAS_MMR_LAMBDA=%q: got %v, want %v", in, got, want)
		}
	}
}
