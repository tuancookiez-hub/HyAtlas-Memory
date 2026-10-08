package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/tuancookiez-hub/hyatlas-v4/graph"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Version is the single source of truth for the server version string.
// It is exposed on /api/v1/status and /api/info so every client (Desktop pane,
// web dashboard, CLI) reports the real running version instead of hardcoding
// a "v4" badge that silently goes stale on each release. Bump in one place.
const Version = "4.4.0"

// Server mirrors the HyAtlas REST contract for drop-in parity.
type Server struct {
	store    *MemoryStore
	llm      *LLMClient
	llmModel string
	llmBase  string
	mode     Mode
	sync     Sync
	cons     *Consolidator
	start    time.Time
	dataDir  string
	// allowedHosts are the extra hostnames (HYATLAS_ALLOWED_HOSTS) a request may
	// name in Host or Origin, beyond localhost and IP literals (see guardLocal).
	allowedHosts []string
	// ownerAliases maps a user ID to every ID of the same person
	// (HYATLAS_USER_ALIASES), so search covers all of them.
	ownerAliases map[string][]string
	// minScore and dedupeScore are HYATLAS_MIN_SCORE and HYATLAS_DEDUPE_SCORE. The
	// zero value turns each off, which is what tests built without main get.
	minScore    float64
	dedupeScore float64

	// mu guards lastExtractErr: the extraction goroutines write it from
	// background contexts while /api/v1/status reads it on request.
	mu             sync.RWMutex
	lastExtractErr string
}

func (s *Server) setExtractErr(err string) {
	s.mu.Lock()
	s.lastExtractErr = err
	s.mu.Unlock()
}

func (s *Server) extractErr() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.lastExtractErr
}

// llmState is the single place that decides what to report about the LLM, so
// /api/v1/status, the dashboard and the startup line cannot disagree.
//
//	unused        — this mode makes no LLM call
//	unconfigured  — the mode needs one but endpoint, model or key is unset
//	ok            — a credential resolved
func (s *Server) llmState() string {
	if !s.mode.UsesLLM() {
		return "unused"
	}
	if !s.llm.Configured() {
		return "unconfigured"
	}
	return "ok"
}

// Status is the /api/v1/status payload. It is marshaled directly, so these
// json tags are the wire contract — do not restate the shape in a map literal.
type Status struct {
	Status        string         `json:"status"`
	Version       string         `json:"version"`
	VDB           string         `json:"vdb"`
	Embed         string         `json:"embed"`
	LLM           string         `json:"llm"`
	LLMModel      string         `json:"llm_model"`
	LLMBase       string         `json:"llm_base"`
	VDBProvider   string         `json:"vdb_provider"`
	VDBCollection string         `json:"vdb_collection"`
	VDBPoints     int            `json:"vdb_points"`
	EmbedDims     int            `json:"embed_dims"`
	WritePipeline string         `json:"write_pipeline"`
	Writes        uint64         `json:"writes"`
	Searches      uint64         `json:"searches"`
	Layers        map[string]int `json:"layers"`
	GraphNodes    int            `json:"graph_nodes"`
	GraphEdges    int            `json:"graph_edges"`
	Mode          Mode           `json:"mode"`
	ModeDetail    string         `json:"mode_detail"`
	UsesLLM       bool           `json:"uses_llm"`
	// Slow-path observability. ExtractSync tells the caller whether a write
	// blocks, so pro and ultra cannot be mistaken for one another on latency
	// alone. Consolidations is the number of completed passes; -1 means this
	// mode has no slow path at all, which is distinguishable from zero passes.
	ExtractSync      string  `json:"extract_sync"`
	Consolidations   int     `json:"consolidations"`
	LastConsolidated *Report `json:"last_consolidated,omitempty"`
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	write := "ok"
	if errStr2 := s.extractErr(); errStr2 != "" {
		write = "degraded: " + errStr2
	}
	writesCount, searchesCount := s.store.Usage()
	// -1 means "no slow path in this mode"; 0 means "has one, never completed".
	consRuns, lastCons := -1, (*Report)(nil)
	if s.cons != nil {
		n, last, _ := s.cons.Stats()
		consRuns, lastCons = n, last
	}
	// Lite never calls an LLM, so reporting llm=ok there would claim a
	// capability this mode deliberately does not use. A mode that does need one
	// but has no credential must not report ok either — that is the single most
	// confusing state a fresh install can land in.
	llmState := s.llmState()
	jsonResponse(w, 200, Status{
		Status:           "ok",
		Version:          Version,
		VDB:              "ok",
		Embed:            "ok",
		LLM:              llmState,
		LLMModel:         s.llmModel,
		LLMBase:          s.llmBase,
		VDBProvider:      "chromem",
		VDBCollection:    "layers",
		VDBPoints:        s.store.TotalMemories(),
		EmbedDims:        384,
		WritePipeline:    write,
		Writes:           writesCount,
		Searches:         searchesCount,
		Layers:           s.store.LayerCounts(),
		GraphNodes:       s.store.Graph().NodeCount(),
		GraphEdges:       s.store.Graph().EdgeCount(),
		Mode:             s.mode.OrDefault(),
		ModeDetail:       s.modeDetail(),
		UsesLLM:         s.mode.UsesLLM(),
		ExtractSync:      s.sync.Describe(s.mode),
		Consolidations:   consRuns,
		LastConsolidated: lastCons,
	})
}

// promoteExtraction writes one System1 pass to the layers that pass owns:
// L3 facts (and the L1 profile mirror for preferences), L4 summary, L7 intention.
//
// L5 knowledge and L6 schemas are deliberately NOT written here. They are
// System2 products: a knowledge relation worth keeping is one corroborated by
// more than a single turn, and a schema is a *recurring* pattern, which no
// single turn can evidence. Writing them per turn made pro and ultra look
// identical and filled L6 with guesses that competed with the real thing at
// retrieval time. The consolidation pass owns both layers.
//
// sourceID is the L2 raw memory id. It is recorded on every L3 fact as
// source_id so the slow path can trace a consolidated claim back to the
// conversations that produced it, and so raw decay can protect the rows a live
// claim still depends on.
//
// It never de-duplicates; the server path uses promoteExtractionDedupe.
func promoteExtraction(store *MemoryStore, ex *Extraction, userID, agentID, sourceID string) {
	promoteExtractionDedupe(store, ex, userID, agentID, sourceID, 0)
}

// promoteExtractionDedupe is promoteExtraction that also folds restatements. Before an
// L3 fact is written, the owner's nearest live L3 fact is looked up; if it scores at
// least dedupe, the new fact is written and the old one, with its L1 Profile mirrors,
// is superseded by it. Newest wins, so an updated value replaces the stale one rather
// than being dropped as a duplicate. dedupe <= 0 turns this off.
func promoteExtractionDedupe(store *MemoryStore, ex *Extraction, userID, agentID, sourceID string, dedupe float64) {
	now := time.Now().UTC().Format(time.RFC3339)
	// L3 Facts
	for _, f := range ex.Facts {
		if f.Data == "" {
			continue
		}
		// Found before the new rows are written, so the new L1 mirror is not
		// taken for one of the old fact's mirrors.
		var stale []string
		if dedupe > 0 {
			near, err := store.search(f.Data, 1, memory.L3Fact, userID, agentID)
			if err == nil && len(near) > 0 && float64(near[0].Score) >= dedupe {
				old := near[0]
				stale = append([]string{old.ID}, store.MirrorsOf(memory.L1Profile, old.Meta["source_id"], old.Content)...)
			}
		}
		factID := newID()
		_ = store.Add(memory.L3Fact, factID, f.Data, map[string]string{
			"user_id": userID, "agent_id": agentID,
			"source_layer_label": f.Layer, "source_id": sourceID, "ts": now,
		})
		// L1 Profile: user_preferences are stable identity — mirror to profile layer.
		if f.Layer == "user_preferences" {
			_ = store.Add(memory.L1Profile, newID(), f.Data, map[string]string{
				"user_id": userID, "agent_id": agentID,
				"source_id": sourceID, "ts": now,
			})
		}
		if len(stale) > 0 {
			if _, err := store.Supersede(stale, factID); err != nil {
				log.Printf("dedupe: supersede %s: %v", stale[0], err)
			}
		}
	}
	// L4 Summary (enabled layer — the narrative arc)
	if ex.Summary != nil && strings.TrimSpace(ex.Summary.Text) != "" {
		_ = store.Add(memory.L4Summary, newID(), ex.Summary.Text, map[string]string{
			"user_id": userID, "agent_id": agentID, "ts": now,
		})
	}
	// L7 Intention
	if ex.Intention != nil && strings.TrimSpace(ex.Intention.Goal) != "" {
		_ = store.Add(memory.L7Intention, newID(), ex.Intention.Goal, map[string]string{
			"user_id": userID, "agent_id": agentID, "ts": now,
		})
	}
}

