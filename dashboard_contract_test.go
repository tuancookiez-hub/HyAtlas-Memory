package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// These tests pin the response fields the embedded dashboard (dashboard/dist)
// reads. If a handler renames or drops one, the dashboard silently shows
// dashes or an empty view, so the contract is checked here instead.

// dashGet runs one handler against a local request and decodes the JSON body.
func dashGet(t *testing.T, h http.HandlerFunc, method, target, body string) map[string]any {
	t.Helper()
	var rd *strings.Reader
	if body != "" {
		rd = strings.NewReader(body)
	}
	var req *http.Request
	if rd != nil {
		req = localRequest(method, target, rd)
		req.Header.Set("Content-Type", "application/json")
	} else {
		req = localRequest(method, target, nil)
	}
	w := httptest.NewRecorder()
	h(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("%s %s: status = %d, body = %s", method, target, w.Code, w.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatalf("%s %s: bad JSON: %v", method, target, err)
	}
	return out
}

func requireKeys(t *testing.T, where string, m map[string]any, keys ...string) {
	t.Helper()
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			t.Errorf("%s: missing field %q (dashboard reads it)", where, k)
		}
	}
}

// firstItem returns the first object of the array at m[key].
func firstItem(t *testing.T, where string, m map[string]any, key string) map[string]any {
	t.Helper()
	arr, ok := m[key].([]any)
	if !ok || len(arr) == 0 {
		t.Fatalf("%s: %q is not a non-empty array", where, key)
	}
	item, ok := arr[0].(map[string]any)
	if !ok {
		t.Fatalf("%s: %q[0] is not an object", where, key)
	}
	return item
}

func TestDashboardL5GraphContract(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnedGraph(t, srv)

	out := dashGet(t, srv.handleDashL5Graph, "GET", "/api/l5/graph?agent_id=all", "")
	requireKeys(t, "/api/l5/graph", out, "nodes", "relations", "total")
	node := firstItem(t, "/api/l5/graph", out, "nodes")
	requireKeys(t, "/api/l5/graph nodes[0]", node, "id", "label", "type", "user_id", "agent_id")
	rel := firstItem(t, "/api/l5/graph", out, "relations")
	requireKeys(t, "/api/l5/graph relations[0]", rel, "from", "to", "relation", "weight", "sources")
	if _, ok := rel["sources"].([]any); !ok {
		t.Errorf("relations[0].sources is not an array")
	}

	// L6/L7 pseudo-nodes: the dashboard maps these to memory items.
	if err := srv.store.Add(memory.L6Schema, "schema1", "schema text", map[string]string{"layer": "l6_schema", "ts": "2026-01-02T03:04:05Z"}); err != nil {
		t.Fatal(err)
	}
	l6 := dashGet(t, srv.handleDashL5Graph, "GET", "/api/l5/graph?layer=l6_schema&n=500&rels=false", "")
	requireKeys(t, "/api/l5/graph?layer=l6_schema", l6, "nodes", "total")
	requireKeys(t, "/api/l5/graph?layer=l6_schema nodes[0]", firstItem(t, "l6_schema", l6, "nodes"), "id", "label", "layer")
}

func TestDashboardMemoriesContract(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	if err := srv.store.Add(memory.L2Raw, "mem1", "dashboard contract memory", map[string]string{
		"user_id": "u1", "agent_id": "default", "ts": "2026-01-02T03:04:05Z", "layer": "l2_raw",
	}); err != nil {
		t.Fatal(err)
	}
	out := dashGet(t, srv.handleDashMemories, "GET", "/api/memories?limit=100&agent_id=default", "")
	requireKeys(t, "/api/memories", out, "total", "memories")
	buckets, ok := out["memories"].(map[string]any)
	if !ok {
		t.Fatalf("/api/memories: memories is not an object of buckets")
	}
	requireKeys(t, "/api/memories memories", buckets, "profile", "proactive", "normal")
	item := firstItem(t, "/api/memories", buckets, "normal")
	requireKeys(t, "/api/memories item", item, "memory_id", "content", "layer", "user_id", "agent_id", "gmt_created")
	if _, ok := item["gmt_created"].(float64); !ok {
		t.Errorf("/api/memories item gmt_created is not a number")
	}
}

