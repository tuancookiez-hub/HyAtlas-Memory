// Package graph implements the L5 knowledge graph: entities (nodes) and
// directed relations (edges), persisted as JSON. Pure Go — no native graph DB.
//
// The graph is exact-match traversal (find edges for a node), not similarity
// search, so a vector store is the wrong shape. Nodes/edges live in an
// in-memory index guarded by a mutex, flushed atomically to a JSON file.
package graph

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// Node is an L5 knowledge entity.
type Node struct {
	ID    string            `json:"id"`
	Label string            `json:"label"` // canonical display name
	Type  string            `json:"type"`  // entity/domain/artifact/...
	Props map[string]string `json:"props,omitempty"`
}

// Edge is a directed relation between two nodes.
type Edge struct {
	From       string  `json:"from"`
	To         string  `json:"to"`
	Relation   string  `json:"relation"` // e.g. "depends_on", "fixed_by", "part_of"
	Weight     float64 `json:"weight"`
	Source     string  `json:"source,omitempty"`       // source_memory_id (L2) — evidence citation
	RecordedAt int64   `json:"recorded_at,omitempty"` // unix seconds — when the system learned this
}

// Store holds the graph and persists to a JSON file.
type Store struct {
	mu    sync.RWMutex
	path  string
	nodes map[string]Node
	edges []Edge
}

// New creates a graph store rooted at path (a .json file). Loads existing state.
func New(path string) (*Store, error) {
	s := &Store{
		path:  path,
		nodes: map[string]Node{},
		edges: []Edge{},
	}
	if b, err := os.ReadFile(path); err == nil && len(b) > 0 {
		var state struct {
			Nodes map[string]Node `json:"nodes"`
			Edges []Edge          `json:"edges"`
		}
		if err := json.Unmarshal(b, &state); err != nil {
			return nil, err
		}
		if state.Nodes != nil {
			s.nodes = state.Nodes
		}
		if state.Edges != nil {
			s.edges = state.Edges
		}
	}
	return s, nil
}

func (s *Store) persistLocked() error {
	if s.path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return err
	}
	state := struct {
		Nodes map[string]Node `json:"nodes"`
		Edges []Edge          `json:"edges"`
	}{s.nodes, s.edges}
	b, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	// atomic-ish: write temp then rename
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

// UpsertNode adds or updates a node. Returns the node id (stable).
func (s *Store) UpsertNode(node Node) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if node.ID == "" {
		// derive a stable id from the label so re-upserts collapse
		node.ID = idForLabel(node.Label)
	}
	s.nodes[node.ID] = node
	return node.ID, s.persistLocked()
}

// AddEdgeWithSource adds or updates a directed relation with a source citation.
// Source is the L2 memory id that produced the L5 triple. RecordedAt is the
// unix-second timestamp the system learned the relation.
func (s *Store) AddEdgeWithSource(fromLabel, rel, toLabel, sourceID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	fromID := s.ensureNodeLocked(fromLabel, "")
	toID := s.ensureNodeLocked(toLabel, "")
	now := time.Now().Unix()

	// If edge exists, only update source if the new one is non-empty.
	for i, e := range s.edges {
		if e.From == fromID && e.To == toID && e.Relation == rel {
			if sourceID != "" {
				s.edges[i].Source = sourceID
				s.edges[i].RecordedAt = now
			}
			return s.persistLocked()
		}
	}
	s.edges = append(s.edges, Edge{
		From: fromID, To: toID, Relation: rel, Weight: 1.0,
		Source: sourceID, RecordedAt: now,
	})
	return s.persistLocked()
}

// AddEdge adds a directed relation between two node labels (auto-creating nodes).
func (s *Store) AddEdge(fromLabel, rel, toLabel string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	fromID := s.ensureNodeLocked(fromLabel, "")
	toID := s.ensureNodeLocked(toLabel, "")
	// dedupe
	for _, e := range s.edges {
		if e.From == fromID && e.To == toID && e.Relation == rel {
			return nil
		}
	}
	s.edges = append(s.edges, Edge{From: fromID, To: toID, Relation: rel, Weight: 1.0})
	return s.persistLocked()
}

func (s *Store) ensureNodeLocked(label, typ string) string {
	for id, n := range s.nodes {
		if n.Label == label {
			return id
		}
	}
	id := idForLabel(label)
	s.nodes[id] = Node{ID: id, Label: label, Type: typ}
	return id
}

// Neighbors returns nodes connected to the given node (by label or id),
// both directions, with the relation labeled.
type Neighbor struct {
	Label    string `json:"label"`
	Relation string `json:"relation"`
	Incoming bool   `json:"incoming"` // true if edge points TO this node
}

func (s *Store) Neighbors(label string) []Neighbor {
	s.mu.RLock()
	defer s.mu.RUnlock()
	id := ""
	for nid, n := range s.nodes {
		if n.Label == label || nid == label {
			id = nid
			break
		}
	}
	if id == "" {
		return nil
	}
	var out []Neighbor
	for _, e := range s.edges {
		if e.From == id {
			out = append(out, Neighbor{Label: s.nodes[e.To].Label, Relation: e.Relation, Incoming: false})
		}
		if e.To == id {
			out = append(out, Neighbor{Label: s.nodes[e.From].Label, Relation: e.Relation, Incoming: true})
		}
	}
	return out
}

// NodeCount returns the number of distinct entities.
func (s *Store) NodeCount() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.nodes)
}

// EdgeCount returns the number of relations.
func (s *Store) EdgeCount() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.edges)
}

// Snapshot returns bounded node + relation lists for the dashboard graph view.
// Edges that point at a node outside the bound are dropped so the client never
// receives dangling from/to ids.
func (s *Store) Snapshot(maxNodes int) ([]Node, []Edge) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	nodes := make([]Node, 0, len(s.nodes))
	for _, n := range s.nodes {
		nodes = append(nodes, n)
	}
	sort.Slice(nodes, func(i, j int) bool { return nodes[i].Label < nodes[j].Label })
	if maxNodes > 0 && len(nodes) > maxNodes {
		nodes = nodes[:maxNodes]
	}
	keep := make(map[string]struct{}, len(nodes))
	for _, n := range nodes {
		keep[n.ID] = struct{}{}
	}
	rels := make([]Edge, 0, len(s.edges))
	for _, e := range s.edges {
		if _, ok := keep[e.From]; !ok {
			continue
		}
		if _, ok := keep[e.To]; !ok {
			continue
		}
		rels = append(rels, e)
	}
	return nodes, rels
}

func idForLabel(label string) string {
	// stable, simple slug id from label (collision chance negligible for a memory graph)
	h := fnv(label)
	return "n" + h
}

func fnv(s string) string {
	const prime = 16777619
	h := uint32(2166136261)
	for i := 0; i < len(s); i++ {
		h ^= uint32(s[i])
		h *= prime
	}
	const hexDigits = "0123456789abcdef"
	digits := make([]byte, 8)
	for i := 7; i >= 0; i-- {
		digits[i] = hexDigits[h&0xf]
		h >>= 4
	}
	return string(digits)
}