func (s *Server) handleAdd(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Text    string            `json:"text"`
		Data    string            `json:"data"`
		UserID  string            `json:"user_id"`
		AgentID string            `json:"agent_id"`
		Session string            `json:"session_id"`
		Meta    map[string]string `json:"metadata"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		jsonResponse(w, 400, map[string]any{"error": "bad body", "success": false})
		return
	}
	text := body.Text
	if text == "" {
		text = body.Data
	}
	if text == "" {
		jsonResponse(w, 400, map[string]any{"error": "text required", "success": false})
		return
	}

	// Store raw immediately (the L2 trace). Extraction runs async to fill higher layers.
	id := newID()
	meta := body.Meta
	if meta == nil {
		meta = map[string]string{}
	}
	meta["user_id"] = body.UserID
	meta["agent_id"] = body.AgentID
	// The top-level session_id is what plugin clients send. Keep an explicit
	// metadata.session_id if the caller set one, so it is never clobbered.
	if body.Session != "" && meta["session_id"] == "" {
		meta["session_id"] = body.Session
	}
	meta["layer"] = string(memory.L2Raw)
	meta["ts"] = time.Now().UTC().Format(time.RFC3339)

	err := s.store.Add(memory.L2Raw, id, text, meta)
	resp := map[string]any{"success": err == nil, "memory_id": id}
	if err != nil {
		resp["error"] = err.Error()
		jsonResponse(w, 500, resp)
		return
	}

	// Whether extraction happens at all is the mode's decision; whether the
	// write waits for it is the sync knob's. Lite stops here with only the raw
	// trace stored, so no LLM call is made.
	resp["extraction_status"] = s.extractForMode(text, body.UserID, body.AgentID, id)

	jsonResponse(w, 200, resp)
}

// extractForMode applies the configured mode to one stored raw memory and
// reports what the caller should expect.
//
// One extraction call is System1: promoteExtraction writes L3 Fact, L4 Summary,
// L7 Intention, and L1 Profile when a fact is a user preference, all from the L2
// raw doc. L5 Knowledge and L6 Schema belong to System2 and are only ever written
// by the consolidation pass, so a mode's layer count depends on whether that pass
// runs at all — see Mode.LayersActive.
func (s *Server) extractForMode(text, userID, agentID, id string) string {
	if !s.mode.UsesLLM() {
		return "skipped"
	}
	if !s.llm.Configured() {
		return "unconfigured"
	}
	// Blocking versus background is the separate sync knob, not the mode: pro
	// defaults to blocking so the caller sees the outcome, ultra to background
	// so the write never waits. Either can be overridden with
	// HYATLAS_SYNC_EXTRACT.
	if !s.sync.Blocks(s.mode) {
		go s.extract(text, userID, agentID, id)
		return "pending"
	}
	// Blocking: the response tells the truth about this write instead of
	// reporting "pending" and leaving the caller to poll.
	if err := s.extract(text, userID, agentID, id); err != nil {
		return "failed"
	}
	return "done"
}

// extract runs one LLM extraction and promotes the result. Shared by the
// synchronous and background paths so the two cannot drift.
func (s *Server) extract(text, userID, agentID, id string) error {
	ctx, cancel := context.WithTimeout(context.Background(), extractTimeout)
	defer cancel()
	if s.llm == nil {
		return fmt.Errorf("no LLM client configured")
	}
	ex, err := s.llm.Complete(ctx, utf8Trunc(text, maxExtractInput))
	if err != nil {
		s.setExtractErr(err.Error())
		return err
	}
	promoteExtractionDedupe(s.store, ex, userID, agentID, id, s.dedupeScore)
	_ = s.store.SetExtracted(id, true)
	s.setExtractErr("")
	return nil
}

func (s *Server) handleSearch(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Query    string   `json:"query"`
		Limit    int      `json:"limit"`
		Layer    string   `json:"layer"`     // optional: filter to one memory layer
		UserIDs  []string `json:"user_ids"`  // optional: restrict to these users
		AgentIDs []string `json:"agent_ids"` // optional: restrict to these agents
		UserID   string   `json:"user_id"`   // what the Hermes plugin sends
		AgentID  string   `json:"agent_id"`  // what the Hermes plugin sends
		MinScore *float64 `json:"min_score"` // optional: overrides HYATLAS_MIN_SCORE
		Reader   string   `json:"reader"`    // optional: see parseReader; empty is hybrid
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		jsonResponse(w, 400, map[string]any{"error": "bad body"})
		return
	}
	if body.Query == "" {
		jsonResponse(w, 400, map[string]any{"error": "query required"})
		return
	}
	var users []string
	seenUser := map[string]bool{}
	for _, id := range append([]string{body.UserID}, body.UserIDs...) {
		if id == "" || seenUser[id] {
			continue
		}
		seenUser[id] = true
		users = append(users, id)
	}
	agentID := body.AgentID
	if agentID == "" && len(body.AgentIDs) > 0 {
		agentID = body.AgentIDs[0]
	}
	users = s.expandOwners(users)
	limit := body.Limit
	if limit <= 0 {
		limit = 5
	}
	minScore := s.minScore
	if body.MinScore != nil {
		minScore = *body.MinScore
	}
	// Fetch double so the floor and the duplicate drop still leave limit hits. The
	// vector search runs in every mode, because it counts the request as a search.
	res, err := s.store.SearchOwners(body.Query, limit*2, memory.Layer(body.Layer), users, agentID)
	if err != nil {
		jsonResponse(w, 500, map[string]any{"error": err.Error()})
		return
	}
	// Keyword search catches exact identifiers (HYATLAS_SYNC_EXTRACT, a port, a
	// ticker) that the embedding scores low; the two rankings are fused.
	switch reader := parseReader(body.Reader); reader {
	case readVector:
		res = refineHits(res, minScore, limit)
	default:
		kw, err := s.store.KeywordSearch(body.Query, limit*2, memory.Layer(body.Layer), users, agentID)
		if err != nil {
			jsonResponse(w, 500, map[string]any{"error": err.Error()})
			return
		}
		if reader == readKeyword {
			res = nil
		}
		res = fuseHits(res, kw, minScore, limit)
	}
	type hit struct {
		MemoryID   string  `json:"memory_id"`
		Content    string  `json:"content"`
		Score      float64 `json:"score"`
		Layer      string  `json:"layer"`
		GmtCreated int64   `json:"gmt_created"`
		UserID     string  `json:"user_id,omitempty"`
		AgentID    string  `json:"agent_id,omitempty"`
	}
	profileHits := []hit{}
	proactiveHits := []hit{}
	normalHits := []hit{}
	for _, h := range res {
		it := hit{MemoryID: h.ID, Content: h.Content, Score: float64(h.Score),
			Layer: string(h.Layer), GmtCreated: gmtCreated(h.Meta["ts"]),
			UserID: h.Meta["user_id"], AgentID: h.Meta["agent_id"]}
		switch h.Layer {
		case memory.L1Profile, memory.L6Schema:
			profileHits = append(profileHits, it)
		case memory.L7Intention:
			proactiveHits = append(proactiveHits, it)
		default:
			normalHits = append(normalHits, it)
		}
	}
	// plugin channel order: profile -> proactive -> normal
	jsonResponse(w, 200, map[string]any{"memories": map[string]any{
		"profile": profileHits, "proactive": proactiveHits, "normal": normalHits,
	}})
}

// refineHits drops hits scoring below minScore, then drops hits whose text repeats an
// earlier hit's, and keeps the best limit. Text is compared case-insensitively with
// whitespace collapsed. An L3 preference and its L1 Profile mirror carry the same
// text, so of two equal texts the L1 Profile one is kept: it is the one the profile
// channel shows. hits must be sorted best first, and the result is too.
func refineHits(hits []SearchHit, minScore float64, limit int) []SearchHit {
	kept := []SearchHit{}
	pos := map[string]int{}
	for _, h := range hits {
		if float64(h.Score) < minScore {
			continue
		}
		key := strings.ToLower(strings.Join(strings.Fields(h.Content), " "))
		if i, dup := pos[key]; dup {
			if h.Layer == memory.L1Profile && kept[i].Layer != memory.L1Profile {
				kept[i] = h
			}
			continue
		}
		pos[key] = len(kept)
		kept = append(kept, h)
	}
	if limit > 0 && len(kept) > limit {
		kept = kept[:limit]
	}
	return kept
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	// Accept both GET query params and the v3.5 client's POST JSON body.
	q := r.URL.Query()
	layer := q.Get("layer")
	userID := q.Get("user_id")
	agentID := q.Get("agent_id")
	limit := atoi(q.Get("limit"), 20)
	offset := atoi(q.Get("offset"), 0)
	includeRaw := q.Get("include_raw")
	// Superseded rows are history. They stay hidden unless the caller asks.
	includeSuperseded := q.Get("include_superseded") == "true"
	if r.Method == http.MethodPost {
		var body struct {
			Limit             int    `json:"limit"`
			Offset            int    `json:"offset"`
			Layer             string `json:"layer"`
			UserID            string `json:"user_id"`
			AgentID           string `json:"agent_id"`
			IncludeRaw        *bool  `json:"include_raw"`
			IncludeSuperseded bool   `json:"include_superseded"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err == nil {
			if body.Limit > 0 {
				limit = body.Limit
			}
			if body.Offset > 0 {
				offset = body.Offset
			}
			layer = body.Layer
			userID = body.UserID
			agentID = body.AgentID
			if body.IncludeRaw != nil {
				includeRaw = map[bool]string{true: "true", false: "false"}[*body.IncludeRaw]
			}
			includeSuperseded = includeSuperseded || body.IncludeSuperseded
		}
	}
	list := s.store.List
	if includeSuperseded {
		list = s.store.ListAll
	}
	items, total := list(memory.Layer(layer), userID, agentID, limit, offset, includeRaw == "false" && layer == "")

	out := make([]map[string]any, 0, len(items))
	for _, it := range items {
		m := map[string]any{
			"memory_id": it.ID, "content": it.Content, "layer": it.Layer,
			"user_id": it.UserID, "agent_id": it.AgentID, "ts": it.Ts,
			"gmt_created": gmtCreated(it.Ts), "extracted": it.Extracted,
		}
		if it.Meta != nil {
			m["session_id"] = it.Meta["session_id"]
		}
		// A superseded row says what replaced it, so history can be followed.
		if isSuperseded(it) {
			m["invalid_at"] = it.Meta["invalid_at"]
			m["superseded_by"] = it.Meta["superseded_by"]
		}
		out = append(out, m)
	}
	counts := s.store.LayerCounts()
	jsonResponse(w, 200, map[string]any{
		"total": total, "offset": offset, "limit": limit,
		"memories": out, "layers": counts,
		"graph_nodes": s.store.Graph().NodeCount(),
		"graph_edges": s.store.Graph().EdgeCount(),
	})
}

