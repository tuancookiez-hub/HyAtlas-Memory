package main

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"sync/atomic"
	"time"

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
	// Meta carries the full metadata bag at index-write time. Lets list
	// endpoints surface session_id, source_layer_label, etc. without a
	// second lookup. May be nil for older index files.
	Meta map[string]string `json:"meta,omitempty"`
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
	// superseded is the set of index ids that are superseded, with their layer, and
	// hidden counts those per layer. Search reads hidden to decide how far to
	// over-fetch, and checks membership in superseded, so neither scans the index.
	// Every index write goes through putLocked or forgetLocked to keep them in step.
	superseded map[string]memory.Layer
	hidden     map[memory.Layer]int
	// supMu serializes Supersede passes. It is separate from mu so Adds and
	// Searches do not wait on the chromem rewrites a pass makes.
	supMu sync.Mutex
	// rowMu serializes each chromem row write made by Add with the rewrite of the
	// same row made by Supersede. Held only around one row at a time.
	rowMu sync.Mutex
	// supersedeHook is a test seam. Supersede calls it with stage "rewrite" (id = the
	// row about to be rewritten) and "commit" (once, before the index update), which
	// are the points where a concurrent Add or Delete can interleave. Nil in production.
	supersedeHook func(stage, id string)
	// l5Dedupe is the similarity (HYATLAS_DEDUPE_SCORE) at or above which a graph
	// relation is not indexed as an L5 document because one for the same owner
	// already says it. Zero turns the check off. Set once, before any indexing.
	l5Dedupe float64
	// persisted index path (same dir as the chromem DB)
	indexPath string
	// usage counters — atomic so reads from /api/v1/status never block writes.
	// Persisted as JSON next to the doc index so they survive restart.
	writes     atomic.Uint64
	searches   atomic.Uint64
	countsPath string

	// pending tracks in-flight persistUsageAsync goroutines so Close can wait
	// for them; without it a goroutine writes into the data dir after the caller
	// has moved on (which makes t.TempDir() cleanup fail with "directory not
	// empty"). countsMu serializes the file write itself: concurrent callers
	// share one fixed .tmp path, so unserialized writes can clobber each other.
	pending   sync.WaitGroup
	countsMu  sync.Mutex
	closeMu   sync.Mutex // guards the closed flag against pending.Add
	closeOnce sync.Once
	closed    bool
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
		superseded: map[string]memory.Layer{}, hidden: map[memory.Layer]int{},
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
	// Prefer the persisted doc index (carries exact-match fields chromem
	// metadata loses, e.g. the extracted flag); rebuild from chromem only when
	// the file is missing or its size disagrees with the collections.
	if !s.loadIndex() {
		if err := s.rebuildIndex(); err != nil {
			return nil, err
		}
	}
	return s, nil
}

// loadIndex restores the exact index from doc_index.json. Returns false when
// the file is absent, corrupt, or out of sync with the chromem collections —
// the caller then falls back to rebuildIndex.
func (s *MemoryStore) loadIndex() bool {
	if s.indexPath == "" {
		return false
	}
	b, err := os.ReadFile(s.indexPath)
	if err != nil || len(b) == 0 {
		return false
	}
	var idx map[string]DocIndex
	if err := json.Unmarshal(b, &idx); err != nil {
		return false
	}
	total := 0
	for _, l := range memory.All() {
		total += s.cols[l].Count()
	}
	if len(idx) != total {
		return false
	}
	s.index = idx
	s.rebuildSupersededLocked()
	return true
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
	// Embed before rowMu. The embedder can be a slow or hung HTTP call, and holding
	// the row lock across it would stall every Add and Supersede. Chromem uses the
	// vector given here rather than embedding the content a second time.
	emb, err := s.embed.Embed(s.ctx, content)
	if err != nil {
		return fmt.Errorf("couldn't create embedding of document: %w", err)
	}
	doc.Embedding = emb
	// rowMu keeps this write apart from a Supersede rewrite of the same row, so a
	// rewrite can never put a stale row back over this one. The index update stays
	// under it too, so a rewrite never sees a chromem row the index does not match.
	s.rowMu.Lock()
	err = s.cols[layer].AddDocument(s.ctx, doc)
	if err == nil {
		s.mu.Lock()
		s.putLocked(docIndexFrom(id, string(layer), content, meta))
		s.mu.Unlock()
	}
	s.rowMu.Unlock()
	if err != nil {
		return err
	}
	s.writes.Add(1)
	s.persistUsageAsync()
	return s.persistIndex()
}

