package main

import "testing"

// TestClassifyQueryTable checks that each kind of query gets the intent it should, for
// navigational, conceptual and factual queries alike.
func TestClassifyQueryTable(t *testing.T) {
	cases := []struct {
		query string
		want  queryIntent
	}{
		{"RSI(2) threshold", intentNavigational},
		{"19528", intentNavigational},
		{"HYATLAS_SYNC_EXTRACT", intentNavigational},
		{"how do we deploy the server", intentConceptual},
		{"Why is it slow", intentConceptual},
		{"what usually breaks on upgrade", intentConceptual},
		{"we tend to deploy on fridays", intentConceptual},
		{"explain the outage", intentConceptual},
		{"what does HYATLAS_SYNC_EXTRACT do with the queue", intentFactual},
		{"my preferred editor", intentFactual},
		{"what port does the server use", intentFactual},
	}
	for _, c := range cases {
		if got := classifyQuery(c.query); got != c.want {
			t.Errorf("classifyQuery(%q) = %d, want %d", c.query, got, c.want)
		}
	}
}

// TestIntentWeights checks the vector and keyword weights of each intent.
func TestIntentWeights(t *testing.T) {
	cases := []struct {
		intent     queryIntent
		vector, kw float64
	}{
		{intentNavigational, 0.6, 1.4},
		{intentConceptual, 1.15, 0.85},
		{intentFactual, 1, 1},
	}
	for _, c := range cases {
		v, k := intentWeights(c.intent)
		if v != c.vector || k != c.kw {
			t.Errorf("intentWeights(%d) = (%v, %v), want (%v, %v)", c.intent, v, k, c.vector, c.kw)
		}
	}
}
