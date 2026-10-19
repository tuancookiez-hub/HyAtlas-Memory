package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// addOwned stores a row for the given owner with extra metadata and returns its id.
func addOwned(t *testing.T, st *MemoryStore, layer memory.Layer, text, user string, extra map[string]string) string {
	t.Helper()
	meta := map[string]string{"user_id": user, "agent_id": "a", "ts": nowTS()}
	for k, v := range extra {
		meta[k] = v
	}
	id := newID()
	if err := st.Add(layer, id, text, meta); err != nil {
		t.Fatal(err)
	}
	return id
}

// TestParseLinksIsTolerant checks that malformed items, unknown relations and empty
// ids are skipped while good items are kept in order.
func TestParseLinksIsTolerant(t *testing.T) {
	got := parseLinks("m1:contrasts, m7:same_series,bad,:related,m2:unknown,m1:related,m3:RELATED,")
	want := []factLink{{"m1", "contrasts"}, {"m7", "same_series"}, {"m3", "related"}}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("link %d: got %v, want %v", i, got[i], want[i])
		}
	}
	if len(parseLinks("")) != 0 {
		t.Error("empty string should give no links")
	}
}

// TestAdmitLinkedBringsInMissedFact checks that a hit linked to a fact the query
// missed brings it into the visible results, while a superseded row, another owner's
// row, a missing id and malformed links are ignored.
func TestAdmitLinkedBringsInMissedFact(t *testing.T) {
	st, _ := newPagesStore(t)
	other := addOwned(t, st, memory.L3Fact, "other owner fact", "v", nil)
	dead := addOwned(t, st, memory.L3Fact, "retired fact", "u", map[string]string{metaInvalidAt: nowTS()})
	live := addOwned(t, st, memory.L3Fact, "the contrasting fact", "u", nil)
	src := addOwned(t, st, memory.L3Fact, "source fact", "u",
		map[string]string{metaLinks: other + ":related," + dead + ":related,ghost:related,junk," + live + ":contrasts"})
	docs := st.GetMany([]string{src})
	hits := []SearchHit{{ID: src, Content: "source fact", Score: 0.8, Layer: memory.L3Fact, Meta: docs[0].Meta},
		{ID: "x1", Layer: memory.L3Fact}, {ID: "x2", Layer: memory.L3Fact}, {ID: "x3", Layer: memory.L3Fact}}
	out := admitLinked(st, hits, []string{"u"}, "a", 4, nil)
	if len(out) != 5 {
		t.Fatalf("got %d hits, want 5: %v", len(out), out)
	}
	if out[0].ID != src {
		t.Errorf("source hit moved: %v", out[0].ID)
	}
	found := -1
	for i, h := range out {
		switch h.ID {
		case other, dead:
			t.Errorf("ignored link %q was admitted", h.ID)
		case live:
			found = i
		}
	}
	if found < 0 || found >= 4 {
		t.Errorf("linked fact at %d, want inside the first 4", found)
	}
	// An already present fact is not added twice.
	if again := admitLinked(st, out, []string{"u"}, "a", 4, nil); len(again) != len(out) {
		t.Errorf("repeat admit grew the list to %d", len(again))
	}
}

// TestSearchAdmitsLinkedFactTheQueryMisses checks the whole pipeline: a keyword
// search finds only the source fact, and its link adds the other fact.
func TestSearchAdmitsLinkedFactTheQueryMisses(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	st, _ := newPagesStore(t)
	srv.store = st
	live := addOwned(t, st, memory.L3Fact, "zorblax contradicts the earlier plan", "u", nil)
	addOwned(t, st, memory.L3Fact, "quixotic gizmo shipped on friday", "u", map[string]string{metaLinks: live + ":contrasts"})
	body := `{"query":"quixotic gizmo","user_id":"u","agent_id":"a","reader":"keyword","min_score":0,"limit":6}`
	w := httptest.NewRecorder()
	srv.handleSearch(w, httptest.NewRequest("POST", "/api/v1/search", strings.NewReader(body)))
	if w.Code != 200 {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "zorblax") {
		t.Errorf("linked fact missing: %s", w.Body.String())
	}
}

