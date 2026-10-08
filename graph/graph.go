// Package graph implements the L5 knowledge graph: entities (nodes) and
// directed relations (edges), persisted as JSON. Pure Go — no native graph DB.
//
// The graph is exact-match traversal (find edges for a node), not similarity
// search, so a vector store is the wrong shape. Nodes/edges live in an
// in-memory index guarded by a mutex, flushed atomically to a JSON file.
//
// Every node and edge belongs to an owner (user_id, agent_id). The same label
// under two owners is two nodes, and an edge's dedupe key is
// (owner, from, relation, to). Rows written before owners existed carry the
// empty owner ("", ""), which only the unscoped reads return.
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
	ID      string            `json:"id"`
	Label   string            `json:"label"` // canonical display name
	Type    string            `json:"type"`  // entity/domain/artifact/...
	UserID  string            `json:"user_id,omitempty"`
	AgentID string            `json:"agent_id,omitempty"`
	Props   map[string]string `json:"props,omitempty"`
}

// Edge is a directed relation between two nodes.
type Edge struct {
	From          string   `json:"from"`
	To            string   `json:"to"`
	Relation      string   `json:"relation"` // e.g. "depends_on", "fixed_by", "part_of"
	Weight        float64  `json:"weight"`
	UserID        string   `json:"user_id,omitempty"`
	AgentID       string   `json:"agent_id,omitempty"`
	Source        string   `json:"source,omitempty"`         // primary source_memory_id (L2) — the first citation recorded
	Sources       []string `json:"sources,omitempty"`        // every source_memory_id (L2) that evidences the edge, primary first
	RecordedAt    int64    `json:"recorded_at,omitempty"`    // unix seconds — when the system learned this
	ValidFrom     int64    `json:"valid_from,omitempty"`     // unix seconds — when it became true in the world
	ValidTo       int64    `json:"valid_to,omitempty"`       // unix seconds — when it stopped being true (0 = ongoing)
	InvalidatedAt int64    `json:"invalidated_at,omitempty"` // unix seconds — when a correction superseded this edge
}

// Scope selects the owner whose rows a read returns. An empty field matches any
// owner, so the zero Scope is the whole graph.
type Scope struct {
	UserID  string
	AgentID string
}

// matches reports whether a row owned by (userID, agentID) is visible under sc.
// Owner-less rows (both fields empty) predate owners, so they belong to the one
// install that wrote them and stay visible under every scope. Without this, a
// user filter after upgrade would show an empty graph.
func (sc Scope) matches(userID, agentID string) bool {
	if userID == "" && agentID == "" {
		return true
	}
	return (sc.UserID == "" || sc.UserID == userID) && (sc.AgentID == "" || sc.AgentID == agentID)
}

// Store holds the graph and persists to a JSON file.
type Store struct {
	mu    sync.RWMutex
	path  string
	nodes map[string]Node
	edges []Edge
}

// New creates a graph store rooted at path (a .json file). Loads existing state.
// Edges written before multi-source citations carry only Source; they load with
// Sources = [Source], so every reader can rely on Sources.
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
			for i := range s.edges {
				normalizeEdge(&s.edges[i])
			}
		}
	}
	return s, nil
}

func normalizeEdge(e *Edge) {
	e.Sources = cleanSources(e.Source, e.Sources)
	if e.Source == "" && len(e.Sources) > 0 {
		e.Source = e.Sources[0]
	}
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
		// derive a stable id from the owner and label so re-upserts collapse
		node.ID = nodeID(node.UserID, node.AgentID, node.Label)
	}
	s.nodes[node.ID] = node
	return node.ID, s.persistLocked()
}

// AddEdgeWithSource records one citation of a relation for one owner. It is
// AddEdgeWithSources with a single source.
func (s *Store) AddEdgeWithSource(userID, agentID, fromLabel, rel, toLabel, sourceID string) error {
	_, err := s.AddEdgeWithSources(userID, agentID, fromLabel, rel, toLabel, []string{sourceID})
	return err
}

