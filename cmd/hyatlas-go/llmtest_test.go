package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
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

// Two requests that arrive together make at most one model call: the rate limit is
// checked and set under one lock.
func TestLLMTestConcurrentMakesOneCall(t *testing.T) {
	var calls atomic.Int32
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		time.Sleep(50 * time.Millisecond)
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer stub.Close()
	srv := newTestServer(t, "test-model", stub.URL+"/v1")
	srv.mode = ModePro
	srv.llm = NewLLMClient(stub.URL+"/v1", "k", "test-model")

	var wg sync.WaitGroup
	var limited atomic.Int32
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if code, _ := postLLMTest(srv, "POST", "127.0.0.1:5000"); code == 429 {
				limited.Add(1)
			}
		}()
	}
	wg.Wait()
	if n := calls.Load(); n != 1 {
		t.Fatalf("%d model calls, want exactly 1", n)
	}
	if n := limited.Load(); n != 7 {
		t.Fatalf("%d requests were rate limited, want 7", n)
	}
}

// fallbackStub answers 404 to the model "bad-model" and 200 to any other model,
// and records which models were asked.
func fallbackStub(t *testing.T, fallbackStatus int) (*httptest.Server, *[]string) {
	t.Helper()
	var mu sync.Mutex
	asked := []string{}
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var req struct {
			Model string `json:"model"`
		}
		_ = json.Unmarshal(raw, &req)
		mu.Lock()
		asked = append(asked, req.Model)
		mu.Unlock()
		if req.Model == "bad-model" {
			w.WriteHeader(404)
			_, _ = w.Write([]byte(`{"error":"model not found"}`))
			return
		}
		w.WriteHeader(fallbackStatus)
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"OK"}}]}`))
	}))
	t.Cleanup(stub.Close)
	return stub, &asked
}

// A failing primary is reported as failed even when the fallback answers, and the
// fallback is named so setup can say extraction still works through it.
func TestLLMTestCreditsTheFallback(t *testing.T) {
	stub, asked := fallbackStub(t, 200)
	srv := newTestServer(t, "bad-model", stub.URL+"/v1")
	srv.mode = ModePro
	srv.llm = NewLLMClient(stub.URL+"/v1", "k", "bad-model")
	srv.llm.SetFallback("", "good-model", "")

	code, out := postLLMTest(srv, "POST", "127.0.0.1:5000")
	if code != 200 || out["ok"] != false || out["model"] != "bad-model" {
		t.Fatalf("got %d %v", code, out)
	}
	if msg, _ := out["error"].(string); !strings.Contains(msg, "404") {
		t.Fatalf("error should be the primary's: %v", out)
	}
	if out["fallback_model"] != "good-model" || out["fallback_ok"] != true {
		t.Fatalf("fallback not reported: %v", out)
	}
	if len(*asked) != 2 || (*asked)[0] != "bad-model" || (*asked)[1] != "good-model" {
		t.Fatalf("models asked: %v, want primary then fallback", *asked)
	}
}

// A failing fallback is reported as failed too.
func TestLLMTestFallbackAlsoFails(t *testing.T) {
	stub, _ := fallbackStub(t, 500)
	srv := newTestServer(t, "bad-model", stub.URL+"/v1")
	srv.mode = ModePro
	srv.llm = NewLLMClient(stub.URL+"/v1", "k", "bad-model")
	srv.llm.SetFallback("", "good-model", "")

	_, out := postLLMTest(srv, "POST", "127.0.0.1:5000")
	if out["ok"] != false || out["fallback_model"] != "good-model" || out["fallback_ok"] != false {
		t.Fatalf("got %v", out)
	}
}

// A working primary never touches the fallback, and a failing primary with no
// fallback configured reports no fallback fields.
func TestLLMTestPrimaryOnly(t *testing.T) {
	stub, asked := fallbackStub(t, 200)
	srv := newTestServer(t, "good-model", stub.URL+"/v1")
	srv.mode = ModePro
	srv.llm = NewLLMClient(stub.URL+"/v1", "k", "good-model")
	srv.llm.SetFallback("", "other-model", "")
	_, out := postLLMTest(srv, "POST", "127.0.0.1:5000")
	if out["ok"] != true || out["fallback_model"] != nil || len(*asked) != 1 {
		t.Fatalf("got %v, asked %v", out, *asked)
	}

	srv2 := newTestServer(t, "bad-model", stub.URL+"/v1")
	srv2.mode = ModePro
	srv2.llm = NewLLMClient(stub.URL+"/v1", "k", "bad-model")
	_, out = postLLMTest(srv2, "POST", "127.0.0.1:5000")
	if out["ok"] != false || out["fallback_model"] != nil || out["fallback_ok"] != nil {
		t.Fatalf("got %v", out)
	}
}