func atoi(s string, def int) int {
	if s == "" {
		return def
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return n
}

func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request) {
	// A delete is destructive, so only the verbs that name one are accepted. A
	// GET from a link prefetcher or a crawler must not reach the wipe path.
	if r.Method != http.MethodPost && r.Method != http.MethodDelete {
		w.Header().Set("Allow", "POST, DELETE")
		jsonResponse(w, 405, map[string]any{"deleted_count": 0, "error": "method not allowed: use POST or DELETE"})
		return
	}
	// Scoping may arrive as query params (curl style) OR as a JSON body
	// (the hyatlas plugin's client style). Read both, query wins.
	var body struct {
		ID      string `json:"id"`
		Layer   string `json:"layer"`
		UserID  string `json:"user_id"`
		AgentID string `json:"agent_id"`
		All     bool   `json:"all"`
		Confirm string `json:"confirm"`
	}
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&body)
	}
	q := r.URL.Query()
	first := func(a, b string) string {
		if a != "" {
			return a
		}
		return b
	}
	layer := first(q.Get("layer"), body.Layer)
	userID := first(q.Get("user_id"), body.UserID)
	agentID := first(q.Get("agent_id"), body.AgentID)
	// all=true is the explicit wipe. confirm=wipe-all is the older spelling of
	// the same opt-in, kept so existing callers keep working.
	all := q.Get("all") == "true" || body.All || first(q.Get("confirm"), body.Confirm) == "wipe-all"
	ids := []string{}
	if idStr := first(q.Get("id"), body.ID); idStr != "" {
		ids = append(ids, idStr)
	}
	// layer "*" means "everything" — same as an unscoped wipe.
	if layer == "*" {
		layer = ""
	}
	// Guard: a call with no filter is a full-store wipe. Require at least one
	// scope, or an explicit all=true.
	if len(ids) == 0 && layer == "" && userID == "" && agentID == "" && !all {
		jsonResponse(w, 400, map[string]any{
			"deleted_count": 0,
			"error":         "unscoped delete refused: pass layer/user_id/agent_id/id, or all=true to wipe the entire store",
		})
		return
	}
	deleted, err := s.store.Delete(ids, memory.Layer(layer), userID, agentID)
	jsonResponse(w, 200, map[string]any{"deleted_count": deleted, "error": errStr(err)})
}

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	counts := s.store.LayerCounts()
	jsonResponse(w, 200, map[string]any{
		"layers":      counts,
		"total":       s.store.TotalMemories(),
		"graph_nodes": s.store.Graph().NodeCount(),
		"graph_edges": s.store.Graph().EdgeCount(),
	})
}

// handleDigest reports the slow path's state, and runs a pass on demand.
//
// It used to return a hardcoded digest_ok with a note saying "a full scheduled
// digest runs here", which meant a caller could not tell whether consolidation
// had ever run. Now GET reports the last pass and POST triggers one, so the
// slow path is observable and testable rather than assumed.
func (s *Server) handleDigest(w http.ResponseWriter, r *http.Request) {
	if s.cons == nil {
		jsonResponse(w, 200, map[string]any{
			"digest_ok": false,
			"mode":      string(s.mode.OrDefault()),
			"reason":    "this mode has no slow path; consolidation runs only in ultra",
		})
		return
	}
	if r.Method != http.MethodPost {
		runs, last, at := s.cons.Stats()
		jsonResponse(w, 200, map[string]any{
			"digest_ok":   true,
			"runs":        runs,
			"last":        last,
			"last_at":     at,
			"graph_nodes": s.store.Graph().NodeCount(),
			"graph_edges": s.store.Graph().EdgeCount(),
		})
		return
	}
	// The pass is deliberately detached from the request. It rewrites facts and
	// graph edges, so a client that stops waiting — a cron with a shorter
	// timeout, a dropped connection — must not cancel work already in flight.
	// The server's own bound still caps it, and a caller that wants the report
	// back should wait longer than consolidateTimeout.
	ctx, cancel := context.WithTimeout(context.Background(), consolidateTimeout)
	defer cancel()
	rep, err := s.cons.Once(ctx)
	if errors.Is(err, errBusy) {
		jsonResponse(w, 200, map[string]any{
			"digest_ok": false,
			"reason":    "a consolidation pass is already running",
		})
		return
	}
	if err != nil {
		jsonResponse(w, 500, map[string]any{"digest_ok": false, "error": err.Error()})
		return
	}
	jsonResponse(w, 200, map[string]any{
		"digest_ok":   true,
		"report":      rep,
		"graph_nodes": s.store.Graph().NodeCount(),
		"graph_edges": s.store.Graph().EdgeCount(),
	})
}

