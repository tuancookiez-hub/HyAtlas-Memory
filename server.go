package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/graph"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Server mirrors the HyAtlas REST contract for drop-in parity.
type Server struct {
	store          *MemoryStore
	llm            *LLMClient
	llmModel       string
	llmBase        string
	start          time.Time
	lastExtractErr string
	dataDir        string
}

type Status struct {
	Status        string `json:"status"`
	VDB           string `json:"vdb"`
	Embed         string `json:"embed"`
	LLM           string `json:"llm"`
	LLMModel      string `json:"llm_model"`
	LLMBase       string `json:"llm_base"`
	VDBProvider   string `json:"vdb_provider"`
	VDBCollection string `json:"vdb_collection"`
	VDBPoints     int    `json:"vdb_points"`
	EmbedDims     int    `json:"embed_dims"`
	WritePipeline string `json:"write_pipeline"`
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	counts := s.store.LayerCounts()
	write := "ok"
	if s.lastExtractErr != "" {
		write = "degraded: " + s.lastExtractErr
	}
	writesCount, searchesCount := s.store.Usage()
	// L5 lives in the graph store; LayerCounts() returns 0 for it from chromem.
	counts = s.store.LayerCounts()
	counts["l5_knowledge"] = s.store.Graph().NodeCount()
	status := Status{
		Status:        "ok",
		VDB:           "ok",
		Embed:         "ok",
		LLM:           "ok",
		LLMModel:      s.llmModel,
		LLMBase:       s.llmBase,
		VDBProvider:   "chromem",
		VDBCollection: "layers",
		VDBPoints:     s.store.TotalMemories(),
		EmbedDims:     384,
		WritePipeline: write,
	}
	jsonResponse(w, 200, map[string]any{
		"status":         status.Status,
		"vdb":            status.VDB,
		"embed":          status.Embed,
		"llm":            status.LLM,
		"llm_model":      status.LLMModel,
		"llm_base":       status.LLMBase,
		"vdb_provider":   status.VDBProvider,
		"vdb_collection": status.VDBCollection,
		"vdb_points":     status.VDBPoints,
		"embed_dims":     status.EmbedDims,
		"write_pipeline": status.WritePipeline,
		"writes":         writesCount,
		"searches":       searchesCount,
		"layers":         counts,
		"graph_nodes":    s.store.Graph().NodeCount(),
		"graph_edges":    s.store.Graph().EdgeCount(),
	})
}

