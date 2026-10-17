package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// searchHits runs one search through the handler and returns the normal channel.
func searchHits(t *testing.T, srv *Server, query string) []map[string]any {
	t.Helper()
	body := `{"query":"` + query + `","user_id":"alice","agent_id":"a1","min_score":0}`
	w := httptest.NewRecorder()
	srv.handleSearch(w, httptest.NewRequest("POST", "/api/v1/search", strings.NewReader(body)))
	if w.Code != 200 {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	var resp struct {
		Memories map[string][]map[string]any `json:"memories"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	return resp.Memories["normal"]
}

// A hit that replaced an older fact carries that fact under "history", and a fact
// with an event date carries it as "event_date".
func TestSearchReturnsHistoryAndEventDate(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	old, cur := newID(), newID()
	if err := srv.store.Add(memory.L3Fact, old, "HyAtlas extraction model is poolside",
		map[string]string{"user_id": "alice", "agent_id": "a1", "ts": "2026-10-01T00:00:00Z"}); err != nil {
		t.Fatal(err)
	}
	if err := srv.store.Add(memory.L3Fact, cur, "HyAtlas extraction model is stepfun",
		map[string]string{"user_id": "alice", "agent_id": "a1", "ts": "2026-10-12T00:00:00Z", metaEventAt: "2026-10-12"}); err != nil {
		t.Fatal(err)
	}
	if _, err := srv.store.supersedeWith([]string{old}, cur, map[string]string{metaSupersededHow: howReplaced}); err != nil {
		t.Fatal(err)
	}
	var hit map[string]any
	for _, h := range searchHits(t, srv, "HyAtlas extraction model") {
		if h["memory_id"] == cur {
			hit = h
		}
	}
	if hit == nil {
		t.Fatal("current fact not returned")
	}
	if hit["event_date"] != "2026-10-12" {
		t.Errorf("event_date = %v, want 2026-10-12", hit["event_date"])
	}
	hist, _ := hit["history"].([]any)
	if len(hist) != 1 || !strings.Contains(hist[0].(map[string]any)["content"].(string), "poolside") {
		t.Errorf("history = %v, want the poolside fact", hit["history"])
	}
}

// A query naming a day ranks a fact dated that day above an equally relevant fact
// dated elsewhere.
func TestSearchTimeWindowFavoursDatedFact(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	day := time.Now().AddDate(0, 0, -3).Format("2006-01-02")
	other := time.Now().AddDate(0, 0, -40).Format("2006-01-02")
	in, out := newID(), newID()
	for id, d := range map[string]string{in: day, out: other} {
		if err := srv.store.Add(memory.L3Fact, id, "deployed the trading desk release",
			map[string]string{"user_id": "alice", "agent_id": "a1", "ts": time.Now().UTC().Format(time.RFC3339), metaEventAt: d}); err != nil {
			t.Fatal(err)
		}
	}
	hits := searchHits(t, srv, "deployed the trading desk release on "+day)
	if len(hits) == 0 || hits[0]["memory_id"] != in {
		t.Fatalf("first hit = %v, want the fact dated %s", hits, day)
	}
}

// In-window facts may score windowFloorRelax below the floor, never below zero.
func TestWindowFloor(t *testing.T) {
	for in, want := range map[float64]float64{0.60: 0.40, 0.10: 0, 0: 0} {
		if got := windowFloor(in); got < want-1e-9 || got > want+1e-9 {
			t.Errorf("windowFloor(%v) = %v, want %v", in, got, want)
		}
	}
}
