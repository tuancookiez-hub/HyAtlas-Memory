package main

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// nowAddRow stores one row for owner "u" (or the given user) stamped `age` ago.
func nowAddRow(t *testing.T, s *MemoryStore, layer memory.Layer, id, text, user string, age time.Duration) {
	t.Helper()
	ctxAdd(t, s, layer, id, text, user, time.Now().Add(-age))
}

// nowCalls counts the requests the fake LLM has received so far.
func nowCalls(rec *ctxLLM) int {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	return len(rec.bodies)
}

func TestNowRefreshWritesAndPersists(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return "Active:\n- ship P2" })
	nowAddRow(t, srv.store, memory.L7Intention, "i1", "ship P2", "u", time.Hour)

	dir := t.TempDir()
	n := NewNowPages(dir, srv.store, srv.llm, time.Hour)
	if err := n.refresh(context.Background(), "u", "a"); err != nil {
		t.Fatal(err)
	}
	p, ok := n.Get("u", "a")
	if !ok || p.Text != "Active:\n- ship P2" {
		t.Fatalf("page: want written reply, got %+v ok=%v", p, ok)
	}
	if p.Updated == "" {
		t.Fatal("page: Updated not set")
	}

	reloaded := NewNowPages(dir, srv.store, srv.llm, time.Hour)
	q, ok := reloaded.Get("u", "a")
	if !ok || q.Text != p.Text {
		t.Fatalf("reload: want %q, got %+v ok=%v", p.Text, q, ok)
	}
}

func TestNowPromptContainsWindowedRowsOnly(t *testing.T) {
	srv, rec := newCtxServer(t, func() string { return "Active:\n- x" })
	nowAddRow(t, srv.store, memory.L7Intention, "i-mine", "intent-mine", "u", time.Hour)
	nowAddRow(t, srv.store, memory.L7Intention, "i-old", "intent-old", "u", 10*24*time.Hour)
	nowAddRow(t, srv.store, memory.L7Intention, "i-other", "intent-other", "other", time.Hour)
	nowAddRow(t, srv.store, memory.L4Summary, "s-mine", "summary-mine", "u", 24*time.Hour)
	nowAddRow(t, srv.store, memory.L4Summary, "s-old", "summary-old", "u", 5*24*time.Hour)
	nowAddRow(t, srv.store, memory.L4Summary, "s-other", "summary-other", "other", time.Hour)

	n := NewNowPages(t.TempDir(), srv.store, srv.llm, time.Hour)
	n.pages[nowOwnerKey("u", "a")] = NowPage{Text: "current-page-text"}
	if err := n.refresh(context.Background(), "u", "a"); err != nil {
		t.Fatal(err)
	}
	body := rec.last()
	for _, want := range []string{"intent-mine", "summary-mine", "current-page-text"} {
		if !strings.Contains(body, want) {
			t.Errorf("prompt missing %q", want)
		}
	}
	for _, bad := range []string{"intent-other", "intent-old", "summary-other", "summary-old"} {
		if strings.Contains(body, bad) {
			t.Errorf("prompt should not contain %q", bad)
		}
	}
}

func TestNowTouchNilLLMDoesNothing(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return "x" })
	nowAddRow(t, srv.store, memory.L7Intention, "i1", "goal", "u", time.Hour)

	n := NewNowPages(t.TempDir(), srv.store, nil, time.Hour)
	n.Touch("u", "a")
	time.Sleep(50 * time.Millisecond)
	if _, ok := n.Get("u", "a"); ok {
		t.Fatal("nil llm: Touch should not create a page")
	}
}

func TestNowTouchTwiceCallsLLMOnce(t *testing.T) {
	srv, rec := newCtxServer(t, func() string { return "Active:\n- x" })
	nowAddRow(t, srv.store, memory.L7Intention, "i1", "goal", "u", time.Hour)

	n := NewNowPages(t.TempDir(), srv.store, srv.llm, time.Hour)
	n.Touch("u", "a")
	n.Touch("u", "a")

	deadline := time.Now().Add(2 * time.Second)
	for nowCalls(rec) == 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	// Give a second, wrongly started refresh time to show up before counting.
	time.Sleep(100 * time.Millisecond)
	if got := nowCalls(rec); got != 1 {
		t.Fatalf("LLM calls: want 1, got %d", got)
	}
}