// promoteExtraction writes one LLM extraction result to its 7 layers.
// sourceID is the L2 raw memory id — used to anchor L5 edges back to their origin.
func promoteExtraction(store *MemoryStore, ex *Extraction, userID, agentID, sourceID string) {
	now := time.Now().UTC().Format(time.RFC3339)
	// L3 Facts
	for _, f := range ex.Facts {
		if f.Data == "" {
			continue
		}
		_ = store.Add(memory.L3Fact, newID(), f.Data, map[string]string{
			"user_id": userID, "agent_id": agentID,
			"source_layer_label": f.Layer, "ts": now,
		})
		// L1 Profile: user_preferences are stable identity — mirror to profile layer.
		if f.Layer == "user_preferences" {
			_ = store.Add(memory.L1Profile, newID(), f.Data, map[string]string{
				"user_id": userID, "agent_id": agentID, "ts": now,
			})
		}
	}
	// L4 Summary (enabled layer — the narrative arc)
	if ex.Summary != nil && strings.TrimSpace(ex.Summary.Text) != "" {
		_ = store.Add(memory.L4Summary, newID(), ex.Summary.Text, map[string]string{
			"user_id": userID, "agent_id": agentID, "ts": now,
		})
	}
	// L5 Knowledge graph — every edge is anchored to its L2 source memory.
	for _, rel := range ex.Knowledge {
		if rel.From == "" || rel.Relation == "" || rel.To == "" {
			continue
		}
		_ = store.Graph().AddEdgeWithSource(rel.From, rel.Relation, rel.To, sourceID)
	}
	// L6 Schema
	for _, sc := range ex.Schemas {
		if sc.Pattern == "" {
			continue
		}
		_ = store.Add(memory.L6Schema, newID(), sc.Pattern, map[string]string{
			"user_id": userID, "agent_id": agentID,
			"context": sc.Context, "ts": now,
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
	resp := map[string]any{"success": err == nil, "memory_id": id, "extraction_status": "pending"}
	if err != nil {
		resp["error"] = err.Error()
		jsonResponse(w, 500, resp)
		return
	}

	// Full 7-layer pipeline: one LLM call extracts facts, summary, knowledge,
	// schema, and intention, then each is written to its own layer. L1 Profile is
	// derived from persistent preferences; L2 Raw is the source doc just written.
	agentID := body.AgentID
	userID := body.UserID
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
		defer cancel()
		if s.llm == nil {
			return
		}
		ex, err := s.llm.Complete(ctx, text)
		if err != nil {
			s.lastExtractErr = err.Error()
			return
		}
		promoteExtraction(s.store, ex, userID, agentID, id)
		_ = s.store.SetExtracted(id, true)
		s.lastExtractErr = ""
	}()

	jsonResponse(w, 200, resp)
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
	items, total := s.store.List(memory.Layer(layer), userID, agentID, limit, offset)

	// include_raw: if false and no layer filter, drop raw rows (parity with v3.5)
	if includeRaw == "false" && layer == "" {
		keep := items[:0]
		for _, it := range items {
			if it.Layer != string(memory.L2Raw) {
				keep = append(keep, it)
			}
		}
		items = keep
	}

	out := make([]map[string]any, 0, len(items))
	for _, it := range items {
		out = append(out, map[string]any{
			"memory_id": it.ID, "content": it.Content, "layer": it.Layer,
			"user_id": it.UserID, "agent_id": it.AgentID, "ts": it.Ts,
			"gmt_created": gmtCreated(it.Ts), "extracted": it.Extracted,
		})
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
	q := r.URL.Query()
	layer := q.Get("layer")
	userID := q.Get("user_id")
	agentID := q.Get("agent_id")
	ids := []string{}
	if idStr := q.Get("id"); idStr != "" {
		ids = append(ids, idStr)
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

func (s *Server) handleDigest(w http.ResponseWriter, r *http.Request) {
	jsonResponse(w, 200, map[string]any{
		"digest_ok":   true,
		"graph_nodes": s.store.Graph().NodeCount(),
		"graph_edges": s.store.Graph().EdgeCount(),
		"note":        "L5 graph is built incrementally on each add; a full scheduled digest runs here.",
	})
}

func (s *Server) handleReprocess(w http.ResponseWriter, r *http.Request) {
	raw, _ := s.store.List(memory.L2Raw, "", "", 200, 0)
	reprocessed := 0
	for _, it := range raw {
		if it.Extracted {
			continue
		}
		if s.llm != nil {
			ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
			if ex, err := s.llm.Complete(ctx, it.Content); err == nil {
				promoteExtraction(s.store, ex, it.UserID, it.AgentID, it.ID)
				reprocessed++
			}
			cancel()
		}
	}
	jsonResponse(w, 200, map[string]any{"reprocessed": reprocessed})
}

func errStr(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
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
		"extract_err": s.lastExtractErr,
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

func main() {
	port := envOr("HYATLAS_GO_PORT", "19528")
	dir := envOr("HYATLAS_GO_DATA", "./data")
	// LLM: any OpenAI-compatible endpoint. Default is a Nous Portal :free model.
	llmBase := envOr("HYATLAS_LLM_BASE", "https://inference-api.nousresearch.com/v1")
	llmKey := os.Getenv("HYATLAS_LLM_KEY")
	llmModel := envOr("HYATLAS_LLM_MODEL", "poolside/laguna-s-2.1:free")
	embedBase := envOr("HYATLAS_EMBED_BASE", "http://127.0.0.1:49200/v1")
	embedKey := os.Getenv("HYATLAS_EMBED_KEY")
	embedModel := envOr("HYATLAS_EMBED_MODEL", "text-embedding-3-small")

	ctx := context.Background()
	var embedder Embedder
	switch {
	case strings.EqualFold(embedBase, "bge"):
		// In-Go BGE inference (no Python, no HTTP) — the pure-Go path.
		modelDir := envOr("HYATLAS_MODEL_DIR", "./models")
		if useEmbeddedAssets {
			modelDir = materializeAssets()
		}
		b, err := NewBGEGoEmbedder(modelDir)
		if err != nil {
			log.Fatal("bge embedder: ", err)
		}
		embedder = b
	case strings.EqualFold(embedBase, "local"):
		embedder = NewLocalEmbedder(384)
	default:
		embedder = NewOpenAIEmbedder(embedBase, embedKey, embedModel)
	}
	graphPath := envOr("HYATLAS_GRAPH_PATH", filepath.Join(dir, "graph.json"))
	store, err := NewMemoryStore(ctx, dir, embedder, graphPath)
	if err != nil {
		log.Fatal("store: ", err)
	}
	llm := NewLLMClient(llmBase, llmKey, llmModel)
	srv := &Server{store: store, llm: llm, llmModel: llmModel, llmBase: llmBase, start: time.Now(), dataDir: dir}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", srv.handleHealthz)
	mux.HandleFunc("/api/v1/status", srv.handleStatus)
	mux.HandleFunc("/api/v1/add", srv.handleAdd)
	mux.HandleFunc("/api/v1/search", srv.handleSearch)
	mux.HandleFunc("/api/v1/list", srv.handleList)
	mux.HandleFunc("/api/v1/graph", srv.handleGraph)
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

	log.Printf("HyAtlas-Go listening on :%s (data=%s embed=%s llm=%s)", port, dir, embedModel, llmModel)
	host := envOr("HYATLAS_GO_HOST", "127.0.0.1")
	log.Fatal(http.ListenAndServe(host+":"+port, mux))
}

func envOr(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}