func (s *Server) handleReprocess(w http.ResponseWriter, r *http.Request) {
	// Optional body: {"ids": [...], "max": N}. With explicit ids the caller has
	// already chosen the exact rows (e.g. backfilling an outage window), so the
	// extracted-skip does not apply. Otherwise up to `max` (default 200) raw rows
	// that were never extracted are processed, oldest first (see unextractedRaw).
	var body struct {
		IDs []string `json:"ids"`
		Max int      `json:"max"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)

	var raw []DocIndex
	if len(body.IDs) > 0 {
		raw = s.store.GetMany(body.IDs)
	} else {
		max := body.Max
		if max <= 0 {
			max = 200
		}
		raw = s.unextractedRaw(max)
	}
	// Lite has no extraction to reprocess, so say so instead of silently
	// reporting zero work done.
	if !s.mode.UsesLLM() {
		jsonResponse(w, 200, map[string]any{
			"reprocessed": 0, "failed": 0, "skipped": len(raw),
			"note": "mode is " + string(s.mode) + "; extraction is disabled, nothing to reprocess",
		})
		return
	}
	reprocessed, failed, skipped := 0, 0, 0
	for _, it := range raw {
		if s.llm == nil {
			failed++
			continue
		}
		// Same extraction path as a normal write, so pro and ultra behave
		// consistently here rather than this handler keeping its own copy.
		if err := s.extract(it.Content, it.UserID, it.AgentID, it.ID); err != nil {
			failed++
			continue
		}
		reprocessed++
	}
	jsonResponse(w, 200, map[string]any{"reprocessed": reprocessed, "failed": failed, "skipped": skipped})
}

// unextractedRaw returns up to max raw rows that extraction has not reached, oldest
// first (ts, then id, so equal timestamps order the same way each time). Extracted
// rows are removed before the cut: a page of already-extracted rows must not hide
// older unextracted ones, and the newest rows must not be re-picked while an old
// backlog starves.
func (s *Server) unextractedRaw(max int) []DocIndex {
	_, total := s.store.List(memory.L2Raw, "", "", 1, 0, false)
	all, _ := s.store.List(memory.L2Raw, "", "", total, 0, false)
	out := make([]DocIndex, 0, len(all))
	for _, d := range all {
		if !d.Extracted {
			out = append(out, d)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Ts != out[j].Ts {
			return out[i].Ts < out[j].Ts
		}
		return out[i].ID < out[j].ID
	})
	if len(out) > max {
		out = out[:max]
	}
	return out
}

func errStr(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// handleStarmapGraph returns a Hermes-Desktop-style StarmapGraph payload
// (nodes, edges, memory) so the hyatlas plugin can render the exact same
// view the built-in starmap shows. Designed for desktop pane "Graph" tab.
//
// The shape mirrors apps/desktop/src/types/hermes.ts::StarmapGraph:
//
//	{ nodes: StarmapNode[], edges: StarmapEdge[], memory: StarmapMemoryCard[] }
//
// utf8Trunc caps a string to max bytes without splitting a rune, appending an
// ellipsis when truncated. Used for starmap payload fields — some raw L2
// memories carry huge session dumps, and shipping full bodies made the graph
// payload hundreds of MB.
func utf8Trunc(s string, max int) string {
	if len(s) <= max {
		return s
	}
	cut := max
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + "…"
}

func (s *Server) handleStarmapGraph(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit := atoi(q.Get("n"), 500)
	kSem := atoi(q.Get("k_semantic"), 2)
	uid, aid := graphOwner(q.Get("user_id"), q.Get("agent_id"))

	// 1. Nodes: all L3 facts + a sampled set of L2 raw entries. Layer type
	//    becomes the visual "kind" (memory in the starmap sense).
	items, _ := s.store.List("", uid, aid, limit, 0, false)
	nodes := make([]map[string]any, 0, len(items))
	memCards := make([]map[string]any, 0, len(items))
	for _, it := range items {
		ts := gmtCreated(it.Ts)
		// strip long content for the node label; card body capped below — the
		// hover tooltip only ever previews a snippet
		label := utf8Trunc(it.Content, 80)
		nodes = append(nodes, map[string]any{
			"id":         it.ID,
			"label":      label,
			"kind":       "memory",
			"timestamp":  ts,
			"category":   it.Layer,
			"use_count":  0,
			"state":      "active",
			"created_by": "agent",
			"pinned":     false,
		})
		memCards = append(memCards, map[string]any{
			"source":    "memory",
			"timestamp": ts,
			"title":     label,
			"body":      utf8Trunc(it.Content, 2048),
		})
	}

	// 2. Edges: knowledge (L5) + co_session + semantic. All unified into
	//    { source, target } pairs like the starmap expects.
	edges := []map[string]any{}

	// knowledge
	graphNodes, graphRels := s.store.Graph().SnapshotScoped(graphScope(uid, aid), limit)
	_ = graphNodes
	for _, e := range graphRels {
		edges = append(edges, map[string]any{
			"source":   e.From,
			"target":   e.To,
			"type":     "knowledge",
			"relation": e.Relation,
		})
	}

	// co_session
	sessionBuckets := map[string][]string{}
	for _, l := range []string{"l2_raw", "l3_fact", "l4_summary", "l5_knowledge", "l6_schema", "l7_intention"} {
		lItems, _ := s.store.List(memory.Layer(l), uid, aid, 200, 0, false)
		for _, it := range lItems {
			sid := ""
			if it.Meta != nil {
				sid = it.Meta["session_id"]
			}
			if sid == "" {
				continue
			}
			sessionBuckets[sid] = append(sessionBuckets[sid], it.ID)
		}
	}
	coCount := 0
	for _, members := range sessionBuckets {
		if coCount >= 300 {
			break
		}
		if len(members) < 2 || len(members) > 20 {
			continue
		}
		for i := 1; i < 6 && i < len(members); i++ {
			edges = append(edges, map[string]any{
				"source": members[0], "target": members[i],
				"type": "co_session",
			})
			coCount++
		}
	}

	// semantic
	semCount := 0
	maxSem := 200
	if kSem < 1 {
		kSem = 2
	}
	recent, _ := s.store.List(memory.L3Fact, uid, aid, 20, 0, false)
	for _, it := range recent {
		if semCount >= maxSem {
			break
		}
		hits, err := s.store.Search(it.Content, kSem+1, "", uid, aid)
		if err != nil {
			continue
		}
		added := 0
		for _, h := range hits {
			if h.ID == it.ID {
				continue
			}
			edges = append(edges, map[string]any{
				"source": it.ID, "target": h.ID,
				"score": float64(h.Score),
				"type":  "semantic",
			})
			added++
			semCount++
			if added >= kSem {
				break
			}
		}
	}

	jsonResponse(w, 200, map[string]any{
		"nodes":  nodes,
		"edges":  edges,
		"memory": memCards,
		"stats": map[string]any{
			"node_count":   len(nodes),
			"edge_count":   len(edges),
			"memory_count": len(memCards),
		},
	})
}

// handleGraphEdges returns three edge types for the Mind Palace canvas:
//   - knowledge:  L5 explicit graph edges (existing)
//   - co_session: memories sharing a session_id (inferred)
//   - semantic:   top-K nearest neighbors per memory (VDB cosine)
//
// Caps output to keep payload bounded; takes ~50ms even with 2,500 memories.
func (s *Server) handleGraphEdges(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	semanticK := atoi(q.Get("k_semantic"), 3)
	limitAll := atoi(q.Get("n"), 500)
	uid, aid := graphOwner(q.Get("user_id"), q.Get("agent_id"))

	// 1. Knowledge: L5 explicit edges, for the owner named by the filter (all owners when none)
	nodes, rels := s.store.Graph().SnapshotScoped(graphScope(uid, aid), limitAll)
	nodesArr := make([]map[string]any, 0, len(nodes))
	for _, n := range nodes {
		nodesArr = append(nodesArr, map[string]any{
			"id": n.ID, "label": n.Label, "type": n.Type,
			"user_id": n.UserID, "agent_id": n.AgentID,
		})
	}
	knowledge := make([]map[string]any, 0, len(rels))
	for _, e := range rels {
		knowledge = append(knowledge, map[string]any{
			"from": e.From, "to": e.To,
			"relation": e.Relation, "weight": e.Weight,
			"source":      e.Source,
			"sources":     e.Sources,
			"user_id":     e.UserID,
			"agent_id":    e.AgentID,
			"recorded_at": e.RecordedAt,
			"valid_from":  e.ValidFrom,
			"valid_to":    e.ValidTo,
			"type":        "knowledge",
		})
	}

	// 2. Co-session: group memories by session_id, emit all-pairs edges
	coSession := []map[string]any{}
	sessionBuckets := map[string][]string{}
	for layer := range s.store.LayerCounts() {
		_ = layer
	}
	for _, l := range []string{"l2_raw", "l3_fact", "l4_summary", "l5_knowledge", "l6_schema", "l7_intention"} {
		items, _ := s.store.List(memory.Layer(l), uid, aid, 200, 0, false)
		for _, it := range items {
			sid := it.Meta["session_id"]
			if sid == "" {
				continue
			}
			sessionBuckets[sid] = append(sessionBuckets[sid], it.ID)
		}
	}
	coSessionCount := 0
	for sid, members := range sessionBuckets {
		if coSessionCount >= 300 {
			break
		}
		if len(members) < 2 || len(members) > 20 {
			continue
		}
		// emit at most 5 edges per session (limit combinatorial blowup)
		maxPer := 5
		if len(members)-1 < maxPer {
			maxPer = len(members) - 1
		}
		for i := 0; i < maxPer && i+1 < len(members); i++ {
			coSession = append(coSession, map[string]any{
				"from": members[0], "to": members[i+1],
				"session": sid, "type": "co_session",
			})
			coSessionCount++
		}
	}

	// 3. Semantic: top-K nearest neighbors per recent memory (cap total)
	semantic := []map[string]any{}
	semCount := 0
	maxSem := 200
	if semanticK < 1 {
		semanticK = 2
	}
	// iterate only the most recent 20 L3 memories (kept fast; full-graph
	// similarity is a separate scan). Coalesces well to ~20 VDB queries.
	recent, _ := s.store.List(memory.L3Fact, uid, aid, 20, 0, false)
	for _, it := range recent {
		if semCount >= maxSem {
			break
		}
		hits, err := s.store.Search(it.Content, semanticK+1, "", uid, aid)
		if err != nil {
			continue
		}
		added := 0
		for _, h := range hits {
			if h.ID == it.ID {
				continue
			}
			semantic = append(semantic, map[string]any{
				"from": it.ID, "to": h.ID,
				"score": float64(h.Score),
				"type":  "semantic",
			})
			added++
			semCount++
			if added >= semanticK {
				break
			}
		}
	}

	jsonResponse(w, 200, map[string]any{
		"nodes":       nodesArr,
		"knowledge":   knowledge,
		"co_session":  coSession,
		"semantic":    semantic,
		"total_edges": len(knowledge) + len(coSession) + len(semantic),
	})
}

func (s *Server) handleGraph(w http.ResponseWriter, r *http.Request) {
	// The node and owner filter may come in the JSON body or the query string; the
	// body wins.
	var body struct {
		Node    string `json:"node"`
		UserID  string `json:"user_id"`
		AgentID string `json:"agent_id"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	q := r.URL.Query()
	node := firstNonEmpty(body.Node, q.Get("node"))
	uid, aid := graphOwner(firstNonEmpty(body.UserID, q.Get("user_id")), firstNonEmpty(body.AgentID, q.Get("agent_id")))
	scope := graphScope(uid, aid)
	neighbors := s.store.Graph().NeighborsScoped(scope, node)
	if neighbors == nil {
		neighbors = []graph.Neighbor{}
	}
	nodeCount, edgeCount := s.store.Graph().CountsScoped(scope)
	jsonResponse(w, 200, map[string]any{
		"node":        node,
		"neighbors":   neighbors,
		"node_count":  nodeCount,
		"edge_count":  edgeCount,
		"extract_err": s.extractErr(),
	})
}

// firstNonEmpty returns the first of its arguments that is not empty.
func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

func (s *Server) handleGraphAsOf(w http.ResponseWriter, r *http.Request) {
	ts := atoi(r.URL.Query().Get("ts"), int(time.Now().Unix()))
	n := atoi(r.URL.Query().Get("n"), 500)
	uid, aid := graphOwner(r.URL.Query().Get("user_id"), r.URL.Query().Get("agent_id"))
	nodes, rels := s.store.Graph().SnapshotAsOfScoped(graphScope(uid, aid), int64(ts), n)
	jsonResponse(w, 200, map[string]any{
		"nodes":     nodes,
		"relations": rels,
		"as_of":     ts,
		"total":     len(nodes),
	})
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	jsonResponse(w, 200, map[string]string{"status": "ok"})
}

func jsonResponse(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// runtimeCfg is the resolved server configuration. Every default lives here, in
// one place, rather than as an inline literal inside main() — so the defaults
// are something a test can assert on instead of something only visible by
// running the binary.
type runtimeCfg struct {
	Host       string
	Port       string
	DataDir    string
	GraphPath  string
	LLMBase    string
	LLMModel   string
	EmbedBase  string
	EmbedModel string
	ModelDir   string
	// ModelTried lists the directories searched for the BGE model, in order, so
	// the fatal message can name every place that was checked.
	ModelTried []string
	// AllowedHosts is HYATLAS_ALLOWED_HOSTS: extra hostnames a request may name
	// in Host or Origin (see guardLocal). Empty means loopback and IP literals only.
	AllowedHosts []string
	// UserAliases is HYATLAS_USER_ALIASES: groups of user IDs that belong to one
	// person, so a search for any of them covers all of them. Empty means none.
	UserAliases [][]string
	// MinScore is HYATLAS_MIN_SCORE: hits below it are dropped from /api/v1/search.
	MinScore float64
	// DedupeScore is HYATLAS_DEDUPE_SCORE: a new fact this similar to the owner's
	// nearest existing fact supersedes it instead of sitting beside it.
	DedupeScore float64
	Mode        Mode
	Sync        Sync
	// Slow-path (ultra) tuning. Zero retention means raw history is never decayed.
	Consolidate time.Duration
	Retention   time.Duration
	Batch       int
	// Graph is HYATLAS_CONSOLIDATE_GRAPH: whether consolidation also writes L5
	// knowledge edges and the cross-session arc. Off unless set to on/true/1/yes.
	Graph bool
}

// Defaults that decide what leaves the machine:
//
//   - EmbedBase is "bge", the in-process local embedder, so embeddings need no
//     network by default. It used to be one developer's machine-local proxy,
//     which exists on nobody else's.
//   - LLMBase and LLMModel are deliberately empty. Extraction is the one thing
//     that sends memory text off-machine, so the endpoint is the user's to
//     choose rather than ours to assume. An unconfigured server stores the raw
//     trace and reports "unconfigured" instead of quietly picking a provider.
//     The installer and `hermes memory setup` suggest a free Nous Portal
//     endpoint the user can accept or overwrite.
//
// suggestLLMBase / suggestLLMModel are what the installer and `hermes memory
// setup` offer as a starting point, and what the startup warning prints as an
// example. They are never read as a default: resolveRuntime leaves the endpoint
// empty until the user chooses one, so nothing is sent anywhere by default.
const (
	suggestLLMBase  = "https://inference-api.nousresearch.com/v1"
	suggestLLMModel = "poolside/laguna-s-2.1:free"
)

const (
	defaultPort       = "19528"
	defaultHost       = "127.0.0.1"
	defaultDataDir    = "./data"
	defaultLLMBase    = ""
	defaultLLMModel   = ""
	defaultEmbedBase  = "bge"
	defaultEmbedModel = "text-embedding-3-small"
)

func resolveRuntime() runtimeCfg {
	dataDir := envOr("HYATLAS_GO_DATA", defaultDataDir)
	mode := resolveMode()
	modelDir, modelTried := resolveModelDir(envOr("HYATLAS_MODEL_DIR", ""))
	return runtimeCfg{
		Mode:         mode,
		Sync:         resolveSync(),
		Consolidate:  resolveConsolidate(mode),
		Retention:    parseDuration("HYATLAS_RAW_RETENTION", 0),
		Batch:        envInt("HYATLAS_CONSOLIDATE_BATCH", defaultBatch),
		Graph:        envOn("HYATLAS_CONSOLIDATE_GRAPH"),
		Host:         strings.Trim(envOr("HYATLAS_GO_HOST", defaultHost), "[]"),
		Port:         envOr("HYATLAS_GO_PORT", defaultPort),
		DataDir:      dataDir,
		GraphPath:    envOr("HYATLAS_GRAPH_PATH", filepath.Join(dataDir, "graph.json")),
		LLMBase:      envOr("HYATLAS_LLM_BASE", defaultLLMBase),
		LLMModel:     envOr("HYATLAS_LLM_MODEL", defaultLLMModel),
		EmbedBase:    envOr("HYATLAS_EMBED_BASE", defaultEmbedBase),
		EmbedModel:   envOr("HYATLAS_EMBED_MODEL", defaultEmbedModel),
		ModelDir:     modelDir,
		ModelTried:   modelTried,
		AllowedHosts: parseHostList(envOr("HYATLAS_ALLOWED_HOSTS", "")),
		UserAliases:  parseUserAliases(envOr("HYATLAS_USER_ALIASES", "")),
		MinScore:     envFloat("HYATLAS_MIN_SCORE", defaultMinScore),
		DedupeScore:  envFloat("HYATLAS_DEDUPE_SCORE", defaultDedupeScore),
	}
}

// resolveMode reads HYATLAS_MODE. An invalid value is fatal rather than a silent
// fallback to ultra: someone who typos "lite" and quietly gets ultra would have
// their conversation text sent to an extraction LLM they believed they had
// turned off, which is the exact privacy boundary this selector exists to give.
func resolveMode() Mode {
	m, err := ParseMode(os.Getenv("HYATLAS_MODE"))
	if err != nil {
		log.Fatal(err)
	}
	return m
}

// resolveConsolidate reads HYATLAS_CONSOLIDATE_EVERY. Fatal when ultra is given a
// zero or negative interval: that silently turns ultra into pro, because the
// slow path never ticks. Pro and lite have no slow path, so there it is ignored.
func resolveConsolidate(m Mode) time.Duration {
	d := parseDuration("HYATLAS_CONSOLIDATE_EVERY", defaultConsolidate)
	if err := checkConsolidateEvery(m, d); err != nil {
		log.Fatal(err)
	}
	return d
}

// checkConsolidateEvery is the pure rule behind resolveConsolidate, split out so
// a test can assert it without the process exiting.
func checkConsolidateEvery(m Mode, d time.Duration) error {
	if m.Consolidates() && d <= 0 {
		return fmt.Errorf("HYATLAS_CONSOLIDATE_EVERY must be a positive duration in ultra mode (got %s): "+
			"a zero interval disables the slow path, so ultra would silently behave like pro; "+
			"unset it for the 6h default, or set HYATLAS_MODE=pro", d)
	}
	return nil
}

// resolveSync reads HYATLAS_SYNC_EXTRACT. Fatal on an invalid value, for the
// same reason as resolveMode: a silently-ignored knob reads as working while
// doing nothing.
func resolveSync() Sync {
	s, err := ParseSync(os.Getenv(syncKey))
	if err != nil {
		log.Fatal(err)
	}
	return s
}

// attachSlowPath wires the consolidation worker onto a server.
//
// Lifted out of main so the wiring is testable: the slow path is the entire
// difference between ultra and pro, and a construction site that forgets it
// would leave ultra silently identical to pro. Returns whether it attached.
func (s *Server) attachSlowPath(ctx context.Context, rt runtimeCfg) bool {
	if !s.mode.Consolidates() {
		return false
	}
	s.cons = NewConsolidator(s.store, s.llm, rt.Consolidate, rt.Retention, rt.Batch)
	s.cons.graph = rt.Graph
	go s.cons.Run(ctx)
	return true
}

// modeDetail is the mode's description, noting when the slow path runs without the
// L5 graph and arc (HYATLAS_CONSOLIDATE_GRAPH off), which Describe cannot know.
func (s *Server) modeDetail() string {
	d := s.mode.Describe()
	if s.cons != nil && !s.cons.graph {
		d += "; L5 graph and arc writing off (HYATLAS_CONSOLIDATE_GRAPH)"
	}
	return d
}

// envOn reports whether key is set to on, true, 1 or yes (any case).
func envOn(key string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "on", "true", "1", "yes":
		return true
	}
	return false
}

// envFloat reads key as a float64. Unset, blank or unparsable means def.
func envFloat(key string, def float64) float64 {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		log.Printf("%s=%q is not a number; using %v", key, v, def)
		return def
	}
	return f
}

