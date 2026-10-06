package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
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

func NewLLMClient(baseURL, key, model string) *LLMClient {
	return &LLMClient{BaseURL: baseURL, APIKey: key, Model: model,
		Client: &http.Client{Timeout: 180 * time.Second}}
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
	system := `You are a memory extraction engine. Given one user input, output a JSON object with EXACTLY these keys:
{
  "facts": [{"data": "<durable atomic fact>", "layer": "user_preferences|project_state|technical_lesson|decision|negative_knowledge"}],
  "summary": {"text": "<1-2 sentence narrative of what this input is about and why it matters>"},
  "knowledge": [{"from": "<entity/subject>", "relation": "<relation>", "to": "<entity/object>"}],
  "schemas": [{"pattern": "<recurring structural pattern>", "context": "<when it applies>"}],
  "intention": {"goal": "<what the user is trying to achieve right now>"}
}
Rules:
- facts: ONLY durable, non-obvious facts worth remembering. Do not fabricate.
- summary: synthesize the ARC of this input, not just restate it.
- knowledge: extract 0-4 entity-relation-entity triples ONLY if meaningful.
- schemas: extract 0-2 recurring patterns ONLY if this is a repeated/structural case.
- intention: the immediate goal, or null if none.
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
	body, _ := json.Marshal(map[string]any{
		"model":       l.Model,
		"messages":    messages,
		"temperature": 0.2,
	})
	req, err := http.NewRequestWithContext(ctx, "POST", l.BaseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	// The Nous Portal sits behind Cloudflare, which 403s Go's default
	// "Go-http-client" User-Agent; identify honestly so the WAF lets the
	// extraction call through.
	req.Header.Set("User-Agent", "HyAtlas/4.1 (+https://github.com/tuancookiez-hub/HyAtlas-Memory)")
	// Resolve the key per request so a rotating credential (KeyFile) never
	// goes stale the way a startup-frozen env value does.
	if k := l.resolveKey(); k != "" {
		req.Header.Set("Authorization", "Bearer "+k)
	}
	resp, err := l.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("LLM HTTP %d: %s", resp.StatusCode, truncStr(data, 200))
	}
	var out struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	if len(out.Choices) == 0 {
		return nil, fmt.Errorf("LLM: no choices")
	}
	return parseExtraction(out.Choices[0].Message.Content)
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
