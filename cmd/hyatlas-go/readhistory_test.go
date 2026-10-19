package main

import (
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// rhAddEvent adds a fact with a ts and extra metadata, such as event_at, which addRow
// does not take.
func rhAddEvent(t *testing.T, st *MemoryStore, text, ts string, extra map[string]string) string {
	t.Helper()
	id := newID()
	meta := map[string]string{"user_id": "u", "agent_id": "a", "ts": ts}
	for k, v := range extra {
		meta[k] = v
	}
	if err := st.Add(memory.L3Fact, id, text, meta); err != nil {
		t.Fatal(err)
	}
	return id
}

// rhHistoryStore returns a store with the hashed local embedder, which accepts any text.
func rhHistoryStore(t *testing.T) *MemoryStore {
	t.Helper()
	st := openHistoryStore(t, t.TempDir())
	t.Cleanup(st.Close)
	return st
}

// A fact replaced by a newer one is returned under the newer row with its content,
// date and how.
func TestHistoryOfReturnsReplacedFact(t *testing.T) {
	st := rhHistoryStore(t)
	old := addRow(t, st, memory.L3Fact, "the model is gpt-4", "2026-01-05T10:00:00Z")
	n := addRow(t, st, memory.L3Fact, "the model is claude", "2026-03-01T10:00:00Z")
	if _, err := st.supersedeWith([]string{old}, n, map[string]string{metaSupersededHow: howReplaced}); err != nil {
		t.Fatal(err)
	}

	entries := st.historyOf([]string{n}, historyPerHit)[n]
	if len(entries) != 1 {
		t.Fatalf("history for %s has %d entries, want 1", n, len(entries))
	}
	got := entries[0]
	if got.Content != "the model is gpt-4" || got.Date != "2026-01-05" || got.How != howReplaced {
		t.Errorf("entry = %+v, want content gpt-4, date 2026-01-05, how replaced", got)
	}
}

// Rows superseded as updated, or with no superseded_how, are not history.
func TestHistoryOfSkipsUpdatedAndUnmarked(t *testing.T) {
	st := rhHistoryStore(t)
	old1 := addRow(t, st, memory.L3Fact, "the office is in Hanoi", "2026-01-05T10:00:00Z")
	old2 := addRow(t, st, memory.L3Fact, "the office has a desk", "2026-01-06T10:00:00Z")
	n := addRow(t, st, memory.L3Fact, "the office is in Hanoi and has desks", "2026-02-01T10:00:00Z")
	if _, err := st.supersedeWith([]string{old1}, n, map[string]string{metaSupersededHow: howUpdated}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.supersedeWith([]string{old2}, n, nil); err != nil {
		t.Fatal(err)
	}

	if got := st.historyOf([]string{n}, historyPerHit); len(got) != 0 {
		t.Errorf("historyOf = %+v, want empty for updated and unmarked rows", got)
	}
}

// Three replaced versions return the newest two, newest first.
func TestHistoryOfKeepsNewestTwo(t *testing.T) {
	st := rhHistoryStore(t)
	jan := addRow(t, st, memory.L3Fact, "status is draft", "2026-01-01T10:00:00Z")
	feb := addRow(t, st, memory.L3Fact, "status is review", "2026-02-01T10:00:00Z")
	mar := addRow(t, st, memory.L3Fact, "status is blocked", "2026-03-01T10:00:00Z")
	n := addRow(t, st, memory.L3Fact, "status is shipped", "2026-04-01T10:00:00Z")
	if _, err := st.supersedeWith([]string{jan, feb, mar}, n, map[string]string{metaSupersededHow: howReplaced}); err != nil {
		t.Fatal(err)
	}

	entries := st.historyOf([]string{n}, historyPerHit)[n]
	if len(entries) != 2 {
		t.Fatalf("history has %d entries, want 2", len(entries))
	}
	if entries[0].Content != "status is blocked" || entries[1].Content != "status is review" {
		t.Errorf("history order = %q, %q; want blocked then review", entries[0].Content, entries[1].Content)
	}
}

// event_at on the old row is used as the entry's date in place of its ts.
func TestHistoryOfUsesEventDate(t *testing.T) {
	st := rhHistoryStore(t)
	old := rhAddEvent(t, st, "the flight is at 9am", "2026-01-05T10:00:00Z",
		map[string]string{metaEventAt: "2026-02-14"})
	n := addRow(t, st, memory.L3Fact, "the flight is at 11am", "2026-03-01T10:00:00Z")
	if _, err := st.supersedeWith([]string{old}, n, map[string]string{metaSupersededHow: howReplaced}); err != nil {
		t.Fatal(err)
	}

	entries := st.historyOf([]string{n}, historyPerHit)[n]
	if len(entries) != 1 {
		t.Fatalf("history has %d entries, want 1", len(entries))
	}
	if entries[0].Date != "2026-02-14" {
		t.Errorf("Date = %q, want event_at 2026-02-14", entries[0].Date)
	}
}

// rhIDs returns the ids of hits in order, for comparing a result's order.
func rhIDs(hits []SearchHit) []string {
	out := make([]string, 0, len(hits))
	for _, h := range hits {
		out = append(out, h.ID)
	}
	return out
}

// rhSameOrder reports whether got lists exactly the ids in want, in order.
func rhSameOrder(got []SearchHit, want ...string) bool {
	ids := rhIDs(got)
	if len(ids) != len(want) {
		return false
	}
	for i := range ids {
		if ids[i] != want[i] {
			return false
		}
	}
	return true
}

// Near-duplicate hits with the same numbers: the second one is demoted after the
// unrelated hit, and the input slice is left unchanged.
func TestDemoteNearDuplicatesSameNumbers(t *testing.T) {
	vecs := vecEmbedder{
		"port 19528 is open":       {1, 0, 0},
		"port 19528 is open today": simTo(0.97),
		"the cache is warm":        {0, 1, 0},
	}
	st := guardStore(t, vecs)
	a := addRow(t, st, memory.L3Fact, "port 19528 is open", "2026-01-01T10:00:00Z")
	b := addRow(t, st, memory.L3Fact, "port 19528 is open today", "2026-01-02T10:00:00Z")
	c := addRow(t, st, memory.L3Fact, "the cache is warm", "2026-01-03T10:00:00Z")
	hits := []SearchHit{
		{ID: a, Content: "port 19528 is open"},
		{ID: b, Content: "port 19528 is open today"},
		{ID: c, Content: "the cache is warm"},
	}

	got := st.demoteNearDuplicates(hits, 0.95)
	if !rhSameOrder(got, a, c, b) {
		t.Errorf("order = %v, want [%s %s %s]", rhIDs(got), a, c, b)
	}
	if !rhSameOrder(hits, a, b, c) {
		t.Errorf("input order changed to %v", rhIDs(hits))
	}
}

// Near-duplicate hits whose numbers differ are not demoted, so the order is unchanged.
func TestDemoteNearDuplicatesDifferentNumbers(t *testing.T) {
	vecs := vecEmbedder{
		"port 19528 is open":       {1, 0, 0},
		"port 19529 is open today": simTo(0.97),
		"the cache is warm":        {0, 1, 0},
	}
	st := guardStore(t, vecs)
	a := addRow(t, st, memory.L3Fact, "port 19528 is open", "2026-01-01T10:00:00Z")
	b := addRow(t, st, memory.L3Fact, "port 19529 is open today", "2026-01-02T10:00:00Z")
	c := addRow(t, st, memory.L3Fact, "the cache is warm", "2026-01-03T10:00:00Z")
	hits := []SearchHit{
		{ID: a, Content: "port 19528 is open"},
		{ID: b, Content: "port 19529 is open today"},
		{ID: c, Content: "the cache is warm"},
	}

	if got := st.demoteNearDuplicates(hits, 0.95); !rhSameOrder(got, a, b, c) {
		t.Errorf("order = %v, want unchanged [%s %s %s]", rhIDs(got), a, b, c)
	}
}

// A hit with no vector stays in place, even when its text matches an earlier hit.
func TestDemoteNearDuplicatesKeepsHitWithoutVector(t *testing.T) {
	vecs := vecEmbedder{
		"port 19528 is open":       {1, 0, 0},
		"port 19528 is open today": simTo(0.97),
		"the cache is warm":        {0, 1, 0},
	}
	st := guardStore(t, vecs)
	a := addRow(t, st, memory.L3Fact, "port 19528 is open", "2026-01-01T10:00:00Z")
	b := addRow(t, st, memory.L3Fact, "port 19528 is open today", "2026-01-02T10:00:00Z")
	c := addRow(t, st, memory.L3Fact, "the cache is warm", "2026-01-03T10:00:00Z")
	missing := "no-such-row"
	hits := []SearchHit{
		{ID: missing, Content: "port 19528 is open"},
		{ID: a, Content: "port 19528 is open"},
		{ID: b, Content: "port 19528 is open today"},
		{ID: c, Content: "the cache is warm"},
	}

	got := st.demoteNearDuplicates(hits, 0.95)
	if !rhSameOrder(got, missing, a, c, b) {
		t.Errorf("order = %v, want [%s %s %s %s]", rhIDs(got), missing, a, c, b)
	}
}
