package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
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

// The shared HTTP client must not carry a global Timeout. One cap silently
// becomes the real bound for every caller, and the two callers here need
// different ones: extraction is bounded at extractTimeout, a consolidation pass
// at consolidateTimeout. A 180s client cap once made the 600s pass bound
// unreachable, so a real pass against a slow endpoint died with "Client.Timeout
// exceeded while awaiting headers" and no consolidation ever completed.
func TestLLMClientHasNoGlobalTimeout(t *testing.T) {
	c := NewLLMClient("http://example.invalid/v1", "k", "m")
	if c.Client == nil {
		t.Fatal("NewLLMClient returned no HTTP client")
	}
	if c.Client.Timeout != 0 {
		t.Errorf("client Timeout = %v, want 0: deadlines belong to each caller's context, "+
			"because a global cap silently overrides a longer bound", c.Client.Timeout)
	}
}

// mockReasoningServer answers with the shape a reasoning model produces when it
// puts everything in reasoning_content and leaves content empty.
func mockReasoningServer(t *testing.T, content, reasoning string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		out, _ := json.Marshal(map[string]any{
			"choices": []map[string]any{{
				"message": map[string]string{"content": content, "reasoning_content": reasoning},
			}},
		})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	}))
}

// A reasoning model may leave `content` empty and put the text in
// `reasoning_content`. Dropping that fallback turned every such reply into an
// empty string, which surfaced as "consolidation parse failed (raw )" — a parse
// error with nothing to show. The v3.5 floor captured the field; the Go rewrite
// must too.
func TestCompleteFallsBackToReasoningContent(t *testing.T) {
	payload := `{"facts":[{"data":"Reasoning-only fact","layer":"technical_lesson"}],"summary":{"text":"s"},"intention":null}`
	srv := mockReasoningServer(t, "", payload)
	defer srv.Close()

	c := NewLLMClient(srv.URL, "k", "m")
	ex, err := c.Complete(context.Background(), "some input")
	if err != nil {
		t.Fatalf("Complete with reasoning-only content: %v", err)
	}
	if len(ex.Facts) != 1 || ex.Facts[0].Data != "Reasoning-only fact" {
		t.Errorf("facts = %+v, want the fact carried in reasoning_content", ex.Facts)
	}
}

// When the endpoint answers with nothing at all, the error must say that rather
// than blaming the parse: the two failures want different fixes.
func TestCompleteNamesAnEmptyReply(t *testing.T) {
	srv := mockReasoningServer(t, "", "")
	defer srv.Close()

	c := NewLLMClient(srv.URL, "k", "m")
	_, err := c.Complete(context.Background(), "some input")
	if err == nil {
		t.Fatal("expected an error for a fully empty reply")
	}
	if !strings.Contains(err.Error(), "empty message") {
		t.Errorf("err = %v, want it to name an empty message", err)
	}
}
