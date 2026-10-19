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
	ex, err := client.CompleteWith(context.Background(), "2 people i admire: my parents and my mentor.", ExtractContext{})
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
	if _, err := client.CompleteWith(context.Background(), "input", ExtractContext{}); err == nil {
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
	ex, err := c.CompleteWith(context.Background(), "some input", ExtractContext{})
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
	_, err := c.CompleteWith(context.Background(), "some input", ExtractContext{})
	if err == nil {
		t.Fatal("expected an error for a fully empty reply")
	}
	if !strings.Contains(err.Error(), "empty message") {
		t.Errorf("err = %v, want it to name an empty message", err)
	}
}

// A chat call that fails at the HTTP level is returned at once. Retrying the same
// request would fail the same way, and a second call only adds load to a provider
// that is already erroring. The request also names the HyAtlas version.
func TestCompleteChatErrorIsNotRetried(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if !strings.HasPrefix(r.Header.Get("User-Agent"), "HyAtlas/"+Version+" ") {
			t.Errorf("User-Agent = %q, want it to carry the HyAtlas version", r.Header.Get("User-Agent"))
		}
		http.Error(w, "upstream down", http.StatusInternalServerError)
	}))
	defer srv.Close()

	client := NewLLMClient(srv.URL, "test-key", "mock-model")
	if _, err := client.CompleteWith(context.Background(), "input", ExtractContext{}); err == nil {
		t.Fatal("want an error for an HTTP 500, got nil")
	}
	if got := calls.Load(); got != 1 {
		t.Errorf("LLM calls: want 1 (a chat error is not retried), got %d", got)
	}
}

// A confidence written as a number, a bool or something else must not fail the
// whole extraction. The lessons decode, and each confidence maps to high, medium,
// low or empty as Experience.UnmarshalJSON describes.
func TestExperienceTolerantConfidence(t *testing.T) {
	ex, err := parseExtraction(`{"facts":[{"data":"fact one","layer":"project_state"}],"experiences":[
		{"task":"t1","solution":"s1","environment":"e1","confidence":0.9},
		{"task":"t2","solution":"s2","environment":"e2","confidence":0.5},
		{"task":"t3","solution":"s3","environment":"e3","confidence":0.1},
		{"task":"t4","solution":"s4","environment":"e4","confidence":true},
		{"task":"t5","solution":"s5","environment":"e5","confidence":false},
		{"task":"t6","solution":"s6","environment":"e6","confidence":"high"},
		{"task":"t7","solution":"s7","environment":"e7","confidence":["very","sure"]},
		{"task":7,"solution":{"x":1},"environment":null,"confidence":{"a":1}},
		"junk",
		{"task":"t10","solution":"s10","environment":"e10","confidence":null}
	]}`)
	if err != nil {
		t.Fatalf("extraction with tolerant experiences: %v", err)
	}
	if len(ex.Facts) != 1 || ex.Facts[0].Data != "fact one" {
		t.Errorf("facts = %+v, want the fact to survive", ex.Facts)
	}
	want := []struct{ task, confidence string }{
		{"t1", "high"}, {"t2", "medium"}, {"t3", "low"}, {"t4", "high"}, {"t5", "low"},
		{"t6", "high"}, {"t7", ""}, {"", ""}, {"", ""}, {"t10", ""},
	}
	if len(ex.Experiences) != len(want) {
		t.Fatalf("experiences = %d, want %d: %+v", len(ex.Experiences), len(want), ex.Experiences)
	}
	for i, w := range want {
		if got := ex.Experiences[i]; got.Task != w.task || got.Confidence != w.confidence {
			t.Errorf("experience %d = {task %q, confidence %q}, want {task %q, confidence %q}",
				i, got.Task, got.Confidence, w.task, w.confidence)
		}
	}
	if e := ex.Experiences[0]; e.Solution != "s1" || e.Environment != "e1" {
		t.Errorf("first experience = %+v, want its string fields kept", e)
	}
}
