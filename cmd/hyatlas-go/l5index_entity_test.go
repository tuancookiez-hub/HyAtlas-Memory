package main

import (
	"context"
	"fmt"
	"math"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// vecEmbedder returns a fixed vector for each known text and fails on any other, so
// a test sets exactly how similar two relations are.
type vecEmbedder map[string][]float32

func (v vecEmbedder) Embed(_ context.Context, text string) ([]float32, error) {
	if vec, ok := v[text]; ok {
		return vec, nil
	}
	return nil, fmt.Errorf("no test vector for %q", text)
}

// simTo is a unit vector whose cosine with the unit vector {1, 0, 0} is s.
func simTo(s float64) []float32 {
	return []float32{float32(s), float32(math.Sqrt(1 - s*s)), 0}
}

// newL5EntityStore returns a store that embeds with vecs and dedupes at 0.92, the
// default HYATLAS_DEDUPE_SCORE.
func newL5EntityStore(t *testing.T, vecs vecEmbedder) *MemoryStore {
	t.Helper()
	st := guardStore(t, vecs)
	st.l5Dedupe = 0.92
	return st
}

// l5LiveCount is how many L5 documents alice's agent a1 has.
func l5LiveCount(st *MemoryStore) int {
	_, n := st.List(memory.L5Knowledge, "alice", "a1", 50, 0, false)
	return n
}

// A restatement of the same entities at a similarity below the dedupe threshold is
// not indexed again, so only one live L5 document remains.
func TestIndexL5EntityRuleSkipsRestatement(t *testing.T) {
	first := "hyatlas server runs on 127.0.0.1:19528"
	second := "HyAtlas v4 has server 127.0.0.1:19528"
	st := newL5EntityStore(t, vecEmbedder{first: {1, 0, 0}, second: simTo(0.85)})

	if n, err := st.indexL5([]l5Edge{{user: "alice", agent: "a1", from: "hyatlas server",
		rel: "runs_on", to: "127.0.0.1:19528"}}); err != nil || n != 1 {
		t.Fatalf("first edge indexed %d, %v; want 1, nil", n, err)
	}
	if n, err := st.indexL5([]l5Edge{{user: "alice", agent: "a1", from: "HyAtlas v4",
		rel: "has_server", to: "127.0.0.1:19528"}}); err != nil || n != 0 {
		t.Fatalf("restatement indexed %d, %v; want 0, nil", n, err)
	}
	if got := l5LiveCount(st); got != 1 {
		t.Errorf("live L5 documents = %d, want 1", got)
	}
}

// Same relation shape, different endpoint: the entity rule does not apply.
func TestIndexL5EntityRuleKeepsDifferentEndpoint(t *testing.T) {
	first := "hyatlas server runs on 127.0.0.1:19528"
	other := "hermes server runs on 127.0.0.1:8642"
	st := newL5EntityStore(t, vecEmbedder{first: {1, 0, 0}, other: simTo(0.85)})

	if _, err := st.indexL5([]l5Edge{{user: "alice", agent: "a1", from: "hyatlas server",
		rel: "runs_on", to: "127.0.0.1:19528"}}); err != nil {
		t.Fatal(err)
	}
	if n, err := st.indexL5([]l5Edge{{user: "alice", agent: "a1", from: "hermes server",
		rel: "runs_on", to: "127.0.0.1:8642"}}); err != nil || n != 1 {
		t.Fatalf("different endpoint indexed %d, %v; want 1, nil", n, err)
	}
	if got := l5LiveCount(st); got != 2 {
		t.Errorf("live L5 documents = %d, want 2", got)
	}
}

// Same entities but a different number (a port): the numbers must match, so both
// relations are indexed.
func TestIndexL5EntityRuleKeepsDifferentNumbers(t *testing.T) {
	first := "hyatlas server runs on port 19528"
	other := "hyatlas server runs on port 19529"
	st := newL5EntityStore(t, vecEmbedder{first: {1, 0, 0}, other: simTo(0.85)})

	if _, err := st.indexL5([]l5Edge{{user: "alice", agent: "a1", from: "hyatlas server",
		rel: "runs_on", to: "port 19528"}}); err != nil {
		t.Fatal(err)
	}
	if n, err := st.indexL5([]l5Edge{{user: "alice", agent: "a1", from: "hyatlas server",
		rel: "runs_on", to: "port 19529"}}); err != nil || n != 1 {
		t.Fatalf("different number indexed %d, %v; want 1, nil", n, err)
	}
	if got := l5LiveCount(st); got != 2 {
		t.Errorf("live L5 documents = %d, want 2", got)
	}
}

// Same entities below the entity score (0.80): not a restatement, so it is indexed.
func TestIndexL5EntityRuleNeedsEntityScore(t *testing.T) {
	first := "hyatlas server runs on 127.0.0.1:19528"
	second := "HyAtlas v4 has server 127.0.0.1:19528"
	st := newL5EntityStore(t, vecEmbedder{first: {1, 0, 0}, second: simTo(0.70)})

	if _, err := st.indexL5([]l5Edge{{user: "alice", agent: "a1", from: "hyatlas server",
		rel: "runs_on", to: "127.0.0.1:19528"}}); err != nil {
		t.Fatal(err)
	}
	if n, err := st.indexL5([]l5Edge{{user: "alice", agent: "a1", from: "HyAtlas v4",
		rel: "has_server", to: "127.0.0.1:19528"}}); err != nil || n != 1 {
		t.Fatalf("score 0.70 indexed %d, %v; want 1, nil", n, err)
	}
	if got := l5LiveCount(st); got != 2 {
		t.Errorf("live L5 documents = %d, want 2", got)
	}
}

func TestSameEdgeEntities(t *testing.T) {
	cases := []struct {
		name string
		meta map[string]string
		e    l5Edge
		want bool
	}{
		{"same direction", map[string]string{"from": "HyAtlas", "to": "127.0.0.1:19528"},
			l5Edge{from: "HyAtlas", to: "127.0.0.1:19528"}, true},
		{"reversed direction", map[string]string{"from": "127.0.0.1:19528", "to": "hyatlas server"},
			l5Edge{from: "hyatlas server", to: "127.0.0.1:19528"}, true},
		{"subset name on from", map[string]string{"from": "hyatlas server", "to": "127.0.0.1:19528"},
			l5Edge{from: "HyAtlas v4", to: "127.0.0.1:19528"}, true},
		{"different from", map[string]string{"from": "hermes server", "to": "127.0.0.1:19528"},
			l5Edge{from: "hyatlas server", to: "127.0.0.1:19528"}, false},
		{"different to", map[string]string{"from": "HyAtlas", "to": "127.0.0.1:19528"},
			l5Edge{from: "HyAtlas", to: "127.0.0.1:8642"}, false},
		{"missing from", map[string]string{"to": "127.0.0.1:19528"},
			l5Edge{from: "HyAtlas", to: "127.0.0.1:19528"}, false},
		{"missing to", map[string]string{"from": "HyAtlas"},
			l5Edge{from: "HyAtlas", to: "127.0.0.1:19528"}, false},
		{"nil meta", nil, l5Edge{from: "HyAtlas", to: "127.0.0.1:19528"}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := sameEdgeEntities(c.meta, c.e); got != c.want {
				t.Errorf("sameEdgeEntities(%v, %+v) = %v, want %v", c.meta, c.e, got, c.want)
			}
		})
	}
}