// isSuperseded reports whether the slow path replaced or dropped a doc. Such a
// doc keeps its row for provenance but is invisible to every live read.
func isSuperseded(d DocIndex) bool { return d.Meta["invalid_at"] != "" }

// Search does vector search, scoped to user/agent when provided. Superseded
// docs are never returned.
func (s *MemoryStore) Search(query string, limit int, layer memory.Layer, userID, agentID string) ([]SearchHit, error) {
	hits, err := s.search(query, limit, layer, userID, agentID)
	if err != nil {
		return nil, err
	}
	s.searches.Add(1)
	s.persistUsageAsync()
	return hits, nil
}

// SearchOwners is Search across several user IDs, for one person known by more than
// one ID. Each ID is searched with the same agent filter, the hits are merged by
// score, duplicates (same ID) are dropped, and the best limit are kept. With no user
// ID it is exactly Search. It counts as one search.
//
// Memories with no owner at all (no user_id and no agent_id: rows written before
// owners were recorded, which is most of an older store's graph) are included under
// any owner, as the graph endpoints already do, so single-user data from older
// releases stays reachable.
func (s *MemoryStore) SearchOwners(query string, limit int, layer memory.Layer, userIDs []string, agentID string) ([]SearchHit, error) {
	if len(userIDs) == 0 {
		return s.Search(query, limit, layer, "", agentID)
	}
	if limit <= 0 {
		limit = 5
	}
	// Embed once for every ID and layer; chromem's Query would embed the query
	// again for each of them.
	qv, err := s.embed.Embed(s.ctx, query)
	if err != nil {
		return nil, fmt.Errorf("couldn't create embedding of query: %w", err)
	}
	var merged []SearchHit
	for _, id := range userIDs {
		hits, err := s.searchVec(qv, limit, layer, id, agentID)
		if err != nil {
			return nil, err
		}
		merged = append(merged, hits...)
	}
	ownerless, err := s.searchWhere(qv, limit, layer, map[string]string{"user_id": "", "agent_id": ""})
	if err != nil {
		return nil, err
	}
	merged = append(merged, ownerless...)
	sort.SliceStable(merged, func(i, j int) bool { return merged[i].Score > merged[j].Score })
	seen := make(map[string]bool, len(merged))
	var out []SearchHit
	for _, h := range merged {
		if seen[h.ID] {
			continue
		}
		seen[h.ID] = true
		out = append(out, h)
	}
	if len(out) > limit {
		out = out[:limit]
	}
	s.searches.Add(1)
	s.persistUsageAsync()
	return out, nil
}

// search is the body of Search without the usage counter, so SearchOwners can
// count a multi-ID search once.
func (s *MemoryStore) search(query string, limit int, layer memory.Layer, userID, agentID string) ([]SearchHit, error) {
	qv, err := s.embed.Embed(s.ctx, query)
	if err != nil {
		return nil, fmt.Errorf("couldn't create embedding of query: %w", err)
	}
	return s.searchVec(qv, limit, layer, userID, agentID)
}

// searchVec is search with the query already embedded, so one embedding serves
// every layer (and, from SearchOwners, every user ID) instead of one per query.
func (s *MemoryStore) searchVec(qv []float32, limit int, layer memory.Layer, userID, agentID string) ([]SearchHit, error) {
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
	return s.searchWhere(qv, limit, layer, where)
}

