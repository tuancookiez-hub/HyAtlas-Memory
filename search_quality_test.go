package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// sqHit is one hit as /api/v1/search returns it, enough for these tests.
type sqHit struct {
	MemoryID string `json:"memory_id"`
	Content  string `json:"content"`
	Layer    string `json:"layer"`
}

// sqResp is the three-channel body of /api/v1/search.
type sqResp struct {
	Memories struct {
		Profile   []sqHit `json:"profile"`
		Proactive []sqHit `json:"proactive"`
		Normal    []sqHit `json:"normal"`
	} `json:"memories"`
}

// sqSearch runs handleSearch with body and returns the status code and the hits in
// all three channels, in channel order.
func sqSearch(t *testing.T, srv *Server, body string) (int, []sqHit) {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/v1/search", strings.NewReader(body))
	w := httptest.NewRecorder()
	srv.handleSearch(w, req)
	if w.Code != http.StatusOK {
		return w.Code, nil
	}
	var resp sqResp
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode search response: %v", err)
	}
	var all []sqHit
	all = append(all, resp.Memories.Profile...)
	all = append(all, resp.Memories.Proactive...)
	all = append(all, resp.Memories.Normal...)
	return w.Code, all
}

// sqIDs returns the IDs of hits, in order.
func sqIDs(hits []SearchHit) []string {
	var ids []string
	for _, h := range hits {
		ids = append(ids, h.ID)
	}
	return ids
}

// TestRefineHitsFloorDedupeAndLimit checks the floor, the duplicate drop and the limit
// of refineHits on a fixed list of hits.
func TestRefineHitsFloorDedupeAndLimit(t *testing.T) {
	hits := []SearchHit{
		{ID: "a", Content: "Likes  Tea", Score: 0.9, Layer: memory.L3Fact},
		{ID: "b", Content: "likes tea", Score: 0.9, Layer: memory.L1Profile},
		{ID: "c", Content: "rides bikes", Score: 0.8, Layer: memory.L3Fact},
		{ID: "d", Content: "noise", Score: 0.5, Layer: memory.L3Fact},
	}
	if got := sqIDs(refineHits(hits, 0.6, 5)); strings.Join(got, ",") != "b,c" {
		t.Errorf("floor 0.6 limit 5: want [b c], got %v", got)
	}
	if got := sqIDs(refineHits(hits, 0, 1)); strings.Join(got, ",") != "b" {
		t.Errorf("floor 0 limit 1: want [b], got %v", got)
	}
	if got := sqIDs(refineHits(hits, 0, 5)); strings.Join(got, ",") != "b,c,d" {
		t.Errorf("floor 0 limit 5: want [b c d], got %v", got)
	}
}

// TestSearchMinScoreFloor checks that the score floor drops off-topic hits, and that a
// request can override the server floor with min_score.
func TestSearchMinScoreFloor(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	if err := srv.store.Add(memory.L3Fact, newID(), "alice drinks green tea every morning",
		map[string]string{"user_id": "alice", "agent_id": "a1"}); err != nil {
		t.Fatal(err)
	}
	srv.minScore = 0.999

	const onTopic = `{"query":"alice drinks green tea every morning","user_id":"alice","agent_id":"a1"}`
	if code, hits := sqSearch(t, srv, onTopic); code != http.StatusOK || len(hits) != 1 {
		t.Errorf("on-topic query: want 1 hit, got code %d and %d hits", code, len(hits))
	}

	const offTopic = `{"query":"completely different words here","user_id":"alice","agent_id":"a1"}`
	if code, hits := sqSearch(t, srv, offTopic); code != http.StatusOK || len(hits) != 0 {
		t.Errorf("off-topic query with server floor: want 0 hits, got code %d and %d hits", code, len(hits))
	}

	const offTopicNoFloor = `{"query":"completely different words here","user_id":"alice","agent_id":"a1","min_score":0}`
	if code, hits := sqSearch(t, srv, offTopicNoFloor); code != http.StatusOK || len(hits) < 1 {
		t.Errorf("off-topic query with min_score 0: want at least 1 hit, got code %d and %d hits", code, len(hits))
	}
}

