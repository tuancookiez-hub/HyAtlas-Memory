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
		req := httptest.NewRequest("POST", "/api/v1/llm/key", strings.NewReader(body))
		req.RemoteAddr = "127.0.0.1:50000"
		srv.handleLLMKey(w, req)
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

// The key endpoint refuses a caller that is not on this machine and leaves the
// stored key untouched, while 127.0.0.0/8 and ::1 callers are accepted.
func TestLLMKeyLoopbackOnly(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	srv.mode = ModePro
	srv.llm = NewLLMClient("http://127.0.0.1:1/v1", "", "m")

	call := func(remote string) int {
		w := httptest.NewRecorder()
		req := httptest.NewRequest("POST", "/api/v1/llm/key", strings.NewReader(`{"key":"abc"}`))
		req.RemoteAddr = remote
		srv.handleLLMKey(w, req)
		return w.Code
	}
	for _, remote := range []string{"192.0.2.1:1234", "10.0.0.5:80", "[2001:db8::1]:443", "example.com:80", "garbage"} {
		if code := call(remote); code != 403 {
			t.Errorf("remote %q: code %d, want 403", remote, code)
		}
		if srv.llm.resolveKey() != "" {
			t.Fatalf("remote %q: a refused call still stored a key", remote)
		}
	}
	for _, remote := range []string{"127.0.0.1:1234", "127.5.6.7:1", "[::1]:8080"} {
		if code := call(remote); code != 200 {
			t.Errorf("remote %q: code %d, want 200", remote, code)
		}
	}
}
