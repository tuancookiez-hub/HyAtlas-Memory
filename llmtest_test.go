package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// llmTestServer builds a pro-mode server whose model endpoint is a stub that
// answers with status and body.
func llmTestServer(t *testing.T, key string, status int, body string) *Server {
	t.Helper()
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(stub.Close)
	srv := newTestServer(t, "test-model", stub.URL+"/v1")
	srv.mode = ModePro
	srv.llm = NewLLMClient(stub.URL+"/v1", key, "test-model")
	return srv
}

// postLLMTest calls the handler as a caller from addr and decodes the reply.
func postLLMTest(srv *Server, method, addr string) (int, map[string]any) {
	w := httptest.NewRecorder()
	req := httptest.NewRequest(method, "/api/v1/llm/test", nil)
	req.RemoteAddr = addr
	srv.handleLLMTest(w, req)
	var out map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return w.Code, out
}

// A working endpoint gives ok=true with the model and the host only.
func TestLLMTestOK(t *testing.T) {
	srv := llmTestServer(t, "secret-key-1", 200, `{"choices":[{"message":{"content":"OK"}}]}`)
	code, out := postLLMTest(srv, "POST", "127.0.0.1:5000")
	if code != 200 || out["ok"] != true || out["model"] != "test-model" || out["base_host"] != "127.0.0.1" {
		t.Fatalf("got %d %v", code, out)
	}
}

// A failing endpoint gives ok=false and an error without the URL or the key.
func TestLLMTestError(t *testing.T) {
	srv := llmTestServer(t, "secret-key-1", 401, `{"error":"bad key secret-key-1 at http://x/y"}`)
	code, out := postLLMTest(srv, "POST", "127.0.0.1:5000")
	msg, _ := out["error"].(string)
	if code != 200 || out["ok"] != false || !strings.Contains(msg, "401") {
		t.Fatalf("got %d %v", code, out)
	}
	if strings.Contains(msg, "secret-key-1") || strings.Contains(msg, "http://") {
		t.Fatalf("error leaks the key or a URL: %q", msg)
	}
}

// Lite mode makes no call and says so.
func TestLLMTestLite(t *testing.T) {
	srv := llmTestServer(t, "k", 200, `{}`)
	srv.mode = ModeLite
	code, out := postLLMTest(srv, "POST", "127.0.0.1:5000")
	if code != 200 || out["ok"] != false || out["error"] != "lite mode makes no model calls" {
		t.Fatalf("got %d %v", code, out)
	}
}

// With no key the answer is awaiting_key and nothing is called.
func TestLLMTestNoKey(t *testing.T) {
	srv := llmTestServer(t, "", 200, `{}`)
	code, out := postLLMTest(srv, "POST", "127.0.0.1:5000")
	if code != 200 || out["ok"] != false || out["error"] != "awaiting_key" {
		t.Fatalf("got %d %v", code, out)
	}
}

// Only a loopback caller is accepted, and only POST.
func TestLLMTestCallerAndMethod(t *testing.T) {
	srv := llmTestServer(t, "k", 200, `{"choices":[{"message":{"content":"OK"}}]}`)
	if code, _ := postLLMTest(srv, "POST", "10.1.2.3:5000"); code != 403 {
		t.Fatalf("remote caller: %d, want 403", code)
	}
	if code, _ := postLLMTest(srv, "GET", "127.0.0.1:5000"); code != 405 {
		t.Fatalf("GET: %d, want 405", code)
	}
}

// A second call within the interval is refused with retry_after.
func TestLLMTestRateLimit(t *testing.T) {
	srv := llmTestServer(t, "k", 200, `{"choices":[{"message":{"content":"OK"}}]}`)
	if code, _ := postLLMTest(srv, "POST", "127.0.0.1:5000"); code != 200 {
		t.Fatalf("first call: %d", code)
	}
	code, out := postLLMTest(srv, "POST", "127.0.0.1:5000")
	if code != 429 || out["retry_after"] == nil {
		t.Fatalf("second call: %d %v", code, out)
	}
}
