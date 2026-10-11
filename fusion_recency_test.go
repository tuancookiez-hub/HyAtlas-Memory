package main

import (
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// recencyHit builds a SearchHit created at ts. A zero ts leaves the timestamp unset,
// which the recency code must treat as unknown.
func recencyHit(id string, layer memory.Layer, label string, ts time.Time) SearchHit {
	meta := map[string]string{}
	if label != "" {
		meta["source_layer_label"] = label
	}
	if !ts.IsZero() {
		meta["ts"] = ts.UTC().Format(time.RFC3339)
	}
	return SearchHit{ID: id, Content: id, Layer: layer, Meta: meta}
}

func TestRecencyHalfLife(t *testing.T) {
	cases := []struct {
		name string
		hit  SearchHit
		want time.Duration
	}{
		{"L1 profile is durable", SearchHit{Layer: memory.L1Profile}, 0},
		{"L6 schema is durable", SearchHit{Layer: memory.L6Schema}, 0},
		{"L7 intention fades in three days", SearchHit{Layer: memory.L7Intention}, 3 * 24 * time.Hour},
		{"L3 user_preferences lasts 180 days", recencyHit("a", memory.L3Fact, "user_preferences", time.Time{}), 180 * 24 * time.Hour},
		{"L3 decision lasts 180 days", recencyHit("a", memory.L3Fact, "decision", time.Time{}), 180 * 24 * time.Hour},
		{"L3 technical_lesson lasts 180 days", recencyHit("a", memory.L3Fact, "technical_lesson", time.Time{}), 180 * 24 * time.Hour},
		{"L3 negative_knowledge lasts 180 days", recencyHit("a", memory.L3Fact, "negative_knowledge", time.Time{}), 180 * 24 * time.Hour},
		{"L3 project_state fades in 30 days", recencyHit("a", memory.L3Fact, "project_state", time.Time{}), 30 * 24 * time.Hour},
		{"L3 with no label fades in 30 days", recencyHit("a", memory.L3Fact, "", time.Time{}), 30 * 24 * time.Hour},
		{"L2 raw fades in 30 days", SearchHit{Layer: memory.L2Raw}, 30 * 24 * time.Hour},
		{"L4 summary fades in 30 days", SearchHit{Layer: memory.L4Summary}, 30 * 24 * time.Hour},
		{"L5 knowledge fades in 30 days", SearchHit{Layer: memory.L5Knowledge}, 30 * 24 * time.Hour},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := recencyHalfLife(c.hit); got != c.want {
				t.Fatalf("recencyHalfLife = %v, want %v", got, c.want)
			}
		})
	}
}

func TestRerankRecencyNewerWinsCloseCall(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	old := recencyHit("old", memory.L3Fact, "project_state", now.AddDate(0, 0, -200))
	fresh := recencyHit("fresh", memory.L3Fact, "project_state", now.AddDate(0, 0, -1))
	got := rerankRecency([]SearchHit{old, fresh}, now)
	if len(got) != 2 || got[0].ID != "fresh" || got[1].ID != "old" {
		t.Fatalf("order = %v, want fresh first", ids(got))
	}
}

func TestRerankRecencyRelevanceDominates(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	// The oldest hit keeps recencyFloor (92%) of its position-0 weight, which is about
	// five places' worth, so a fresh hit that starts six places behind stays behind.
	// Filler hits are undated durable L1 rows, so they keep their position weights and
	// only the old-versus-fresh order is asserted.
	hits := []SearchHit{recencyHit("old0", memory.L3Fact, "project_state", now.AddDate(0, 0, -200))}
	for i := 1; i < 6; i++ {
		hits = append(hits, SearchHit{ID: "filler", Layer: memory.L1Profile})
	}
	hits = append(hits, recencyHit("fresh30", memory.L3Fact, "project_state", now.AddDate(0, 0, -1)))
	got := rerankRecency(hits, now)
	oldAt, freshAt := -1, -1
	for i, h := range got {
		switch h.ID {
		case "old0":
			oldAt = i
		case "fresh30":
			freshAt = i
		}
	}
	if oldAt < 0 || freshAt < 0 || oldAt > freshAt {
		t.Fatalf("old0 at %d, fresh30 at %d: want old0 ahead", oldAt, freshAt)
	}
}

