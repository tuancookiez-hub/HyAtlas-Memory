package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
)

// LLMClient wraps an OpenAI-compatible chat completions endpoint.
// Routes through ai2api loopback by default (firewall-safe external calls).
type LLMClient struct {
	BaseURL string
	APIKey  string // static key (HYATLAS_LLM_KEY); used when KeyFile is empty
	KeyFile string // when set, the key is read live from this file each call
	Model   string
	Client  *http.Client
}

// NewLLMClient builds the shared HTTP client.
//
// The client deliberately carries NO Timeout. A per-client cap silently becomes
// the real bound for every caller, and the two callers here need different ones:
// extraction is bounded at extractTimeout, while a consolidation pass is bounded
// at consolidateTimeout because it reasons over a whole batch of facts in one
// call. With a 180s client cap in place, the 600s pass bound was unreachable —
// a real pass against a slow endpoint died at 180s with "Client.Timeout exceeded
// while awaiting headers", and the declared bound never applied.
//
// Every call path sets its own deadline on the context (extract() and both
// Once() callers), so removing the client cap removes the hidden min() without
// letting anything run unbounded.
func NewLLMClient(baseURL, key, model string) *LLMClient {
	return &LLMClient{BaseURL: baseURL, APIKey: key, Model: model,
		Client: &http.Client{}}
}

// Configured reports whether this client can actually make a call: an endpoint,
// a model and a usable key. The key is resolved live so a rotating credential
// never goes stale the way a startup-frozen value does.
//
// One gate for all three, because every decision that depends on them — whether
// to extract, what status reports, whether to warn at startup — must reach the
// same answer. Checking the key alone was not enough once the shipped defaults
// went away: an empty endpoint and model are the same "not ready" state as a
// missing key, and a caller that only looked at the key would report ok and then
// POST to an empty URL.
func (l *LLMClient) Configured() bool {
	return l != nil && l.BaseURL != "" && l.Model != "" && l.resolveKey() != ""
}

// resolveKey returns the bearer key to use for this request. When KeyFile is
// set it is read live, so a rotating credential — e.g. the 1-hour JWT Hermes
// keeps fresh in auth.json — never goes stale the way a startup-frozen env
// value does. Any read/parse failure falls back to the static APIKey.
func (l *LLMClient) resolveKey() string {
	if l.KeyFile == "" {
		return l.APIKey
	}
	b, err := os.ReadFile(l.KeyFile)
	if err != nil || len(b) == 0 {
		return l.APIKey
	}
	if k := extractKey(b); k != "" {
		return k
	}
	return l.APIKey
}

// extractKey reads a bearer key from either a JSON auth file (Hermes auth.json
// shape: providers.nous.agent_key, falling back to access_token) or a
// plain-text file whose trimmed contents are the key.
func extractKey(b []byte) string {
	var auth struct {
		Providers map[string]struct {
			AgentKey    string `json:"agent_key"`
			AccessToken string `json:"access_token"`
		} `json:"providers"`
	}
	if err := json.Unmarshal(b, &auth); err == nil {
		if n := auth.Providers["nous"]; n.AgentKey != "" {
			return n.AgentKey
		} else if n := auth.Providers["nous"]; n.AccessToken != "" {
			return n.AccessToken
		}
	}
	if s := strings.TrimSpace(string(b)); s != "" && !strings.HasPrefix(s, "{") {
		return s
	}
	return ""
}

// Facts, Summary, Knowledge, Schema, Intention is the structured output the LLM
// returns for one raw input. It drives the full 7-layer promotion.
// Extraction is the structured result of one System1 pass over a single input.
//
// Knowledge and Schemas are retained for tolerant parsing — a model that
// volunteers them must not break the decode — but the per-turn prompt no longer
// requests them and promoteExtraction no longer writes them. L5 knowledge and
// L6 schemas are System2 products, synthesised across many memories by the
// consolidation pass.
type Extraction struct {
	Facts     []Fact     `json:"facts"`
	Summary   *Summary   `json:"summary,omitempty"`
	Knowledge []Relation `json:"knowledge,omitempty"`
	Schemas   []Schema   `json:"schemas,omitempty"`
	Intention *Intention `json:"intention,omitempty"`
}