// TestSearchDropsMirrorDuplicate checks that an L3 fact and its L1 Profile mirror with
// the same text come back as one hit, the profile one.
func TestSearchDropsMirrorDuplicate(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	const text = "alice prefers dark mode"
	meta := map[string]string{"user_id": "alice", "agent_id": "a1", "source_id": "r1"}
	if err := srv.store.Add(memory.L3Fact, newID(), text, meta); err != nil {
		t.Fatal(err)
	}
	if err := srv.store.Add(memory.L1Profile, newID(), text, meta); err != nil {
		t.Fatal(err)
	}

	const body = `{"query":"alice prefers dark mode","user_id":"alice","agent_id":"a1"}`
	code, hits := sqSearch(t, srv, body)
	if code != http.StatusOK {
		t.Fatalf("status: want 200, got %d", code)
	}
	if len(hits) != 1 {
		t.Fatalf("want exactly 1 hit after mirror drop, got %d: %+v", len(hits), hits)
	}
	if hits[0].Layer != string(memory.L1Profile) {
		t.Errorf("want layer %s, got %s", memory.L1Profile, hits[0].Layer)
	}
	var resp sqResp
	req := httptest.NewRequest("POST", "/api/v1/search", strings.NewReader(body))
	w := httptest.NewRecorder()
	srv.handleSearch(w, req)
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Memories.Profile) != 1 || len(resp.Memories.Normal) != 0 {
		t.Errorf("want the hit in the profile channel only, got profile %d normal %d",
			len(resp.Memories.Profile), len(resp.Memories.Normal))
	}
}

// sqLive returns the live (not superseded) and superseded docs of a layer for one owner.
func sqLive(t *testing.T, srv *Server, layer memory.Layer, all bool) (live, old []DocIndex) {
	t.Helper()
	var docs []DocIndex
	if all {
		docs, _ = srv.store.ListAll(layer, "alice", "a1", 50, 0, false)
	} else {
		docs, _ = srv.store.List(layer, "alice", "a1", 50, 0, false)
	}
	for _, d := range docs {
		if d.Meta["invalid_at"] != "" {
			old = append(old, d)
		} else {
			live = append(live, d)
		}
	}
	return live, old
}

// TestPromoteDedupeSupersedesRestatement checks that a restated fact replaces the old
// one and its L1 mirror, and that the old row keeps a superseded_by pointer.
func TestPromoteDedupeSupersedesRestatement(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ex := func() *Extraction {
		return &Extraction{Facts: []Fact{{Data: "alice prefers dark mode", Layer: "user_preferences"}}}
	}
	promoteExtractionDedupe(srv.store, ex(), "alice", "a1", "r1", 0.999)
	promoteExtractionDedupe(srv.store, ex(), "alice", "a1", "r2", 0.999)

	liveL3, _ := sqLive(t, srv, memory.L3Fact, false)
	if len(liveL3) != 1 {
		t.Fatalf("live L3 facts: want 1, got %d", len(liveL3))
	}
	liveL1, _ := sqLive(t, srv, memory.L1Profile, false)
	if len(liveL1) != 1 {
		t.Fatalf("live L1 profile rows: want 1, got %d", len(liveL1))
	}
	allL3, _ := srv.store.ListAll(memory.L3Fact, "alice", "a1", 50, 0, false)
	if len(allL3) != 2 {
		t.Fatalf("all L3 rows: want 2, got %d", len(allL3))
	}
	_, oldL3 := sqLive(t, srv, memory.L3Fact, true)
	if len(oldL3) != 1 {
		t.Fatalf("superseded L3 rows: want 1, got %d", len(oldL3))
	}
	if got := oldL3[0].Meta["superseded_by"]; got != liveL3[0].ID {
		t.Errorf("superseded_by: want %s, got %q", liveL3[0].ID, got)
	}
}

// TestPromoteDedupeOffKeepsBoth checks that with de-duplication off, a restatement is
// kept beside the old fact.
func TestPromoteDedupeOffKeepsBoth(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ex := func() *Extraction {
		return &Extraction{Facts: []Fact{{Data: "alice prefers dark mode", Layer: "user_preferences"}}}
	}
	promoteExtractionDedupe(srv.store, ex(), "alice", "a1", "r1", 0)
	promoteExtractionDedupe(srv.store, ex(), "alice", "a1", "r2", 0)

	live, _ := sqLive(t, srv, memory.L3Fact, false)
	if len(live) != 2 {
		t.Errorf("live L3 facts with dedupe off: want 2, got %d", len(live))
	}
}

// TestPromoteDedupeKeepsDifferentFacts checks that de-duplication leaves two facts with
// different text alone.
func TestPromoteDedupeKeepsDifferentFacts(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	promoteExtractionDedupe(srv.store, &Extraction{Facts: []Fact{{Data: "alice prefers dark mode", Layer: "user_preferences"}}},
		"alice", "a1", "r1", 0.999)
	promoteExtractionDedupe(srv.store, &Extraction{Facts: []Fact{{Data: "bob builds rockets in ohio", Layer: "user_preferences"}}},
		"alice", "a1", "r2", 0.999)

	live, _ := sqLive(t, srv, memory.L3Fact, false)
	if len(live) != 2 {
		t.Errorf("live L3 facts with different text: want 2, got %d", len(live))
	}
}