// AddEdgeWithSources records a relation for one owner, citing every source in
// sourceIDs. The dedupe key is (owner, from, relation, to): a repeat call adds
// its sources that are new to the existing edge, so the edge keeps all of its
// evidence. Source (the first citation) never changes, and neither do the
// bitemporal anchors ValidFrom/ValidTo/InvalidatedAt, which move only via the
// dedicated mutation path. It reports whether the edge was newly created.
func (s *Store) AddEdgeWithSources(userID, agentID, fromLabel, rel, toLabel string, sourceIDs []string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	fromID := s.ensureNodeLocked(userID, agentID, fromLabel)
	toID := s.ensureNodeLocked(userID, agentID, toLabel)
	now := time.Now().Unix()
	srcs := cleanSources("", sourceIDs)

	for i := range s.edges {
		e := &s.edges[i]
		if e.UserID != userID || e.AgentID != agentID || e.From != fromID || e.To != toID || e.Relation != rel {
			continue
		}
		added := false
		for _, src := range srcs {
			if !containsString(e.Sources, src) {
				e.Sources = append(e.Sources, src)
				added = true
			}
		}
		// RecordedAt is not moved: it is when the relation was first recorded, so
		// earlier as-of snapshots keep the edge. New sources are appended above.
		if added {
			if e.ValidFrom == 0 {
				e.ValidFrom = now
			}
		}
		normalizeEdge(e)
		return false, s.persistLocked()
	}
	e := Edge{
		From: fromID, To: toID, Relation: rel, Weight: 1.0,
		UserID: userID, AgentID: agentID,
		Sources:    srcs,
		RecordedAt: now, ValidFrom: now,
	}
	if len(srcs) > 0 {
		e.Source = srcs[0]
	}
	s.edges = append(s.edges, e)
	return true, s.persistLocked()
}

// AddEdge adds an unowned relation between two node labels, auto-creating the
// nodes. It is the owner-less form kept for callers with no owner. It stamps no
// citation or validity time, as before, so the edge is valid at every instant; a
// repeat is a no-op.
func (s *Store) AddEdge(fromLabel, rel, toLabel string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	fromID := s.ensureNodeLocked("", "", fromLabel)
	toID := s.ensureNodeLocked("", "", toLabel)
	for _, e := range s.edges {
		if e.UserID == "" && e.AgentID == "" && e.From == fromID && e.To == toID && e.Relation == rel {
			return nil
		}
	}
	s.edges = append(s.edges, Edge{From: fromID, To: toID, Relation: rel, Weight: 1.0})
	return s.persistLocked()
}

// ensureNodeLocked returns the id of the node for label under the owner,
// creating it if needed. Two owners' nodes never collide: the id is derived
// from the owner as well, and a hash collision with a different node is probed
// past rather than overwriting it.
func (s *Store) ensureNodeLocked(userID, agentID, label string) string {
	for id, n := range s.nodes {
		if n.Label == label && n.UserID == userID && n.AgentID == agentID {
			return id
		}
	}
	id := nodeID(userID, agentID, label)
	for {
		if _, taken := s.nodes[id]; !taken {
			break
		}
		id += "+"
	}
	s.nodes[id] = Node{ID: id, Label: label, UserID: userID, AgentID: agentID}
	return id
}

// Neighbor is one edge seen from a node: the other end's label, the relation,
// and which way it points.
type Neighbor struct {
	Label    string `json:"label"`
	Relation string `json:"relation"`
	Incoming bool   `json:"incoming"` // true if edge points TO this node
}

// Neighbors returns nodes connected to the given node (by label or id) across
// every owner, both directions, with the relation labeled.
func (s *Store) Neighbors(label string) []Neighbor {
	return s.NeighborsScoped(Scope{}, label)
}

// NeighborsScoped is Neighbors restricted to the owner named by sc: only nodes
// and edges that belong to that owner take part.
func (s *Store) NeighborsScoped(sc Scope, label string) []Neighbor {
	s.mu.RLock()
	defer s.mu.RUnlock()
	ids := map[string]bool{}
	for nid, n := range s.nodes {
		if (n.Label == label || nid == label) && sc.matches(n.UserID, n.AgentID) {
			ids[nid] = true
		}
	}
	if len(ids) == 0 {
		return nil
	}
	var out []Neighbor
	for _, e := range s.edges {
		if !sc.matches(e.UserID, e.AgentID) {
			continue
		}
		if ids[e.From] {
			out = append(out, Neighbor{Label: s.nodes[e.To].Label, Relation: e.Relation, Incoming: false})
		}
		if ids[e.To] {
			out = append(out, Neighbor{Label: s.nodes[e.From].Label, Relation: e.Relation, Incoming: true})
		}
	}
	return out
}

// NodeCount returns the number of distinct entities, across all owners.
func (s *Store) NodeCount() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.nodes)
}

// EdgeCount returns the number of relations, across all owners.
func (s *Store) EdgeCount() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.edges)
}

// CountsScoped returns how many nodes and relations belong to the owner named by sc.
func (s *Store) CountsScoped(sc Scope) (nodes, edges int) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, n := range s.nodes {
		if sc.matches(n.UserID, n.AgentID) {
			nodes++
		}
	}
	for _, e := range s.edges {
		if sc.matches(e.UserID, e.AgentID) {
			edges++
		}
	}
	return nodes, edges
}

