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
// System1 (the per-turn pass) owns L3/L4/L7 and must NOT touch L5/L6: those are
// System2 products, synthesised across many memories by the consolidation pass.
// A knowledge relation worth keeping is one corroborated by more than a single
// turn, and a schema is a *recurring* pattern — neither is observable from one
// turn. This test pins both halves of that contract: provenance is threaded onto
// the fact, and no graph edge or schema row is written here.
func TestPromoteExtractionThreadsSource(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	ex := &Extraction{
		Facts: []Fact{{Data: "Apple is in Cupertino", Layer: "project_state"}},
		// Even when the model volunteers these, the per-turn pass must ignore
		// them: the prompt no longer requests them and System2 owns the layers.
		Knowledge: []Relation{{From: "Apple", Relation: "based_in", To: "Cupertino"}},
		Schemas:   []Schema{{Pattern: "companies have a HQ", Context: "geo facts"}},
	}
	sourceID := "mem-test-source-abc"
	promoteExtraction(srv.store, ex, "default", "default", sourceID)

	// No L5 edge from the per-turn pass.
	if _, rels := srv.store.Graph().Snapshot(50); len(rels) != 0 {
		t.Errorf("per-turn pass wrote %d L5 edge(s); L5 belongs to the slow path", len(rels))
	}
	// No L6 schema row either.
	if _, n := srv.store.List("l6_schema", "", "", 1, 0, false); n != 0 {
		t.Errorf("per-turn pass wrote %d L6 schema(s); L6 belongs to the slow path", n)
	}

	// The L3 fact must carry its L2 source so the slow path can cite it and raw
	// decay can protect the row a live claim still depends on.
	_, total := srv.store.List("l3_fact", "", "", 1, 0, false)
	rows, _ := srv.store.List("l3_fact", "", "", total, 0, false)
	var found bool
	for _, d := range rows {
		if d.Content != "Apple is in Cupertino" {
			continue
		}
		found = true
		if d.Meta["source_id"] != sourceID {
			t.Errorf("L3 source_id = %q, want %q (provenance lost)", d.Meta["source_id"], sourceID)
		}
	}
	if !found {
		t.Error("the L3 fact was not stored")
	}
}

// A preference fact mirrors into L1 Profile, and that mirror carries provenance
// too — otherwise the profile channel cannot be traced back to a conversation.
func TestPromoteExtractionMirrorsPreferencesToProfile(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	sourceID := "mem-pref-source"
	promoteExtraction(srv.store, &Extraction{
		Facts: []Fact{{Data: "prefers tabs over spaces", Layer: "user_preferences"}},
	}, "default", "default", sourceID)

	_, total := srv.store.List("l1_profile", "", "", 1, 0, false)
	rows, _ := srv.store.List("l1_profile", "", "", total, 0, false)
	if total != 1 {
		t.Fatalf("L1 profile count = %d, want 1", total)
	}
	if rows[0].Meta["source_id"] != sourceID {
		t.Errorf("L1 source_id = %q, want %q", rows[0].Meta["source_id"], sourceID)
	}
}

// Summary and intention are System1 products and must still be written.
func TestPromoteExtractionWritesSummaryAndIntention(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	promoteExtraction(srv.store, &Extraction{
		Summary:   &Summary{Text: "configured the service port"},
		Intention: &Intention{Goal: "ship the listener"},
	}, "default", "default", "mem-si-source")

	if _, n := srv.store.List("l4_summary", "", "", 1, 0, false); n != 1 {
		t.Errorf("L4 summary count = %d, want 1", n)
	}
	if _, n := srv.store.List("l7_intention", "", "", 1, 0, false); n != 1 {
		t.Errorf("L7 intention count = %d, want 1", n)
	}
}
