package main

import (
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Supersede runs without the store lock, so a Delete or a same-id Add can land
// between its snapshot and its writes. These tests inject those writes at the
// two interleaving points, via supersedeHook, and check the outcome.

// interleaveStore returns a store with two live facts, X and Y, and a hook that
// runs fn once when Supersede reaches stage for row X.
func interleaveStore(t *testing.T, stage string, fn func(st *MemoryStore)) *MemoryStore {
	t.Helper()
	st := newTestServer(t, "m", "x").store
	addFact(t, st, memory.L3Fact, "X", "old content of X", nil)
	addFact(t, st, memory.L3Fact, "Y", "content of Y", nil)
	fired := false
	st.supersedeHook = func(s, id string) {
		if s == stage && (id == "X" || stage == "commit") && !fired {
			fired = true
			fn(st)
		}
	}
	return st
}

func TestDeleteDuringSupersedeLeavesRowDeleted(t *testing.T) {
	for _, stage := range []string{"rewrite", "commit"} {
		t.Run(stage, func(t *testing.T) {
			st := interleaveStore(t, stage, func(st *MemoryStore) {
				if n, err := st.Delete([]string{"X"}, "", "", ""); err != nil || n != 1 {
					t.Errorf("delete X: n=%d err=%v", n, err)
				}
			})
			n, err := st.Supersede([]string{"X", "Y"}, "merged")
			if err != nil {
				t.Fatalf("Supersede returned error for a row deleted mid-pass: %v", err)
			}
			if n != 1 {
				t.Errorf("marked = %d, want 1 (Y only)", n)
			}
			if _, all := st.ListAll(memory.L3Fact, "", "", 10, 0, false); all != 1 {
				t.Errorf("history rows = %d, want 1 (X is gone, not resurrected)", all)
			}
			if got := st.GetMany([]string{"X"}); len(got) != 0 {
				t.Errorf("X came back in the index: %+v", got)
			}
			if c := st.cols[memory.L3Fact].Count(); c != 1 {
				t.Errorf("chromem rows = %d, want 1 (X must not be re-created)", c)
			}
			st.mu.RLock()
			hidden, sup := st.hidden[memory.L3Fact], len(st.superseded)
			st.mu.RUnlock()
			if hidden != 1 || sup != 1 {
				t.Errorf("superseded bookkeeping hidden=%d set=%d, want 1/1", hidden, sup)
			}
		})
	}
}

func TestSameIDOverwriteDuringSupersedeKeepsNewerContent(t *testing.T) {
	for _, stage := range []string{"rewrite", "commit"} {
		t.Run(stage, func(t *testing.T) {
			st := interleaveStore(t, stage, func(st *MemoryStore) {
				addFact(t, st, memory.L3Fact, "X", "newer content of X", nil)
			})
			n, err := st.Supersede([]string{"X", "Y"}, "merged")
			if err != nil {
				t.Fatalf("Supersede: %v", err)
			}
			if n != 1 {
				t.Errorf("marked = %d, want 1 (Y only; X was replaced by a newer write)", n)
			}
			got := st.GetMany([]string{"X"})
			if len(got) != 1 || got[0].Content != "newer content of X" {
				t.Fatalf("X index = %+v, want the newer content", got)
			}
			if isSuperseded(got[0]) {
				t.Errorf("newer X was marked superseded by a pass aimed at the old one")
			}
			// Chromem must agree with the index, not hold the stale rewrite.
			doc, err := st.cols[memory.L3Fact].GetByID(st.ctx, "X")
			if err != nil {
				t.Fatal(err)
			}
			if doc.Content != "newer content of X" || doc.Metadata["invalid_at"] != "" {
				t.Errorf("chromem X = %q meta=%v, want newer live row", doc.Content, doc.Metadata)
			}
			if live, _ := st.List(memory.L3Fact, "", "", 10, 0, false); len(live) != 1 || live[0].ID != "X" {
				t.Errorf("live facts = %+v, want only X", live)
			}
		})
	}
}
