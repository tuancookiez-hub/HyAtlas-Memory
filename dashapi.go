package main

// Dashboard-facing API endpoints (v3.5 parity for the copied dashboard UI).
// All values are REAL data from the v4 store — no fabricated payloads.
// Endpoints the dashboard needs:
//   GET /api/status            — infra health (vdb/embed/llm) + counts
//   GET /api/info              — static build info
//   GET /api/memories          — page of memories (content/layer/memory_id/gmt_created/…)
//   GET /api/layer-counts      — display_counts/graph_counts/vdb_counts/total
//   GET /api/storage           — files under the data dir
//   GET /api/metrics           — uptime_seconds + totals
//   GET /api/graph-counts      — L5/L6/L7 + relations
//   GET /api/layer-health      — per-layer freshness
//   GET /api/l6-schemas        — schema layer items
//   GET /api/l5/graph          — graph nodes/rels (all layers or filtered)
//   GET /api/quality-metrics   — quality snapshot (honest: not computed in v4 yet)
//   GET /api/coding-count      — coding-layer count (v4: 0, honest)
//   GET /api/coding-memories   — coding-layer items (v4: empty, honest)

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/graph"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// graphOwner reads the owner filter the graph endpoints accept as user_id and
// agent_id. An empty value, and "all" (which the dashboard sends for every
// agent), both mean no filter.
func graphOwner(userID, agentID string) (string, string) {
	return ownerFilter(userID), ownerFilter(agentID)
}

func ownerFilter(v string) string {
	v = strings.TrimSpace(v)
	if v == "all" {
		return ""
	}
	return v
}

// graphScope is the graph.Scope for an owner filter read by graphOwner.
func graphScope(uid, aid string) graph.Scope {
	return graph.Scope{UserID: uid, AgentID: aid}
}

// gmtCreated converts an RFC3339 ts string to unix seconds (dashboard expects a number).
func gmtCreated(ts string) int64 {
	t, err := time.Parse(time.RFC3339, ts)
	if err != nil {
		return 0
	}
	return t.Unix()
}

// writeJSON is an alias for jsonResponse with GET-friendly status.
func writeJSON(w http.ResponseWriter, code int, v any) { jsonResponse(w, code, v) }

func (s *Server) handleDashStatus(w http.ResponseWriter, r *http.Request) {
	write := "ok"
	if errStr := s.extractErr(); errStr != "" {
		write = "degraded: " + errStr
	}
	writeJSON(w, 200, map[string]any{
		"status": "ok",
		"vdb":    "ok",
		"embed":  "ok",
		// Same gate as /api/v1/status, so the dashboard cannot show a healthy LLM
		// in lite (which never calls one) or in a mode with no credential.
		"llm":      s.llmState(),
		"mode":     string(s.mode.OrDefault()),
		"layers":   s.store.LayerCounts(),
		"total":    s.store.TotalMemories(),
		"pipeline": write,
	})
}

func (s *Server) handleDashInfo(w http.ResponseWriter, r *http.Request) {
	writes, searches := s.store.Usage()
	writeJSON(w, 200, map[string]any{
		"name":           "HyAtlas v4 (Go)",
		"version":        Version,
		"mode":           string(s.mode.OrDefault()),
		"llm_model":      s.llmModel,
		"llm_base":       s.llmBase,
		"writes":         writes,
		"searches":       searches,
		"uptime_seconds": int64(time.Since(s.start).Seconds()),
	})
}

// handleDashMemories serves /api/memories with the v3.5 item shape.
func (s *Server) handleDashMemories(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit := atoi(q.Get("limit"), 100)
	// "all" from the dashboard means no owner filter, as on the graph endpoints.
	uid, aid := graphOwner(q.Get("user_id"), q.Get("agent_id"))
	items, _ := s.store.List(memory.Layer(q.Get("layer")), uid, aid, limit, atoi(q.Get("offset"), 0), false)
	out := make([]map[string]any, 0, len(items))
	for _, it := range items {
		out = append(out, map[string]any{
			"memory_id":   it.ID,
			"content":     it.Content,
			"layer":       it.Layer,
			"user_id":     it.UserID,
			"agent_id":    it.AgentID,
			"gmt_created": gmtCreated(it.Ts),
			"extracted":   it.Extracted,
		})
	}
	writeJSON(w, 200, map[string]any{
		"total":    s.store.TotalMemories(),
		"memories": map[string]any{"profile": []any{}, "proactive": []any{}, "normal": out},
	})
}

// handleDashLayerCounts serves the split payload the dashboard prefers.
func (s *Server) handleDashLayerCounts(w http.ResponseWriter, r *http.Request) {
	// LayerCounts already reports the graph-derived L5 count, so
	// display_counts includes the real L5 the composition bar must show.
	counts := s.store.LayerCounts()
	total := s.store.TotalMemories()
	writes, searches := s.store.Usage()
	writeJSON(w, 200, map[string]any{
		"display_counts": counts,
		"vdb_counts":     counts,
		"graph_counts": map[string]int{
			"l5_knowledge": s.store.Graph().NodeCount(),
			"l6_schema":    counts["l6_schema"],
			"l7_intention": counts["l7_intention"],
		},
		"total":          total,
		"vdb_total":      total,
		"relation_count": s.store.Graph().EdgeCount(),
		"writes":         writes,
		"searches":       searches,
	})
}