func TestNowEmptyReplyKeepsOldPage(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return "" })
	nowAddRow(t, srv.store, memory.L7Intention, "i1", "goal", "u", time.Hour)

	n := NewNowPages(t.TempDir(), srv.store, srv.llm, time.Hour)
	n.pages[nowOwnerKey("u", "a")] = NowPage{Text: "old page", Updated: "2020-01-01T00:00:00Z"}
	// chat reports an empty message as an error; either way the page must survive.
	_ = n.refresh(context.Background(), "u", "a")
	p, _ := n.Get("u", "a")
	if p.Text != "old page" {
		t.Fatalf("empty reply: want old page kept, got %q", p.Text)
	}
}

func TestNowReplyIsClampedToTwelveLines(t *testing.T) {
	var lines []string
	for i := 1; i <= 20; i++ {
		lines = append(lines, "line "+strings.Repeat("y", i))
	}
	reply := strings.Join(lines, "\n")
	srv, _ := newCtxServer(t, func() string { return reply })
	nowAddRow(t, srv.store, memory.L7Intention, "i1", "goal", "u", time.Hour)

	n := NewNowPages(t.TempDir(), srv.store, srv.llm, time.Hour)
	if err := n.refresh(context.Background(), "u", "a"); err != nil {
		t.Fatal(err)
	}
	p, _ := n.Get("u", "a")
	got := strings.Split(p.Text, "\n")
	if len(got) != 12 {
		t.Fatalf("lines: want 12, got %d", len(got))
	}
	if got[11] != lines[11] {
		t.Fatalf("last kept line: want %q, got %q", lines[11], got[11])
	}
}

func TestNowReplyIsClampedToBytes(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return strings.Repeat("é", 2000) })
	nowAddRow(t, srv.store, memory.L7Intention, "i1", "goal", "u", time.Hour)

	n := NewNowPages(t.TempDir(), srv.store, srv.llm, time.Hour)
	if err := n.refresh(context.Background(), "u", "a"); err != nil {
		t.Fatal(err)
	}
	p, _ := n.Get("u", "a")
	if len(p.Text) > nowMaxBytes {
		t.Fatalf("bytes: want <= %d, got %d", nowMaxBytes, len(p.Text))
	}
	if !strings.HasPrefix(strings.Repeat("é", 2000), p.Text) {
		t.Fatal("clamped page is not valid UTF-8 text")
	}
}

func TestNowHandleNow(t *testing.T) {
	n := NewNowPages(t.TempDir(), nil, nil, time.Hour)
	n.pages[nowOwnerKey("u", "a")] = NowPage{Text: "Active:\n- x", Updated: "2024-01-01T00:00:00Z"}

	get := func(query string) []byte {
		t.Helper()
		w := httptest.NewRecorder()
		n.HandleNow(w, httptest.NewRequest("GET", "/api/v1/now?"+query, nil))
		if w.Code != 200 {
			t.Fatalf("status: want 200, got %d", w.Code)
		}
		return w.Body.Bytes()
	}

	var hit struct {
		Now *NowPage `json:"now"`
	}
	if err := json.Unmarshal(get("user_id=u&agent_id=a"), &hit); err != nil {
		t.Fatal(err)
	}
	if hit.Now == nil || hit.Now.Text != "Active:\n- x" {
		t.Fatalf("hit: want page, got %+v", hit.Now)
	}

	var miss struct {
		Now *NowPage `json:"now"`
	}
	if err := json.Unmarshal(get("user_id=nobody&agent_id=a"), &miss); err != nil {
		t.Fatal(err)
	}
	if miss.Now != nil {
		t.Fatalf("miss: want null, got %+v", miss.Now)
	}
}

func TestResolveNowEvery(t *testing.T) {
	cases := map[string]time.Duration{
		"":        15 * time.Minute,
		"off":     0,
		"0":       0,
		"2m":      2 * time.Minute,
		"garbage": 15 * time.Minute,
	}
	for v, want := range cases {
		t.Setenv("HYATLAS_NOW_EVERY", v)
		if got := resolveNowEvery(); got != want {
			t.Errorf("HYATLAS_NOW_EVERY=%q: want %s, got %s", v, want, got)
		}
	}
}
