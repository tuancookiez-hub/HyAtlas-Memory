package graph

import (
	"path/filepath"
	"testing"
	"time"
)

// Re-citing an edge must not move its RecordedAt. An as-of snapshot taken
// before the re-citation still has to see the edge, and the new source is
// appended to the citations.
func TestRecitingEdgeKeepsRecordedAt(t *testing.T) {
	s, err := New(filepath.Join(t.TempDir(), "g.json"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.AddEdgeWithSources("u", "a", "x", "r", "y", []string{"s1"}); err != nil {
		t.Fatal(err)
	}
	// Pretend the relation was recorded long ago, then cite it again now.
	past := time.Now().Unix() - 1000
	s.mu.Lock()
	s.edges[0].RecordedAt = past
	s.edges[0].ValidFrom = past
	s.mu.Unlock()

	if created, err := s.AddEdgeWithSources("u", "a", "x", "r", "y", []string{"s2"}); err != nil || created {
		t.Fatalf("re-cite: created=%v err=%v, want false/nil", created, err)
	}
	s.mu.RLock()
	got := s.edges[0].RecordedAt
	s.mu.RUnlock()
	if got != past {
		t.Errorf("RecordedAt moved to %d, want original %d", got, past)
	}
	_, edges := s.SnapshotAsOfScoped(Scope{UserID: "u", AgentID: "a"}, past+10, 0)
	if len(edges) != 1 {
		t.Errorf("as-of snapshot after the original record lost the edge: %d edges", len(edges))
	}
	if want := []string{"s1", "s2"}; !equalStrings(edges[0].Sources, want) {
		t.Errorf("sources = %v, want %v", edges[0].Sources, want)
	}
}
