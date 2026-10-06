package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/graph"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// L5 knowledge lives in the graph store, never in chromem. Every endpoint
// that reports layer counts must agree with /api/v1/status — the old
// per-handler overrides left /api/v1/list and /api/v1/metrics reporting
// l5_knowledge: 0 while status reported the real graph node count.
func TestLayerCountsL5IsGraphDerived(t *testing.T) {
	srv := newTestServer(t, "test-model", "http://127.0.0.1:1")
	g := srv.store.Graph()
	for _, label := range []string{"entity-a", "entity-b", "entity-c"} {
		if _, err := g.UpsertNode(graph.Node{Label: label, Type: "concept"}); err != nil {
			t.Fatalf("upsert node %s: %v", label, err)
		}
	}
	if got := srv.store.LayerCounts()[string(memory.L5Knowledge)]; got != 3 {
		t.Fatalf("LayerCounts l5 = %d, want 3 (graph node count)", got)
	}
}

func TestAllCountEndpointsAgreeOnL5(t *testing.T) {
	srv := newTestServer(t, "test-model", "http://127.0.0.1:1")
	if _, err := srv.store.Graph().UpsertNode(graph.Node{Label: "entity-x", Type: "concept"}); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	if err := srv.store.Add(memory.L3Fact, "doc-l5-agree", "a fact", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": "2026-10-07T00:00:00Z",
	}); err != nil {
		t.Fatalf("add: %v", err)
	}

	call := func(name string, h func(http.ResponseWriter, *http.Request), query string) map[string]any {
		t.Helper()
		rec := httptest.NewRecorder()
		h(rec, httptest.NewRequest(http.MethodGet, "/"+name+query, nil))
		if rec.Code != 200 {
			t.Fatalf("%s -> %d: %s", name, rec.Code, rec.Body.String())
		}
		var out map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("%s: bad json: %v", name, err)
		}
		return out
	}

	l5 := func(m map[string]any, path string) float64 {
		t.Helper()
		layers, ok := m["layers"].(map[string]any)
		if !ok {
			t.Fatalf("%s: no layers object", path)
		}
		v, ok := layers["l5_knowledge"].(float64)
		if !ok {
			t.Fatalf("%s: l5_knowledge missing or not numeric", path)
		}
		return v
	}

	handlers := []struct {
		name string
		fn   func(http.ResponseWriter, *http.Request)
	}{
		{"status", srv.handleStatus},
		{"list", srv.handleList},
		{"metrics", srv.handleMetrics},
	}
	responses := map[string]map[string]any{}
	for _, h := range handlers {
		responses[h.name] = call(h.name, h.fn, "?limit=1")
	}
	want := l5(responses["status"], "status")
	if want != 1 {
		t.Fatalf("status l5 = %v, want 1", want)
	}
	for _, h := range handlers {
		if got := l5(responses[h.name], h.name); got != want {
			t.Errorf("%s reports l5_knowledge=%v, status reports %v — endpoints disagree", h.name, got, want)
		}
		// graph_nodes must equal the L5 count wherever it appears
		if gn, ok := responses[h.name]["graph_nodes"].(float64); ok && gn != want {
			t.Errorf("%s: graph_nodes=%v != l5=%v", h.name, gn, want)
		}
	}
}

func TestDashLayerCountsMatchesStatus(t *testing.T) {
	srv := newTestServer(t, "test-model", "http://127.0.0.1:1")
	if _, err := srv.store.Graph().UpsertNode(graph.Node{Label: "entity-y", Type: "concept"}); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	rec := httptest.NewRecorder()
	srv.handleDashLayerCounts(rec, httptest.NewRequest(http.MethodGet, "/api/layer-counts", nil))
	if rec.Code != 200 {
		t.Fatalf("layer-counts -> %d", rec.Code)
	}
	var out struct {
		DisplayCounts map[string]int `json:"display_counts"`
		GraphCounts   map[string]int `json:"graph_counts"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if out.DisplayCounts["l5_knowledge"] != 1 {
		t.Errorf("display_counts l5 = %d, want 1", out.DisplayCounts["l5_knowledge"])
	}
	if out.GraphCounts["l5_knowledge"] != 1 {
		t.Errorf("graph_counts l5 = %d, want 1", out.GraphCounts["l5_knowledge"])
	}
}