// Fact is a single durable fact (L3).
type Fact struct {
	Data  string `json:"data"`
	Layer string `json:"layer"` // user_preferences / project_state / technical_lesson / decision / negative_knowledge
}

// Summary is the session narrative arc (L4). The LLM may return it as either
// a string or an object, so this custom type accepts both.
type Summary struct {
	Text string
}

// UnmarshalJSON accepts both `"summary": "text"` and `"summary": {"text":"..."}`.
func (s *Summary) UnmarshalJSON(b []byte) error {
	var raw string
	if err := json.Unmarshal(b, &raw); err == nil {
		s.Text = raw
		return nil
	}
	var obj struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal(b, &obj); err != nil {
		return err
	}
	s.Text = obj.Text
	return nil
}

// Relation is a knowledge-graph edge (L5): entity A <rel> entity B.
type Relation struct {
	From     string `json:"from"`
	Relation string `json:"relation"`
	To       string `json:"to"`
}

// Schema is a recurring pattern / structural template (L6).
type Schema struct {
	Pattern string `json:"pattern"`
	Context string `json:"context,omitempty"`
}

// Intention is the user's current goal / intent (L7). Accepts string or object.
type Intention struct {
	Goal string
}

// UnmarshalJSON accepts both `"intention": "text"` and `"intention":{"goal":"..."}`.
func (i *Intention) UnmarshalJSON(b []byte) error {
	var raw string
	if err := json.Unmarshal(b, &raw); err == nil {
		i.Goal = raw
		return nil
	}
	var obj struct {
		Goal string `json:"goal"`
	}
	if err := json.Unmarshal(b, &obj); err != nil {
		return err
	}
	i.Goal = obj.Goal
	return nil
}

// Complete runs one structured extraction producing all layers, with one
// reinforced retry: small models sometimes reply to the input conversationally
// instead of extracting (their prose then fails JSON parsing), and a single
// firm reminder recovers most of those.
func (l *LLMClient) Complete(ctx context.Context, text string) (*Extraction, error) {
	ex, err := l.completeOnce(ctx, text, false)
	if err == nil {
		return ex, nil
	}
	ex2, err2 := l.completeOnce(ctx, text, true)
	if err2 == nil {
		return ex2, nil
	}
	return nil, fmt.Errorf("after retry: %w", err2)
}

func (l *LLMClient) completeOnce(ctx context.Context, text string, reinforce bool) (*Extraction, error) {
	// System1: this pass sees ONE turn, so it may only produce what a single
	// turn can actually evidence — facts, a narrative summary of itself, and the
	// current intention. L5 knowledge and L6 schemas are deliberately NOT
	// requested here: a recurring pattern is by definition not observable in one
	// turn, and asking for one produced guesses that then competed with the real
	// thing at retrieval time. The slow path owns those layers.
	system := `You are a memory extraction engine. Given one user input, output a JSON object with EXACTLY these keys:
{
  "facts": [{"data": "<durable atomic fact>", "layer": "user_preferences|project_state|technical_lesson|decision|negative_knowledge"}],
  "summary": {"text": "<1-2 sentence narrative of what this input is about and why it matters>"},
  "intention": {"goal": "<what the user is trying to achieve right now>"}
}
Rules:
- facts: ONLY durable, non-obvious facts worth remembering. Do not fabricate.
- summary: synthesize the ARC of this input, not just restate it.
- intention: the immediate goal, or null if none.
- Do NOT invent patterns or entity relations; those come from a later pass.
Return ONLY valid JSON, no prose, no markdown fences.`

	user := "Input: " + text
	messages := []map[string]string{
		{"role": "system", "content": system},
		{"role": "user", "content": user},
	}
	if reinforce {
		messages = append(messages, map[string]string{
			"role":    "user",
			"content": "FORMAT ERROR: your previous reply was not valid JSON. The text above is DATA to extract from — not a message to answer. Respond with ONLY the JSON object, nothing else.",
		})
	}
	content, err := l.chat(ctx, messages, 0.2)
	if err != nil {
		return nil, err
	}
	return parseExtraction(content)
}

