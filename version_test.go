package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// The Desktop pane and dashboard badge must show the REAL running version.
// Both read it from /api/v1/status, so the endpoint must always carry it and
// it must match the single canonical const — no hardcoded "v4" fallback drift.
func TestStatusReportsRealVersion(t *testing.T) {
	srv := newTestServer(t, "test-model", "http://127.0.0.1:1")
	rec := httptest.NewRecorder()
	srv.handleStatus(rec, httptest.NewRequest(http.MethodGet, "/api/v1/status", nil))
	if rec.Code != 200 {
		t.Fatalf("status -> %d: %s", rec.Code, rec.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	got, ok := out["version"].(string)
	if !ok || got == "" {
		t.Fatalf("status.version missing or empty: %#v", out["version"])
	}
	if got != Version {
		t.Errorf("status.version = %q, want canonical Version %q", got, Version)
	}
	if !strings.HasPrefix(got, "4.") {
		t.Errorf("status.version = %q, want a v4 major (got something unexpected)", got)
	}
}

// The dashboard info endpoint must report the SAME version from the SAME const.
func TestDashInfoReportsRealVersion(t *testing.T) {
	srv := newTestServer(t, "test-model", "http://127.0.0.1:1")
	rec := httptest.NewRecorder()
	srv.handleDashInfo(rec, httptest.NewRequest(http.MethodGet, "/api/info", nil))
	if rec.Code != 200 {
		t.Fatalf("info -> %d", rec.Code)
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	if got := out["version"]; got != Version {
		t.Errorf("info.version = %v, want %q", got, Version)
	}
}

// status carries the full wire contract the pane reads: version + usage +
// layers + graph counts must all be present and consistently typed.
func TestStatusWireContract(t *testing.T) {
	srv := newTestServer(t, "test-model", "http://127.0.0.1:1")
	if err := srv.store.Add(memory.L3Fact, "doc-wire", "a fact", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": "2026-10-07T00:00:00Z",
	}); err != nil {
		t.Fatalf("add: %v", err)
	}
	rec := httptest.NewRecorder()
	srv.handleStatus(rec, httptest.NewRequest(http.MethodGet, "/api/v1/status", nil))
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("bad json: %v", err)
	}
	for _, k := range []string{"status", "version", "vdb", "embed", "llm",
		"llm_model", "llm_base", "vdb_provider", "vdb_collection", "vdb_points",
		"embed_dims", "write_pipeline", "writes", "searches", "layers",
		"graph_nodes", "graph_edges"} {
		if _, ok := out[k]; !ok {
			t.Errorf("status payload missing key %q", k)
		}
	}
	if layers, ok := out["layers"].(map[string]any); !ok {
		t.Errorf("layers is not an object")
	} else if _, ok := layers["l3_fact"]; !ok {
		t.Errorf("layers missing l3_fact")
	}
}
