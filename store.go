package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"maps"
	"os"
	"path/filepath"
	"slices"
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
	// semantic is false when the embedder's vectors carry no meaning (the hashed
	// local stub), so similarity guards must not judge by them.
	semantic bool
	ctx      context.Context
	cols     map[memory.Layer]*chromem.Collection
	// clauses holds the search-only clause rows of long L3 facts (see clauses.go).
	// It is not a memory.Layer, so counts, lists and normal search never see it.
	clauses *chromem.Collection

	// aliases maps a user ID to every ID of the same person (HYATLAS_USER_ALIASES),
	// the whole group, itself included. It is set once at start with SetAliases and
	// guarded by aliasMu, not mu, so reading it never waits on the index lock.
	aliasMu sync.RWMutex
	aliases map[string][]string

	mu    sync.RWMutex
	index map[string]DocIndex
	// replacedBy maps a successor id to the ids of the rows it replaced, for rows whose
	// superseded_how is howReplaced. historyOf reads it so it never walks every
	// superseded row. It is kept in step with superseded by putLocked, forgetLocked
	// and rebuildSupersededLocked.
	replacedBy map[string][]string
	// kw is the inverted keyword index over the live rows (see keyword.go). It
	// is kept in step with index by putLocked and forgetLocked and rebuilt with the
	// superseded bookkeeping, and read under mu like the index itself.
	kw *kwIndex
	// superseded is the set of index ids that are superseded, with their layer, and
	// hidden counts those per layer. Search reads hidden to decide how far to
	// over-fetch, and checks membership in superseded, so neither scans the index.
	// Every index write goes through putLocked or forgetLocked to keep them in step.
	superseded map[string]memory.Layer
	hidden     map[memory.Layer]int
	// labelRows counts the live rows of each source_layer_label per layer, so a search
	// that leaves one label out knows how far to over-fetch. mirrors maps a live row's
	// (layer, source_id) to the ids of the live rows that carry it, so MirrorsOf reads
	// one bucket instead of the whole index. Both are kept in step with index by
	// putLocked, forgetIndexLocked and rebuildSupersededLocked.
	labelRows map[memory.Layer]map[string]int
	mirrors   map[mirrorKey][]string
	// wantExtract counts the live raw rows that were written to be extracted and have
	// not been yet (see awaitsExtraction), so /api/v1/status reads one number instead
	// of scanning the index. It follows the same hooks as labelRows.
	wantExtract int
	// keywordRaw is whether raw (L2) rows are kept in the keyword index. Only a server
	// that does not extract needs them there (see SetKeywordRaw); otherwise a keyword
	// search that asks for raw rows ranks them on the fly.
	keywordRaw bool
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
	// pages is the entity-page store (L8). It is created with the store so every
	// path that opens one gets pages.
	pages *PageStore
	// persisted index path (same dir as the chromem DB)
	indexPath string
	// usage counters — atomic so reads from /api/v1/status never block writes.
	// Persisted as JSON next to the doc index so they survive restart.
	writes     atomic.Uint64
	searches   atomic.Uint64
	countsPath string

	// dirty is set (by markDirtyLocked, with s.mu held) when the index has changed
	// since the last write to indexPath. The flusher goroutine and Flush clear it.
	dirty atomic.Bool
	// flushMu serialises index file writes, so two flushes never share the .tmp path.
	flushMu sync.Mutex
	// indexWrites counts index file writes. It is read only by tests.
	indexWrites atomic.Uint64
	// stopFlush asks the flusher goroutine to exit, and flushDone closes when it has.
	stopFlush chan struct{}
	flushDone chan struct{}

	// pending tracks in-flight persistUsageAsync goroutines so Close can wait
	// for them; without it a goroutine writes into the data dir after the caller
	// has moved on (which makes t.TempDir() cleanup fail with "directory not
	// empty"). countsMu serializes the file write itself: concurrent callers
	// share one fixed .tmp path, so unserialized writes can clobber each other.
	pending  sync.WaitGroup
	countsMu sync.Mutex
	// usageQueued is set while a persistUsageAsync write is queued and not yet started.
	usageQueued atomic.Bool
	closeMu     sync.Mutex // guards the closed flag against pending.Add
	closeOnce   sync.Once
	closed      bool
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
	s := &MemoryStore{db: db, g: g, embed: embed, semantic: isSemantic(embed), ctx: ctx,
		cols: map[memory.Layer]*chromem.Collection{}, index: map[string]DocIndex{},
		superseded: map[string]memory.Layer{}, hidden: map[memory.Layer]int{},
		labelRows: map[memory.Layer]map[string]int{}, mirrors: map[mirrorKey][]string{},
		replacedBy: map[string][]string{}, kw: newKwIndex(),
		indexPath:  filepath.Join(dir, "doc_index.json"),
		countsPath: filepath.Join(dir, "usage.json"),
		stopFlush:  make(chan struct{}), flushDone: make(chan struct{})}
	// load persisted counters before rebuildIndex so writes/searches survive restart.
	s.loadUsage()
	for _, l := range memory.All() {
		col, err := db.GetOrCreateCollection(string(l), nil, ef)
		if err != nil {
			return nil, err
		}
		s.cols[l] = col
	}
	clauses, err := db.GetOrCreateCollection(clauseCollection, nil, ef)
	if err != nil {
		return nil, err
	}
	s.clauses = clauses
	pages, err := NewPageStore(dir, s)
	if err != nil {
		return nil, err
	}
	s.pages = pages
	// Prefer the persisted doc index (carries exact-match fields chromem
	// metadata loses, e.g. the extracted flag); rebuild from chromem only when
	// the file is missing or its size disagrees with the collections.
	if !s.loadIndex() {
		if err := s.rebuildIndex(); err != nil {
			return nil, err
		}
	}
	// Repair history left by older versions: links to rows deleted since, and
	// drops made before reasons were recorded. A store with nothing to repair is
	// not written.
	if _, err := s.relinkHistory(nil); err != nil {
		log.Printf("history: relink at start: %v", err)
	}
	go s.flushLoop()
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
	return s.addWithVector(layer, id, content, meta, nil)
}

