package main

import (
	"net/http/httptest"
	"strings"
	"testing"
)

// The key handed over at runtime is used for LLM calls, never echoed, and wins
// over the static configuration; clearing it restores that configuration.
func TestLLMKeyHandOver(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	srv.mode = ModePro
	srv.llm = NewLLMClient("http://127.0.0.1:1/v1", "", "m")
	if got := srv.llmState(); got != "awaiting_key" {
		t.Fatalf("before hand-over: llm = %q, want awaiting_key", got)
	}
	if got := srv.extractForMode("text", "u", "a", "raw1"); got != "awaiting_key" {
		t.Fatalf("extraction before hand-over = %q, want awaiting_key", got)
	}

	post := func(body string) (int, string) {
		w := httptest.NewRecorder()
		srv.handleLLMKey(w, httptest.NewRequest("POST", "/api/v1/llm/key", strings.NewReader(body)))
		return w.Code, w.Body.String()
	}
	code, out := post(`{"key":"secret-token-123"}`)
	if code != 200 || strings.Contains(out, "secret-token-123") {
		t.Fatalf("hand-over: %d %s", code, out)
	}
	if srv.llm.resolveKey() != "secret-token-123" || srv.llmState() != "ok" {
		t.Fatalf("pushed key not used: state %q", srv.llmState())
	}
	srv.llm.APIKey = "static"
	if srv.llm.resolveKey() != "secret-token-123" {
		t.Fatal("pushed key does not win over the static key")
	}
	if code, _ := post(`{"key":""}`); code != 200 || srv.llm.resolveKey() != "static" {
		t.Fatalf("clearing: code %d, key %q", code, srv.llm.resolveKey())
	}
	for _, bad := range []string{`{"key":"two words"}`, `{"key":"line\nbreak"}`, `not json`} {
		if code, _ := post(bad); code != 400 {
			t.Errorf("%s: code %d, want 400", bad, code)
		}
	}
	w := httptest.NewRecorder()
	srv.handleLLMKey(w, httptest.NewRequest("GET", "/api/v1/llm/key", nil))
	if w.Code != 405 {
		t.Errorf("GET: code %d, want 405", w.Code)
	}
}
