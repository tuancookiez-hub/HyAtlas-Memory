package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
)

func newTestServer(t *testing.T, model, base string) *Server {
	t.Helper()
	dir := t.TempDir()
	em := NewLocalEmbedder(384)
	store, err := NewMemoryStore(ctxForTest(), dir, em, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	t.Cleanup(func() { store.Close() })
	return &Server{
		store:    store,
		llmModel: model,
		llmBase:  base,
		start:    timeForTest(),
	}
}

func TestHandleStatusReportsLiveLLMModel(t *testing.T) {
	srv := newTestServer(t,
		"poolside/laguna-s-2.1:free",
		"https://inference-api.nousresearch.com/v1")
	req := httptest.NewRequest("GET", "/api/v1/status", nil)
	w := httptest.NewRecorder()
	srv.handleStatus(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status code: want 200, got %d", w.Code)
	}
	var body map[string]any
	if err := json.NewDecoder(w.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if got, _ := body["llm_model"].(string); got != "poolside/laguna-s-2.1:free" {
		t.Errorf("llm_model: want poolside/laguna-s-2.1:free, got %q", got)
	}
	if got, _ := body["llm_base"].(string); got != "https://inference-api.nousresearch.com/v1" {
		t.Errorf("llm_base: got %q", got)
	}
}

func TestHandleDashInfoReportsLiveLLMModel(t *testing.T) {
	srv := newTestServer(t,
		"inclusionai/ling-3.0-flash-fin:free",
		"https://inference-api.nousresearch.com/v1")
	req := httptest.NewRequest("GET", "/api/info", nil)
	w := httptest.NewRecorder()
	srv.handleDashInfo(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status code: want 200, got %d", w.Code)
	}
	var body map[string]any
	if err := json.NewDecoder(w.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if got, _ := body["llm_model"].(string); got != "inclusionai/ling-3.0-flash-fin:free" {
		t.Errorf("llm_model: got %q", got)
	}
	if got, _ := body["llm_base"].(string); got != "https://inference-api.nousresearch.com/v1" {
		t.Errorf("llm_base: got %q", got)
	}
}

// TestPromoteExtractionThreadsSource asserts that L5 edges are anchored to the
// L2 memory id passed in. This is the evidence-citation contract.
func TestPromoteExtractionThreadsSource(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	ex := &Extraction{
		Facts: []Fact{{Data: "Apple is in Cupertino", Layer: "project_state"}},
		Knowledge: []Relation{
			{From: "Apple", Relation: "based_in", To: "Cupertino"},
		},
	}
	sourceID := "mem-test-source-abc"
	promoteExtraction(srv.store, ex, "default", "default", sourceID)

	_, rels := srv.store.Graph().Snapshot(50)
	if len(rels) == 0 {
		t.Fatal("expected L5 edge to be created, got 0")
	}
	// Find our edge (we can't be sure of order with other tests writing here)
	found := false
	for _, r := range rels {
		if r.Source == sourceID {
			found = true
			if r.RecordedAt == 0 {
				t.Errorf("RecordedAt should be set, got 0")
			}
			if r.Relation != "based_in" {
				t.Errorf("relation: want based_in, got %q", r.Relation)
			}
		}
	}
	if !found {
		t.Errorf("no L5 edge with source %q found in %d edges", sourceID, len(rels))
	}
}