func TestDashboardCountsContract(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnedGraph(t, srv)

	st := dashGet(t, srv.handleStatus, "GET", "/api/v1/status", "")
	requireKeys(t, "/api/v1/status", st, "status", "vdb", "embed", "llm", "vdb_provider",
		"vdb_collection", "vdb_points", "embed_dims", "writes", "searches", "layers")

	lc := dashGet(t, srv.handleDashLayerCounts, "GET", "/api/layer-counts?agent_id=all", "")
	requireKeys(t, "/api/layer-counts", lc, "display_counts", "vdb_counts", "graph_counts",
		"total", "vdb_total", "relation_count", "writes", "searches")
	requireKeys(t, "/api/layer-counts graph_counts", lc["graph_counts"].(map[string]any),
		"l5_knowledge", "l6_schema", "l7_intention")

	gc := dashGet(t, srv.handleDashGraphCounts, "GET", "/api/graph-counts", "")
	requireKeys(t, "/api/graph-counts", gc, "l5_knowledge", "l6_schema", "l7_intention", "relation_count")

	cc := dashGet(t, srv.handleDashCodingCount, "GET", "/api/coding-count", "")
	requireKeys(t, "/api/coding-count", cc, "count")

	st2 := dashGet(t, srv.handleDashStorage, "GET", "/api/storage", "")
	requireKeys(t, "/api/storage", st2, "files")

	mt := dashGet(t, srv.handleDashMetrics, "GET", "/api/metrics?minutes=10080", "")
	requireKeys(t, "/api/metrics", mt, "uptime_seconds", "total", "layers")

	lh := dashGet(t, srv.handleDashLayerHealth, "GET", "/api/layer-health", "")
	requireKeys(t, "/api/layer-health", lh, "layers")

	l6 := dashGet(t, srv.handleDashL6Schemas, "GET", "/api/l6-schemas?n=6", "")
	requireKeys(t, "/api/l6-schemas", l6, "schemas", "total")
}

func TestDashboardSearchAndStarmapContract(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	if err := srv.store.Add(memory.L2Raw, "mem2", "vector database notes", map[string]string{
		"user_id": "u1", "agent_id": "default", "ts": "2026-01-02T03:04:05Z", "layer": "l2_raw",
	}); err != nil {
		t.Fatal(err)
	}

	// The dashboard POSTs to /api/v1/search (not /api/search).
	out := dashGet(t, srv.handleSearch, "POST", "/api/v1/search",
		`{"query":"vector","limit":20,"user_ids":[],"agent_ids":[]}`)
	buckets, ok := out["memories"].(map[string]any)
	if !ok {
		t.Fatalf("/api/v1/search: memories is not an object")
	}
	requireKeys(t, "/api/v1/search memories", buckets, "profile", "proactive", "normal")
	item := firstItem(t, "/api/v1/search", buckets, "normal")
	requireKeys(t, "/api/v1/search item", item, "memory_id", "content", "score", "layer", "gmt_created")

	// The starmap (js/starmap.js) reads nodes, edges and memory cards.
	sm := dashGet(t, srv.handleStarmapGraph, "GET", "/api/v1/learning/graph?n=5000", "")
	requireKeys(t, "/api/v1/learning/graph", sm, "nodes", "edges", "memory", "stats")
	node := firstItem(t, "/api/v1/learning/graph", sm, "nodes")
	requireKeys(t, "/api/v1/learning/graph nodes[0]", node, "id", "label", "kind", "timestamp",
		"category", "use_count", "state", "created_by", "pinned")
	card := firstItem(t, "/api/v1/learning/graph", sm, "memory")
	requireKeys(t, "/api/v1/learning/graph memory[0]", card, "source", "timestamp", "title", "body")
}
