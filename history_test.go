package main

import (
	"context"
	"path/filepath"
	"testing"
	"time"
)

func openHistoryStore(t *testing.T, dir string) *MemoryStore {
	t.Helper()
	st, err := NewMemoryStore(context.Background(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	return st
}

func addHistFact(t *testing.T, st *MemoryStore, text string) string {
	t.Helper()
	id := newID()
	if err := st.Add("l3_fact", id, text, map[string]string{
		"user_id": "u", "agent_id": "a", "ts": time.Now().UTC().Format(time.RFC3339),
	}); err != nil {
		t.Fatal(err)
	}
	return id
}

func metaOf(t *testing.T, st *MemoryStore, id string) map[string]string {
	t.Helper()
	ds := st.GetMany([]string{id})
	if len(ds) != 1 {
		t.Fatalf("row %s is gone", id)
	}
	return ds[0].Meta
}

// Deleting a replacement re-points the rows it had replaced at its own successor,
// and when there is none, turns them into drops that say why.
func TestDeleteRelinksHistory(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	defer st.Close()
	a := addHistFact(t, st, "old fact a")
	b := addHistFact(t, st, "newer fact b")
	c := addHistFact(t, st, "newest fact c")
	if _, err := st.Supersede([]string{a}, b); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Supersede([]string{b}, c); err != nil {
		t.Fatal(err)
	}

	if _, err := st.Delete([]string{b}, "", "", ""); err != nil {
		t.Fatal(err)
	}
	if m := metaOf(t, st, a); m["superseded_by"] != c || m["invalid_at"] == "" {
		t.Fatalf("after deleting b: a superseded_by=%q invalid_at=%q, want %s", m["superseded_by"], m["invalid_at"], c)
	}

	if _, err := st.Delete([]string{c}, "", "", ""); err != nil {
		t.Fatal(err)
	}
	m := metaOf(t, st, a)
	if m["superseded_by"] != "" || m["drop_reason"] != reasonReplacementDeleted || m["invalid_at"] == "" {
		t.Fatalf("after deleting c: a superseded_by=%q drop_reason=%q invalid_at=%q",
			m["superseded_by"], m["drop_reason"], m["invalid_at"])
	}
	// The row stays history: hidden from live reads.
	if live, _ := st.List("l3_fact", "", "", 10, 0, false); len(live) != 0 {
		t.Fatalf("live facts = %d, want 0", len(live))
	}
}

// Opening a store repairs history written by older versions: a link to a row that
// no longer exists, and a drop without a reason. A drop that has a reason, and a
// link to a row that exists, are left alone.
func TestOpenRepairsOldHistory(t *testing.T) {
	dir := t.TempDir()
	st := openHistoryStore(t, dir)
	dangling := addHistFact(t, st, "replaced by a row deleted long ago")
	bare := addHistFact(t, st, "dropped by an old version")
	reasoned := addHistFact(t, st, "dropped with a reason")
	linked := addHistFact(t, st, "replaced by a live row")
	live := addHistFact(t, st, "the live row")
	if _, err := st.Supersede([]string{dangling}, "m-ghost"); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Supersede([]string{bare}, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := st.supersedeWith([]string{reasoned}, "", map[string]string{"drop_reason": "stale"}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Supersede([]string{linked}, live); err != nil {
		t.Fatal(err)
	}
	st.Close()

	st = openHistoryStore(t, dir)
	defer st.Close()
	if m := metaOf(t, st, dangling); m["superseded_by"] != "" || m["drop_reason"] != reasonReplacementDeleted {
		t.Errorf("dangling: superseded_by=%q drop_reason=%q", m["superseded_by"], m["drop_reason"])
	}
	if m := metaOf(t, st, bare); m["drop_reason"] != reasonNotRecorded {
		t.Errorf("bare drop: drop_reason=%q, want %q", m["drop_reason"], reasonNotRecorded)
	}
	if m := metaOf(t, st, reasoned); m["drop_reason"] != "stale" {
		t.Errorf("reasoned drop rewritten: drop_reason=%q", m["drop_reason"])
	}
	if m := metaOf(t, st, linked); m["superseded_by"] != live || m["drop_reason"] != "" {
		t.Errorf("linked: superseded_by=%q drop_reason=%q", m["superseded_by"], m["drop_reason"])
	}
	// The repair is persisted: a third open finds nothing to do.
	if n, err := st.relinkHistory(nil); err != nil || n != 0 {
		t.Errorf("second relink rewrote %d rows (err %v), want 0", n, err)
	}
}

// Paging through a layer returns every row exactly once, even when many rows
// share a timestamp, as the rows of one consolidation pass do.
func TestListPagesAreStable(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	defer st.Close()
	ts := time.Now().UTC().Format(time.RFC3339)
	want := map[string]bool{}
	for i := 0; i < 60; i++ {
		id := newID()
		if err := st.Add("l3_fact", id, "same second fact", map[string]string{"user_id": "u", "agent_id": "a", "ts": ts}); err != nil {
			t.Fatal(err)
		}
		want[id] = true
	}
	seen := map[string]bool{}
	for off := 0; off < 60; off += 7 {
		page, total := st.ListAll("l3_fact", "", "", 7, off, false)
		if total != 60 {
			t.Fatalf("total = %d, want 60", total)
		}
		for _, d := range page {
			if seen[d.ID] {
				t.Fatalf("row %s returned on two pages", d.ID)
			}
			seen[d.ID] = true
		}
	}
	if len(seen) != len(want) {
		t.Fatalf("paging returned %d of %d rows", len(seen), len(want))
	}
}

// Restore makes a superseded row live again and leaves a live row alone.
func TestRestoreMakesRowLive(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	defer st.Close()
	a := addHistFact(t, st, "wrongly merged fact")
	b := addHistFact(t, st, "unrelated merged text")
	if _, err := st.Supersede([]string{a}, b); err != nil {
		t.Fatal(err)
	}
	n, err := st.Restore([]string{a, b, "m-missing"})
	if err != nil || n != 1 {
		t.Fatalf("Restore = %d, %v; want 1 row", n, err)
	}
	m := metaOf(t, st, a)
	if m["invalid_at"] != "" || m["superseded_by"] != "" || m["restored_at"] == "" {
		t.Fatalf("restored row meta = %v", m)
	}
	if live, _ := st.List("l3_fact", "", "", 10, 0, false); len(live) != 2 {
		t.Fatalf("live facts = %d, want 2", len(live))
	}
	// A search finds it again: the superseded bookkeeping was updated.
	hits, err := st.Search("wrongly merged fact", 5, "l3_fact", "", "")
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, h := range hits {
		found = found || h.ID == a
	}
	if !found {
		t.Error("restored fact is not searchable")
	}
}