// searchWhere is searchVec with the metadata filter given as is. An empty value
// matches only rows whose field is empty or absent, which is how SearchOwners asks
// for ownerless rows; searchVec instead leaves an empty owner out of the filter.
func (s *MemoryStore) searchWhere(qv []float32, limit int, layer memory.Layer, where map[string]string) ([]SearchHit, error) {
	if limit <= 0 {
		limit = 5
	}
	layers := []memory.Layer{}
	if layer != "" {
		layers = []memory.Layer{layer}
	} else {
		layers = memory.All()
	}

	// Superseded rows still sit in chromem, so they can take nearest-neighbour
	// slots. The store keeps a per-layer count of them, so each layer asks chromem
	// for that many extra neighbours and the live results still fill the limit.
	var hits []SearchHit
	for _, l := range layers {
		col := s.cols[l]
		n := col.Count()
		s.mu.RLock()
		k := limit + s.hidden[l] // note: chromem requires k <= n; guard below
		s.mu.RUnlock()
		if k > n {
			k = n
		}
		if k <= 0 {
			continue
		}
		res, err := col.QueryEmbedding(s.ctx, qv, k, where, nil)
		if err != nil {
			return nil, err
		}
		s.mu.RLock()
		for _, r := range res {
			if _, dead := s.superseded[r.ID]; dead {
				continue
			}
			hits = append(hits, SearchHit{ID: r.ID, Content: r.Content,
				Score: r.Similarity, Layer: memory.Layer(l), Meta: r.Metadata})
		}
		s.mu.RUnlock()
	}
	sort.Slice(hits, func(i, j int) bool { return hits[i].Score > hits[j].Score })
	if len(hits) > limit {
		hits = hits[:limit]
	}
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

// List returns live exact-match docs, optionally filtered by layer/user/agent,
// with pagination. Superseded docs are left out; use ListAll for history.
// excludeRaw drops l2_raw rows BEFORE pagination (and from total), so a raw-heavy
// head cannot empty a page — that is the include_raw=false contract.
func (s *MemoryStore) List(layer memory.Layer, userID, agentID string, limit, offset int, excludeRaw bool) ([]DocIndex, int) {
	return s.list(layer, userID, agentID, limit, offset, excludeRaw, false)
}

// ListAll is List including superseded docs, for the history view
// (/api/v1/list with include_superseded).
func (s *MemoryStore) ListAll(layer memory.Layer, userID, agentID string, limit, offset int, excludeRaw bool) ([]DocIndex, int) {
	return s.list(layer, userID, agentID, limit, offset, excludeRaw, true)
}

func (s *MemoryStore) list(layer memory.Layer, userID, agentID string, limit, offset int, excludeRaw, includeSuperseded bool) ([]DocIndex, int) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var all []DocIndex
	for _, d := range s.index {
		if layer != "" && d.Layer != string(layer) {
			continue
		}
		if excludeRaw && d.Layer == string(memory.L2Raw) {
			continue
		}
		if !includeSuperseded && isSuperseded(d) {
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
		s.forgetLocked(id)
		deleted++
	}
	return deleted, s.persistIndexLocked()
}

// LayerCounts returns the number of live docs per layer (exact). L5 is the
// exception: knowledge lives in the graph store (entities are the durable
// rows), never in chromem, so its count is the graph node count. Every
// caller gets the same numbers — no per-handler overrides.
func (s *MemoryStore) LayerCounts() map[string]int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := map[string]int{}
	for _, l := range memory.All() {
		out[string(l)] = 0
	}
	for _, d := range s.index {
		if isSuperseded(d) {
			continue
		}
		out[d.Layer]++
	}
	out[string(memory.L5Knowledge)] = s.g.NodeCount()
	return out
}

// TotalMemories counts live docs across all layers.
func (s *MemoryStore) TotalMemories() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	n := 0
	for _, d := range s.index {
		if !isSuperseded(d) {
			n++
		}
	}
	return n
}

// Supersede marks live docs as replaced by `by`, or as dropped when by is empty,
// instead of deleting them. The row keeps its content, vector and provenance, so
// history stays reachable through ListAll. Returns how many docs were marked.
//
// A pass runs in three steps: it snapshots the live rows it was asked about,
// rewrites each chromem row with the new metadata, then updates the index. The
// rewrite and the index update each hold the lock only briefly, so Adds and
// Searches keep moving. Because Adds can land in between, a row is rewritten only
// while the index still holds the version that was snapshotted, and it is marked
// only if that version is still current at the end. A row deleted or replaced by
// a same-id Add in the meantime is left as the newer write made it: a stale
// snapshot is never written back, and a vanished row is skipped without error.
func (s *MemoryStore) Supersede(ids []string, by string) (int, error) {
	return s.supersedeWith(ids, by, nil)
}

