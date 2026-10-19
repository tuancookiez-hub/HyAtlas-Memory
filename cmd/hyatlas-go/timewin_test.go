package main

import (
	"fmt"
	"math"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// twLoc is the fixed zone the time-window tests run in.
var twLoc = time.FixedZone("MYT", 8*3600)

// twNow is the fixed "now" for the time-window tests: Saturday 2026-10-17 15:00 MYT.
var twNow = time.Date(2026, time.October, 17, 15, 0, 0, 0, twLoc)

// twDay returns local midnight of the given date in twLoc.
func twDay(y int, m time.Month, d int) time.Time {
	return time.Date(y, m, d, 0, 0, 0, 0, twLoc)
}

// TestParseTimeWindow checks each rule of the time-phrase table, plus the edge
// cases the table calls out, against the fixed now.
func TestParseTimeWindow(t *testing.T) {
	cases := []struct {
		name  string
		query string
		ok    bool
		from  time.Time
		to    time.Time
		label string
	}{
		{"last week in a sentence", "what did I do last week", true,
			twDay(2026, 10, 5), twDay(2026, 10, 12), "last week"},
		{"month with no year", "in november", true,
			twDay(2025, 11, 1), twDay(2025, 12, 1), "2025-11"},
		{"month name in current year", "in october", true,
			twDay(2026, 10, 1), twDay(2026, 11, 1), "2026-10"},
		{"month and year", "march 2025", true,
			twDay(2025, 3, 1), twDay(2025, 4, 1), "2025-03"},
		{"month and day", "oct 5", true,
			twDay(2026, 10, 5), twDay(2026, 10, 6), "2026-10-05"},
		{"day, month and year with ordinal", "sept 3rd 2026", true,
			twDay(2026, 9, 3), twDay(2026, 9, 4), "2026-09-03"},
		{"day then month", "5 oct", true,
			twDay(2026, 10, 5), twDay(2026, 10, 6), "2026-10-05"},
		{"bare month word is not a time", "may I ask", false,
			time.Time{}, time.Time{}, ""},
		{"full ISO date", "2026-10-05", true,
			twDay(2026, 10, 5), twDay(2026, 10, 6), "2026-10-05"},
		{"ISO month", "2026-10", true,
			twDay(2026, 10, 1), twDay(2026, 11, 1), "2026-10"},
		{"invalid ISO date is not a month", "2026-10-45", false,
			time.Time{}, time.Time{}, ""},
		{"today", "what happened today", true,
			twDay(2026, 10, 17), twDay(2026, 10, 18), "today"},
		{"yesterday", "yesterday", true,
			twDay(2026, 10, 16), twDay(2026, 10, 17), "yesterday"},
		{"N days ago", "3 days ago", true,
			twDay(2026, 10, 14), twDay(2026, 10, 15), "3 days ago"},
		{"past N days includes today", "past 3 days", true,
			twDay(2026, 10, 14), twDay(2026, 10, 18), "past 3 days"},
		{"last N weeks", "last 2 weeks", true,
			twDay(2026, 10, 3), twDay(2026, 10, 18), "last 2 weeks"},
		{"last N months", "last 3 months", true,
			twDay(2026, 7, 17), twDay(2026, 10, 18), "last 3 months"},
		{"this week starts Monday", "this week", true,
			twDay(2026, 10, 12), twDay(2026, 10, 18), "this week"},
		{"last week", "last week", true,
			twDay(2026, 10, 5), twDay(2026, 10, 12), "last week"},
		{"this month", "this month", true,
			twDay(2026, 10, 1), twDay(2026, 10, 18), "this month"},
		{"last month", "last month", true,
			twDay(2026, 9, 1), twDay(2026, 10, 1), "last month"},
		{"last weekday before today", "last monday", true,
			twDay(2026, 10, 12), twDay(2026, 10, 13), "last monday"},
		{"on weekday short form", "on fri", true,
			twDay(2026, 10, 16), twDay(2026, 10, 17), "on fri"},
		{"in month with year", "in march 2026", true,
			twDay(2026, 3, 1), twDay(2026, 4, 1), "2026-03"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := parseTimeWindow(tc.query, twNow)
			if ok != tc.ok {
				t.Fatalf("parseTimeWindow(%q) ok = %v, want %v", tc.query, ok, tc.ok)
			}
			if !ok {
				return
			}
			if !got.From.Equal(tc.from) || !got.To.Equal(tc.to) {
				t.Errorf("parseTimeWindow(%q) = [%v, %v), want [%v, %v)",
					tc.query, got.From, got.To, tc.from, tc.to)
			}
			if got.Label != tc.label {
				t.Errorf("parseTimeWindow(%q) label = %q, want %q", tc.query, got.Label, tc.label)
			}
		})
	}
}

// TestHitTimeRange checks that event_at wins over ts, that a month event_at gives a
// month span, that ts alone gives an instant, and that neither gives ok=false.
func TestHitTimeRange(t *testing.T) {
	ts := "2026-10-17T10:00:00+08:00"

	from, to, ok := hitTimeRange(map[string]string{"event_at": "2026-10-05", "ts": ts}, twLoc)
	if !ok || !from.Equal(twDay(2026, 10, 5)) || !to.Equal(twDay(2026, 10, 6)) {
		t.Errorf("event_at day over ts: got [%v, %v) ok=%v, want the single day 2026-10-05", from, to, ok)
	}

	from, to, ok = hitTimeRange(map[string]string{"event_at": "2026-10", "ts": ts}, twLoc)
	if !ok || !from.Equal(twDay(2026, 10, 1)) || !to.Equal(twDay(2026, 11, 1)) {
		t.Errorf("event_at month: got [%v, %v) ok=%v, want all of October", from, to, ok)
	}

	from, to, ok = hitTimeRange(map[string]string{"ts": ts}, twLoc)
	want, _ := time.Parse(time.RFC3339, ts)
	if !ok || !from.Equal(want) || !to.Equal(want.Add(time.Second)) {
		t.Errorf("ts only: got [%v, %v) ok=%v, want [%v, +1s)", from, to, ok, want)
	}

	if _, _, ok := hitTimeRange(map[string]string{"content": "x"}, twLoc); ok {
		t.Errorf("no event_at and no ts: ok = true, want false")
	}
}

