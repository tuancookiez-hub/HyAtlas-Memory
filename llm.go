package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync/atomic"
	"time"
	"unicode"
)

// LLMClient wraps an OpenAI-compatible chat completions endpoint.
// Routes through ai2api loopback by default (firewall-safe external calls).
type LLMClient struct {
	BaseURL string
	APIKey  string // static key (HYATLAS_LLM_KEY); used when KeyFile is empty
	KeyFile string // when set, the key is read live from this file each call
	Model   string
	Client  *http.Client

	// FallbackBaseURL is the endpoint the fallback model is called on. Empty
	// means the same endpoint as BaseURL.
	FallbackBaseURL string
	// FallbackModel is the model tried when the primary fails. Empty means no
	// fallback, and chat behaves exactly as it did before fallbacks existed.
	FallbackModel string
	// FallbackKey is the bearer key for the fallback. Empty means the primary's
	// key resolution (Key / KeyFile) is used.
	FallbackKey string

	// pushed is a key handed over at runtime through POST /api/v1/llm/key, typically
	// by the Hermes plugin, which holds the user's provider credential. It is kept
	// in memory only and wins over APIKey and KeyFile. This lets the server run
	// without ever reading another application's credential file, which security
	// software rightly treats as a credential-theft pattern.
	pushed atomic.Pointer[string]

	// keyFileWarned makes the "key file looks like JSON" warning appear once.
	keyFileWarned atomic.Bool
}

// SetPushedKey replaces the in-memory key handed over at runtime. An empty key
// clears it, so the static configuration applies again.
func (l *LLMClient) SetPushedKey(key string) {
	if l == nil {
		return
	}
	if key == "" {
		l.pushed.Store(nil)
		return
	}
	l.pushed.Store(&key)
}

// AwaitingKey reports whether the endpoint and model are set but no key is
// available yet, as when the plugin has not handed one over since the server
// started.
func (l *LLMClient) AwaitingKey() bool {
	return l != nil && l.BaseURL != "" && l.Model != "" && l.resolveKey() == ""
}

// fallbackKey is the bearer token for a fallback call to base: FallbackKey when set,
// otherwise the primary's key, but only when base is the primary's host. The
// primary key (on this setup a live Nous Portal token) must never be sent to some
// other provider just because its own key was left unset; that call goes out
// without one and fails visibly instead.
func (l *LLMClient) fallbackKey(base string) string {
	if l.FallbackKey != "" {
		return l.FallbackKey
	}
	if sameHost(base, l.BaseURL) {
		return l.resolveKey()
	}
	return ""
}

// sameHost reports whether two endpoint URLs name the same host (ports aside).
func sameHost(a, b string) bool {
	ua, errA := url.Parse(a)
	ub, errB := url.Parse(b)
	return errA == nil && errB == nil && ua.Hostname() != "" && strings.EqualFold(ua.Hostname(), ub.Hostname())
}

