package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

// mockExtractionServer serves the OpenAI chat-completions shape: the first
// call answers with prose (the conversational-reply failure mode), every
// later call answers with a valid extraction JSON.
func mockExtractionServer(t *testing.T, calls *atomic.Int32) *httptest.Server {
	t.Helper()
	valid := `{"facts":[{"data":"Mock retry fact","layer":"technical_lesson"}],"summary":{"text":"s"},"knowledge":[],"schemas":[],"intention":null}`
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := calls.Add(1)
		content := "1. My parents. Thanks for sharing — that says a lot about you!"
		if n > 1 {
			content = valid
		}
		out, _ := json.Marshal(map[string]any{
			"choices": []map[string]any{{"message": map[string]string{"content": content}}},
		})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	}))
}

// TestCompleteRetriesOnProseReply pins the reinforced-retry contract: a
// conversational first reply must not fail the extraction — one retry with
// the format-error reminder recovers it.
func TestCompleteRetriesOnProseReply(t *testing.T) {
	var calls atomic.Int32
	srv := mockExtractionServer(t, &calls)
	defer srv.Close()

	client := NewLLMClient(srv.URL, "test-key", "mock-model")
	ex, err := client.Complete(context.Background(), "2 people i admire: my parents and my mentor.")
	if err != nil {
		t.Fatalf("Complete after retry: %v", err)
	}
	if got := calls.Load(); got != 2 {
		t.Errorf("LLM calls: want 2 (prose + retry), got %d", got)
	}
	if len(ex.Facts) != 1 || ex.Facts[0].Data != "Mock retry fact" {
		t.Errorf("extraction facts: %+v", ex.Facts)
	}
}

// TestCompleteFailsAfterRetry covers the give-up path: persistent prose
// replies must surface an error, not a silent empty extraction.
func TestCompleteFailsAfterRetry(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		out, _ := json.Marshal(map[string]any{
			"choices": []map[string]any{{"message": map[string]string{"content": "just chatting, no JSON here"}}},
		})
		_, _ = w.Write(out)
	}))
	defer srv.Close()

	client := NewLLMClient(srv.URL, "test-key", "mock-model")
	if _, err := client.Complete(context.Background(), "input"); err == nil {
		t.Fatal("want error after both attempts return prose, got nil")
	}
}
