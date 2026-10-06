package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// TestSetExtractedSurvivesRestart guards the doc-index persistence contract:
// SetExtracted must survive a store reopen. Before loadIndex existed, startup
// always rebuilt the index from chromem metadata — which never carries the
// extracted flag — silently resetting every doc to unextracted.
func TestSetExtractedSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	em := NewLocalEmbedder(384)
	graphPath := filepath.Join(dir, "graph.json")

	s1, err := NewMemoryStore(context.Background(), dir, em, graphPath)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	if err := s1.Add(memory.L2Raw, "doc-extract-1", "raw memory text", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": "2026-10-06T00:00:00Z",
	}); err != nil {
		t.Fatalf("add: %v", err)
	}
	if err := s1.SetExtracted("doc-extract-1", true); err != nil {
		t.Fatalf("set extracted: %v", err)
	}
	s1.Close()

	s2, err := NewMemoryStore(context.Background(), dir, em, graphPath)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	t.Cleanup(s2.Close)

	docs, _ := s2.List(memory.L2Raw, "", "", 10, 0, false)
	if len(docs) != 1 {
		t.Fatalf("reopened store: want 1 raw doc, got %d", len(docs))
	}
	if !docs[0].Extracted {
		t.Error("extracted flag did not survive restart: want true, got false")
	}
}

// TestLoadIndexFallsBackToRebuild checks the size-mismatch guard: a stale or
// corrupt doc_index.json must trigger a chromem rebuild, not serve a wrong index.
func TestLoadIndexFallsBackToRebuild(t *testing.T) {
	dir := t.TempDir()
	em := NewLocalEmbedder(384)
	graphPath := filepath.Join(dir, "graph.json")

	s1, err := NewMemoryStore(context.Background(), dir, em, graphPath)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	for i, id := range []string{"doc-a", "doc-b", "doc-c"} {
		if err := s1.Add(memory.L2Raw, id, "text "+id, map[string]string{
			"user_id": "u", "agent_id": "a", "ts": "2026-10-0" + string(rune('1'+i)) + "T00:00:00Z",
		}); err != nil {
			t.Fatalf("add %s: %v", id, err)
		}
	}
	s1.Close()

	// Corrupt the index: keep only one of three docs — size mismatch forces rebuild.
	if err := os.WriteFile(filepath.Join(dir, "doc_index.json"),
		[]byte(`{"doc-a":{"id":"doc-a","layer":"l2_raw","content":"stale"}}`), 0o644); err != nil {
		t.Fatalf("corrupt index: %v", err)
	}

	s2, err := NewMemoryStore(context.Background(), dir, em, graphPath)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	t.Cleanup(s2.Close)

	docs, total := s2.List(memory.L2Raw, "", "", 10, 0, false)
	if total != 3 || len(docs) != 3 {
		t.Errorf("stale index not rebuilt: want 3 docs, got %d (total %d)", len(docs), total)
	}
}

