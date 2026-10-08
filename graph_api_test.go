package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// seedOwnedGraph writes the same triple under two owners: alice/a1 and bob/b1.
func seedOwnedGraph(t *testing.T, srv *Server) {
	t.Helper()
	g := srv.store.Graph()
	if err := g.AddEdgeWithSource("alice", "a1", "skyhook", "listens_on", "4471", "ra"); err != nil {
		t.Fatal(err)
	}
	if err := g.AddEdgeWithSource("bob", "b1", "skyhook", "listens_on", "4471", "rb"); err != nil {
		t.Fatal(err)
	}
}

func decodeBody(t *testing.T, w *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", w.Code, w.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestGraphEdgesFiltersByOwner(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnedGraph(t, srv)

	cases := []struct {
		query string
		want  int
	}{
		{"", 2},                           // no filter: everything, as before
		{"?agent_id=all", 2},              // the dashboard's "every agent"
		{"?user_id=alice", 1},             // one owner
		{"?user_id=alice&agent_id=b1", 0}, // both must match
	}
	for _, c := range cases {
		w := httptest.NewRecorder()
		srv.handleGraphEdges(w, httptest.NewRequest("GET", "/api/v1/edges"+c.query, nil))
		body := decodeBody(t, w)
		knowledge, _ := body["knowledge"].([]any)
		if len(knowledge) != c.want {
			t.Errorf("GET /api/v1/edges%s: knowledge = %d, want %d", c.query, len(knowledge), c.want)
		}
		if c.want == 1 {
			e := knowledge[0].(map[string]any)
			if e["user_id"] != "alice" || e["agent_id"] != "a1" {
				t.Errorf("owner fields = %v/%v, want alice/a1", e["user_id"], e["agent_id"])
			}
			if src, _ := e["sources"].([]any); len(src) != 1 || src[0] != "ra" {
				t.Errorf("sources = %v, want [ra]", e["sources"])
			}
		}
	}
}

func TestGraphNeighboursFilterByOwnerInBodyOrQuery(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnedGraph(t, srv)

	do := func(url, body string) map[string]any {
		w := httptest.NewRecorder()
		srv.handleGraph(w, httptest.NewRequest("POST", url, strings.NewReader(body)))
		return decodeBody(t, w)
	}
	// Body filter.
	out := do("/api/v1/graph", `{"node":"skyhook","user_id":"alice"}`)
	if n, _ := out["neighbors"].([]any); len(n) != 1 || n[0].(map[string]any)["label"] != "4471" {
		t.Errorf("alice body neighbours = %v, want one (4471)", out["neighbors"])
	}
	if out["node_count"] != float64(2) || out["edge_count"] != float64(1) {
		t.Errorf("alice counts = %v/%v, want 2/1", out["node_count"], out["edge_count"])
	}
	// Query filter, body without owner.
	out = do("/api/v1/graph?user_id=bob", `{"node":"skyhook"}`)
	if n, _ := out["neighbors"].([]any); len(n) != 1 {
		t.Errorf("bob query neighbours = %v, want one", out["neighbors"])
	}
	// No filter: both owners' neighbours.
	out = do("/api/v1/graph", `{"node":"skyhook"}`)
	if n, _ := out["neighbors"].([]any); len(n) != 2 {
		t.Errorf("unfiltered neighbours = %d, want 2", len(n))
	}
	if out["node_count"] != float64(4) || out["edge_count"] != float64(2) {
		t.Errorf("unfiltered counts = %v/%v, want 4/2", out["node_count"], out["edge_count"])
	}
}

func TestGraphAsOfAndDashL5FilterByOwner(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnedGraph(t, srv)

	w := httptest.NewRecorder()
	srv.handleGraphAsOf(w, httptest.NewRequest("GET", "/api/v1/graph-as-of?user_id=bob", nil))
	out := decodeBody(t, w)
	if rels, _ := out["relations"].([]any); len(rels) != 1 || rels[0].(map[string]any)["user_id"] != "bob" {
		t.Errorf("as-of bob relations = %v, want one bob edge", out["relations"])
	}

	// The dashboard sends agent_id=all for every agent, which must stay unfiltered.
	w = httptest.NewRecorder()
	srv.handleDashL5Graph(w, httptest.NewRequest("GET", "/api/l5/graph?agent_id=all", nil))
	out = decodeBody(t, w)
	if out["total"] != float64(4) {
		t.Errorf("dashboard agent_id=all nodes total = %v, want 4", out["total"])
	}
	if rels, _ := out["relations"].([]any); len(rels) != 2 {
		t.Errorf("dashboard agent_id=all relations = %d, want 2", len(rels))
	}

	w = httptest.NewRecorder()
	srv.handleDashL5Graph(w, httptest.NewRequest("GET", "/api/l5/graph?user_id=alice", nil))
	out = decodeBody(t, w)
	if rels, _ := out["relations"].([]any); len(rels) != 1 {
		t.Errorf("dashboard user_id=alice relations = %d, want 1", len(rels))
	}
}

func TestStarmapKnowledgeFiltersByOwner(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnedGraph(t, srv)

	w := httptest.NewRecorder()
	srv.handleStarmapGraph(w, httptest.NewRequest("GET", "/api/v1/learning/graph?user_id=alice", nil))
	out := decodeBody(t, w)
	knowledge := 0
	for _, e := range out["edges"].([]any) {
		if e.(map[string]any)["type"] == "knowledge" {
			knowledge++
		}
	}
	if knowledge != 1 {
		t.Errorf("starmap knowledge edges for alice = %d, want 1", knowledge)
	}
}
