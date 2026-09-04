package graph

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSnapshotDropsDanglingEdges(t *testing.T) {
	dir := t.TempDir()
	s, err := New(filepath.Join(dir, "g.json"))
	if err != nil {
		t.Fatal(err)
	}
	labels := []string{"alpha", "bravo", "charlie", "delta", "echo"}
	for _, l := range labels {
		if _, err := s.UpsertNode(Node{Label: l, Type: "entity"}); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.AddEdge("alpha", "connects to", "echo"); err != nil {
		t.Fatal(err)
	}
	if err := s.AddEdge("bravo", "connects to", "charlie"); err != nil {
		t.Fatal(err)
	}

	nodes, rels := s.Snapshot(2)
	if len(nodes) != 2 {
		t.Fatalf("want 2 nodes, got %d", len(nodes))
	}
	keep := map[string]struct{}{}
	for _, n := range nodes {
		keep[n.ID] = struct{}{}
	}
	for _, e := range rels {
		if _, ok := keep[e.From]; !ok {
			t.Fatalf("dangling from %s", e.From)
		}
		if _, ok := keep[e.To]; !ok {
			t.Fatalf("dangling to %s", e.To)
		}
	}
}

func TestAddEdgeDedupes(t *testing.T) {
	dir := t.TempDir()
	s, err := New(filepath.Join(dir, "g.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := s.AddEdge("a", "rel", "b"); err != nil {
		t.Fatal(err)
	}
	if err := s.AddEdge("a", "rel", "b"); err != nil {
		t.Fatal(err)
	}
	if s.EdgeCount() != 1 {
		t.Fatalf("want 1 edge, got %d", s.EdgeCount())
	}
	if s.NodeCount() != 2 {
		t.Fatalf("want 2 nodes, got %d", s.NodeCount())
	}
}

func TestPersistRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "g.json")
	s, err := New(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.AddEdge("left", "owns", "right"); err != nil {
		t.Fatal(err)
	}
	s2, err := New(path)
	if err != nil {
		t.Fatal(err)
	}
	if s2.NodeCount() != 2 || s2.EdgeCount() != 1 {
		t.Fatalf("reload nodes=%d edges=%d", s2.NodeCount(), s2.EdgeCount())
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal(err)
	}
}

func TestSnapshotAsOfFiltersByBothAxes(t *testing.T) {
	dir := t.TempDir()
	s, err := New(filepath.Join(dir, "g.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, l := range []string{"a", "b", "c"} {
		if _, err := s.UpsertNode(Node{Label: l}); err != nil {
			t.Fatal(err)
		}
	}

	now := time.Now().Unix()
	// Edge 1: a-b ongoing, recorded in the past
	if err := s.AddEdge("a", "r1", "b"); err != nil {
		t.Fatal(err)
	}
	// Edge 2: b-c with closed validity window (valid_to in the past)
	if err := s.AddEdge("b", "r2", "c"); err != nil {
		t.Fatal(err)
	}
	// Force b-c's valid_to to a past timestamp by direct field mutation
	// (no public CloseEdge yet — that's Phase 2.4)
	s.mu.Lock()
	for i, e := range s.edges {
		if e.Relation == "r2" {
			s.edges[i].RecordedAt = now - 200
			s.edges[i].ValidFrom = now - 200
			s.edges[i].ValidTo = now - 100
		} else {
			s.edges[i].RecordedAt = now - 200
		}
	}
	s.mu.Unlock()

	// t=now-50 should return both (validity window is open for a-b; b-c's
	// valid_from=now-200 <= now-50 and valid_to=now-100 > now-50... wait, that's
	// not right either. The window is [now-200, now-100] so now-50 is AFTER the
	// close. Use a probe at now-150 (inside the window for b-c).
	_, rels := s.SnapshotAsOf(now-150, 10)
	if len(rels) != 2 {
		t.Errorf("t=now-150 (inside b-c window) want 2 edges, got %d", len(rels))
	}

	// t=now+100: b-c is closed (valid_to=now-100 < t)
	_, rels = s.SnapshotAsOf(now+100, 10)
	if len(rels) != 1 {
		t.Errorf("t=now+100 want 1 edge (a-b only), got %d", len(rels))
	}
	if len(rels) > 0 && rels[0].Relation != "r1" {
		t.Errorf("wrong edge returned: %s", rels[0].Relation)
	}
}

func TestAddEdgeWithSource(t *testing.T) {
	dir := t.TempDir()
	s, err := New(filepath.Join(dir, "g.json"))
	if err != nil {
		t.Fatal(err)
	}
	// New edge with a source citation
	if err := s.AddEdgeWithSource("alice", "knows", "bob", "mem-abc123"); err != nil {
		t.Fatal(err)
	}
	_, rels := s.Snapshot(10)
	if len(rels) != 1 {
		t.Fatalf("want 1 edge, got %d", len(rels))
	}
	if rels[0].Source != "mem-abc123" {
		t.Fatalf("want source=mem-abc123, got %q", rels[0].Source)
	}
	if rels[0].RecordedAt == 0 {
		t.Fatalf("want RecordedAt > 0, got %d", rels[0].RecordedAt)
	}
	// AddEdge without source on the same triple should NOT clobber the existing source
	if err := s.AddEdge("alice", "knows", "bob"); err != nil {
		t.Fatal(err)
	}
	_, rels = s.Snapshot(10)
	if len(rels) != 1 {
		t.Fatalf("want 1 edge after dup, got %d", len(rels))
	}
	if rels[0].Source != "mem-abc123" {
		t.Fatalf("source clobbered: want mem-abc123, got %q", rels[0].Source)
	}
	// AddEdgeWithSource on existing edge with a different source — should update
	if err := s.AddEdgeWithSource("alice", "knows", "bob", "mem-xyz789"); err != nil {
		t.Fatal(err)
	}
	_, rels = s.Snapshot(10)
	if rels[0].Source != "mem-xyz789" {
		t.Fatalf("want updated source=mem-xyz789, got %q", rels[0].Source)
	}
}