// TestDeleteAllGuard covers the unscoped-wipe guard and both scoping styles.
func TestDeleteAllGuard(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	for _, u := range []string{"keep-user", "drop-user"} {
		if err := srv.store.Add(memory.L2Raw, "doc-"+u, "text", map[string]string{
			"user_id": u, "agent_id": "a", "ts": "2026-10-06T00:00:00Z",
		}); err != nil {
			t.Fatalf("add: %v", err)
		}
	}

	// 1. Unscoped call → refused with 400, nothing deleted.
	w := httptest.NewRecorder()
	srv.handleDelete(w, httptest.NewRequest("POST", "/api/v1/delete_all", nil))
	if w.Code != http.StatusBadRequest {
		t.Errorf("unscoped delete: want 400, got %d", w.Code)
	}
	var refused map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &refused); err != nil {
		t.Fatal(err)
	}
	if refused["deleted_count"] != float64(0) {
		t.Errorf("unscoped delete deleted %v docs", refused["deleted_count"])
	}
	if srv.store.TotalMemories() != 2 {
		t.Errorf("store changed by refused delete: %d docs", srv.store.TotalMemories())
	}

	// 2. Scoped via JSON body (plugin client style) → deletes only that scope.
	w = httptest.NewRecorder()
	srv.handleDelete(w, httptest.NewRequest("POST", "/api/v1/delete_all",
		strings.NewReader(`{"user_id":"drop-user"}`)))
	if w.Code != http.StatusOK {
		t.Fatalf("body-scoped delete: want 200, got %d", w.Code)
	}
	var bodyRes map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &bodyRes); err != nil {
		t.Fatal(err)
	}
	if bodyRes["deleted_count"] != float64(1) {
		t.Errorf("body-scoped delete: want 1 deleted, got %v", bodyRes["deleted_count"])
	}

	// 3. Scoped via query param (curl style) → deletes that scope.
	w = httptest.NewRecorder()
	srv.handleDelete(w, httptest.NewRequest("POST", "/api/v1/delete_all?user_id=keep-user", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("query-scoped delete: want 200, got %d", w.Code)
	}
	if srv.store.TotalMemories() != 0 {
		t.Errorf("after both scoped deletes: want 0 docs, got %d", srv.store.TotalMemories())
	}

	// 4. Explicit wipe-all opt-in on an empty store → allowed, 200.
	w = httptest.NewRecorder()
	srv.handleDelete(w, httptest.NewRequest("POST", "/api/v1/delete_all?confirm=wipe-all", nil))
	if w.Code != http.StatusOK {
		t.Errorf("confirm=wipe-all: want 200, got %d", w.Code)
	}
}

// TestExtractErrConcurrent exercises the mutex around lastExtractErr. Run with
// -race to catch regressions: extraction goroutines write while status reads.
func TestExtractErrConcurrent(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(2)
		go func(n int) {
			defer wg.Done()
			srv.setExtractErr("err")
			srv.setExtractErr("")
		}(i)
		go func() {
			defer wg.Done()
			_ = srv.extractErr()
			w := httptest.NewRecorder()
			srv.handleStatus(w, httptest.NewRequest("GET", "/api/v1/status", nil))
		}()
	}
	wg.Wait()
}

// TestReprocessByIds covers the backfill contract: explicit ids are processed
// even when the extracted flag says otherwise (caller already chose the rows),
// and the response reports reprocessed/failed/skipped.
func TestReprocessByIds(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	var calls atomic.Int32
	mock := mockExtractionServer(t, &calls)
	defer mock.Close()
	srv.llm = NewLLMClient(mock.URL, "test-key", "mock-model")

	for _, id := range []string{"raw-1", "raw-2"} {
		if err := srv.store.Add(memory.L2Raw, id, "text "+id, map[string]string{
			"user_id": "u", "agent_id": "a", "ts": "2026-10-06T00:00:00Z",
		}); err != nil {
			t.Fatalf("add: %v", err)
		}
	}
	// Mark one extracted — with explicit ids it must still be reprocessed.
	if err := srv.store.SetExtracted("raw-1", true); err != nil {
		t.Fatalf("set extracted: %v", err)
	}

	w := httptest.NewRecorder()
	srv.handleReprocess(w, httptest.NewRequest("POST", "/api/v1/reprocess",
		strings.NewReader(`{"ids":["raw-1","raw-2"]}`)))
	if w.Code != http.StatusOK {
		t.Fatalf("reprocess: want 200, got %d", w.Code)
	}
	var res map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if res["reprocessed"] != float64(2) {
		t.Errorf("reprocessed: want 2 (ids bypass the extracted skip), got %v", res["reprocessed"])
	}
	if res["skipped"] != float64(0) {
		t.Errorf("skipped: want 0 for explicit ids, got %v", res["skipped"])
	}
	// Promoted facts landed in L3.
	docs, _ := srv.store.List(memory.L3Fact, "", "", 10, 0, false)
	if len(docs) != 2 {
		t.Errorf("l3 facts after reprocess: want 2, got %d", len(docs))
	}
	// And the rows are now flagged extracted.
	for _, id := range []string{"raw-1", "raw-2"} {
		if d := srv.store.GetMany([]string{id}); len(d) != 1 || !d[0].Extracted {
			t.Errorf("%s: extracted flag not set after reprocess", id)
		}
	}
}