// TestSessionFactor checks that an intention of the current session ranks above one
// of another session, that the reverse holds for another session, and that without a
// session id the order is unchanged.
func TestSessionFactor(t *testing.T) {
	mine := SearchHit{ID: "mine", Layer: memory.L7Intention, Meta: map[string]string{"session_id": "s1"}}
	theirs := SearchHit{ID: "theirs", Layer: memory.L7Intention, Meta: map[string]string{"session_id": "s2"}}
	fact := SearchHit{ID: "fact", Layer: memory.L3Fact, Meta: map[string]string{"session_id": "s1"}}
	if sessionFactor(mine, "s1") <= 1 || sessionFactor(theirs, "s1") >= 1 || sessionFactor(fact, "s1") != 1 {
		t.Error("factors wrong for s1")
	}
	if sessionFactor(mine, "s2") >= 1 || sessionFactor(theirs, "s2") <= 1 {
		t.Error("factors wrong for s2")
	}
	order := func(sid string) string {
		res := []SearchHit{theirs, mine}
		if sid != "" {
			res = rerankByFactor(res, func(_ int, h SearchHit) float64 { return sessionFactor(h, sid) })
		}
		return res[0].ID
	}
	if order("s1") != "mine" || order("s2") != "theirs" || order("") != "theirs" {
		t.Errorf("order: s1=%s s2=%s none=%s", order("s1"), order("s2"), order(""))
	}
}

// TestSearchResponseCarriesChannelOrder checks that the response names the channel
// order next to the memories.
func TestSearchResponseCarriesChannelOrder(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	st, _ := newPagesStore(t)
	srv.store = st
	w := httptest.NewRecorder()
	srv.handleSearch(w, httptest.NewRequest("POST", "/api/v1/search", strings.NewReader(`{"query":"anything","user_id":"u"}`)))
	var resp struct {
		Order    []string       `json:"order"`
		Memories map[string]any `json:"memories"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if strings.Join(resp.Order, ",") != "now,pages,profile,proactive,experiences,normal" || len(resp.Memories) != 5 {
		t.Errorf("order %v, %d channels", resp.Order, len(resp.Memories))
	}
}

// A linked fact reports its own similarity to the query, not the score of the hit
// that links to it, and is marked as having come in through a link.
func TestAdmitLinkedUsesOwnScore(t *testing.T) {
	st, _ := newPagesStore(t)
	live := addOwned(t, st, memory.L3Fact, "the contrasting fact", "u", nil)
	src := addOwned(t, st, memory.L3Fact, "source fact", "u", map[string]string{metaLinks: live + ":contrasts"})
	qv, err := st.embed.Embed(st.ctx, "the contrasting fact")
	if err != nil {
		t.Fatal(err)
	}
	hits := []SearchHit{{ID: src, Content: "source fact", Score: 0.99, Layer: memory.L3Fact, Meta: st.GetMany([]string{src})[0].Meta}}
	out := admitLinked(st, hits, []string{"u"}, "a", 4, qv)
	if len(out) != 2 {
		t.Fatalf("got %d hits, want 2", len(out))
	}
	linked := out[1]
	if linked.ID != live || linked.Meta[metaVia] != viaLink {
		t.Fatalf("linked hit = %+v", linked)
	}
	if linked.Score < 0.9 || linked.Score > 1.001 || linked.Score == 0.99 {
		t.Errorf("linked score = %v, want its own cosine to the query (about 1)", linked.Score)
	}
	if out[0].Meta[metaVia] != "" {
		t.Error("the query's own hit was marked as linked")
	}
	raw, err := json.Marshal(searchHitJSON{MemoryID: live, Via: linked.Meta[metaVia]})
	if err != nil || !strings.Contains(string(raw), `"via":"link"`) {
		t.Errorf("via missing from JSON: %s", raw)
	}
}
