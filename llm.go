package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// LLMClient wraps an OpenAI-compatible chat completions endpoint.
// Routes through ai2api loopback by default (firewall-safe external calls).
type LLMClient struct {
	BaseURL string
	APIKey  string
	Model   string
	Client  *http.Client
}

func NewLLMClient(baseURL, key, model string) *LLMClient {
	return &LLMClient{BaseURL: baseURL, APIKey: key, Model: model,
		Client: &http.Client{Timeout: 180 * time.Second}}
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

// Complete runs one structured extraction producing all layers.
func (l *LLMClient) Complete(ctx context.Context, text string) (*Extraction, error) {
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
	body, _ := json.Marshal(map[string]any{
		"model": l.Model,
		"messages": []map[string]string{
			{"role": "system", "content": system},
			{"role": "user", "content": user},
		},
		"temperature": 0.2,
	})
	req, err := http.NewRequestWithContext(ctx, "POST", l.BaseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if l.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+l.APIKey)
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
