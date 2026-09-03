package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"sync/atomic"

	"github.com/philippgille/chromem-go"
	"github.com/tuancookiez-hub/hyatlas-v4/graph"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// UsageCounters is the JSON shape persisted next to the doc index.
type UsageCounters struct {
	Writes   uint64 `json:"writes"`
	Searches uint64 `json:"searches"`
}

// DocIndex is an exact-match record for a stored memory doc. It powers list /
// delete / metrics / scoping without relying on approximate vector search.
type DocIndex struct {
	ID        string `json:"id"`
	Layer     string `json:"layer"`
	Content   string `json:"content"`
	UserID    string `json:"user_id"`
	AgentID   string `json:"agent_id"`
	Ts        string `json:"ts"`
	Extracted bool   `json:"extracted"`
}

// MemoryStore holds layer collections (vectors) + a doc index (exact) + the L5 graph.
type MemoryStore struct {
	db    *chromem.DB
	g     *graph.Store
	embed Embedder
	ctx   context.Context
	cols  map[memory.Layer]*chromem.Collection

	mu    sync.RWMutex
	index map[string]DocIndex
	// persisted index path (same dir as the chromem DB)
	indexPath string
	// usage counters — atomic so reads from /api/v1/status never block writes.
	// Persisted as JSON next to the doc index so they survive restart.
	writes     atomic.Uint64
	searches   atomic.Uint64
	countsPath string
}

// NewMemoryStore opens (or creates) the persistent layer DB + graph + doc index.
func NewMemoryStore(ctx context.Context, dir string, embed Embedder, graphPath string) (*MemoryStore, error) {
	db, err := chromem.NewPersistentDB(dir, false)
	if err != nil {
		return nil, err
	}
	g, err := graph.New(graphPath)
	if err != nil {
		return nil, err
	}
	ef := func(c context.Context, text string) ([]float32, error) {
		return embed.Embed(c, text)
	}
	s := &MemoryStore{db: db, g: g, embed: embed, ctx: ctx,
		cols: map[memory.Layer]*chromem.Collection{}, index: map[string]DocIndex{},
		indexPath:  filepath.Join(dir, "doc_index.json"),
		countsPath: filepath.Join(dir, "usage.json")}
	// load persisted counters before rebuildIndex so writes/searches survive restart.
	s.loadUsage()
	for _, l := range memory.All() {
		col, err := db.GetOrCreateCollection(string(l), nil, ef)
		if err != nil {
			return nil, err
		}
		s.cols[l] = col
	}
	// rebuild the exact index from chromem's persisted docs
	if err := s.rebuildIndex(); err != nil {
		return nil, err
	}
	return s, nil
}

// Add writes a doc into a layer collection and updates the exact index.
func (s *MemoryStore) Add(layer memory.Layer, id, content string, meta map[string]string) error {
	doc := chromem.Document{ID: id, Content: content}
	if meta != nil {
		doc.Metadata = meta
	}
	if doc.Metadata == nil {
		doc.Metadata = map[string]string{}
	}
	doc.Metadata["layer"] = string(layer)
	if err := s.cols[layer].AddDocument(s.ctx, doc); err != nil {
		return err
	}
	s.mu.Lock()
	s.index[id] = docIndexFrom(id, string(layer), content, meta)
	s.mu.Unlock()
	s.writes.Add(1)
	s.persistUsageAsync()
	return s.persistIndex()
}

// Search does vector search, scoped to user/agent when provided.
func (s *MemoryStore) Search(query string, limit int, layer memory.Layer, userID, agentID string) ([]SearchHit, error) {
	if limit <= 0 {
		limit = 5
	}
	layers := []memory.Layer{}
	if layer != "" {
		layers = []memory.Layer{layer}
	} else {
		layers = memory.All()
	}
	where := map[string]string{}
	if userID != "" {
		where["user_id"] = userID
	}
	if agentID != "" {
		where["agent_id"] = agentID
	}
	if len(where) == 0 {
		where = nil
	}

	var hits []SearchHit
	for _, l := range layers {
		col := s.cols[l]
		n := col.Count()
		k := limit // note: chromem requires k <= n; guard below
		if k > n {
			k = n
		}
		if k <= 0 {
			continue
		}
		res, err := col.Query(s.ctx, query, k, where, nil)
		if err != nil {
			return nil, err
		}
		for _, r := range res {
			hits = append(hits, SearchHit{ID: r.ID, Content: r.Content,
				Score: r.Similarity, Layer: memory.Layer(l), Meta: r.Metadata})
		}
	}
	sort.Slice(hits, func(i, j int) bool { return hits[i].Score > hits[j].Score })
	if len(hits) > limit {
		hits = hits[:limit]
	}
	s.searches.Add(1)
	s.persistUsageAsync()
	return hits, nil
}

// SearchHit is a ranked result tagged with its layer.
type SearchHit struct {
	ID      string
	Content string
	Score   float32
	Layer   memory.Layer
	Meta    map[string]string
}

// List returns exact-match docs, optionally filtered by layer/user/agent, with pagination.
func (s *MemoryStore) List(layer memory.Layer, userID, agentID string, limit, offset int) ([]DocIndex, int) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var all []DocIndex
	for _, d := range s.index {
		if layer != "" && d.Layer != string(layer) {
			continue
		}
		if userID != "" && d.UserID != userID {
			continue
		}
		if agentID != "" && d.AgentID != agentID {
			continue
		}
		all = append(all, d)
	}
	// stable sort by ts desc
	sort.Slice(all, func(i, j int) bool { return all[i].Ts > all[j].Ts })
	total := len(all)
	if offset > total {
		offset = total
	}
	end := offset + limit
	if end > total {
		end = total
	}
	if offset > end {
		offset = end
	}
	return all[offset:end], total
}

