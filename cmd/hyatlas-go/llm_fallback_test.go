package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeChatEndpoint is a chat-completions stand-in that records every request's
// model and Authorization header, and answers with status and reply.
type fakeChatEndpoint struct {
	srv    *httptest.Server
	calls  atomic.Int32
	mu     sync.Mutex
	models []string
	auths  []string
}

func newFakeChatEndpoint(t *testing.T, status int, reply string) *fakeChatEndpoint {
	t.Helper()
	f := &fakeChatEndpoint{}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls.Add(1)
		var req struct {
			Model string `json:"model"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		f.mu.Lock()
		f.models = append(f.models, req.Model)
		f.auths = append(f.auths, r.Header.Get("Authorization"))
		f.mu.Unlock()
		if status != http.StatusOK {
			http.Error(w, "upstream "+strings.Repeat("x", 3), status)
			return
		}
		out, _ := json.Marshal(map[string]any{
			"choices": []map[string]any{{"message": map[string]string{"content": reply}}},
		})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	}))
	t.Cleanup(f.srv.Close)
	return f
}

// hangingChatEndpoint answers only after the client gives up, so the primary's
// per-call deadline is the only thing that ends its request.
func hangingChatEndpoint(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(10 * time.Second):
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestChatPrimarySuccessSkipsFallback(t *testing.T) {
	primary := newFakeChatEndpoint(t, http.StatusOK, "from primary")
	fallback := newFakeChatEndpoint(t, http.StatusOK, "from fallback")

	l := NewLLMClient(primary.srv.URL, "k", "primary-model")
	l.SetFallback(fallback.srv.URL, "fallback-model", "")

	got, err := l.chat(context.Background(), []map[string]string{{"role": "user", "content": "hi"}}, 0)
	if err != nil {
		t.Fatalf("chat: %v", err)
	}
	if got != "from primary" {
		t.Errorf("reply = %q, want primary's reply", got)
	}
	if n := fallback.calls.Load(); n != 0 {
		t.Errorf("fallback called %d times, want 0", n)
	}
}

func TestChatPrimaryErrorUsesFallbackModel(t *testing.T) {
	primary := newFakeChatEndpoint(t, http.StatusInternalServerError, "")
	fallback := newFakeChatEndpoint(t, http.StatusOK, "from fallback")

	l := NewLLMClient(primary.srv.URL, "k", "primary-model")
	l.SetFallback(fallback.srv.URL, "fallback-model", "")

	got, err := l.chat(context.Background(), []map[string]string{{"role": "user", "content": "hi"}}, 0)
	if err != nil {
		t.Fatalf("chat: %v", err)
	}
	if got != "from fallback" {
		t.Errorf("reply = %q, want fallback's reply", got)
	}
	if len(fallback.models) != 1 || fallback.models[0] != "fallback-model" {
		t.Errorf("fallback request models = %v, want [fallback-model]", fallback.models)
	}
}

func TestChatHungPrimaryFallsBackWithinParentDeadline(t *testing.T) {
	primary := hangingChatEndpoint(t)
	fallback := newFakeChatEndpoint(t, http.StatusOK, "from fallback")

	l := NewLLMClient(primary.URL, "k", "primary-model")
	l.SetFallback(fallback.srv.URL, "fallback-model", "")

	// The primary gets 2s of this 3s budget; the fallback must finish in the
	// remaining second, well inside the parent deadline.
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	start := time.Now()
	got, err := l.chat(ctx, []map[string]string{{"role": "user", "content": "hi"}}, 0)
	if err != nil {
		t.Fatalf("chat: %v", err)
	}
	if got != "from fallback" {
		t.Errorf("reply = %q, want fallback's reply", got)
	}
	if elapsed := time.Since(start); elapsed >= 3*time.Second {
		t.Errorf("chat took %v, want it to finish inside the 3s parent deadline", elapsed)
	}
}

func TestChatBothFailMentionsBoth(t *testing.T) {
	primary := newFakeChatEndpoint(t, http.StatusInternalServerError, "")
	fallback := newFakeChatEndpoint(t, http.StatusBadGateway, "")

	l := NewLLMClient(primary.srv.URL, "k", "primary-model")
	l.SetFallback(fallback.srv.URL, "fallback-model", "")

	_, err := l.chat(context.Background(), []map[string]string{{"role": "user", "content": "hi"}}, 0)
	if err == nil {
		t.Fatal("chat succeeded, want error when both models fail")
	}
	msg := err.Error()
	if !strings.Contains(msg, "primary:") || !strings.Contains(msg, "fallback:") {
		t.Errorf("error = %q, want it to mention both primary and fallback", msg)
	}
}

func TestChatNoFallbackConfiguredIsUnchanged(t *testing.T) {
	primary := newFakeChatEndpoint(t, http.StatusInternalServerError, "")

	l := NewLLMClient(primary.srv.URL, "k", "primary-model")

	_, err := l.chat(context.Background(), []map[string]string{{"role": "user", "content": "hi"}}, 0)
	if err == nil {
		t.Fatal("chat succeeded, want the primary's error")
	}
	if strings.Contains(err.Error(), "fallback") {
		t.Errorf("error = %q, want the plain primary error with no fallback wording", err)
	}
	if n := primary.calls.Load(); n != 1 {
		t.Errorf("primary called %d times, want exactly 1", n)
	}
}

func TestChatFallbackKeyOverridesPrimaryKey(t *testing.T) {
	primary := newFakeChatEndpoint(t, http.StatusInternalServerError, "")
	fallback := newFakeChatEndpoint(t, http.StatusOK, "from fallback")

	l := NewLLMClient(primary.srv.URL, "primary-key", "primary-model")
	l.SetFallback(fallback.srv.URL, "fallback-model", "fallback-key")

	if _, err := l.chat(context.Background(), []map[string]string{{"role": "user", "content": "hi"}}, 0); err != nil {
		t.Fatalf("chat: %v", err)
	}
	if len(primary.auths) != 1 || primary.auths[0] != "Bearer primary-key" {
		t.Errorf("primary Authorization = %v, want [Bearer primary-key]", primary.auths)
	}
	if len(fallback.auths) != 1 || fallback.auths[0] != "Bearer fallback-key" {
		t.Errorf("fallback Authorization = %v, want [Bearer fallback-key]", fallback.auths)
	}
}

func TestChatFallbackWithoutKeyUsesPrimaryKey(t *testing.T) {
	primary := newFakeChatEndpoint(t, http.StatusInternalServerError, "")
	fallback := newFakeChatEndpoint(t, http.StatusOK, "from fallback")

	l := NewLLMClient(primary.srv.URL, "primary-key", "primary-model")
	l.SetFallback(fallback.srv.URL, "fallback-model", "")

	if _, err := l.chat(context.Background(), []map[string]string{{"role": "user", "content": "hi"}}, 0); err != nil {
		t.Fatalf("chat: %v", err)
	}
	if len(fallback.auths) != 1 || fallback.auths[0] != "Bearer primary-key" {
		t.Errorf("fallback Authorization = %v, want [Bearer primary-key]", fallback.auths)
	}
}

func TestSetFallbackTrimsSpaces(t *testing.T) {
	l := NewLLMClient("http://primary", "k", "m")
	l.SetFallback("  http://fb \n", " fb-model ", "\tfb-key ")
	if l.FallbackBaseURL != "http://fb" || l.FallbackModel != "fb-model" || l.FallbackKey != "fb-key" {
		t.Errorf("fallback = %q / %q / %q, want trimmed values", l.FallbackBaseURL, l.FallbackModel, l.FallbackKey)
	}
}

func TestResolveExtractTimeout(t *testing.T) {
	t.Setenv("HYATLAS_EXTRACT_TIMEOUT", "")
	if got := resolveExtractTimeout(); got != 300*time.Second {
		t.Errorf("unset: got %v, want 300s", got)
	}

	t.Setenv("HYATLAS_EXTRACT_TIMEOUT", "90s")
	if got := resolveExtractTimeout(); got != 90*time.Second {
		t.Errorf("90s: got %v, want 90s", got)
	}

	t.Setenv("HYATLAS_EXTRACT_TIMEOUT", "not-a-duration")
	if got := resolveExtractTimeout(); got != 300*time.Second {
		t.Errorf("invalid: got %v, want default 300s", got)
	}

	t.Setenv("HYATLAS_EXTRACT_TIMEOUT", "0")
	if got := resolveExtractTimeout(); got != 300*time.Second {
		t.Errorf("zero: got %v, want default 300s", got)
	}
}

// The primary's key goes to the fallback only on the primary's own host. A fallback
// on another host with no key of its own gets no Authorization header, so a live
// primary token is never handed to a different provider.
func TestChatFallbackOnOtherHostGetsNoPrimaryKey(t *testing.T) {
	primary := newFakeChatEndpoint(t, http.StatusInternalServerError, "")
	fallback := newFakeChatEndpoint(t, http.StatusOK, "from fallback")
	// Same server, but named by a different host than the primary's 127.0.0.1.
	other := strings.Replace(fallback.srv.URL, "127.0.0.1", "localhost", 1)

	l := NewLLMClient(primary.srv.URL, "primary-key", "primary-model")
	l.SetFallback(other, "fallback-model", "")
	if _, err := l.chat(context.Background(), []map[string]string{{"role": "user", "content": "hi"}}, 0); err != nil {
		t.Fatalf("chat: %v", err)
	}
	if len(fallback.auths) != 1 || fallback.auths[0] != "" {
		t.Errorf("fallback on another host got Authorization %v, want none", fallback.auths)
	}
}

func TestSameHost(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{"https://inference-api.nousresearch.com/v1", "https://inference-api.nousresearch.com/v2", true},
		{"http://127.0.0.1:1/v1", "http://127.0.0.1:2/v1", true},
		{"http://localhost:1", "http://127.0.0.1:1", false},
		{"https://a.example/v1", "https://b.example/v1", false},
		{"", "https://a.example", false},
	} {
		if got := sameHost(c.a, c.b); got != c.want {
			t.Errorf("sameHost(%q, %q) = %v, want %v", c.a, c.b, got, c.want)
		}
	}
}
