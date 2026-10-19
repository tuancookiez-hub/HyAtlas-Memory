package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// A backlog must be extracted oldest first, and a page of already-extracted
// newer rows must not hide the older unextracted ones.
func TestReprocessWalksUnextractedOldestFirst(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	var calls atomic.Int32
	mock := mockExtractionServer(t, &calls)
	defer mock.Close()
	srv.llm = NewLLMClient(mock.URL, "test-key", "mock-model")

	// Two unextracted rows, oldest of all, then four newer rows already extracted.
	for i, id := range []string{"old-1", "old-2", "new-1", "new-2", "new-3", "new-4"} {
		ts := fmt.Sprintf("2026-10-06T00:00:%02dZ", i+1)
		if err := srv.store.Add(memory.L2Raw, id, "text "+id, map[string]string{
			"user_id": "u", "agent_id": "a", "ts": ts,
		}); err != nil {
			t.Fatalf("add %s: %v", id, err)
		}
		if strings.HasPrefix(id, "new-") {
			if err := srv.store.SetExtracted(id, true); err != nil {
				t.Fatal(err)
			}
		}
	}

	post := func(body string) map[string]any {
		w := httptest.NewRecorder()
		srv.handleReprocess(w, httptest.NewRequest("POST", "/api/v1/reprocess", strings.NewReader(body)))
		if w.Code != http.StatusOK {
			t.Fatalf("reprocess: status %d", w.Code)
		}
		var res map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &res); err != nil {
			t.Fatal(err)
		}
		return res
	}
	extracted := func(id string) bool {
		d := srv.store.GetMany([]string{id})
		return len(d) == 1 && d[0].Extracted
	}

	// The first page of one takes the oldest unextracted row, not a newer one.
	if res := post(`{"max":1}`); res["reprocessed"] != float64(1) {
		t.Fatalf("first pass reprocessed = %v, want 1", res["reprocessed"])
	}
	if !extracted("old-1") {
		t.Errorf("old-1 (oldest unextracted) was not extracted first")
	}
	if extracted("old-2") {
		t.Errorf("old-2 extracted before its turn")
	}

	// The next page takes the remaining backlog row, even though four extracted
	// rows are newer than it.
	if res := post(`{"max":1}`); res["reprocessed"] != float64(1) {
		t.Fatalf("second pass reprocessed = %v, want 1", res["reprocessed"])
	}
	if !extracted("old-2") {
		t.Errorf("old-2 not extracted on the second pass")
	}
	if res := post(`{"max":5}`); res["reprocessed"] != float64(0) {
		t.Errorf("nothing left to extract, reprocessed = %v", res["reprocessed"])
	}
}
