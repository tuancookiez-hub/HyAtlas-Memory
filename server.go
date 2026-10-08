package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
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
const Version = "4.3.3"

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
		ModeDetail:       s.mode.Describe(),
		UsesLLM:          s.mode.UsesLLM(),
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
func promoteExtraction(store *MemoryStore, ex *Extraction, userID, agentID, sourceID string) {
	now := time.Now().UTC().Format(time.RFC3339)
	// L3 Facts
	for _, f := range ex.Facts {
		if f.Data == "" {
			continue
		}
		_ = store.Add(memory.L3Fact, newID(), f.Data, map[string]string{
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
	ex, err := s.llm.Complete(ctx, text)
	if err != nil {
		s.setExtractErr(err.Error())
		return err
	}
	promoteExtraction(s.store, ex, userID, agentID, id)
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
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		jsonResponse(w, 400, map[string]any{"error": "bad body"})
		return
	}
	if body.Query == "" {
		jsonResponse(w, 400, map[string]any{"error": "query required"})
		return
	}
	userID, agentID := "", ""
	if len(body.UserIDs) > 0 {
		userID = body.UserIDs[0]
	}
	if len(body.AgentIDs) > 0 {
		agentID = body.AgentIDs[0]
	}
	res, err := s.store.Search(body.Query, body.Limit, memory.Layer(body.Layer), userID, agentID)
	if err != nil {
		jsonResponse(w, 500, map[string]any{"error": err.Error()})
		return
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

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	// Accept both GET query params and the v3.5 client's POST JSON body.
	q := r.URL.Query()
	layer := q.Get("layer")
	userID := q.Get("user_id")
	agentID := q.Get("agent_id")
	limit := atoi(q.Get("limit"), 20)
	offset := atoi(q.Get("offset"), 0)
	includeRaw := q.Get("include_raw")
	if r.Method == http.MethodPost {
		var body struct {
			Limit      int    `json:"limit"`
			Offset     int    `json:"offset"`
			Layer      string `json:"layer"`
			UserID     string `json:"user_id"`
			AgentID    string `json:"agent_id"`
			IncludeRaw *bool  `json:"include_raw"`
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
		}
	}
	items, total := s.store.List(memory.Layer(layer), userID, agentID, limit, offset, includeRaw == "false" && layer == "")

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
	// Scoping may arrive as query params (curl style) OR as a JSON body
	// (the hyatlas plugin's client style). Read both, query wins.
	var body struct {
		ID      string `json:"id"`
		Layer   string `json:"layer"`
		UserID  string `json:"user_id"`
		AgentID string `json:"agent_id"`
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
	confirm := first(q.Get("confirm"), body.Confirm) == "wipe-all"
	ids := []string{}
	if idStr := first(q.Get("id"), body.ID); idStr != "" {
		ids = append(ids, idStr)
	}
	// layer "*" means "everything" — same as an unscoped wipe.
	if layer == "*" {
		layer = ""
	}
	// Guard: an unscoped call is a full-store wipe. Require an explicit opt-in.
	if len(ids) == 0 && layer == "" && userID == "" && agentID == "" && !confirm {
		jsonResponse(w, 400, map[string]any{
			"deleted_count": 0,
			"error":         "unscoped delete refused: pass layer/user_id/agent_id/id, or confirm=wipe-all to wipe the entire store",
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
	// extracted-skip does not apply; otherwise walk up to `max` (default 200)
	// oldest unextracted raw rows.
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
		raw, _ = s.store.List(memory.L2Raw, "", "", max, 0, false)
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
		if len(body.IDs) == 0 && it.Extracted {
			skipped++
			continue
		}
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

	// 1. Nodes: all L3 facts + a sampled set of L2 raw entries. Layer type
	//    becomes the visual "kind" (memory in the starmap sense).
	items, _ := s.store.List("", "", "", limit, 0, false)
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
	graphNodes, graphRels := s.store.Graph().Snapshot(limit)
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
		lItems, _ := s.store.List(memory.Layer(l), "", "", 200, 0, false)
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
	recent, _ := s.store.List(memory.L3Fact, "", "", 20, 0, false)
	for _, it := range recent {
		if semCount >= maxSem {
			break
		}
		hits, err := s.store.Search(it.Content, kSem+1, "", "", "")
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

	// 1. Knowledge: L5 explicit edges
	nodes, rels := s.store.Graph().Snapshot(limitAll)
	nodesArr := make([]map[string]any, 0, len(nodes))
	for _, n := range nodes {
		nodesArr = append(nodesArr, map[string]any{
			"id": n.ID, "label": n.Label, "type": n.Type,
		})
	}
	knowledge := make([]map[string]any, 0, len(rels))
	for _, e := range rels {
		knowledge = append(knowledge, map[string]any{
			"from": e.From, "to": e.To,
			"relation": e.Relation, "weight": e.Weight,
			"source":      e.Source,
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
		items, _ := s.store.List(memory.Layer(l), "", "", 200, 0, false)
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
	recent, _ := s.store.List(memory.L3Fact, "", "", 20, 0, false)
	for _, it := range recent {
		if semCount >= maxSem {
			break
		}
		hits, err := s.store.Search(it.Content, semanticK+1, "", "", "")
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
	var body struct {
		Node string `json:"node"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	neighbors := s.store.Graph().Neighbors(body.Node)
	if neighbors == nil {
		neighbors = []graph.Neighbor{}
	}
	jsonResponse(w, 200, map[string]any{
		"node":        body.Node,
		"neighbors":   neighbors,
		"node_count":  s.store.Graph().NodeCount(),
		"edge_count":  s.store.Graph().EdgeCount(),
		"extract_err": s.extractErr(),
	})
}

func (s *Server) handleGraphAsOf(w http.ResponseWriter, r *http.Request) {
	ts := atoi(r.URL.Query().Get("ts"), int(time.Now().Unix()))
	n := atoi(r.URL.Query().Get("n"), 500)
	nodes, rels := s.store.Graph().SnapshotAsOf(int64(ts), n)
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
	Port       string
	DataDir    string
	GraphPath  string
	LLMBase    string
	LLMModel   string
	EmbedBase  string
	EmbedModel string
	ModelDir   string
	Mode       Mode
	Sync       Sync
	// Slow-path (ultra) tuning. Zero retention means raw history is never decayed.
	Consolidate time.Duration
	Retention   time.Duration
	Batch       int
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
	defaultDataDir    = "./data"
	defaultLLMBase    = ""
	defaultLLMModel   = ""
	defaultEmbedBase  = "bge"
	defaultEmbedModel = "text-embedding-3-small"
	defaultModelDir   = "./models"
)

func resolveRuntime() runtimeCfg {
	dataDir := envOr("HYATLAS_GO_DATA", defaultDataDir)
	return runtimeCfg{
		Mode:        resolveMode(),
		Sync:        resolveSync(),
		Consolidate: parseDuration("HYATLAS_CONSOLIDATE_EVERY", defaultConsolidate),
		Retention:   parseDuration("HYATLAS_RAW_RETENTION", 0),
		Batch:       envInt("HYATLAS_CONSOLIDATE_BATCH", defaultBatch),
		Port:        envOr("HYATLAS_GO_PORT", defaultPort),
		DataDir:     dataDir,
		GraphPath:   envOr("HYATLAS_GRAPH_PATH", filepath.Join(dataDir, "graph.json")),
		LLMBase:     envOr("HYATLAS_LLM_BASE", defaultLLMBase),
		LLMModel:    envOr("HYATLAS_LLM_MODEL", defaultLLMModel),
		EmbedBase:   envOr("HYATLAS_EMBED_BASE", defaultEmbedBase),
		EmbedModel:  envOr("HYATLAS_EMBED_MODEL", defaultEmbedModel),
		ModelDir:    resolveModelDir(envOr("HYATLAS_MODEL_DIR", defaultModelDir)),
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
	go s.cons.Run(ctx)
	return true
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
	// LLM: any OpenAI-compatible endpoint. Default is a Nous Portal :free model.
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
		// model and then fail on the shared library. Absolute paths work from
		// any cwd, so prefer the cwd, then the executable's own directory.
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
				"Fix one of:\n"+
				"  1. install via scripts/install.sh (fetches the model for you)\n"+
				"  2. download a release binary built with -tags embedded, which\n"+
				"     carries the model inside it\n"+
				"  3. put bge-small-en-v1.5.onnx and onnxruntime.<ext> in %s,\n"+
				"     or point HYATLAS_MODEL_DIR at a directory that has them\n"+
				"Or set HYATLAS_EMBED_BASE to an OpenAI-compatible embeddings URL\n"+
				"(memory text would then leave the machine) or to \"local\" for the\n"+
				"offline deterministic stub.\n", err, modelDir)
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

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", srv.handleHealthz)
	mux.HandleFunc("/api/v1/status", srv.handleStatus)
	mux.HandleFunc("/api/v1/add", srv.handleAdd)
	mux.HandleFunc("/api/v1/search", srv.handleSearch)
	mux.HandleFunc("/api/v1/list", srv.handleList)
	mux.HandleFunc("/api/v1/graph", srv.handleGraph)
	mux.HandleFunc("/api/v1/graph-as-of", srv.handleGraphAsOf)
	mux.HandleFunc("/api/v1/edges", srv.handleGraphEdges)
	mux.HandleFunc("/api/v1/learning/graph", srv.handleStarmapGraph)
	mux.HandleFunc("/api/v1/delete_all", srv.handleDelete)
	mux.HandleFunc("/api/v1/metrics", srv.handleMetrics)
	mux.HandleFunc("/api/v1/digest", srv.handleDigest)
	mux.HandleFunc("/api/v1/reprocess", srv.handleReprocess)
	// Dashboard UI (embedded single-file frontend)
	// --- v3.5 dashboard adapter endpoints (real v4 data, v3.5 shapes) ---
	mux.HandleFunc("/api/status", srv.handleDashStatus)
	mux.HandleFunc("/api/info", srv.handleDashInfo)
	mux.HandleFunc("/api/memories", srv.handleDashMemories)
	mux.HandleFunc("/api/layer-counts", srv.handleDashLayerCounts)
	mux.HandleFunc("/api/storage", srv.handleDashStorage)
	mux.HandleFunc("/api/metrics", srv.handleDashMetrics)
	mux.HandleFunc("/api/graph-counts", srv.handleDashGraphCounts)
	mux.HandleFunc("/api/layer-health", srv.handleDashLayerHealth)
	mux.HandleFunc("/api/l6-schemas", srv.handleDashL6Schemas)
	mux.HandleFunc("/api/l5/graph", srv.handleDashL5Graph)
	mux.HandleFunc("/api/quality-metrics", srv.handleDashQuality)
	mux.HandleFunc("/api/coding-count", srv.handleDashCodingCount)
	mux.HandleFunc("/api/coding-memories", srv.handleDashCodingMemories)
	mux.Handle("/dashboard/", http.StripPrefix("/dashboard/", srv.handleDashboard()))

	if w := startupWarning(rt, llm); w != "" {
		log.Print(w)
	}
	log.Print(listeningLine(rt))
	host := envOr("HYATLAS_GO_HOST", "127.0.0.1")
	log.Fatal(http.ListenAndServe(host+":"+port, mux))
}

func envOr(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

// resolveModelDir turns a configured model directory into an absolute path.
//
// The relative default "./models" cannot be handed straight to the embedder: the
// onnxruntime loader and the directory-existence check resolve it against
// different bases on Windows, so the same path finds the model file and then
// fails looking for onnxruntime.dll. Trying the cwd first and the executable's
// directory second keeps the documented default working from either layout,
// because installers put the model beside the binary while a dev checkout runs
// from the repo root.
func resolveModelDir(dir string) string {
	if filepath.IsAbs(dir) {
		return filepath.Clean(dir)
	}
	for _, base := range modelBaseDirs() {
		cand := filepath.Join(base, dir)
		if fi, err := os.Stat(cand); err == nil && fi.IsDir() {
			if abs, err := filepath.Abs(cand); err == nil {
				return abs
			}
			return filepath.Clean(cand)
		}
	}
	// Nothing on disk matched; return an absolute cwd-relative path so the
	// error the user sees names one real location instead of two possible ones.
	if abs, err := filepath.Abs(dir); err == nil {
		return abs
	}
	return filepath.Clean(dir)
}

func modelBaseDirs() []string {
	bases := make([]string, 0, 2)
	if wd, err := os.Getwd(); err == nil {
		bases = append(bases, wd)
	}
	if exe, err := os.Executable(); err == nil {
		bases = append(bases, filepath.Dir(exe))
	}
	return bases
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
	return fmt.Sprintf("HyAtlas-Go listening on :%s (data=%s embed=%s llm=%s mode=%s)",
		rt.Port, rt.DataDir, describeEmbed(rt.EmbedBase, rt.EmbedModel), llm, rt.Mode.OrDefault())
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
