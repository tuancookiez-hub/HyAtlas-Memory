package main

import (
	"fmt"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Three clauses of one long fact, each on its own topic, so a test sets each one's
// vector and the parent's vector separately.
const (
	clauseA = "TradingMaker is a paper-only lab whose ledger is kept apart from every live account, so no trade it records can move real money."
	clauseB = "The ledger is checked against TWS each night, and TWS is the source of truth whenever the two disagree about a position."
	clauseC = "Strategy runs write to their own folder, and the folder is archived after thirty days so the history stays readable."
	// longFact is the three clauses joined by spaces: over clauseMinChars characters.
	longFact = clauseA + " " + clauseB + " " + clauseC
)

// longFactStore returns a store that knows the long fact and its three clauses, with
// clause A on {1,0,0}, clause B on {0,1,0}, clause C on {0,0,1} and the whole fact on
// {1,1,1}.
func longFactStore(t *testing.T) *MemoryStore {
	t.Helper()
	return guardStore(t, vecEmbedder{
		clauseA:  {1, 0, 0},
		clauseB:  {0, 1, 0},
		clauseC:  {0, 0, 1},
		longFact: {1, 1, 1},
	})
}

func TestSplitClausesThreeSentences(t *testing.T) {
	if len(longFact) <= clauseMinChars {
		t.Fatalf("test text is %d characters, want more than %d", len(longFact), clauseMinChars)
	}
	got := splitClauses(longFact)
	if len(got) != 3 {
		t.Fatalf("clauses = %d, want 3: %q", len(got), got)
	}
	if got[0] != clauseA || got[1] != clauseB || got[2] != clauseC {
		t.Fatalf("clauses = %q, want the three sentences in order", got)
	}
}

func TestSplitClausesMergesShortFragments(t *testing.T) {
	got := splitClauses("Short. " + clauseA + " " + clauseB)
	if len(got) != 2 {
		t.Fatalf("clauses = %d, want 2: %q", len(got), got)
	}
	if !strings.HasPrefix(got[0], "Short. ") || !strings.Contains(got[0], "TradingMaker") {
		t.Fatalf("first clause = %q, want the fragment merged into the next sentence", got[0])
	}
}

func TestSplitClausesCapHolds(t *testing.T) {
	var sentences []string
	for i := 1; i <= 9; i++ {
		sentences = append(sentences, fmt.Sprintf("Sentence number %d is written to fill out the test text well.", i))
	}
	got := splitClauses(strings.Join(sentences, " "))
	if len(got) != clauseMax {
		t.Fatalf("clauses = %d, want the cap %d: %q", len(got), clauseMax, got)
	}
	// The overflow joins the last clause, so no sentence is lost.
	for i := 1; i <= 9; i++ {
		if !strings.Contains(strings.Join(got, " "), fmt.Sprintf("number %d ", i)) {
			t.Errorf("sentence %d is missing from the clauses", i)
		}
	}
}

func TestShortFactIsNotIndexed(t *testing.T) {
	const short = "The user prefers green tea in the afternoon."
	st := guardStore(t, vecEmbedder{short: {1, 0, 0}})
	addRow(t, st, memory.L3Fact, short, "2026-10-18T00:00:00Z")
	if n := st.clauses.Count(); n != 0 {
		t.Fatalf("clause rows = %d for a short fact, want 0", n)
	}
}

func TestClauseRowsFollowLifecycle(t *testing.T) {
	st := longFactStore(t)
	id := addRow(t, st, memory.L3Fact, longFact, "2026-10-18T00:00:00Z")
	if n := st.clauses.Count(); n != 3 {
		t.Fatalf("after add: clause rows = %d, want 3", n)
	}

	if _, err := st.Supersede([]string{id}, ""); err != nil {
		t.Fatal(err)
	}
	if n := st.clauses.Count(); n != 0 {
		t.Fatalf("after supersede: clause rows = %d, want 0", n)
	}

	if n, err := st.Restore([]string{id}); err != nil || n != 1 {
		t.Fatalf("restore = %d, %v; want 1, nil", n, err)
	}
	if n := st.clauses.Count(); n != 3 {
		t.Fatalf("after restore: clause rows = %d, want 3", n)
	}

	if _, err := st.Delete([]string{id}, "", "", ""); err != nil {
		t.Fatal(err)
	}
	if n := st.clauses.Count(); n != 0 {
		t.Fatalf("after delete: clause rows = %d, want 0", n)
	}
}

func TestClauseFailureDoesNotFailAdd(t *testing.T) {
	// The embedder knows the whole fact but none of its clauses.
	st := guardStore(t, vecEmbedder{longFact: {1, 1, 1}})
	id := newID()
	if err := st.Add(memory.L3Fact, id, longFact, map[string]string{"user_id": "u", "agent_id": "a"}); err != nil {
		t.Fatalf("Add failed because of its clauses: %v", err)
	}
	if got := st.GetMany([]string{id}); len(got) != 1 {
		t.Fatal("parent fact was not stored")
	}
	if n := st.clauses.Count(); n != 0 {
		t.Fatalf("clause rows = %d after a failed clause write, want 0", n)
	}
}

func TestClauseHitsMapToParent(t *testing.T) {
	st := longFactStore(t)
	id := addRow(t, st, memory.L3Fact, longFact, "2026-10-18T00:00:00Z")

	// A query on clause A alone scores the parent by that clause.
	hits := st.clauseHits([]float32{1, 0, 0}, nil, 5)
	if len(hits) != 1 {
		t.Fatalf("hits = %d, want 1", len(hits))
	}
	if hits[0].ID != id || hits[0].Layer != memory.L3Fact || hits[0].Content != longFact {
		t.Fatalf("hit = %+v, want the parent fact", hits[0])
	}
	if hits[0].Score < 0.99 {
		t.Fatalf("score = %v, want the clause's similarity of about 1", hits[0].Score)
	}

	// Two clauses of one parent match, and the parent still appears once.
	hits = st.clauseHits([]float32{1, 1, 0}, nil, 5)
	if len(hits) != 1 || hits[0].ID != id {
		t.Fatalf("hits = %+v, want the parent once", hits)
	}

	// The owner filter applies to clause rows as it does to normal search.
	if hits := st.clauseHits([]float32{1, 0, 0}, map[string]string{"user_id": "someone-else"}, 5); len(hits) != 0 {
		t.Fatalf("hits for another owner = %d, want 0", len(hits))
	}
	if hits := st.clauseHits([]float32{1, 0, 0}, map[string]string{"user_id": "u"}, 5); len(hits) != 1 {
		t.Fatalf("hits for its owner = %d, want 1", len(hits))
	}
}

func TestClauseHitsSkipSupersededParent(t *testing.T) {
	st := longFactStore(t)
	id := addRow(t, st, memory.L3Fact, longFact, "2026-10-18T00:00:00Z")
	if _, err := st.Supersede([]string{id}, ""); err != nil {
		t.Fatal(err)
	}
	if hits := st.clauseHits([]float32{1, 0, 0}, nil, 5); len(hits) != 0 {
		t.Fatalf("hits for a superseded parent = %d, want 0", len(hits))
	}
}

func TestClauseRowsAreIsolated(t *testing.T) {
	st := longFactStore(t)
	addRow(t, st, memory.L3Fact, longFact, "2026-10-18T00:00:00Z")
	if n := st.LayerCounts()[string(memory.L3Fact)]; n != 1 {
		t.Fatalf("LayerCounts l3_fact = %d, want 1 (clause rows must not count)", n)
	}
	if n := st.TotalMemories(); n != 1 {
		t.Fatalf("TotalMemories = %d, want 1", n)
	}
	if _, n := st.List("", "", "", 10, 0, false); n != 1 {
		t.Fatalf("List total = %d, want 1", n)
	}
}

func TestBackfillIndexesOnce(t *testing.T) {
	st := longFactStore(t)
	id := addRow(t, st, memory.L3Fact, longFact, "2026-10-18T00:00:00Z")
	// Simulate a fact stored before clause indexing existed.
	st.dropClauses(id)
	if st.hasClauses(id) {
		t.Fatal("clause rows still present after the simulated pre-clause state")
	}

	if n := st.backfillClauses(); n != 1 {
		t.Fatalf("first backfill = %d, want 1", n)
	}
	if n := st.clauses.Count(); n != 3 {
		t.Fatalf("clause rows after backfill = %d, want 3", n)
	}
	if n := st.backfillClauses(); n != 0 {
		t.Fatalf("second backfill = %d, want 0", n)
	}
	if n := st.clauses.Count(); n != 3 {
		t.Fatalf("clause rows after second backfill = %d, want 3", n)
	}
}