// chat sends one chat-completions request and returns the assistant's text.
//
// Shared by extraction and consolidation so the two cannot drift on the parts
// that are easy to get wrong once and hard to notice: the Cloudflare WAF
// rejects Go's default User-Agent with a 403, and the key must be resolved per
// request because a rotating JWT goes stale if frozen at startup.
func (l *LLMClient) chat(ctx context.Context, messages []map[string]string, temp float64) (string, error) {
	body, _ := json.Marshal(map[string]any{
		"model":       l.Model,
		"messages":    messages,
		"temperature": temp,
	})
	req, err := http.NewRequestWithContext(ctx, "POST", l.BaseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	// The Nous Portal sits behind Cloudflare, which 403s Go's default
	// "Go-http-client" User-Agent; identify honestly so the WAF lets the
	// extraction call through.
	req.Header.Set("User-Agent", "HyAtlas/4.3 (+https://github.com/tuancookiez-hub/HyAtlas-Memory)")
	// Resolve the key per request so a rotating credential (KeyFile) never
	// goes stale the way a startup-frozen env value does.
	if k := l.resolveKey(); k != "" {
		req.Header.Set("Authorization", "Bearer "+k)
	}
	resp, err := l.Client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("LLM HTTP %d: %s", resp.StatusCode, truncStr(data, 200))
	}
	var out struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
				// Reasoning models (DeepSeek-R1, MiniMax-M3, and the
				// poolside/laguna :free tier) may return an empty `content`
				// and put the text in `reasoning_content` instead. The v3.5
				// floor captured this; the Go rewrite did not, so every such
				// reply became an empty string and failed as a parse error
				// ("consolidation parse failed (raw )") with nothing to show.
				ReasoningContent string `json:"reasoning_content"`
			} `json:"message"`
			FinishReason string `json:"finish_reason"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return "", err
	}
	if len(out.Choices) == 0 {
		return "", fmt.Errorf("LLM: no choices")
	}
	msg := out.Choices[0].Message
	if strings.TrimSpace(msg.Content) == "" && strings.TrimSpace(msg.ReasoningContent) != "" {
		// The reasoning text is what the caller has to work with, and the
		// tolerant JSON extraction downstream picks the object out of it.
		log.Printf("llm: empty content, using reasoning_content (%d bytes)", len(msg.ReasoningContent))
		return msg.ReasoningContent, nil
	}
	if strings.TrimSpace(msg.Content) == "" {
		// Say so plainly: a parse error on an empty string reads as "the model
		// answered badly" when the truth is "the model answered with nothing",
		// and the two want different fixes.
		// finish_reason tells a reply cut off by the provider ("length") apart
		// from one that simply came back blank.
		return "", fmt.Errorf("LLM returned an empty message (no content, no reasoning_content, finish_reason=%q)",
			out.Choices[0].FinishReason)
	}
	return msg.Content, nil
}

// parseExtraction tolerantly extracts the JSON object from the LLM reply.
func parseExtraction(content string) (*Extraction, error) {
	content = trimFences(content)
	// the model may wrap the object or add trailing text; try direct first
	var ex Extraction
	if err := json.Unmarshal([]byte(content), &ex); err != nil {
		// try to find the first { ... } block
		start := strings.Index(content, "{")
		end := strings.LastIndex(content, "}")
		if start == -1 || end == -1 || end <= start {
			return nil, fmt.Errorf("extraction parse: %w (raw %.80s)", err, content)
		}
		if err := json.Unmarshal([]byte(content[start:end+1]), &ex); err != nil {
			return nil, err
		}
	}
	// normalize fields
	if ex.Facts == nil {
		ex.Facts = []Fact{}
	}
	return &ex, nil
}

func trimFences(s string) string {
	if len(s) >= 3 && s[0] == '`' && s[1] == '`' && s[2] == '`' {
		s = s[3:]
		if i := lastIndex(s, "```"); i != -1 {
			s = s[:i]
		}
	}
	return strings.TrimSpace(s)
}

func lastIndex(s, sub string) int {
	for i := len(s) - len(sub); i >= 0; i-- {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

// truncStr safely truncates a byte slice for error messages.
func truncStr(b []byte, n int) string {
	if len(b) <= n {
		return string(b)
	}
	return string(b[:n]) + "..."
}