// SnapshotAsOf returns nodes + edges that were true at unix time t, across all owners.
// "True at t" means both axes:
//
//	world:     valid_from <= t  AND (valid_to == 0 OR valid_to > t)
//	recorded:  recorded_at <= t AND (invalidated_at == 0 OR invalidated_at > t)
func (s *Store) SnapshotAsOf(t int64, maxNodes int) ([]Node, []Edge) {
	return s.SnapshotAsOfScoped(Scope{}, t, maxNodes)
}

// SnapshotAsOfScoped is SnapshotAsOf restricted to the owner named by sc.
func (s *Store) SnapshotAsOfScoped(sc Scope, t int64, maxNodes int) ([]Node, []Edge) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	nodes := make([]Node, 0, len(s.nodes))
	for _, n := range s.nodes {
		if sc.matches(n.UserID, n.AgentID) {
			nodes = append(nodes, n)
		}
	}
	sort.Slice(nodes, func(i, j int) bool {
		if nodes[i].Label != nodes[j].Label {
			return nodes[i].Label < nodes[j].Label
		}
		return nodes[i].ID < nodes[j].ID
	})
	if maxNodes > 0 && len(nodes) > maxNodes {
		nodes = nodes[:maxNodes]
	}
	keep := make(map[string]struct{}, len(nodes))
	for _, n := range nodes {
		keep[n.ID] = struct{}{}
	}
	rels := make([]Edge, 0, len(s.edges))
	for _, e := range s.edges {
		if !sc.matches(e.UserID, e.AgentID) {
			continue
		}
		if e.RecordedAt > t || (e.InvalidatedAt != 0 && e.InvalidatedAt <= t) {
			continue
		}
		if e.ValidFrom > t || (e.ValidTo != 0 && e.ValidTo <= t) {
			continue
		}
		if _, ok := keep[e.From]; !ok {
			continue
		}
		if _, ok := keep[e.To]; !ok {
			continue
		}
		rels = append(rels, cloneEdge(e))
	}
	return nodes, rels
}

// Snapshot returns bounded node + relation lists for the dashboard graph view,
// across all owners. Edges that point at a node outside the bound are dropped so
// the client never receives dangling from/to ids.
func (s *Store) Snapshot(maxNodes int) ([]Node, []Edge) {
	return s.SnapshotScoped(Scope{}, maxNodes)
}

// SnapshotScoped is Snapshot restricted to the owner named by sc.
func (s *Store) SnapshotScoped(sc Scope, maxNodes int) ([]Node, []Edge) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	nodes := make([]Node, 0, len(s.nodes))
	for _, n := range s.nodes {
		if sc.matches(n.UserID, n.AgentID) {
			nodes = append(nodes, n)
		}
	}
	sort.Slice(nodes, func(i, j int) bool {
		if nodes[i].Label != nodes[j].Label {
			return nodes[i].Label < nodes[j].Label
		}
		return nodes[i].ID < nodes[j].ID
	})
	if maxNodes > 0 && len(nodes) > maxNodes {
		nodes = nodes[:maxNodes]
	}
	keep := make(map[string]struct{}, len(nodes))
	for _, n := range nodes {
		keep[n.ID] = struct{}{}
	}
	rels := make([]Edge, 0, len(s.edges))
	for _, e := range s.edges {
		if !sc.matches(e.UserID, e.AgentID) {
			continue
		}
		if _, ok := keep[e.From]; !ok {
			continue
		}
		if _, ok := keep[e.To]; !ok {
			continue
		}
		rels = append(rels, cloneEdge(e))
	}
	return nodes, rels
}

// nodeID is the stable id for a label under an owner. An unowned label keeps
// the original id, so an existing graph.json still resolves.
func nodeID(userID, agentID, label string) string {
	if userID == "" && agentID == "" {
		return idForLabel(label)
	}
	return idForLabel(userID + "\x00" + agentID + "\x00" + label)
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

// cleanSources returns the non-empty, de-duplicated sources with primary first.
func cleanSources(primary string, srcs []string) []string {
	var out []string
	for _, s := range append([]string{primary}, srcs...) {
		if s != "" && !containsString(out, s) {
			out = append(out, s)
		}
	}
	return out
}

func containsString(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

// cloneEdge copies an edge so a snapshot does not share its Sources backing
// array with the store.
func cloneEdge(e Edge) Edge {
	if e.Sources != nil {
		e.Sources = append([]string(nil), e.Sources...)
	}
	return e
}