// addWithVector is Add with the content's embedding already computed, so a caller
// that embedded the text to judge it does not pay for a second embedding. A nil
// emb is computed here.
func (s *MemoryStore) addWithVector(layer memory.Layer, id, content string, meta map[string]string, emb []float32) error {
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
	var err error
	if emb == nil {
		if emb, err = s.embed.Embed(s.ctx, content); err != nil {
			return fmt.Errorf("couldn't create embedding of document: %w", err)
		}
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
		s.markDirtyLocked()
		s.mu.Unlock()
	}
	s.rowMu.Unlock()
	if err != nil {
		return err
	}
	s.writes.Add(1)
	s.persistUsageAsync()
	// Clause rows are written after the parent is indexed, so a clause hit can map
	// back to it. A failure there is logged and does not fail the write.
	s.indexClauses(docIndexFrom(id, string(layer), content, meta))
	return nil
}

// newerFirst orders rows newest first, ties broken by id. Rows come out of a map
// in random order and a consolidation pass writes many rows with the same second,
// so without the tie-break two requests disagree on the order and paging skips or
// repeats rows.
func newerFirst(a, b DocIndex) bool {
	if a.Ts != b.Ts {
		return a.Ts > b.Ts
	}
	return a.ID > b.ID
}

// isSuperseded reports whether the slow path replaced or dropped a doc. Such a
// doc keeps its row for provenance but is invisible to every live read.
func isSuperseded(d DocIndex) bool { return d.Meta[metaInvalidAt] != "" }

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
	// Embed once for every ID and layer; chromem's Query would embed the query
	// again for each of them.
	qv, err := s.embed.Embed(s.ctx, query)
	if err != nil {
		return nil, fmt.Errorf("couldn't create embedding of query: %w", err)
	}
	return s.SearchOwnersVec(qv, limit, layer, userIDs, agentID)
}