func TestRerankRecencyLayerOneNeverMoves(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	// A profile hit with a very old ts keeps factor 1.0, so it must not fall behind a
	// newer hit that sits at the same or a lower position.
	profile := recencyHit("profile", memory.L1Profile, "", now.AddDate(-5, 0, 0))
	fact := recencyHit("fact", memory.L3Fact, "project_state", now)
	got := rerankRecency([]SearchHit{profile, fact}, now)
	if got[0].ID != "profile" {
		t.Fatalf("order = %v, want profile kept first", ids(got))
	}
}

func TestRerankRecencyUnknownAndFutureKeepFactorOne(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	unknown := recencyHit("unknown", memory.L3Fact, "project_state", time.Time{})
	future := recencyHit("future", memory.L3Fact, "project_state", now.AddDate(0, 0, 10))
	dated := recencyHit("dated", memory.L3Fact, "project_state", now.AddDate(0, 0, -20))
	// Position weights decrease, so with factor 1.0 the order must be unchanged.
	got := rerankRecency([]SearchHit{unknown, future, dated}, now)
	want := []string{"unknown", "future", "dated"}
	if !sameIDs(got, want) {
		t.Fatalf("order = %v, want %v", ids(got), want)
	}
}

func TestRerankRecencyDoesNotMutateInput(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	old := recencyHit("old", memory.L3Fact, "project_state", now.AddDate(0, 0, -200))
	old.Score = 0.5
	fresh := recencyHit("fresh", memory.L3Fact, "project_state", now.AddDate(0, 0, -1))
	fresh.Score = 0.4
	in := []SearchHit{old, fresh}
	_ = rerankRecency(in, now)
	if in[0].ID != "old" || in[1].ID != "fresh" || in[0].Score != 0.5 || in[1].Score != 0.4 {
		t.Fatalf("input changed: %v", ids(in))
	}
}

func TestRerankRecencyEmpty(t *testing.T) {
	if got := rerankRecency(nil, time.Now()); got != nil {
		t.Fatalf("nil input returned %v, want nil", got)
	}
	empty := []SearchHit{}
	if got := rerankRecency(empty, time.Now()); len(got) != 0 {
		t.Fatalf("empty input returned %v", got)
	}
}

func TestDropStaleIntentions(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	ttl := defaultIntentionTTL
	hits := []SearchHit{
		recencyHit("l7-old", memory.L7Intention, "", now.AddDate(0, 0, -20)),
		recencyHit("l7-new", memory.L7Intention, "", now.AddDate(0, 0, -3)),
		recencyHit("l3-ancient", memory.L3Fact, "decision", now.AddDate(0, 0, -300)),
		recencyHit("l7-unknown", memory.L7Intention, "", time.Time{}),
	}
	got := dropStaleIntentions(hits, ttl, now)
	want := []string{"l7-new", "l3-ancient", "l7-unknown"}
	if !sameIDs(got, want) {
		t.Fatalf("kept %v, want %v", ids(got), want)
	}
}

func TestDropStaleIntentionsZeroTTLKeepsAll(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	hits := []SearchHit{
		recencyHit("l7-old", memory.L7Intention, "", now.AddDate(0, 0, -400)),
	}
	if got := dropStaleIntentions(hits, 0, now); !sameIDs(got, []string{"l7-old"}) {
		t.Fatalf("ttl 0 kept %v, want all", ids(got))
	}
}

func ids(hits []SearchHit) []string {
	out := make([]string, len(hits))
	for i, h := range hits {
		out[i] = h.ID
	}
	return out
}

func sameIDs(hits []SearchHit, want []string) bool {
	got := ids(hits)
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}
