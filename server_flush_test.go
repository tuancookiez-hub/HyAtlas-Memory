package main

import (
	"net/http/httptest"
	"os"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// TestFlushRouteWritesIndex checks that POST /api/v1/flush writes the buffered doc
// index at once, which is what a launcher relies on before a hard kill, and that
// other methods are refused.
func TestFlushRouteWritesIndex(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	if err := srv.store.Add(memory.L3Fact, newID(), "a fact", map[string]string{"user_id": "u"}); err != nil {
		t.Fatal(err)
	}
	_ = os.Remove(srv.store.indexPath)

	w := httptest.NewRecorder()
	srv.handleFlush(w, httptest.NewRequest("POST", "/api/v1/flush", nil))
	if w.Code != 200 {
		t.Fatalf("POST flush = %d, want 200: %s", w.Code, w.Body.String())
	}
	if _, err := os.Stat(srv.store.indexPath); err != nil {
		t.Fatalf("index not written by flush: %v", err)
	}

	w = httptest.NewRecorder()
	srv.handleFlush(w, httptest.NewRequest("GET", "/api/v1/flush", nil))
	if w.Code != 405 {
		t.Errorf("GET flush = %d, want 405", w.Code)
	}
}