// SetFallback configures the fallback model used when the primary call fails.
// Values are trimmed of surrounding spaces, so an env var with a stray newline
// or space still yields a usable model name.
func (l *LLMClient) SetFallback(base, model, key string) {
	l.FallbackBaseURL = strings.TrimSpace(base)
	l.FallbackModel = strings.TrimSpace(model)
	l.FallbackKey = strings.TrimSpace(key)
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
// set it is read live, so a rotating token that something of your own rewrites
// never goes stale the way a startup-frozen env value does. The file must hold a
// bare token as plain text; a file that looks like JSON is refused. Any read or
// parse failure falls back to the static APIKey.
func (l *LLMClient) resolveKey() string {
	if k := l.pushed.Load(); k != nil && *k != "" {
		return *k
	}
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
	if strings.HasPrefix(strings.TrimSpace(string(b)), "{") && l.keyFileWarned.CompareAndSwap(false, true) {
		log.Printf("HYATLAS_LLM_KEY_FILE looks like JSON, which is refused: it must hold a plain-text token (falling back to HYATLAS_LLM_KEY)")
	}
	return l.APIKey
}

// extractKey reads a bearer key from a plain-text file whose trimmed contents are
// the token. Content that starts with "{" is JSON, such as another program's
// credential file, and yields no key, so the server never parses such files.
func extractKey(b []byte) string {
	s := strings.TrimSpace(string(b))
	if s == "" || strings.HasPrefix(s, "{") {
		return ""
	}
	return s
}

// Extraction is the structured result of one System1 pass over a single input.
// Keys it does not name, such as the retired summary, knowledge and schemas, are
// ignored by the decoder, so a reply that still sends them keeps decoding.
type Extraction struct {
	Facts     []Fact     `json:"facts"`
	Intention *Intention `json:"intention,omitempty"`
	// IntentionsDone lists open intentions (shown to the model as context) that
	// this input shows finished or abandoned. Empty when the model omits it.
	IntentionsDone idList `json:"intentions_done,omitempty"`
	// Pages are entity-page updates: the durable things this input is about, with
	// their current state and what happened. Empty when the model omits them.
	Pages pageUpdates `json:"pages,omitempty"`
	// Experiences are reusable lessons from a task this input shows solved. Empty
	// when the model omits them.
	Experiences []Experience `json:"experiences,omitempty"`
}

// Experience is a reusable lesson from a task solved in this input: what was
// being done, what worked, the environment it depends on, and how sure that is.
type Experience struct {
	Task        string `json:"task"`
	Solution    string `json:"solution"`
	Environment string `json:"environment"`
	Confidence  string `json:"confidence"` // high / medium / low
}

// UnmarshalJSON decodes an experience tolerantly and never returns an error, so
// one malformed lesson cannot discard the whole extraction. A text field takes a
// JSON string and becomes empty for any other type, a number included, which is
// why it uses stringOnly rather than looseString. Confidence takes a string
// as it is, a number (0.75 or more is high, under 0.4 is low, otherwise medium)
// or a bool (true is high, false is low). A value that is not an object yields
// an empty Experience.
func (e *Experience) UnmarshalJSON(b []byte) error {
	*e = Experience{}
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(b, &obj); err != nil {
		return nil
	}
	e.Task = stringOnly(obj["task"])
	e.Solution = stringOnly(obj["solution"])
	e.Environment = stringOnly(obj["environment"])
	e.Confidence = looseConfidence(obj["confidence"])
	return nil
}

// looseString is the one tolerance rule for a text field the model sends. A JSON
// string is used as it is, a number or bool is taken as its JSON spelling, and a
// missing, null, object or array value gives "". It never fails, so a field of the
// wrong type empties itself and the rest of the reply still decodes.
func looseString(raw json.RawMessage) string {
	t := bytes.TrimSpace(raw)
	if len(t) == 0 {
		return ""
	}
	var s string
	if json.Unmarshal(t, &s) == nil {
		return s
	}
	if t[0] == '-' || (t[0] >= '0' && t[0] <= '9') || string(t) == "true" || string(t) == "false" {
		return string(t)
	}
	return ""
}

// stringOnly returns raw as a string, or "" when raw is missing, null or not a JSON
// string. Experience fields use it because a lesson with a number in a text field
// keeps that field empty rather than reading the number as text.
func stringOnly(raw json.RawMessage) string {
	var s string
	if json.Unmarshal(raw, &s) != nil {
		return ""
	}
	return s
}

// looseConfidence maps a raw confidence value to high, medium or low, as
// Experience.UnmarshalJSON describes. A missing, null or unusable value yields "".
func looseConfidence(raw json.RawMessage) string {
	if t := bytes.TrimSpace(raw); len(t) == 0 || string(t) == "null" {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var f float64
	if json.Unmarshal(raw, &f) == nil {
		switch {
		case f >= 0.75:
			return "high"
		case f < 0.4:
			return "low"
		default:
			return "medium"
		}
	}
	var t bool
	if json.Unmarshal(raw, &t) == nil {
		if t {
			return "high"
		}
		return "low"
	}
	return ""
}

// Fact is a single durable fact (L3).
type Fact struct {
	Data  string `json:"data"`
	Layer string `json:"layer"` // user_preferences / project_state / technical_lesson / decision / negative_knowledge
	// When is the date the fact's event happened or is due (YYYY-MM-DD or YYYY-MM),
	// resolved by the model against the write date. Empty for timeless facts.
	When string `json:"when,omitempty"`
	// Replaces names related facts (shown to the model as context) that this fact
	// contradicts: same subject, new value or state.
	Replaces idList `json:"replaces,omitempty"`
	// Updates names related facts this one extends without contradicting: more
	// detail, or the same kind of event again. The new text carries the old one.
	Updates idList `json:"updates,omitempty"`
}

// idList is a list of memory ids the model sent back. Small models get the
// shape wrong — one id as a bare string, a comma-separated string, numbers or
// objects mixed into the list — and a decode error here would throw away the
// whole extraction over a field that only refines it. So it accepts a string or
// a list of strings, drops anything else, and never returns an error.
type idList []string

// UnmarshalJSON implements the tolerant decode described on idList.
func (l *idList) UnmarshalJSON(b []byte) error {
	*l = nil
	var one string
	if err := json.Unmarshal(b, &one); err == nil {
		*l = splitIDs(one)
		return nil
	}
	var many []any
	if err := json.Unmarshal(b, &many); err != nil {
		return nil
	}
	for _, v := range many {
		if s, ok := v.(string); ok {
			*l = append(*l, splitIDs(s)...)
		}
	}
	return nil
}

// splitIDs splits one string on commas and whitespace, since ids contain neither.
func splitIDs(s string) []string {
	return strings.FieldsFunc(s, func(r rune) bool {
		return unicode.IsSpace(r) || strings.ContainsRune(",;\"'", r)
	})
}

// Intention is the user's current goal / intent (L7). Accepts string or object.
type Intention struct {
	Goal string
	// Due is when the goal is due (YYYY-MM-DD or YYYY-MM), resolved by the model
	// against the write date. Empty when no deadline was stated.
	Due string
	// Replaces names open intentions (shown to the model as context) that this
	// goal continues or supersedes.
	Replaces idList
}

// UnmarshalJSON accepts `"intention": "text"` and `"intention":{"goal":"..."}`. Any
// other value never fails the decode: a number or bool becomes the goal text, and
// anything else leaves the goal empty, so the rest of the reply survives.
func (i *Intention) UnmarshalJSON(b []byte) error {
	*i = Intention{}
	var obj struct {
		Goal     json.RawMessage `json:"goal"`
		Due      json.RawMessage `json:"due"`
		Replaces idList          `json:"replaces"`
	}
	if err := json.Unmarshal(b, &obj); err != nil {
		// Not an object: the value itself is the goal text, when it is text.
		i.Goal = looseString(b)
		return nil
	}
	i.Goal = looseString(obj.Goal)
	i.Due = looseString(obj.Due)
	i.Replaces = obj.Replaces
	return nil
}

// CompleteWith runs one structured extraction producing all layers, with one
// reinforced retry: small models sometimes reply to the input conversationally
// instead of extracting (their prose then fails JSON parsing), and a single
// firm reminder recovers most of those. It also shows the model what the store
// already holds about this input, so it can say which stored facts and open
// intentions the input updates, finishes or abandons. An empty ec yields the
// plain prompt.
//
// The reinforced retry runs only when the chat call succeeded and its reply did
// not parse. A failed chat call (an HTTP error, a timeout, an empty reply) is
// returned at once, because the same request would fail the same way again.
func (l *LLMClient) CompleteWith(ctx context.Context, text string, ec ExtractContext) (*Extraction, error) {
	ex, chatFailed, err := l.completeOnce(ctx, text, false, ec)
	if err == nil {
		return ex, nil
	}
	if chatFailed {
		return nil, err
	}
	ex2, _, err2 := l.completeOnce(ctx, text, true, ec)
	if err2 == nil {
		return ex2, nil
	}
	return nil, fmt.Errorf("after retry: %w", err2)
}

// completeOnce makes one chat call and parses its reply. It also reports whether
// the chat call itself failed, which tells CompleteWith a retry would not help.
func (l *LLMClient) completeOnce(ctx context.Context, text string, reinforce bool, ec ExtractContext) (*Extraction, bool, error) {
	// System1: this pass sees ONE turn, so it may only produce what a single turn
	// can actually evidence: facts, the current intention and entity pages.
	// Summaries come from episodes (episode.go), not from each turn. L5 knowledge
	// and L6 schemas are deliberately NOT requested here: a recurring pattern is by
	// definition not observable in one turn, and asking for one produced guesses
	// that then competed with the real thing at retrieval time. The slow path owns
	// those layers.
	system := extractSystemPrompt(!ec.Empty())
	// The date lets the model resolve "yesterday" or "next Tuesday" into a fact's
	// "when"; without it relative dates could only be copied as words.
	user := todayLine(time.Now()) + ec.promptSection() + "Input: " + text
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
		return nil, true, err
	}
	ex, err := parseExtraction(content)
	if err != nil {
		return nil, false, err
	}
	return ex, false, nil
}

// chat sends one chat-completions request and returns the assistant's text,
// retrying once on the fallback model when the primary fails.
//
// The primary gets two thirds of the caller's remaining time, so a hung
// free-tier endpoint cannot consume the whole budget and leave nothing for the
// fallback. With no caller deadline the primary is bounded at 120s. A fallback
// is tried on any primary error, because a 5xx, a timeout and an empty reply
// all mean the same thing to the caller: this model did not answer this time.
func (l *LLMClient) chat(ctx context.Context, messages []map[string]string, temp float64) (string, error) {
	if l.FallbackModel == "" {
		return l.chatOnce(ctx, l.BaseURL, l.Model, l.resolveKey(), messages, temp)
	}
	budget := 120 * time.Second
	if dl, ok := ctx.Deadline(); ok {
		budget = time.Until(dl)
	}
	pctx, cancel := context.WithTimeout(ctx, budget*2/3)
	content, errPrimary := l.chatOnce(pctx, l.BaseURL, l.Model, l.resolveKey(), messages, temp)
	cancel()
	if errPrimary == nil {
		return content, nil
	}
	// A caller that has already given up gets the primary's error, not a
	// fallback call nobody is waiting on.
	if ctx.Err() != nil {
		return "", errPrimary
	}
	base := l.FallbackBaseURL
	if base == "" {
		base = l.BaseURL
	}
	log.Printf("llm: primary %s failed (%v); trying fallback %s", l.Model, errPrimary, l.FallbackModel)
	content, errFallback := l.chatOnce(ctx, base, l.FallbackModel, l.fallbackKey(base), messages, temp)
	if errFallback != nil {
		return "", fmt.Errorf("primary: %v; fallback: %w", errPrimary, errFallback)
	}
	return content, nil
}

// chatOnce sends one request to one model and returns the assistant's text.
//
// Shared by the primary and fallback paths so the two cannot drift on the parts
// that are easy to get wrong once and hard to notice: the Cloudflare WAF
// rejects Go's default User-Agent with a 403, and the key must be resolved per
// request because a rotating JWT goes stale if frozen at startup. A non-empty
// key overrides that resolution; an empty one falls back to resolveKey.
func (l *LLMClient) chatOnce(ctx context.Context, base, model, key string, messages []map[string]string, temp float64) (string, error) {
	body, _ := json.Marshal(map[string]any{
		"model":       model,
		"messages":    messages,
		"temperature": temp,
	})
	req, err := http.NewRequestWithContext(ctx, "POST", base+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	// The Nous Portal sits behind Cloudflare, which 403s Go's default
	// "Go-http-client" User-Agent; identify honestly so the WAF lets the
	// extraction call through.
	req.Header.Set("User-Agent", "HyAtlas/"+Version+" (+https://github.com/tuancookiez-hub/HyAtlas-Memory)")
	// Resolve the key per request so a rotating credential (KeyFile) never
	// goes stale the way a startup-frozen env value does.
	k := key
	if k != "" {
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

// decodeLenient decodes the JSON object in an LLM reply into v. It tries the whole
// reply first, then the span from the first "{" to the last "}", since a model may
// wrap the object in fences or prose. On failure the error names up to 80 bytes of
// the raw reply.
func decodeLenient(content string, v any) error {
	content = trimFences(content)
	err := json.Unmarshal([]byte(content), v)
	if err == nil {
		return nil
	}
	start, end := strings.Index(content, "{"), strings.LastIndex(content, "}")
	if start != -1 && end > start {
		err = json.Unmarshal([]byte(content[start:end+1]), v)
		if err == nil {
			return nil
		}
	}
	return fmt.Errorf("%w (raw %.80s)", err, content)
}

// parseExtraction tolerantly extracts the JSON object from the LLM reply.
func parseExtraction(content string) (*Extraction, error) {
	var ex Extraction
	if err := decodeLenient(content, &ex); err != nil {
		return nil, fmt.Errorf("extraction parse: %w", err)
	}
	// normalize fields
	if ex.Facts == nil {
		ex.Facts = []Fact{}
	}
	return &ex, nil
}

// todayLine is the date line put before an extraction input, in local time
// because "today" is the user's day, not UTC's.
func todayLine(now time.Time) string {
	return "Today: " + now.Format("2006-01-02") + " (" + now.Weekday().String() + ")\n\n"
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