// envInt reads a positive integer or falls back.
func envInt(key string, def int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		log.Printf("%s=%q is not a positive integer; using %d", key, v, def)
		return def
	}
	return n
}

func main() {
	rt := resolveRuntime()
	port := rt.Port
	dir := rt.DataDir
	// LLM: any OpenAI-compatible endpoint. No default: extraction stays off until
	// HYATLAS_LLM_BASE, HYATLAS_LLM_MODEL and HYATLAS_LLM_KEY are all set.
	llmBase := rt.LLMBase
	llmKey := os.Getenv("HYATLAS_LLM_KEY")
	// Optional: read the key live from a file each call, for rotating
	// credentials (e.g. Hermes keeps a fresh 1-hour JWT in auth.json).
	// When set, this wins over the frozen HYATLAS_LLM_KEY value.
	llmKeyFile := os.Getenv("HYATLAS_LLM_KEY_FILE")
	llmModel := rt.LLMModel
	embedBase := rt.EmbedBase
	embedKey := os.Getenv("HYATLAS_EMBED_KEY")
	embedModel := rt.EmbedModel

	ctx := context.Background()
	var embedder Embedder
	switch {
	case strings.EqualFold(embedBase, "bge"):
		// In-Go BGE inference (no Python, no HTTP) — the pure-Go path.
		//
		// Resolved to an absolute path before use. A relative "./models" is not
		// portable on Windows: the onnxruntime loader and the directory check
		// disagree about what it is relative to, so the same path can find the
		// model and then fail on the shared library. resolveModelDir returns
		// absolute paths, searched in order (see findModelDir).
		modelDir := rt.ModelDir
		if useEmbeddedAssets {
			modelDir = materializeAssets()
		}
		b, err := NewBGEGoEmbedder(modelDir)
		if err != nil {
			// Failing fast is right here: the alternative is a server that
			// answers every request and silently mis-embeds or 500s on write.
			// Say how to fix it instead of leaving a bare error, because
			// "bge" is now the default and a plain build with no models/
			// directory lands here on first run.
			log.Fatalf("bge embedder: %v\n\n"+
				"The in-process embedder needs the BGE model next to the binary.\n"+
				"Searched for bge-small-en-v1.5.onnx in:\n    %s\n"+
				"Fix one of:\n"+
				"  1. install via scripts/install.sh (fetches the model for you)\n"+
				"  2. download a release binary built with -tags embedded, which\n"+
				"     carries the model inside it\n"+
				"  3. put bge-small-en-v1.5.onnx and onnxruntime.<ext> in one of\n"+
				"     the directories above, or point HYATLAS_MODEL_DIR at a directory\n"+
				"     that has them (then only that directory is searched)\n"+
				"Or set HYATLAS_EMBED_BASE to an OpenAI-compatible embeddings URL\n"+
				"(memory text would then leave the machine) or to \"local\" for the\n"+
				"offline deterministic stub.\n", err, strings.Join(rt.ModelTried, "\n    "))
		}
		embedder = b
	case strings.EqualFold(embedBase, "local"):
		embedder = NewLocalEmbedder(384)
	default:
		embedder = NewOpenAIEmbedder(embedBase, embedKey, embedModel)
	}
	graphPath := rt.GraphPath
	store, err := NewMemoryStore(ctx, dir, embedder, graphPath)
	if err != nil {
		log.Fatal("store: ", err)
	}
	llm := NewLLMClient(llmBase, llmKey, llmModel)
	llm.KeyFile = llmKeyFile
	srv := &Server{store: store, llm: llm, llmModel: llmModel, llmBase: llmBase,
		mode: rt.Mode, sync: rt.Sync, start: time.Now(), dataDir: dir}

	srv.attachSlowPath(ctx, rt)

	if w := startupWarning(rt, llm); w != "" {
		log.Print(w)
	}
	log.Print(listeningLine(rt))
	srv.allowedHosts = rt.AllowedHosts
	srv.ownerAliases = aliasMap(rt.UserAliases)
	srv.minScore = rt.MinScore
	srv.dedupeScore = rt.DedupeScore
	hs := &http.Server{Addr: net.JoinHostPort(rt.Host, port), Handler: srv.routes(), ReadHeaderTimeout: readHeaderTimeout}
	log.Fatal(hs.ListenAndServe())
}