// supersedeWith is Supersede that also records extra metadata on each marked row,
// such as why a fact was dropped.
func (s *MemoryStore) supersedeWith(ids []string, by string, extra map[string]string) (int, error) {
	s.supMu.Lock()
	defer s.supMu.Unlock()
	now := time.Now().UTC().Format(time.RFC3339)

	// 1. Snapshot the live rows named by ids.
	s.mu.RLock()
	var todo []DocIndex
	seen := make(map[string]bool, len(ids))
	for _, id := range ids {
		d, ok := s.index[id]
		if !ok || isSuperseded(d) || seen[id] {
			continue
		}
		seen[id] = true
		todo = append(todo, d)
	}
	s.mu.RUnlock()

	// 2. Rewrite each chromem row, under rowMu, while the index still holds the
	// snapshotted version. Content and vector come from chromem, read under the
	// read lock so a Delete cannot land halfway through the read.
	type rewrite struct {
		snap DocIndex
		meta map[string]string
	}
	var firstErr error
	var done []rewrite
	for _, d := range todo {
		s.hook("rewrite", d.ID)
		s.rowMu.Lock()
		s.mu.RLock()
		cur, ok := s.index[d.ID]
		current := ok && !isSuperseded(cur) && sameVersion(cur, d)
		col := s.cols[memory.Layer(d.Layer)]
		var old chromem.Document
		var getErr error
		if current && col != nil {
			old, getErr = col.GetByID(s.ctx, d.ID)
		}
		s.mu.RUnlock()
		if !current || col == nil {
			// Deleted, or replaced by a same-id Add: nothing to supersede.
			s.rowMu.Unlock()
			continue
		}
		meta := make(map[string]string, len(d.Meta)+3)
		for k, v := range d.Meta {
			meta[k] = v
		}
		meta["invalid_at"] = now
		meta["superseded_by"] = by
		for k, v := range extra {
			meta[k] = v
		}
		meta["layer"] = d.Layer
		if getErr != nil {
			if firstErr == nil {
				firstErr = getErr
			}
		} else if err := col.AddDocument(s.ctx, chromem.Document{
			ID: d.ID, Content: old.Content, Embedding: old.Embedding, Metadata: meta,
		}); err != nil && firstErr == nil {
			firstErr = err
		}
		s.rowMu.Unlock()
		done = append(done, rewrite{snap: d, meta: meta})
	}

	// 3. Update the exact index under the write lock. A row that changed since the
	// snapshot is left alone: a same-id Add wrote both chromem and the index after
	// our rewrite, so its version is the live one.
	s.hook("commit", "")
	s.mu.Lock()
	defer s.mu.Unlock()
	marked := 0
	for _, r := range done {
		cur, ok := s.index[r.snap.ID]
		if !ok {
			// Deleted while the rewrite was in flight. The rewrite may have
			// re-created the chromem row, so remove it again.
			if col, ok := s.cols[memory.Layer(r.snap.Layer)]; ok {
				_ = col.Delete(s.ctx, nil, nil, r.snap.ID)
			}
			continue
		}
		if isSuperseded(cur) || !sameVersion(cur, r.snap) {
			continue
		}
		cur.Meta = r.meta
		s.putLocked(cur)
		marked++
	}
	if err := s.persistIndexLocked(); err != nil && firstErr == nil {
		firstErr = err
	}
	return marked, firstErr
}

// sameVersion reports whether two index rows hold the same content and metadata,
// which is how Supersede tells that a row has not been replaced since it was read.
func sameVersion(a, b DocIndex) bool {
	return a.Content == b.Content && maps.Equal(a.Meta, b.Meta)
}

// hook runs the supersedeHook test seam, if one is set.
func (s *MemoryStore) hook(stage, id string) {
	if s.supersedeHook != nil {
		s.supersedeHook(stage, id)
	}
}

