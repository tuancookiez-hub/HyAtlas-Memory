package main

import (
	"context"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

// llmTestTimeout bounds the one model call that POST /api/v1/llm/test makes.
const llmTestTimeout = 20 * time.Second

// llmTestInterval is the least time between two real model calls from the test
// endpoint, so a loop cannot burn through a free tier.
const llmTestInterval = 10 * time.Second

// llmTestLast remembers when each server last made a test call. It is keyed by
// server so the Server struct needs no extra field and tests stay independent.
var llmTestLast sync.Map // *Server -> time.Time

// llmTestURL matches a URL inside an error message, so it can be removed.
var llmTestURL = regexp.MustCompile(`https?://\S+`)

// llmTestResult is the JSON body of POST /api/v1/llm/test.
type llmTestResult struct {
	OK        bool   `json:"ok"`
	Model     string `json:"model"`
	BaseHost  string `json:"base_host"`
	LatencyMS int64  `json:"latency_ms"`
	Error     string `json:"error,omitempty"`
}

// handleLLMTest is POST /api/v1/llm/test: it makes one tiny chat call with the
// configured model and reports whether it worked, so setup can tell a wrong model
// id or a bad key from a working endpoint. It goes through the same client, key
// resolution and fallback rules as extraction. Only a loopback caller is accepted,
// and at most one call is made per llmTestInterval. The key is never logged or
// returned, and the error text carries no URL.
func (s *Server) handleLLMTest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		jsonResponse(w, 405, map[string]any{"error": "method not allowed: use POST"})
		return
	}
	if !isLoopbackRemote(r.RemoteAddr) {
		jsonResponse(w, 403, map[string]any{"error": "the model test can only be run from this machine"})
		return
	}
	res := llmTestResult{Model: s.llmModel, BaseHost: urlHostname(s.llmBase)}
	if s.llm != nil {
		res.Model, res.BaseHost = s.llm.Model, urlHostname(s.llm.BaseURL)
	}
	switch {
	case !s.mode.UsesLLM():
		res.Error = "lite mode makes no model calls"
		jsonResponse(w, 200, res)
		return
	case s.llm == nil || (!s.llm.Configured() && !s.llm.AwaitingKey()):
		res.Error = "no model endpoint is configured"
		jsonResponse(w, 200, res)
		return
	case s.llm.AwaitingKey():
		res.Error = "awaiting_key"
		jsonResponse(w, 200, res)
		return
	}
	now := time.Now()
	if prev, ok := llmTestLast.Load(s); ok {
		if wait := llmTestInterval - now.Sub(prev.(time.Time)); wait > 0 {
			jsonResponse(w, 429, map[string]any{
				"error":       "model test rate limited",
				"retry_after": int(wait/time.Second) + 1,
			})
			return
		}
	}
	llmTestLast.Store(s, now)

	ctx, cancel := context.WithTimeout(r.Context(), llmTestTimeout)
	defer cancel()
	messages := []map[string]string{{"role": "user", "content": "Reply with the single word OK."}}
	_, err := s.llm.chat(ctx, messages, 0)
	res.LatencyMS = time.Since(now).Milliseconds()
	if err != nil {
		res.Error = s.llm.scrubError(err.Error())
	} else {
		res.OK = true
	}
	jsonResponse(w, 200, res)
}

// urlHostname returns the hostname of a URL, or "" when it has none.
func urlHostname(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return u.Hostname()
}

// scrubError shortens an error message for a client: URLs are replaced by
// "<url>", any key in use is masked, and the text is cut to 200 bytes.
func (l *LLMClient) scrubError(msg string) string {
	msg = llmTestURL.ReplaceAllString(msg, "<url>")
	for _, k := range []string{l.resolveKey(), l.FallbackKey} {
		if k != "" {
			msg = strings.ReplaceAll(msg, k, "<key>")
		}
	}
	msg = strings.Join(strings.Fields(msg), " ")
	if len(msg) > 200 {
		msg = msg[:200]
	}
	return msg
}