// routes is the complete HTTP surface. Lifted out of main so the body limit and
// method guards are exercised by the same mux the server serves, not a copy.
func (s *Server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/api/v1/status", s.handleStatus)
	mux.HandleFunc("/api/v1/add", s.handleAdd)
	mux.HandleFunc("/api/v1/search", s.handleSearch)
	mux.HandleFunc("/api/v1/list", s.handleList)
	mux.HandleFunc("/api/v1/graph", s.handleGraph)
	mux.HandleFunc("/api/v1/graph-as-of", s.handleGraphAsOf)
	mux.HandleFunc("/api/v1/edges", s.handleGraphEdges)
	mux.HandleFunc("/api/v1/learning/graph", s.handleStarmapGraph)
	mux.HandleFunc("/api/v1/delete_all", s.handleDelete)
	mux.HandleFunc("/api/v1/metrics", s.handleMetrics)
	mux.HandleFunc("/api/v1/digest", s.handleDigest)
	mux.HandleFunc("/api/v1/reprocess", s.handleReprocess)
	mux.HandleFunc("/api/v1/admin/compact_raw", s.handleCompactRaw)
	mux.HandleFunc("/api/v1/admin/dedupe_facts", s.handleDedupeFacts)
	// Dashboard UI (embedded single-file frontend)
	// --- v3.5 dashboard adapter endpoints (real v4 data, v3.5 shapes) ---
	mux.HandleFunc("/api/status", s.handleDashStatus)
	mux.HandleFunc("/api/info", s.handleDashInfo)
	mux.HandleFunc("/api/memories", s.handleDashMemories)
	mux.HandleFunc("/api/layer-counts", s.handleDashLayerCounts)
	mux.HandleFunc("/api/storage", s.handleDashStorage)
	mux.HandleFunc("/api/metrics", s.handleDashMetrics)
	mux.HandleFunc("/api/graph-counts", s.handleDashGraphCounts)
	mux.HandleFunc("/api/layer-health", s.handleDashLayerHealth)
	mux.HandleFunc("/api/l6-schemas", s.handleDashL6Schemas)
	mux.HandleFunc("/api/l5/graph", s.handleDashL5Graph)
	mux.HandleFunc("/api/quality-metrics", s.handleDashQuality)
	mux.HandleFunc("/api/coding-count", s.handleDashCodingCount)
	mux.HandleFunc("/api/coding-memories", s.handleDashCodingMemories)
	mux.Handle("/dashboard/", http.StripPrefix("/dashboard/", s.handleDashboard()))
	return guardLocal(s.allowedHosts, limitBody(mux))
}

