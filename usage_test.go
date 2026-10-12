package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

func TestUsedInReply(t *testing.T) {
	cases := []struct {
		name   string
		memory string
		reply  string
		query  string
		want   bool
	}{
		{"term overlap yes", "the sync worker batches writes every thirty seconds", "the sync worker batches all writes", "", true},
		{"term overlap no", "the sync worker batches writes every thirty seconds", "weather is mild and the sky is clear", "", false},
		{"identifier match", "sync server listens on port 19528", "I checked it, the server uses 19528 now", "", true},
		{"short memory without identifier", "likes coffee", "likes coffee a lot", "", false},
		{"empty memory", "", "anything", "", false},
		{"empty reply", "the sync worker batches writes", "", "", false},
		// The reply repeats the question; the memory added nothing it used.
		{"only question words", "HyAtlas v4.1.2 fixes extraction pipeline", "HyAtlas extraction now runs on stepfun", "what model does hyatlas use for extraction", false},
		{"beyond the question", "HyAtlas extraction switched from poolside to stepfun after 403s", "HyAtlas extraction runs on stepfun since poolside kept failing", "what model does hyatlas use for extraction", true},
		{"identifier the question named", "sync server listens on port 19528", "yes, 19528 is the port", "is 19528 the port", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := usedInReply(c.memory, c.reply, c.query); got != c.want {
				t.Fatalf("usedInReply(%q, %q) = %v, want %v", c.memory, c.reply, got, c.want)
			}
		})
	}
}

// recordN records n recalls of one memory, each with the given reply.
func recordN(u *UsageStats, id, text, reply string, n int) {
	for i := 0; i < n; i++ {
		u.Record([]string{id}, map[string]string{id: text}, reply, "")
	}
}

func TestRecordAndFactor(t *testing.T) {
	const text = "the extraction model is poolside and returns forbidden"
	dir := t.TempDir()
	u := NewUsageStats(dir)

	recordN(u, "few", text, "unrelated words", 2)
	if f := u.Factor("few"); f != 1.0 {
		t.Fatalf("below 3 shows: factor = %v, want 1.0", f)
	}

	recordN(u, "ignored", text, "unrelated words", 10)
	if f := u.Factor("ignored"); f >= 1.0 {
		t.Fatalf("10 shows, 0 used: factor = %v, want < 1.0", f)
	}

	recordN(u, "used", text, text, 10)
	if f := u.Factor("used"); f <= 1.0 {
		t.Fatalf("10 shows, 10 used: factor = %v, want > 1.0", f)
	}
	if f := u.Factor("unknown"); f != 1.0 {
		t.Fatalf("unknown id: factor = %v, want 1.0", f)
	}

	reloaded := NewUsageStats(dir)
	if a, b := reloaded.Factor("used"), u.Factor("used"); a != b {
		t.Fatalf("reloaded factor = %v, want %v", a, b)
	}
	if a, b := reloaded.Factor("ignored"), u.Factor("ignored"); a != b {
		t.Fatalf("reloaded factor for ignored = %v, want %v", a, b)
	}
}

func TestRerankUsage(t *testing.T) {
	const text = "the extraction model is poolside and returns forbidden"
	u := NewUsageStats(t.TempDir())
	recordN(u, "ignored", text, "unrelated words", 10)
	recordN(u, "used", text, text, 10)

	hits := []SearchHit{{ID: "ignored", Score: 0.5}, {ID: "used", Score: 0.5}}
	out := rerankUsage(hits, u)
	if len(out) != 2 || out[0].ID != "used" || out[1].ID != "ignored" {
		t.Fatalf("rerank order = %v, want used before ignored", out)
	}
	if out[0].Score != 0.5 || out[1].Score != 0.5 {
		t.Fatalf("rerank changed Score: %v %v", out[0].Score, out[1].Score)
	}
	if hits[0].ID != "ignored" {
		t.Fatalf("rerank mutated the input slice")
	}

	same := rerankUsage(hits, nil)
	if len(same) != 2 || same[0].ID != "ignored" || same[1].ID != "used" {
		t.Fatalf("nil stats changed order: %v", same)
	}
}

func TestHandleFeedback(t *testing.T) {
	dir := t.TempDir()
	store, err := NewMemoryStore(context.Background(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(store.Close)

	const uText = "the sync worker batches writes every thirty seconds"
	meta := func(user string) map[string]string {
		return map[string]string{"user_id": user, "agent_id": "a"}
	}
	if err := store.Add(memory.L3Fact, "u1", uText, meta("u")); err != nil {
		t.Fatal(err)
	}
	if err := store.Add(memory.L3Fact, "u2", "cache entries expire after an hour", meta("u")); err != nil {
		t.Fatal(err)
	}
	if err := store.Add(memory.L3Fact, "x1", "billing runs on the first of the month", meta("x")); err != nil {
		t.Fatal(err)
	}

	u := NewUsageStats(dir)
	h := u.HandleFeedback(store, nil)
	body, _ := json.Marshal(map[string]any{
		"recalled_ids": []string{"u1", "u2", "x1"},
		"reply":        "the sync worker batches all writes every thirty seconds",
		"user_id":      "u",
		"agent_id":     "a",
	})
	rec := httptest.NewRecorder()
	h(rec, httptest.NewRequest(http.MethodPost, "/api/v1/feedback", bytes.NewReader(body)))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body %s", rec.Code, rec.Body.String())
	}
	var got struct {
		OK    bool `json:"ok"`
		Shown int  `json:"shown"`
		Used  int  `json:"used"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if !got.OK || got.Shown != 2 || got.Used != 1 {
		t.Fatalf("response = %+v, want ok shown=2 used=1", got)
	}
	if e := u.m["x1"]; e.Shown != 0 {
		t.Fatalf("other owner's memory was counted: %+v", e)
	}

	bad := httptest.NewRecorder()
	h(bad, httptest.NewRequest(http.MethodPost, "/api/v1/feedback", bytes.NewReader([]byte("{not json"))))
	if bad.Code != http.StatusBadRequest {
		t.Fatalf("bad body status = %d, want 400", bad.Code)
	}
}

// A memory written under an alias of the caller's user ID counts; with no alias it
// is another owner's and does not.
func TestHandleFeedbackAliases(t *testing.T) {
	dir := t.TempDir()
	store, err := NewMemoryStore(context.Background(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(store.Close)
	if err := store.Add(memory.L3Fact, "d1", "the sync worker batches writes every thirty seconds",
		map[string]string{"user_id": "discord-123", "agent_id": "a"}); err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(map[string]any{"recalled_ids": []string{"d1"},
		"reply": "the sync worker batches all writes every thirty seconds", "user_id": "default"})
	post := func(expand func([]string) []string) int {
		u := NewUsageStats(t.TempDir())
		rec := httptest.NewRecorder()
		u.HandleFeedback(store, expand)(rec, httptest.NewRequest(http.MethodPost, "/api/v1/feedback", bytes.NewReader(body)))
		var got struct{ Shown int }
		_ = json.Unmarshal(rec.Body.Bytes(), &got)
		return got.Shown
	}
	if n := post(nil); n != 0 {
		t.Fatalf("without aliases shown = %d, want 0", n)
	}
	alias := func(ids []string) []string { return append(ids, "discord-123") }
	if n := post(alias); n != 1 {
		t.Fatalf("with aliases shown = %d, want 1", n)
	}
}
