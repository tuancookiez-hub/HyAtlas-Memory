package main

import (
	"fmt"
	"path/filepath"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// newPersistStore opens a store in dir for the persistence tests, skipping when the
// environment cannot build one.
func newPersistStore(t *testing.T, dir string) *MemoryStore {
	t.Helper()
	store, err := NewMemoryStore(ctxForTest(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	return store
}

// TestIndexWritesAreCoalesced checks that 100 Adds cause far fewer index file writes
// than 100, that Flush writes the latest state, and that a store reopened after Close
// sees every row.
func TestIndexWritesAreCoalesced(t *testing.T) {
	dir := t.TempDir()
	store := newPersistStore(t, dir)
	base := store.indexWrites.Load()
	for i := 0; i < 100; i++ {
		id := fmt.Sprintf("doc-%03d", i)
		if err := store.Add(memory.L3Fact, id, "fact number "+id, map[string]string{
			"user_id": "u", "agent_id": "a", "ts": "2026-10-07T00:00:00Z",
		}); err != nil {
			t.Fatalf("add %s: %v", id, err)
		}
	}
	if got := store.indexWrites.Load() - base; got >= 20 {
		t.Fatalf("100 adds caused %d index writes, want far fewer", got)
	}

	if err := store.Flush(); err != nil {
		t.Fatalf("flush: %v", err)
	}
	after := store.indexWrites.Load()
	if err := store.Flush(); err != nil {
		t.Fatalf("second flush: %v", err)
	}
	if store.indexWrites.Load() != after {
		t.Fatal("a flush with nothing changed wrote the index again")
	}
	if err := store.SetExtracted("doc-042", true); err != nil {
		t.Fatalf("set extracted: %v", err)
	}
	if err := store.Flush(); err != nil {
		t.Fatalf("flush after change: %v", err)
	}
	if store.indexWrites.Load() != after+1 {
		t.Fatalf("flush after a change wrote %d times, want 1", store.indexWrites.Load()-after)
	}
	store.Close()

	reopened := newPersistStore(t, dir)
	defer reopened.Close()
	if n := reopened.TotalMemories(); n != 100 {
		t.Fatalf("reopened store has %d rows, want 100", n)
	}
	got := reopened.GetMany([]string{"doc-042"})
	if len(got) != 1 || !got[0].Extracted {
		t.Fatalf("reopened store lost the extracted flag: %+v", got)
	}
}

// TestCloseFlushesPendingIndex checks that Close writes a change the flusher has not
// written yet.
func TestCloseFlushesPendingIndex(t *testing.T) {
	dir := t.TempDir()
	store := newPersistStore(t, dir)
	if err := store.Add(memory.L3Fact, "only", "a single fact", map[string]string{"user_id": "u"}); err != nil {
		t.Fatalf("add: %v", err)
	}
	store.Close()
	if store.dirty.Load() {
		t.Fatal("index still dirty after Close")
	}
	reopened := newPersistStore(t, dir)
	defer reopened.Close()
	if len(reopened.GetMany([]string{"only"})) != 1 {
		t.Fatal("row added before Close is missing after reopen")
	}
}

// TestReplacedByTracksSuccessors checks that the replaced-by map follows a row that is
// replaced, and that a row re-added live under the same id leaves it.
func TestReplacedByTracksSuccessors(t *testing.T) {
	dir := t.TempDir()
	store := newPersistStore(t, dir)
	defer store.Close()
	meta := func() map[string]string {
		return map[string]string{"user_id": "u", "agent_id": "a", "ts": "2026-10-07T00:00:00Z"}
	}
	if err := store.Add(memory.L3Fact, "old", "the old fact", meta()); err != nil {
		t.Fatal(err)
	}
	if err := store.Add(memory.L3Fact, "new", "the new fact", meta()); err != nil {
		t.Fatal(err)
	}
	if n, err := store.supersedeWith([]string{"old"}, "new", map[string]string{metaSupersededHow: howReplaced}); err != nil || n != 1 {
		t.Fatalf("supersede: n=%d err=%v", n, err)
	}
	store.mu.RLock()
	got := append([]string(nil), store.replacedBy["new"]...)
	store.mu.RUnlock()
	if len(got) != 1 || got[0] != "old" {
		t.Fatalf("replacedBy[new] = %v, want [old]", got)
	}

	if err := store.Add(memory.L3Fact, "old", "the old fact, live again", meta()); err != nil {
		t.Fatal(err)
	}
	store.mu.RLock()
	_, present := store.replacedBy["new"]
	store.mu.RUnlock()
	if present {
		t.Fatal("a row re-added live under the same id is still in replacedBy")
	}
}
