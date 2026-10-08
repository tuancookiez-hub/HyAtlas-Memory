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

func TestAddEdgeWithSourceKeepsEveryCitation(t *testing.T) {
	dir := t.TempDir()
	s, err := New(filepath.Join(dir, "g.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := s.AddEdgeWithSource("", "", "alice", "knows", "bob", "mem-abc123"); err != nil {
		t.Fatal(err)
	}
	_, rels := s.Snapshot(10)
	if len(rels) != 1 {
		t.Fatalf("want 1 edge, got %d", len(rels))
	}
	if rels[0].Source != "mem-abc123" || rels[0].RecordedAt == 0 {
		t.Fatalf("first citation: source=%q recorded_at=%d", rels[0].Source, rels[0].RecordedAt)
	}
	// An unowned AddEdge on the same triple is a no-op and must not clobber anything.
	if err := s.AddEdge("alice", "knows", "bob"); err != nil {
		t.Fatal(err)
	}
	// A second citation is kept alongside the first; the primary Source does not move.
	if err := s.AddEdgeWithSource("", "", "alice", "knows", "bob", "mem-xyz789"); err != nil {
		t.Fatal(err)
	}
	_, rels = s.Snapshot(10)
	if len(rels) != 1 {
		t.Fatalf("want 1 edge after second citation, got %d", len(rels))
	}
	if rels[0].Source != "mem-abc123" {
		t.Errorf("primary source moved: want mem-abc123, got %q", rels[0].Source)
	}
	if want := []string{"mem-abc123", "mem-xyz789"}; !equalStrings(rels[0].Sources, want) {
		t.Errorf("sources = %v, want %v", rels[0].Sources, want)
	}
	// Re-citing an existing source adds nothing.
	if err := s.AddEdgeWithSource("", "", "alice", "knows", "bob", "mem-xyz789"); err != nil {
		t.Fatal(err)
	}
	_, rels = s.Snapshot(10)
	if !equalStrings(rels[0].Sources, []string{"mem-abc123", "mem-xyz789"}) {
		t.Errorf("duplicate citation changed sources: %v", rels[0].Sources)
	}
}

func TestAddEdgeWithSourcesReportsCreation(t *testing.T) {
	s, err := New(filepath.Join(t.TempDir(), "g.json"))
	if err != nil {
		t.Fatal(err)
	}
	created, err := s.AddEdgeWithSources("u", "a", "x", "r", "y", []string{"s1", "s2"})
	if err != nil || !created {
		t.Fatalf("first write: created=%v err=%v, want true/nil", created, err)
	}
	created, err = s.AddEdgeWithSources("u", "a", "x", "r", "y", []string{"s3"})
	if err != nil || created {
		t.Fatalf("repeat write: created=%v err=%v, want false/nil", created, err)
	}
	if s.EdgeCount() != 1 {
		t.Fatalf("edges = %d, want 1", s.EdgeCount())
	}
	_, rels := s.Snapshot(0)
	if want := []string{"s1", "s2", "s3"}; !equalStrings(rels[0].Sources, want) {
		t.Errorf("sources = %v, want %v", rels[0].Sources, want)
	}
}

// The same label under two owners is two nodes, and the same triple under two
// owners is two edges. Scoped reads see only their owner's rows.
func TestOwnersAreDistinctInNodesAndEdges(t *testing.T) {
	s, err := New(filepath.Join(t.TempDir(), "g.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := s.AddEdgeWithSource("alice", "a1", "skyhook", "listens_on", "4471", "ra"); err != nil {
		t.Fatal(err)
	}
	if err := s.AddEdgeWithSource("bob", "b1", "skyhook", "listens_on", "4471", "rb"); err != nil {
		t.Fatal(err)
	}
	if s.NodeCount() != 4 {
		t.Errorf("nodes = %d, want 4 (skyhook and 4471 per owner)", s.NodeCount())
	}
	if s.EdgeCount() != 2 {
		t.Errorf("edges = %d, want 2 (one per owner)", s.EdgeCount())
	}

	alice := Scope{UserID: "alice"}
	nodes, rels := s.SnapshotScoped(alice, 0)
	if len(nodes) != 2 || len(rels) != 1 {
		t.Fatalf("alice scope: nodes=%d rels=%d, want 2/1", len(nodes), len(rels))
	}
	if rels[0].UserID != "alice" || rels[0].Source != "ra" {
		t.Errorf("alice edge = %+v", rels[0])
	}
	if n, e := s.CountsScoped(Scope{UserID: "bob", AgentID: "b1"}); n != 2 || e != 1 {
		t.Errorf("bob/b1 counts = %d/%d, want 2/1", n, e)
	}
	if n, e := s.CountsScoped(Scope{UserID: "nobody"}); n != 0 || e != 0 {
		t.Errorf("unknown owner counts = %d/%d, want 0/0", n, e)
	}
	// An agent filter narrows within a user.
	if _, rels := s.SnapshotScoped(Scope{UserID: "alice", AgentID: "other"}, 0); len(rels) != 0 {
		t.Errorf("agent filter leaked %d edge(s)", len(rels))
	}
	// Unscoped reads still return everything.
	if _, rels := s.Snapshot(0); len(rels) != 2 {
		t.Errorf("unscoped rels = %d, want 2", len(rels))
	}
	// Neighbours of a shared label are per owner.
	if n := s.NeighborsScoped(Scope{UserID: "alice"}, "skyhook"); len(n) != 1 || n[0].Label != "4471" {
		t.Errorf("alice neighbours = %+v", n)
	}
	if n := s.NeighborsScoped(Scope{UserID: "carol"}, "skyhook"); n != nil {
		t.Errorf("carol neighbours = %+v, want none", n)
	}
	// As-of reads respect the owner too.
	_, asOf := s.SnapshotAsOfScoped(Scope{UserID: "bob"}, time.Now().Unix()+10, 0)
	if len(asOf) != 1 || asOf[0].UserID != "bob" {
		t.Errorf("as-of bob = %+v", asOf)
	}
}

// Dedupe is per owner: a second owner's identical triple must not fold into the
// first owner's edge, even though it reuses the same label.
func TestDedupeKeyIncludesOwner(t *testing.T) {
	s, err := New(filepath.Join(t.TempDir(), "g.json"))
	if err != nil {
		t.Fatal(err)
	}
	c1, _ := s.AddEdgeWithSources("u1", "", "x", "r", "y", []string{"s"})
	c2, _ := s.AddEdgeWithSources("u2", "", "x", "r", "y", []string{"s"})
	if !c1 || !c2 {
		t.Fatalf("created = %v/%v, want both true", c1, c2)
	}
	if s.EdgeCount() != 2 {
		t.Fatalf("edges = %d, want 2", s.EdgeCount())
	}
}

// graph.json written before owners and multi-source citations existed must load
// unowned, with its single Source promoted to Sources.
func TestLegacyGraphFileLoads(t *testing.T) {
	path := filepath.Join(t.TempDir(), "g.json")
	legacy := `{
  "nodes": {
    "nlegacy1": {"id": "nlegacy1", "label": "alpha", "type": "entity"},
    "nlegacy2": {"id": "nlegacy2", "label": "bravo", "type": "entity"}
  },
  "edges": [
    {"from": "nlegacy1", "to": "nlegacy2", "relation": "depends_on", "weight": 1,
     "source": "mem-old", "recorded_at": 100, "valid_from": 100}
  ]
}`
	if err := os.WriteFile(path, []byte(legacy), 0o644); err != nil {
		t.Fatal(err)
	}
	s, err := New(path)
	if err != nil {
		t.Fatal(err)
	}
	_, rels := s.Snapshot(0)
	if len(rels) != 1 {
		t.Fatalf("legacy edges = %d, want 1", len(rels))
	}
	e := rels[0]
	if e.UserID != "" || e.AgentID != "" {
		t.Errorf("legacy edge gained an owner: %q/%q", e.UserID, e.AgentID)
	}
	if e.Source != "mem-old" || !equalStrings(e.Sources, []string{"mem-old"}) {
		t.Errorf("legacy citations: source=%q sources=%v", e.Source, e.Sources)
	}
	// Unowned means visible to the unscoped view only.
	if _, rels := s.SnapshotScoped(Scope{UserID: "alice"}, 0); len(rels) != 0 {
		t.Errorf("legacy edge leaked into alice's scope")
	}
	// A new citation of the legacy triple, unowned, extends it rather than duplicating.
	if err := s.AddEdgeWithSource("", "", "alpha", "depends_on", "bravo", "mem-new"); err != nil {
		t.Fatal(err)
	}
	if s.EdgeCount() != 1 {
		t.Fatalf("edges after re-cite = %d, want 1", s.EdgeCount())
	}
	_, rels = s.Snapshot(0)
	if !equalStrings(rels[0].Sources, []string{"mem-old", "mem-new"}) {
		t.Errorf("sources after re-cite = %v", rels[0].Sources)
	}

	// The owner and sources survive a reload.
	if err := s.AddEdgeWithSource("alice", "", "alpha", "depends_on", "bravo", "mem-a"); err != nil {
		t.Fatal(err)
	}
	s2, err := New(path)
	if err != nil {
		t.Fatal(err)
	}
	if n, e := s2.CountsScoped(Scope{UserID: "alice"}); n != 2 || e != 1 {
		t.Errorf("reloaded alice counts = %d/%d, want 2/1", n, e)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
