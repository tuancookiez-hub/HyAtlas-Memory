package graph

import (
	"os"
	"path/filepath"
	"testing"
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