// SearchOwnersVec is SearchOwners with the query already embedded, so a caller that
// shares one query vector across the vector, keyword, clause and time-window searches
// embeds only once. It counts as one search.
func (s *MemoryStore) SearchOwnersVec(qv []float32, limit int, layer memory.Layer, userIDs []string, agentID string) ([]SearchHit, error) {
	if len(userIDs) == 0 {
		hits, err := s.searchVec(qv, limit, layer, "", agentID)
		if err != nil {
			return nil, err
		}
		s.searches.Add(1)
		s.persistUsageAsync()
		return hits, nil
	}
	if limit <= 0 {
		limit = 5
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
	return s.searchWhere(qv, limit, layer, ownerWhere(userID, agentID))
}

// SetAliases gives the store the alias groups of HYATLAS_USER_ALIASES: each user ID
// maps to the whole group of IDs of its person, itself included, in the order the
// group was written (see aliasMap). Call it once at start, before the first write; nil
// means no aliases.
func (s *MemoryStore) SetAliases(groups map[string][]string) {
	s.aliasMu.Lock()
	s.aliases = groups
	s.aliasMu.Unlock()
}

// owners is every user ID of the person behind user: user first, then its aliases in
// group order. Without aliases (or for an empty user) it is just [user].
func (s *MemoryStore) owners(user string) []string {
	out := []string{user}
	if s == nil || user == "" {
		return out
	}
	s.aliasMu.RLock()
	group := s.aliases[user]
	s.aliasMu.RUnlock()
	for _, id := range group {
		if !slices.Contains(out, id) {
			out = append(out, id)
		}
	}
	return out
}

// ownersOrAll is owners(user) for a list filter: every owner (nil) when user is empty.
func (s *MemoryStore) ownersOrAll(user string) []string {
	if user == "" {
		return nil
	}
	return s.owners(user)
}

// canonicalOwner is the ID a person's data is keyed under: the first ID of their alias
// group, the same for every ID in it. A user with no aliases is their own canonical ID.
func (s *MemoryStore) canonicalOwner(user string) string {
	if s == nil {
		return user
	}
	s.aliasMu.RLock()
	defer s.aliasMu.RUnlock()
	if g := s.aliases[user]; len(g) > 0 {
		return g[0]
	}
	return user
}

// searchOwnersWhere runs the layer search for each of users (the agent filter applies
// to all) and merges the hits best first, one per row, keeping at most limit. With one
// user it is exactly searchWhereOpts. It does not count as a search.
func (s *MemoryStore) searchOwnersWhere(qv []float32, limit int, layer memory.Layer, users []string, agentID string, opts searchOpts) ([]SearchHit, error) {
	if len(users) <= 1 {
		u := ""
		if len(users) == 1 {
			u = users[0]
		}
		return s.searchWhereOpts(qv, limit, layer, ownerWhere(u, agentID), opts)
	}
	if limit <= 0 {
		limit = 5
	}
	var merged []SearchHit
	seen := map[string]bool{}
	for _, u := range users {
		hits, err := s.searchWhereOpts(qv, limit, layer, ownerWhere(u, agentID), opts)
		if err != nil {
			return nil, err
		}
		for _, h := range hits {
			if !seen[h.ID] {
				seen[h.ID] = true
				merged = append(merged, h)
			}
		}
	}
	sort.SliceStable(merged, func(i, j int) bool { return merged[i].Score > merged[j].Score })
	if len(merged) > limit {
		merged = merged[:limit]
	}
	return merged, nil
}

// ownerWhere builds the metadata filter for an owner: a key for each non-empty
// value, and nil when both are empty.
func ownerWhere(userID, agentID string) map[string]string {
	where := map[string]string{}
	if userID != "" {
		where["user_id"] = userID
	}
	if agentID != "" {
		where["agent_id"] = agentID
	}
	if len(where) == 0 {
		return nil
	}
	return where
}

// docTime parses an index timestamp as RFC3339 with or without fractional seconds.
// It returns the zero time when ts does not parse.
func docTime(ts string) time.Time {
	t, err := time.Parse(time.RFC3339Nano, ts)
	if err != nil {
		return time.Time{}
	}
	return t
}

// liveDocs returns the live (not superseded) rows of layer whose UserID is in users
// (any user when users is empty) and whose AgentID equals agentID when that is
// non-empty. Rows with no owner are not special-cased: they match only when users is
// empty. Each row's Meta is a copy, so callers may keep it.
func (s *MemoryStore) liveDocs(layer memory.Layer, users []string, agentID string) []DocIndex {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var out []DocIndex
	for _, d := range s.index {
		if d.Layer != string(layer) || isSuperseded(d) {
			continue
		}
		if len(users) > 0 && !slices.Contains(users, d.UserID) {
			continue
		}
		if agentID != "" && d.AgentID != agentID {
			continue
		}
		d.Meta = maps.Clone(d.Meta)
		out = append(out, d)
	}
	return out
}

// searchWhere is searchVec with the metadata filter given as is. An empty value
// matches only rows whose field is empty or absent, which is how SearchOwners asks
// for ownerless rows; searchVec instead leaves an empty owner out of the filter.
func (s *MemoryStore) searchWhere(qv []float32, limit int, layer memory.Layer, where map[string]string) ([]SearchHit, error) {
	return s.searchWhereOpts(qv, limit, layer, where, searchOpts{})
}

// searchOpts are the optional filters of searchWhereOpts.
type searchOpts struct {
	// excludeLabel, when set, leaves out rows whose source_layer_label equals it. The
	// walk skips them as it skips superseded rows, and each layer is over-fetched by the
	// number of live rows carrying the label, so the live rows still fill the limit.
	excludeLabel string
}

// searchWhereOpts is searchWhere with the extra filters of opts.
func (s *MemoryStore) searchWhereOpts(qv []float32, limit int, layer memory.Layer, where map[string]string, opts searchOpts) ([]SearchHit, error) {
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
		if opts.excludeLabel != "" {
			k += s.labelRows[l][opts.excludeLabel]
		}
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
			if opts.excludeLabel != "" && r.Metadata[metaSourceLabel] == opts.excludeLabel {
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

// metaSourceLabel is the metadata key that says what produced a row, such as
// experience or user_preferences.
const metaSourceLabel = "source_layer_label"

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
	return s.list(layer, ownerList(userID), agentID, limit, offset, excludeRaw, false)
}

// liveNewestFirst returns every live row of layer, newest first, in List's order, with
// one pass over the index. Rows share their Meta with the index, as List's do, so
// callers must not change it.
func (s *MemoryStore) liveNewestFirst(layer memory.Layer) []DocIndex {
	s.mu.RLock()
	var out []DocIndex
	for _, d := range s.index {
		if d.Layer == string(layer) && !isSuperseded(d) {
			out = append(out, d)
		}
	}
	s.mu.RUnlock()
	sort.Slice(out, func(i, j int) bool { return newerFirst(out[i], out[j]) })
	return out
}

// ListAll is List including superseded docs, for the history view
// (/api/v1/list with include_superseded).
func (s *MemoryStore) ListAll(layer memory.Layer, userID, agentID string, limit, offset int, excludeRaw bool) ([]DocIndex, int) {
	return s.list(layer, ownerList(userID), agentID, limit, offset, excludeRaw, true)
}

// ListOwners is List (or ListAll with includeSuperseded) for a person known by
// several user IDs: a row of any of users matches. An empty users list matches every
// owner. Callers pass owners(userID) to cover a user's aliases.
func (s *MemoryStore) ListOwners(layer memory.Layer, users []string, agentID string, limit, offset int, excludeRaw, includeSuperseded bool) ([]DocIndex, int) {
	return s.list(layer, users, agentID, limit, offset, excludeRaw, includeSuperseded)
}

// ownerList is the one-element owner list of userID, or nil (every owner) for "".
func ownerList(userID string) []string {
	if userID == "" {
		return nil
	}
	return []string{userID}
}

// listTopKRatio is how many times larger the match set must be than offset+limit for
// list to select the page with a bounded top-k pass instead of sorting every match.
const listTopKRatio = 8

// list returns one page of the matching rows, newest first, and the total number of
// matches. A small page of a large match set is selected with a bounded top-k pass,
// which gives the same order as a full sort without sorting every match.
func (s *MemoryStore) list(layer memory.Layer, users []string, agentID string, limit, offset int, excludeRaw, includeSuperseded bool) ([]DocIndex, int) {
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
		if len(users) > 0 && !slices.Contains(users, d.UserID) {
			continue
		}
		if agentID != "" && d.AgentID != agentID {
			continue
		}
		all = append(all, d)
	}
	total := len(all)
	if need := offset + limit; limit > 0 && offset >= 0 && need*listTopKRatio <= total {
		top := topNewest(all, need)
		return top[offset:], total
	}
	sort.Slice(all, func(i, j int) bool { return newerFirst(all[i], all[j]) })
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

// topNewest returns the n newest rows of docs, newest first, in the order newerFirst
// gives. It keeps a sorted buffer of at most n rows and inserts a row only when it
// beats the buffer's last, so it does not sort the rest. n must be at least 1 and at
// most len(docs).
func topNewest(docs []DocIndex, n int) []DocIndex {
	buf := make([]DocIndex, 0, n)
	for _, d := range docs {
		if len(buf) == n && !newerFirst(d, buf[n-1]) {
			continue
		}
		i := sort.Search(len(buf), func(i int) bool { return newerFirst(d, buf[i]) })
		if len(buf) < n {
			buf = append(buf, DocIndex{})
		}
		copy(buf[i+1:], buf[i:])
		buf[i] = d
	}
	return buf
}

// Delete removes docs by id (or by layer/user/agent scope). Returns count deleted.
//
// Rows that a deleted row had replaced keep a readable history: relinkHistory
// points them at the deleted row's own successor, or records why they were dropped.
func (s *MemoryStore) Delete(ids []string, layer memory.Layer, userID, agentID string) (int, error) {
	deleted, successors, err := s.deleteRows(ids, layer, userID, agentID)
	if deleted > 0 {
		if _, rerr := s.relinkHistory(successors); rerr != nil && err == nil {
			err = rerr
		}
	}
	return deleted, err
}

// deleteRows is Delete without the history repair. It also returns what each
// deleted row had been superseded by. It takes rowMu first, like every other chromem
// row write, so a metadata rewrite in flight cannot re-create a row it removes. A
// row whose chromem delete fails stays in the index and the error is returned, so
// the index never loses a row that a rebuild from chromem would bring back.
func (s *MemoryStore) deleteRows(ids []string, layer memory.Layer, userID, agentID string) (int, map[string]string, error) {
	s.rowMu.Lock()
	defer s.rowMu.Unlock()
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
	successors := make(map[string]string, len(targets))
	var firstErr error
	for id := range targets {
		d := s.index[id]
		if col, ok := s.cols[memory.Layer(d.Layer)]; ok {
			if err := col.Delete(s.ctx, nil, nil, id); err != nil {
				if firstErr == nil {
					firstErr = fmt.Errorf("delete %s: %w", id, err)
				}
				continue
			}
		}
		successors[id] = retirementOf(d).By
		s.dropClauses(id)
		s.forgetLocked(id)
		deleted++
	}
	if deleted > 0 {
		s.markDirtyLocked()
	}
	return deleted, successors, firstErr
}

// embedDim is the length of every stored vector (BGE-small).
const embedDim = 384

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
	return s.retire(ids, Retire{By: by})
}

// Keys of the metadata that records how a row was retired.
const (
	// metaSupersededBy names the row that replaced a superseded row, or is empty for a drop.
	metaSupersededBy = "superseded_by"
	// metaDropReason says why a row was dropped rather than replaced.
	metaDropReason = "drop_reason"
	// metaInvalidAt is the time a row stopped being live.
	metaInvalidAt = "invalid_at"
)

// Retire describes how one batch of rows leaves the live set: by which row, in what
// way, and for what reason.
type Retire struct {
	// By is the id of the row that replaces them, or "" for a drop.
	By string
	// How is one of the how* values. When empty, retire uses howReplaced if By is
	// set and howDropped otherwise.
	How string
	// Reason says why the rows were dropped. When empty and How is howDropped,
	// retire records reasonNotRecorded.
	Reason string
}

// retirementOf reads the retirement record from d's metadata. Fields a row does not
// carry are empty.
func retirementOf(d DocIndex) Retire {
	return Retire{
		By:     d.Meta[metaSupersededBy],
		How:    d.Meta[metaSupersededHow],
		Reason: d.Meta[metaDropReason],
	}
}

// retire marks the live rows named by ids as superseded, as Supersede does, and
// records r on each of them. See Supersede for how a pass runs.
func (s *MemoryStore) retire(ids []string, r Retire) (int, error) {
	if r.How == "" {
		r.How = howReplaced
		if r.By == "" {
			r.How = howDropped
		}
	}
	extra := map[string]string{metaSupersededHow: r.How}
	if r.Reason != "" {
		extra[metaDropReason] = r.Reason
	}
	return s.supersedeWith(ids, r.By, extra)
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
		meta[metaInvalidAt] = now
		meta[metaSupersededBy] = by
		for k, v := range extra {
			meta[k] = v
		}
		// A drop always says why, so history never shows an unexplained gap.
		if by == "" && meta[metaDropReason] == "" {
			meta[metaDropReason] = reasonNotRecorded
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
		// A superseded fact is no longer searchable, so its clause rows go too.
		s.dropClauses(cur.ID)
		marked++
	}
	if marked > 0 {
		s.markDirtyLocked()
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
	for _, id := range s.mirrors[mirrorKey{layer, sourceID}] {
		if d, ok := s.index[id]; ok && d.Content == content && !isSuperseded(d) {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	return ids
}

// SetExtracted marks a doc as extracted (used after successful promotion). The flag
// is written to the chromem row too, so a rebuild of the index keeps it and the row
// is not extracted again. A superseded row's history is not rewritten, so for one the
// flag is set in the index only.
func (s *MemoryStore) SetExtracted(id string, v bool) error {
	flag := ""
	if v {
		flag = "true"
	}
	if err := s.updateMeta(id, map[string]string{"extracted": flag}); err == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if d, ok := s.index[id]; ok {
		d.Extracted = v
		s.putLocked(d)
		s.markDirtyLocked()
	}
	return nil
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

// vectors returns the stored embeddings of the given rows, keyed by id. Missing
// rows and rows whose vector cannot be read are left out.
func (s *MemoryStore) vectors(ids []string) map[string][]float32 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string][]float32, len(ids))
	for _, id := range ids {
		d, ok := s.index[id]
		if !ok {
			continue
		}
		col := s.cols[memory.Layer(d.Layer)]
		if col == nil {
			continue
		}
		if doc, err := col.GetByID(s.ctx, id); err == nil && len(doc.Embedding) > 0 {
			out[id] = doc.Embedding
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

// persistUsageAsync writes the counters without blocking the request path. At most
// one write is queued at a time: a call while one is queued returns at once, since
// the queued write reads the counters when it runs and so includes this call's count.
func (s *MemoryStore) persistUsageAsync() {
	if s.countsPath == "" || !s.usageQueued.CompareAndSwap(false, true) {
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
		// Cleared before the write reads the counters, so a call that lands during
		// the write queues another one and its count is not lost.
		s.usageQueued.Store(false)
		s.persistUsage()
	}()
}

// Close drains any pending async persistence before the store is discarded, so
// no goroutine writes into the data dir afterwards. Safe to call multiple times.
func (s *MemoryStore) Close() {
	s.closeOnce.Do(func() {
		// Stop the index flusher and write what it had not yet, before anything else.
		close(s.stopFlush)
		<-s.flushDone
		if err := s.Flush(); err != nil {
			log.Printf("index: flush at close: %v", err)
		}
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

// putLocked stores d in the exact index and keeps the superseded set, the replaced-by
// map and the per-layer counts in step. The caller holds s.mu for writing.
func (s *MemoryStore) putLocked(d DocIndex) {
	s.forgetIndexLocked(d.ID)
	s.index[d.ID] = d
	s.syncKeywordLocked(d)
	if isSuperseded(d) {
		l := memory.Layer(d.Layer)
		s.superseded[d.ID] = l
		s.hidden[l]++
		s.noteReplacedLocked(d)
	} else {
		s.trackLiveLocked(d)
	}
}

// mirrorKey is the (layer, source_id) pair MirrorsOf looks rows up by.
type mirrorKey struct {
	layer    memory.Layer
	sourceID string
}

// trackLiveLocked adds the live row d to the per-label counts and the mirror map. The
// caller holds s.mu for writing.
func (s *MemoryStore) trackLiveLocked(d DocIndex) {
	l := memory.Layer(d.Layer)
	if awaitsExtraction(d) {
		s.wantExtract++
	}
	if label := d.Meta[metaSourceLabel]; label != "" {
		if s.labelRows[l] == nil {
			s.labelRows[l] = map[string]int{}
		}
		s.labelRows[l][label]++
	}
	if src := d.Meta["source_id"]; src != "" {
		k := mirrorKey{l, src}
		s.mirrors[k] = append(s.mirrors[k], d.ID)
	}
}

// untrackLiveLocked undoes trackLiveLocked for the live row d. The caller holds s.mu
// for writing.
func (s *MemoryStore) untrackLiveLocked(d DocIndex) {
	l := memory.Layer(d.Layer)
	if awaitsExtraction(d) {
		s.wantExtract--
	}
	if label := d.Meta[metaSourceLabel]; label != "" {
		if s.labelRows[l][label]--; s.labelRows[l][label] <= 0 {
			delete(s.labelRows[l], label)
		}
	}
	if src := d.Meta["source_id"]; src != "" {
		k := mirrorKey{l, src}
		ids := slices.DeleteFunc(s.mirrors[k], func(x string) bool { return x == d.ID })
		if len(ids) == 0 {
			delete(s.mirrors, k)
		} else {
			s.mirrors[k] = ids
		}
	}
}

// noteReplacedLocked records d in replacedBy when it was replaced by another row.
// The caller holds s.mu for writing.
func (s *MemoryStore) noteReplacedLocked(d DocIndex) {
	if d.Meta[metaSupersededHow] != howReplaced {
		return
	}
	if by := retirementOf(d).By; by != "" {
		s.replacedBy[by] = append(s.replacedBy[by], d.ID)
	}
}

// forgetLocked removes id from the exact index and from the superseded
// bookkeeping. The caller holds s.mu for writing.
func (s *MemoryStore) forgetLocked(id string) {
	s.forgetIndexLocked(id)
	s.kw.remove(id)
}

// syncKeywordLocked makes the keyword index agree with d: a live row is indexed and a
// superseded row is not. A raw row is indexed only when keywordRaw is set, which is
// only so for a server that does not extract (lite mode finds an identifier in raw
// rows, and nothing else uses them there). The caller holds s.mu for writing.
func (s *MemoryStore) syncKeywordLocked(d DocIndex) {
	if isSuperseded(d) || (d.Layer == string(memory.L2Raw) && !s.keywordRaw) {
		s.kw.remove(d.ID)
		return
	}
	s.kw.put(d.ID, d.Content)
}

// SetKeywordRaw sets whether raw rows are kept in the keyword index and brings the
// index in line. It is called once at start, before the server answers, with true
// for a mode that does not use the LLM. Turning it off drops the raw rows again.
func (s *MemoryStore) SetKeywordRaw(on bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.keywordRaw == on {
		return
	}
	s.keywordRaw = on
	for _, d := range s.index {
		if d.Layer == string(memory.L2Raw) {
			s.syncKeywordLocked(d)
		}
	}
}

// awaitsExtraction reports whether d is a raw row that was written to be extracted
// (it carries extract_wanted) and has not been extracted yet. Rows written before
// 5.0 or in lite mode carry no marker and are never counted or caught up.
func awaitsExtraction(d DocIndex) bool {
	return d.Layer == string(memory.L2Raw) && !d.Extracted && d.Meta[metaExtractWanted] == "true"
}

// awaitingExtraction is how many live raw rows are waiting for extraction. It reads
// a counter kept by the index hooks, so it costs nothing however large the index is.
func (s *MemoryStore) awaitingExtraction() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.wantExtract
}

// forgetIndexLocked is forgetLocked without the keyword index, for putLocked, which
// then decides whether the row stays indexed and skips the work when its text did
// not change.
func (s *MemoryStore) forgetIndexLocked(id string) {
	if d, ok := s.index[id]; ok {
		if _, dead := s.superseded[id]; !dead {
			s.untrackLiveLocked(d)
		}
	}
	if l, ok := s.superseded[id]; ok {
		delete(s.superseded, id)
		s.hidden[l]--
		if s.hidden[l] <= 0 {
			delete(s.hidden, l)
		}
		if d, ok := s.index[id]; ok && d.Meta[metaSupersededHow] == howReplaced {
			by := retirementOf(d).By
			if ids := slices.DeleteFunc(s.replacedBy[by], func(x string) bool { return x == id }); len(ids) == 0 {
				delete(s.replacedBy, by)
			} else {
				s.replacedBy[by] = ids
			}
		}
	}
	delete(s.index, id)
}

// rebuildSupersededLocked recomputes the superseded set, the replaced-by map,
// per-layer counts and the keyword index from the whole index. It runs once after the index is loaded from
// disk.
func (s *MemoryStore) rebuildSupersededLocked() {
	s.superseded = map[string]memory.Layer{}
	s.hidden = map[memory.Layer]int{}
	s.replacedBy = map[string][]string{}
	s.labelRows = map[memory.Layer]map[string]int{}
	s.mirrors = map[mirrorKey][]string{}
	s.wantExtract = 0
	s.kw = newKwIndex()
	for id, d := range s.index {
		s.syncKeywordLocked(d)
		if isSuperseded(d) {
			l := memory.Layer(d.Layer)
			s.superseded[id] = l
			s.hidden[l]++
			s.noteReplacedLocked(d)
		} else {
			s.trackLiveLocked(d)
		}
	}
}

// indexFlushEvery is the most often the flusher writes the index file while rows are
// changing.
const indexFlushEvery = 1 * time.Second

// markDirtyLocked records that the index changed and needs writing. The caller holds
// s.mu; the flusher or Flush does the write.
func (s *MemoryStore) markDirtyLocked() { s.dirty.Store(true) }

// flushLoop writes the index at most once per indexFlushEvery while it is dirty, and
// exits when stopFlush closes. A write error is logged and leaves the index dirty, so
// the next tick retries.
func (s *MemoryStore) flushLoop() {
	defer close(s.flushDone)
	t := time.NewTicker(indexFlushEvery)
	defer t.Stop()
	for {
		select {
		case <-s.stopFlush:
			return
		case <-t.C:
			if err := s.Flush(); err != nil {
				log.Printf("index: flush: %v", err)
			}
		}
	}
}

// Flush writes the index file now when it has changed since the last write. The
// index is marshalled compactly under the read lock and written to a temp file and
// renamed outside it. Flushes are serialised. A failure leaves the index dirty.
// Callers must not hold s.mu.
func (s *MemoryStore) Flush() error {
	s.flushMu.Lock()
	defer s.flushMu.Unlock()
	if !s.dirty.Load() {
		return nil
	}
	if s.indexPath == "" {
		s.dirty.Store(false)
		return nil
	}
	s.mu.RLock()
	// Writers set dirty under the write lock, so clearing it here cannot lose a change.
	s.dirty.Store(false)
	data, err := json.Marshal(s.index)
	s.mu.RUnlock()
	if err == nil {
		err = os.MkdirAll(filepath.Dir(s.indexPath), 0o755)
	}
	tmp := s.indexPath + ".tmp"
	if err == nil {
		err = os.WriteFile(tmp, data, 0o644)
	}
	if err == nil {
		err = os.Rename(tmp, s.indexPath)
	}
	if err != nil {
		s.dirty.Store(true)
		return err
	}
	s.indexWrites.Add(1)
	return nil
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
		res, err := col.QueryEmbedding(s.ctx, make([]float32, embedDim), n, nil, nil)
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
	s.dirty.Store(true)
	return nil
}

// updateMeta sets the metadata keys of patch on the live row id, and deletes a key
// whose value is empty. The row's content and vector are kept: the chromem row is
// written again with the new metadata and the index record follows, under the same
// rowMu and index lock a supersede rewrite takes. It fails for a row that does not
// exist or is superseded, since a retired row's history is not edited this way. The
// row's clause rows are not touched, so a patch should not change its owner.
func (s *MemoryStore) updateMeta(id string, patch map[string]string) error {
	s.rowMu.Lock()
	defer s.rowMu.Unlock()
	s.mu.RLock()
	cur, ok := s.index[id]
	col := s.cols[memory.Layer(cur.Layer)]
	if !ok || col == nil {
		s.mu.RUnlock()
		return fmt.Errorf("memory %s not found", id)
	}
	if isSuperseded(cur) {
		s.mu.RUnlock()
		return fmt.Errorf("memory %s is superseded", id)
	}
	old, err := col.GetByID(s.ctx, id)
	s.mu.RUnlock()
	if err != nil {
		return err
	}
	meta := make(map[string]string, len(cur.Meta)+len(patch))
	maps.Copy(meta, cur.Meta)
	for k, v := range patch {
		if v == "" {
			delete(meta, k)
		} else {
			meta[k] = v
		}
	}
	meta["layer"] = cur.Layer
	if err := col.AddDocument(s.ctx, chromem.Document{
		ID: id, Content: old.Content, Embedding: old.Embedding, Metadata: meta,
	}); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now, ok := s.index[id]
	if !ok {
		// Deleted while the row was being rewritten, which may have re-created it.
		_ = col.Delete(s.ctx, nil, nil, id)
		return fmt.Errorf("memory %s was deleted", id)
	}
	now.Meta = meta
	// The index fields that mirror a metadata key follow it only when the patch names it.
	if _, set := patch["user_id"]; set {
		now.UserID = meta["user_id"]
	}
	if _, set := patch["agent_id"]; set {
		now.AgentID = meta["agent_id"]
	}
	if _, set := patch["ts"]; set {
		now.Ts = meta["ts"]
	}
	if _, set := patch["extracted"]; set {
		now.Extracted = meta["extracted"] == "true"
	}
	s.putLocked(now)
	s.markDirtyLocked()
	return nil
}