// guardLocal refuses requests a web page could make on the user's behalf. The
// API has no authentication; binding to loopback keeps other machines out, but
// not a page open in the user's own browser. Such a page can POST to
// 127.0.0.1 without a CORS preflight (delete_all, add) and, through DNS
// rebinding, read responses under its own hostname. So the guard applies on
// every bind address:
//
//   - Host must be localhost, an IP literal, or listed in allowed (see
//     HYATLAS_ALLOWED_HOSTS). A DNS name is how a rebinding page reaches a
//     server bound to 0.0.0.0, so DNS names are refused unless listed.
//   - Origin, when present, must be loopback, the request's own host:port
//     (same-origin), or an allowed hostname. "null" and non-http(s) are refused.
//   - Sec-Fetch-Site: cross-site is refused. Browsers send it even when Origin
//     is absent, as on a simple GET.
//
// Requests without an Origin (the plugin, curl) and the server's own
// /dashboard/ pages are unaffected.
func guardLocal(allowed []string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !hostAllowed(r.Host, allowed) {
			jsonResponse(w, http.StatusForbidden, map[string]any{"error": "non-local Host refused"})
			return
		}
		if strings.EqualFold(r.Header.Get("Sec-Fetch-Site"), "cross-site") {
			jsonResponse(w, http.StatusForbidden, map[string]any{"error": "cross-site request refused"})
			return
		}
		if o := r.Header.Get("Origin"); o != "" && !originAllowed(o, r.Host, allowed) {
			jsonResponse(w, http.StatusForbidden, map[string]any{"error": "cross-origin request refused"})
			return
		}
		next.ServeHTTP(w, r)
	})
}

// hostAllowed reports whether a Host header names an address this server may
// answer: loopback, an IP literal, or an allowlisted hostname. The port is ignored.
func hostAllowed(host string, allowed []string) bool {
	// No Host header at all (HTTP/1.0 without one) names no other host, so it is local.
	if strings.TrimSpace(host) == "" {
		return true
	}
	h := hostOnly(host)
	if isLoopbackHost(h) || net.ParseIP(h) != nil {
		return true
	}
	return hostListed(h, allowed)
}

// originAllowed reports whether an Origin header may make a request to a server
// that the Host header addresses as reqHost.
func originAllowed(origin, reqHost string, allowed []string) bool {
	u, err := url.Parse(origin)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return false
	}
	if isLoopbackHost(u.Hostname()) {
		return true
	}
	if strings.EqualFold(u.Host, reqHost) {
		return true
	}
	return hostListed(u.Hostname(), allowed)
}

// hostOnly strips the port from a host[:port] string, and the brackets from an
// IPv6 literal, so "[::1]:19528", "localhost:19528" and "evil.example" all reduce
// to a bare name.
func hostOnly(hp string) string {
	if h, _, err := net.SplitHostPort(hp); err == nil {
		return h
	}
	return strings.Trim(hp, "[]")
}

// hostListed reports whether h is one of the allowlisted hostnames. Comparison
// ignores case and a trailing dot.
func hostListed(h string, allowed []string) bool {
	h = normHost(h)
	if h == "" {
		return false
	}
	for _, a := range allowed {
		if normHost(a) == h {
			return true
		}
	}
	return false
}

// normHost lower-cases a hostname and drops one trailing dot, so "Example.COM."
// and "example.com" compare equal.
func normHost(h string) string {
	return strings.ToLower(strings.TrimSuffix(strings.TrimSpace(h), "."))
}

// parseUserAliases reads HYATLAS_USER_ALIASES: groups separated by ";", user IDs in a
// group separated by ",". IDs are trimmed and empty ones dropped; a group needs at
// least two distinct IDs to mean anything, so smaller ones are dropped.
// "123,default;alice,al" is two groups.
func parseUserAliases(raw string) [][]string {
	var out [][]string
	for _, group := range strings.Split(raw, ";") {
		var ids []string
		seen := map[string]bool{}
		for _, part := range strings.Split(group, ",") {
			id := strings.TrimSpace(part)
			if id == "" || seen[id] {
				continue
			}
			seen[id] = true
			ids = append(ids, id)
		}
		if len(ids) >= 2 {
			out = append(out, ids)
		}
	}
	return out
}

// aliasMap indexes alias groups by member: each ID maps to its whole group,
// itself included. An ID in two groups maps to the union of both.
func aliasMap(groups [][]string) map[string][]string {
	out := map[string][]string{}
	for _, group := range groups {
		for _, id := range group {
			seen := map[string]bool{}
			for _, existing := range out[id] {
				seen[existing] = true
			}
			for _, other := range group {
				if !seen[other] {
					seen[other] = true
					out[id] = append(out[id], other)
				}
			}
		}
	}
	return out
}

// expandOwners adds every alias of each user ID (HYATLAS_USER_ALIASES), keeping the
// order IDs were first seen and dropping duplicates. Without aliases it returns ids.
func (s *Server) expandOwners(ids []string) []string {
	var out []string
	seen := map[string]bool{}
	add := func(id string) {
		if id == "" || seen[id] {
			return
		}
		seen[id] = true
		out = append(out, id)
	}
	for _, id := range ids {
		add(id)
		for _, alias := range s.ownerAliases[id] {
			add(alias)
		}
	}
	return out
}

// parseHostList reads HYATLAS_ALLOWED_HOSTS: comma-separated hostnames. An entry
// may carry a port ("myhost:8080" is "myhost"), as a Host header does. Entries are
// lower-cased with any trailing dot dropped, and empty ones are dropped.
func parseHostList(raw string) []string {
	var out []string
	for _, part := range strings.Split(raw, ",") {
		if h := normHost(hostOnly(strings.TrimSpace(part))); h != "" {
			out = append(out, h)
		}
	}
	return out
}

// isLoopbackHost reports whether h names this machine: localhost (with or without
// a trailing dot) or a loopback IP literal (brackets allowed).
func isLoopbackHost(h string) bool {
	h = normHost(strings.Trim(h, "[]"))
	if h == "localhost" {
		return true
	}
	ip := net.ParseIP(h)
	return ip != nil && ip.IsLoopback()
}