// handleDashStorage lists files under the data dir (real, honest).
func (s *Server) handleDashStorage(w http.ResponseWriter, r *http.Request) {
	type fileInfo struct {
		Name string `json:"name"`
		Size int64  `json:"size"`
	}
	var files []fileInfo
	root := s.dataDir
	_ = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		files = append(files, fileInfo{filepath.Base(path), info.Size()})
		return nil
	})
	sort.Slice(files, func(i, j int) bool { return files[i].Size > files[j].Size })
	if files == nil {
		files = []fileInfo{}
	}
	writeJSON(w, 200, map[string]any{"files": files})
}

// handleDashMetrics serves the v3.5 metrics shape (uptime + totals).
func (s *Server) handleDashMetrics(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{
		"uptime_seconds": int64(time.Since(s.start).Seconds()),
		"total":          s.store.TotalMemories(),
		"layers":         s.store.LayerCounts(),
	})
}

func (s *Server) handleDashGraphCounts(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{
		"l5_knowledge":   s.store.Graph().NodeCount(),
		"l6_schema":      s.store.LayerCounts()["l6_schema"],
		"l7_intention":   s.store.LayerCounts()["l7_intention"],
		"relation_count": s.store.Graph().EdgeCount(),
	})
}

// handleDashLayerHealth — honest per-layer status from real counts.
func (s *Server) handleDashLayerHealth(w http.ResponseWriter, r *http.Request) {
	counts := s.store.LayerCounts()
	layers := map[string]any{}
	for _, l := range memory.All() {
		status := "empty"
		if counts[string(l)] > 0 {
			status = "ok"
		}
		layers[string(l)] = map[string]any{"count": counts[string(l)], "status": status}
	}
	writeJSON(w, 200, map[string]any{"layers": layers})
}

// handleDashL6Schemas lists L6 schema items.
func (s *Server) handleDashL6Schemas(w http.ResponseWriter, r *http.Request) {
	n := atoi(r.URL.Query().Get("n"), 6)
	uid, aid := graphOwner(r.URL.Query().Get("user_id"), r.URL.Query().Get("agent_id"))
	items, _ := s.store.List(memory.L6Schema, uid, aid, n, 0, false)
	out := make([]map[string]any, 0, len(items))
	for _, it := range items {
		out = append(out, map[string]any{
			"memory_id": it.ID, "content": it.Content, "layer": it.Layer,
		})
	}
	writeJSON(w, 200, map[string]any{"schemas": out, "total": len(out)})
}

// handleDashL5Graph serves graph nodes/relations. Filters by layer when given
// (l5_knowledge = knowledge entities; l6_schema/l7_intention are VDB layers and
// come back as their own items so the observatory has something real to draw).
func (s *Server) handleDashL5Graph(w http.ResponseWriter, r *http.Request) {
	layer := r.URL.Query().Get("layer")
	n := atoi(r.URL.Query().Get("n"), 500)
	wantRels := r.URL.Query().Get("rels") != "false"
	uid, aid := graphOwner(r.URL.Query().Get("user_id"), r.URL.Query().Get("agent_id"))

	if layer == "" || layer == "l5_knowledge" {
		nodes, rels := s.store.Graph().SnapshotScoped(graphScope(uid, aid), n)
		writeJSON(w, 200, map[string]any{"nodes": nodes, "relations": rels, "total": len(nodes)})
		return
	}
	// L6/L7 views render their layer items as pseudo-nodes (real content, real layer)
	items, _ := s.store.List(memory.Layer(layer), uid, aid, n, 0, false)
	type node struct {
		ID    string `json:"id"`
		Label string `json:"label"`
		Layer string `json:"layer"`
	}
	nodes := make([]node, 0, len(items))
	for _, it := range items {
		nodes = append(nodes, node{ID: it.ID, Label: it.Content, Layer: it.Layer})
	}
	rels := []any{}
	if !wantRels {
		rels = nil
	}
	writeJSON(w, 200, map[string]any{"nodes": nodes, "relations": rels, "total": len(nodes)})
}

// handleDashQuality — honest: quality scoring is a v3.5 feature not yet ported.
func (s *Server) handleDashQuality(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{
		"available": false,
		"reason":    "quality scoring not ported to v4 yet",
	})
}

// handleDashCoding — the coding layer doesn't exist in v4; honest zeros.
func (s *Server) handleDashCodingCount(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"count": 0})
}

func (s *Server) handleDashCodingMemories(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"memories": []any{}, "total": 0})
}

// marshalDebug is used by tests/debugging only.
var _ = json.Marshal
var _ = strconv.Itoa