// TestRerankWindow checks that an in-window hit with a close weight moves up one
// place, and that an in-window hit far down the list does not jump to the top.
func TestRerankWindow(t *testing.T) {
	w := timeWindow{From: twDay(2026, 10, 12), To: twDay(2026, 10, 18), Label: "this week"}
	in := map[string]string{"ts": "2026-10-14T09:00:00+08:00"}
	out := map[string]string{"ts": "2026-09-01T09:00:00+08:00"}

	// Weights: the in-window hit at index 2 scores 1/63, which beats the out-of-window
	// hit at index 1 (0.85/62) but not the top hit at index 0 (1/61). It therefore
	// moves up exactly one place.
	hits := []SearchHit{
		{ID: "a", Score: 0.9, Meta: in},
		{ID: "b", Score: 0.8, Meta: out},
		{ID: "c", Score: 0.7, Meta: in},
	}
	got := rerankWindow(hits, w)
	if len(got) != 3 || got[0].ID != "a" || got[1].ID != "c" || got[2].ID != "b" {
		t.Fatalf("rerank order = %v, want [a c b]", hitIDs(got))
	}
	if got[1].Score != 0.7 {
		t.Errorf("rerank changed a score: %v, want 0.7", got[1].Score)
	}
	if hits[1].ID != "b" {
		t.Errorf("rerank modified the input slice")
	}

	// A lone in-window hit at index 15 among out-of-window hits does not reach the top.
	var far []SearchHit
	for i := 0; i < 16; i++ {
		meta := out
		id := fmt.Sprintf("h%d", i)
		if i == 15 {
			meta = in
		}
		far = append(far, SearchHit{ID: id, Meta: meta})
	}
	got = rerankWindow(far, w)
	if got[0].ID != "h0" {
		t.Errorf("far in-window hit jumped to the top: order starts %s, want h0", got[0].ID)
	}
}

// hitIDs lists the ids of hits in order, for error messages.
func hitIDs(hits []SearchHit) []string {
	out := make([]string, len(hits))
	for i, h := range hits {
		out[i] = h.ID
	}
	return out
}

// TestWindowCandidates checks the store-side window search: an in-window fact is
// returned, an out-of-window, superseded or other owner's fact is not, and minScore
// filters the rest.
func TestWindowCandidates(t *testing.T) {
	vecs := vecEmbedder{
		"in window fact":     simTo(0.9),
		"out of window fact": simTo(0.95),
		"superseded fact":    simTo(0.95),
		"other owner fact":   simTo(0.95),
		"weak fact":          simTo(0.5),
	}
	st := guardStore(t, vecs)
	add := func(id, text string, meta map[string]string) {
		t.Helper()
		if err := st.Add(memory.L3Fact, id, text, meta); err != nil {
			t.Fatal(err)
		}
	}
	add("in", "in window fact", map[string]string{"user_id": "u", "agent_id": "a", "event_at": "2026-10-07"})
	add("out", "out of window fact", map[string]string{"user_id": "u", "agent_id": "a", "event_at": "2026-09-01"})
	add("sup", "superseded fact", map[string]string{"user_id": "u", "agent_id": "a", "event_at": "2026-10-07", "invalid_at": "2026-10-08T00:00:00Z"})
	add("other", "other owner fact", map[string]string{"user_id": "v", "agent_id": "a", "event_at": "2026-10-07"})
	add("weak", "weak fact", map[string]string{"user_id": "u", "agent_id": "a", "event_at": "2026-10-07"})

	// The weak fact is in the window and owned by "u", but its vector scores 0.5, so
	// minScore 0.8 is what keeps it out.
	w := timeWindow{From: twDay(2026, 10, 5), To: twDay(2026, 10, 12), Label: "last week"}
	qv := []float32{1, 0, 0}

	// Owner "u" only, minScore 0.8: just the in-window 0.9 fact.
	got := st.windowCandidates(qv, []string{"u"}, "a", w, 10, 0.8)
	if len(got) != 1 || got[0].ID != "in" {
		t.Fatalf("windowCandidates = %v, want [in]", hitIDs(got))
	}
	if math.Abs(float64(got[0].Score)-0.9) > 1e-4 {
		t.Errorf("score = %v, want about 0.9", got[0].Score)
	}
	if got[0].Layer != memory.L3Fact {
		t.Errorf("layer = %v, want %v", got[0].Layer, memory.L3Fact)
	}

	// minScore above the fact's score filters it out.
	if got := st.windowCandidates(qv, []string{"u"}, "a", w, 10, 0.95); len(got) != 0 {
		t.Errorf("minScore 0.95: got %v, want none", hitIDs(got))
	}

	// Degenerate inputs return nil.
	if got := st.windowCandidates(nil, nil, "", w, 10, 0); got != nil {
		t.Errorf("empty query vector: got %v, want nil", hitIDs(got))
	}
	if got := st.windowCandidates(qv, nil, "", w, 0, 0); got != nil {
		t.Errorf("k=0: got %v, want nil", hitIDs(got))
	}
}