// MirrorsOf returns the live ids of the rows in layer that carry the same
// source_id and the same content. It finds the L1 Profile rows that mirror an L3
// preference: promoteExtraction writes both from one fact, so they share both.
// Superseded rows are excluded. An empty sourceID matches nothing, because rows
// without provenance do not show a mirror. The ids are sorted.
func (s *MemoryStore) MirrorsOf(layer memory.Layer, sourceID, content string) []string {
	if sourceID == "" {
		return nil
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	var ids []string
	for id, d := range s.index {
		if d.Layer != string(layer) || d.Content != content || isSuperseded(d) {
			continue
		}
		if d.Meta["source_id"] != sourceID {
			continue
		}
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// SetExtracted marks a doc as extracted (used after successful promotion).
func (s *MemoryStore) SetExtracted(id string, v bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if d, ok := s.index[id]; ok {
		d.Extracted = v
		s.putLocked(d)
	}
	return s.persistIndexLocked()
}

// GetMany returns the docs for the given ids in order (missing ids are skipped).
func (s *MemoryStore) GetMany(ids []string) []DocIndex {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]DocIndex, 0, len(ids))
	for _, id := range ids {
		if d, ok := s.index[id]; ok {
			out = append(out, d)
		}
	}
	return out
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
	// Hold closeMu across the closed-check and Add so a concurrent Close cannot
	// observe an empty WaitGroup and start Wait while this Add is still pending
	// (concurrent Add/Wait on a zero counter is WaitGroup misuse).
	s.closeMu.Lock()
	if s.closed {
		s.closeMu.Unlock()
		return
	}
	s.pending.Add(1)
	s.closeMu.Unlock()
	go func() {
		defer s.pending.Done()
		s.persistUsage()
	}()
}

// Close drains any pending async persistence before the store is discarded, so
// no goroutine writes into the data dir afterwards. Safe to call multiple times.
func (s *MemoryStore) Close() {
	s.closeOnce.Do(func() {
		// Stop new async writes...
		s.closeMu.Lock()
		s.closed = true
		s.closeMu.Unlock()
		// ...then wait for the ones already in flight. Wait is outside closeMu so
		// a goroutine finishing its Done() cannot deadlock against Close.
		s.pending.Wait()
		// Final synchronous snapshot so the last counters are not lost.
		s.persistUsage()
	})
}

func (s *MemoryStore) persistUsage() error {
	if s.countsPath == "" {
		return nil
	}
	// Callers share one fixed .tmp path; without this lock two concurrent
	// goroutines can interleave their writes and rename a half-written file.
	s.countsMu.Lock()
	defer s.countsMu.Unlock()
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
		d.Meta = meta
	}
	return d
}

// putLocked stores d in the exact index and keeps the superseded set and the
// per-layer counts in step. The caller holds s.mu for writing.
func (s *MemoryStore) putLocked(d DocIndex) {
	s.forgetLocked(d.ID)
	s.index[d.ID] = d
	if isSuperseded(d) {
		l := memory.Layer(d.Layer)
		s.superseded[d.ID] = l
		s.hidden[l]++
	}
}

// forgetLocked removes id from the exact index and from the superseded
// bookkeeping. The caller holds s.mu for writing.
func (s *MemoryStore) forgetLocked(id string) {
	if l, ok := s.superseded[id]; ok {
		delete(s.superseded, id)
		s.hidden[l]--
		if s.hidden[l] <= 0 {
			delete(s.hidden, l)
		}
	}
	delete(s.index, id)
}

// rebuildSupersededLocked recomputes the superseded set and per-layer counts from
// the whole index. It runs once after the index is loaded from disk.
func (s *MemoryStore) rebuildSupersededLocked() {
	s.superseded = map[string]memory.Layer{}
	s.hidden = map[memory.Layer]int{}
	for id, d := range s.index {
		if isSuperseded(d) {
			l := memory.Layer(d.Layer)
			s.superseded[id] = l
			s.hidden[l]++
		}
	}
}

// persistIndex takes the write lock, not a read lock. Concurrent Adds (the
// background extraction goroutines) all write the same doc_index.json.tmp path,
// and two readers holding RLock would interleave those writes and rename a
// half-written file over the index.
func (s *MemoryStore) persistIndex() error {
	s.mu.Lock()
	defer s.mu.Unlock()
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
			s.putLocked(docIndexFrom(r.ID, string(l), r.Content, r.Metadata))
		}
	}
	return s.persistIndex()
}

func dimFor(l memory.Layer) int { return 384 }