// defaultMinScore is the similarity below which /api/v1/search drops a hit. Measured
// on a real store with bge-small: on-topic queries never put a relevant hit below
// 0.67, and off-topic queries never scored above 0.55. 0 disables the floor.
const defaultMinScore = 0.60

// defaultDedupeScore is the similarity at or above which a newly extracted fact is
// treated as a restatement of the owner's nearest existing fact, which it then
// supersedes. 0 disables write-time de-duplication.
const defaultDedupeScore = 0.92

// maxExtractInput caps the text one extraction call sends to the LLM, in bytes. The
// Hermes plugin already sends one turn; this guards against any other client
// posting a whole transcript.
const maxExtractInput = 16000

// maxRequestBody caps every request body. Raw memories can be large session
// dumps (see utf8Trunc), so the cap is generous. Without it, decoding read
// whatever a client sent into memory.
const maxRequestBody = 8 << 20

// readHeaderTimeout stops a client from holding a connection open by trickling
// request headers one byte at a time.
const readHeaderTimeout = 10 * time.Second

func limitBody(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			r.Body = http.MaxBytesReader(w, r.Body, maxRequestBody)
		}
		next.ServeHTTP(w, r)
	})
}

func envOr(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

// modelFileName is the file the in-process embedder cannot start without. A
// directory counts as a model home only if it holds this file.
const modelFileName = "bge-small-en-v1.5.onnx"

// resolveModelDir picks the directory the BGE model is loaded from, given the
// HYATLAS_MODEL_DIR value (empty when unset). It returns the chosen directory and
// every directory searched, in order, for the fatal message.
func resolveModelDir(override string) (string, []string) {
	cwd, _ := os.Getwd()
	exeDir := ""
	if exe, err := os.Executable(); err == nil {
		exeDir = filepath.Dir(exe)
	}
	return findModelDir(override, cwd, exeDir, installModelDir(runtime.GOOS))
}

// findModelDir is the search behind resolveModelDir, with the base directories
// passed in so a test can use temp dirs.
//
// An explicit override is the only candidate. Falling back to another copy of
// the model would load a file the user did not name. Without one, the order is
// <cwd>/models, then <exe dir>/models, then the installer's default. The cwd
// comes first because a dev checkout runs from the repo root, and the executable
// directory second because installers put the model beside the binary while a
// plugin-spawned server runs from some other cwd.
//
// The result is always absolute. The relative "./models" cannot be handed to the
// embedder directly: the onnxruntime loader and the directory check resolve it
// against different bases on Windows. When nothing holds the model, the first
// candidate is returned so the error names one real location.
func findModelDir(override, cwd, exeDir, installDir string) (string, []string) {
	var cands []string
	if override != "" {
		cands = []string{absPath(override)}
	} else {
		for _, base := range []string{cwd, exeDir} {
			if base != "" {
				cands = append(cands, filepath.Join(base, "models"))
			}
		}
		if installDir != "" {
			cands = append(cands, installDir)
		}
	}
	for _, d := range cands {
		if hasModelFile(d) {
			return d, cands
		}
	}
	if len(cands) == 0 {
		return "", nil
	}
	return cands[0], cands
}

// installModelDir is where scripts/install.sh caches the model when no directory
// is given: %LOCALAPPDATA%\hyatlas\models on Windows, ~/.hyatlas/models elsewhere.
// Keep the two in step.
func installModelDir(goos string) string {
	if goos == "windows" {
		base := os.Getenv("LOCALAPPDATA")
		if base == "" {
			home, err := os.UserHomeDir()
			if err != nil {
				return ""
			}
			base = filepath.Join(home, "AppData", "Local")
		}
		return filepath.Join(base, "hyatlas", "models")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".hyatlas", "models")
}

// onnxRuntimeLibName is the onnxruntime shared library the BGE loader looks for
// first on each platform. It matches bge.runtimeLibName.
func onnxRuntimeLibName(goos string) string {
	switch goos {
	case "windows":
		return "onnxruntime.dll"
	case "darwin":
		return "libonnxruntime.dylib"
	default:
		return "libonnxruntime.so"
	}
}

// hasModelFile reports whether dir holds everything the BGE embedder loads: the
// model, its vocab, and an onnxruntime shared library. The library may have the
// platform's name or, as bge.New also accepts, any onnxruntime* file.
func hasModelFile(dir string) bool {
	if !isRegularFile(filepath.Join(dir, modelFileName)) || !isRegularFile(filepath.Join(dir, "vocab.txt")) {
		return false
	}
	if isRegularFile(filepath.Join(dir, onnxRuntimeLibName(runtime.GOOS))) {
		return true
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return false
	}
	for _, e := range entries {
		n := e.Name()
		if !e.IsDir() && (strings.HasPrefix(n, "onnxruntime") || strings.HasPrefix(n, "libonnxruntime")) {
			return true
		}
	}
	return false
}

// isRegularFile reports whether p exists and is not a directory.
func isRegularFile(p string) bool {
	fi, err := os.Stat(p)
	return err == nil && !fi.IsDir()
}

// absPath makes p absolute, falling back to a cleaned p when that fails.
func absPath(p string) string {
	if abs, err := filepath.Abs(p); err == nil {
		return abs
	}
	return filepath.Clean(p)
}

// describeEmbed names the embedder actually in use, for the startup log.
// Reporting embedModel unconditionally said "text-embedding-3-small" even on the
// local BGE path, which reads like a remote OpenAI embedder is configured.
func describeEmbed(embedBase, embedModel string) string {
	switch {
	case strings.EqualFold(embedBase, "bge"):
		return "bge-small (in-process)"
	case strings.EqualFold(embedBase, "local"):
		return "local-stub (deterministic, 384-d)"
	default:
		return embedBase + " (" + embedModel + ")"
	}
}

// listeningLine is the startup banner. A function rather than an inline
// log.Printf so a test can assert the embedder it reports matches the one
// resolved, instead of the message drifting back to embedModel unnoticed.
func listeningLine(rt runtimeCfg) string {
	llm := rt.LLMModel
	if llm == "" {
		llm = "unset"
	}
	return fmt.Sprintf("HyAtlas-Go listening on %s (data=%s embed=%s llm=%s mode=%s)",
		net.JoinHostPort(rt.Host, rt.Port), rt.DataDir, describeEmbed(rt.EmbedBase, rt.EmbedModel), llm, rt.Mode.OrDefault())
}

// startupWarning returns a human-readable setup message for the one state that
// silently produces empty memories: a mode that calls an LLM that is not fully
// configured. Empty means nothing to warn about.
func startupWarning(rt runtimeCfg, llm *LLMClient) string {
	if !rt.Mode.UsesLLM() {
		return ""
	}
	// Same gate status and extraction use, so the three cannot disagree about
	// whether this server is ready. An empty endpoint or model counts as
	// unconfigured too: there is no shipped default to fall back on.
	if llm.Configured() {
		return ""
	}
	return fmt.Sprintf(`
  %s mode calls an LLM but %s, so writes will store the raw trace only and
  extraction will report "unconfigured". No endpoint is assumed: set your own
  OpenAI-compatible one.

      export HYATLAS_LLM_BASE="%s"
      export HYATLAS_LLM_MODEL="%s"
      export HYATLAS_LLM_KEY="***"

  Or run offline with no LLM call at all:

      export HYATLAS_MODE=lite

  In the Hermes plugin, these are the "LLM endpoint", "LLM model" and
  "LLM API key" settings (hermes memory setup, or the Desktop settings form).
`, rt.Mode.OrDefault(), missingLLM(rt, llm), suggestLLMBase, suggestLLMModel)
}

// missingLLM names the unset parts, so the warning tells the user what to fix
// rather than claiming a key is missing when it is the endpoint that is.
func missingLLM(rt runtimeCfg, llm *LLMClient) string {
	parts := make([]string, 0, 3)
	if rt.LLMBase == "" {
		parts = append(parts, "HYATLAS_LLM_BASE")
	}
	if rt.LLMModel == "" {
		parts = append(parts, "HYATLAS_LLM_MODEL")
	}
	if llm == nil || llm.resolveKey() == "" {
		parts = append(parts, "HYATLAS_LLM_KEY")
	}
	switch len(parts) {
	case 0:
		return "it is not fully configured"
	case 1:
		return parts[0] + " is not set"
	}
	return strings.Join(parts[:len(parts)-1], ", ") + " and " + parts[len(parts)-1] + " are not set"
}