// Delete removes docs by id (or by layer/user/agent scope). Returns count deleted.
func (s *MemoryStore) Delete(ids []string, layer memory.Layer, userID, agentID string) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	targets := map[string]bool{}
	if len(ids) > 0 {
		for _, id := range ids {
			if _, ok := s.index[id]; ok {
				targets[id] = true
			}
		}
	} else {
		for id, d := range s.index {
			if layer != "" && d.Layer != string(layer) {
				continue
			}
			if userID != "" && d.UserID != userID {
				continue
			}
			if agentID != "" && d.AgentID != agentID {
				continue
			}
			targets[id] = true
		}
	}
	deleted := 0
	for id := range targets {
		d := s.index[id]
		if col, ok := s.cols[memory.Layer(d.Layer)]; ok {
			_ = col.Delete(s.ctx, nil, nil, id)
		}
		delete(s.index, id)
		deleted++
	}
	return deleted, s.persistIndexLocked()
}

// LayerCounts returns the number of docs per layer (exact).
func (s *MemoryStore) LayerCounts() map[string]int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := map[string]int{}
	for _, l := range memory.All() {
		out[string(l)] = 0
	}
	for _, d := range s.index {
		out[d.Layer]++
	}
	return out
}

// TotalMemories sums all layer docs.
func (s *MemoryStore) TotalMemories() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.index)
}

// SetExtracted marks a doc as extracted (used after successful promotion).
func (s *MemoryStore) SetExtracted(id string, v bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if d, ok := s.index[id]; ok {
		d.Extracted = v
		s.index[id] = d
	}
	return s.persistIndexLocked()
}

// Graph exposes the L5 knowledge graph.
func (s *MemoryStore) Graph() *graph.Store { return s.g }

// ---- usage counters ----

// Usage returns the current atomic counters (writes, searches) — read-only
// snapshot for /api/v1/status. Used by the desktop pane to show whether the
// memory system is actually being read.
func (s *MemoryStore) Usage() (writes, searches uint64) {
	return s.writes.Load(), s.searches.Load()
}

// UsageForJSON flattens Usage into a single map for embedding in
// /api/v1/status, /api/info, and /api/layer-counts. JSON must serialize
// uint64 as a number, and (w, s) tuples do not.
func (s *MemoryStore) UsageForJSON() map[string]uint64 {
	w, q := s.Usage()
	return map[string]uint64{"writes": w, "searches": q}
}

func (s *MemoryStore) loadUsage() {
	if s.countsPath == "" {
		return
	}
	b, err := os.ReadFile(s.countsPath)
	if err != nil || len(b) == 0 {
		return
	}
	var c UsageCounters
	if err := json.Unmarshal(b, &c); err != nil {
		return
	}
	s.writes.Store(c.Writes)
	s.searches.Store(c.Searches)
}

// persistUsageAsync writes the counters without blocking the request path.
// One pending write at a time; the latest call's snapshot wins.
func (s *MemoryStore) persistUsageAsync() {
	if s.countsPath == "" {
		return
	}
	go func() {
		s.persistUsage()
	}()
}

func (s *MemoryStore) persistUsage() error {
	if s.countsPath == "" {
		return nil
	}
	c := UsageCounters{Writes: s.writes.Load(), Searches: s.searches.Load()}
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.countsPath), 0o755); err != nil {
		return err
	}
	tmp := s.countsPath + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.countsPath)
}

// ---- index persistence ----

func docIndexFrom(id, layer, content string, meta map[string]string) DocIndex {
	d := DocIndex{ID: id, Layer: layer, Content: content}
	if meta != nil {
		d.UserID = meta["user_id"]
		d.AgentID = meta["agent_id"]
		d.Ts = meta["ts"]
		d.Extracted = meta["extracted"] == "true"
	}
	return d
}

func (s *MemoryStore) persistIndex() error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.persistIndexLocked()
}

// persistIndexLocked writes the index assuming the caller already holds s.mu.
// It does NOT take any lock (avoids double-lock / RLock-while-WriteLock deadlocks).
func (s *MemoryStore) persistIndexLocked() error {
	if s.indexPath == "" {
		return nil
	}
	data, err := json.MarshalIndent(s.index, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.indexPath), 0o755); err != nil {
		return err
	}
	tmp := s.indexPath + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.indexPath)
}

// rebuildIndex reconstructs the exact index from chromem by enumerating all docs.
func (s *MemoryStore) rebuildIndex() error {
	// chromem has no "list all"; enumerate via QueryEmbedding with zero vector over each layer.
	for _, l := range memory.All() {
		col := s.cols[l]
		n := col.Count()
		if n == 0 {
			continue
		}
		// zero vector queries return all docs (score ~0) in arbitrary order.
		res, err := col.QueryEmbedding(s.ctx, make([]float32, dimFor(l)), n, nil, nil)
		if err != nil {
			// if zero-vector fails, fall back to a neutral query
			res, err = col.Query(s.ctx, "memory", n, nil, nil)
			if err != nil {
				continue
			}
		}
		for _, r := range res {
			s.index[r.ID] = docIndexFrom(r.ID, string(l), r.Content, r.Metadata)
		}
	}
	return s.persistIndex()
}

func dimFor(l memory.Layer) int { return 384 }
