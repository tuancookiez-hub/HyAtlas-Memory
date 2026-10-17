package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// TestRerankRecencyUsedIdleTime checks that a fact still in use is not sunk by its
// creation age. Two hits sit next to each other in rank; the older one, created 200
// days ago, was used yesterday, and the newer one, created two days ago, was not.
func TestRerankRecencyUsedIdleTime(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	old := recencyHit("old", memory.L3Fact, "project_state", now.AddDate(0, 0, -200))
	fresh := recencyHit("fresh", memory.L3Fact, "project_state", now.AddDate(0, 0, -2))
	in := []SearchHit{old, fresh}

	if got := rerankRecencyUsed(in, now, nil); !sameIDs(got, []string{"fresh", "old"}) {
		t.Fatalf("nil lastUsed: order = %v, want plain age order [fresh old]", ids(got))
	}

	yesterday := now.AddDate(0, 0, -1)
	used := func(id string) time.Time {
		if id == "old" {
			return yesterday
		}
		return time.Time{}
	}
	if got := rerankRecencyUsed(in, now, used); !sameIDs(got, []string{"old", "fresh"}) {
		t.Fatalf("used yesterday: order = %v, want [old fresh]", ids(got))
	}

	future := func(id string) time.Time {
		if id == "old" {
			return now.AddDate(0, 0, 1)
		}
		return time.Time{}
	}
	if got := rerankRecencyUsed(in, now, future); !sameIDs(got, []string{"fresh", "old"}) {
		t.Fatalf("future lastUsed: order = %v, want plain age order [fresh old]", ids(got))
	}
}

// TestRerankRecencyWrapperMatchesNil checks that rerankRecency is the nil-lastUsed case.
func TestRerankRecencyWrapperMatchesNil(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	old := recencyHit("old", memory.L3Fact, "project_state", now.AddDate(0, 0, -200))
	fresh := recencyHit("fresh", memory.L3Fact, "project_state", now.AddDate(0, 0, -2))
	in := []SearchHit{old, fresh}
	if !sameIDs(rerankRecency(in, now), ids(rerankRecencyUsed(in, now, nil))) {
		t.Fatalf("rerankRecency differs from rerankRecencyUsed with nil lastUsed")
	}
}

// TestUsageLastUsed checks LastUsed for a nil receiver, a missing id, an unreadable
// timestamp and a recorded use.
func TestUsageLastUsed(t *testing.T) {
	var nilStats *UsageStats
	if got := nilStats.LastUsed("x"); !got.IsZero() {
		t.Fatalf("nil receiver returned %v, want zero", got)
	}

	u := NewUsageStats("")
	if got := u.LastUsed("missing"); !got.IsZero() {
		t.Fatalf("missing id returned %v, want zero", got)
	}

	u.m["bad"] = usageEntry{Shown: 1, Used: 1, Last: "yesterday"}
	if got := u.LastUsed("bad"); !got.IsZero() {
		t.Fatalf("bad timestamp returned %v, want zero", got)
	}

	when := time.Date(2026, 10, 11, 9, 30, 0, 0, time.UTC)
	u.m["good"] = usageEntry{Shown: 1, Used: 1, Last: when.Format(time.RFC3339)}
	if got := u.LastUsed("good"); !got.Equal(when) {
		t.Fatalf("recorded use returned %v, want %v", got, when)
	}
}

// TestDropStaleIntentionsDueDate checks that a valid due_at overrides the ttl, and that
// an invalid due_at falls back to it. The ttl is seven days.
func TestDropStaleIntentionsDueDate(t *testing.T) {
	now := time.Date(2026, 10, 12, 12, 0, 0, 0, time.UTC)
	ttl := 7 * 24 * time.Hour
	withDue := func(id string, created time.Time, due string) SearchHit {
		h := recencyHit(id, memory.L7Intention, "", created)
		if due != "" {
			h.Meta[metaDueAt] = due
		}
		return h
	}
	cases := []struct {
		name string
		hit  SearchHit
		kept bool
	}{
		{"created 10 days ago, due in 5 days is kept", withDue("future-due", now.AddDate(0, 0, -10), "2026-10-17"), true},
		{"created 1 day ago, due 3 days ago is dropped", withDue("past-due", now.AddDate(0, 0, -1), "2026-10-09"), false},
		{"created 10 days ago, no due is dropped by ttl", withDue("no-due", now.AddDate(0, 0, -10), ""), false},
		{"created 1 day ago, invalid due is kept by ttl", withDue("bad-due", now.AddDate(0, 0, -1), "soon"), true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := dropStaleIntentions([]SearchHit{c.hit}, ttl, now)
			if kept := len(got) == 1; kept != c.kept {
				t.Fatalf("kept = %v, want %v", kept, c.kept)
			}
		})
	}
}

// TestStemTerm covers every irregular entry and each suffix rule, plus terms that must
// stay unchanged.
func TestStemTerm(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		// Irregular forms.
		{"broken", "break"},
		{"broke", "break"},
		{"built", "build"},
		{"made", "make"},
		{"went", "go"},
		{"ran", "run"},
		{"running", "run"},
		{"written", "write"},
		{"wrote", "write"},
		{"chosen", "choose"},
		{"chose", "choose"},
		{"taken", "take"},
		{"took", "take"},
		{"given", "give"},
		{"gave", "give"},
		// ies to y.
		{"libraries", "library"},
		// ing, with a doubled consonant reduced.
		{"stopping", "stop"},
		// ed, with a doubled consonant reduced.
		{"stopped", "stop"},
		{"failed", "fail"},
		// es, only after ss, x, ch or sh.
		{"fixes", "fix"},
		{"branches", "branch"},
		// s, unless the word ends in ss, us or is.
		{"tests", "test"},
		{"class", "class"},
		{"status", "status"},
		{"analysis", "analysis"},
		{"sessions", "session"},
		// Unchanged: a digit, a short term, and no suffix rule that applies.
		{"release", "release"},
		{"api2", "api2"},
		{"pass", "pass"},
	}
	for _, c := range cases {
		if got := stemTerm(c.in); got != c.want {
			t.Errorf("stemTerm(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// Usage learning and the store's counters keep separate files, so writing one does
// not erase the other. A pre-4.8.0 usage.json holding entries is still read once.
func TestUsageLearningHasItsOwnFile(t *testing.T) {
	dir := t.TempDir()
	st, err := NewMemoryStore(context.Background(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	u := NewUsageStats(dir)
	u.Record([]string{"m1"}, map[string]string{"m1": "port 19528 is the HyAtlas port"}, "use port 19528", "")
	if err := st.Add("l3_fact", newID(), "a fact", map[string]string{"user_id": "u"}); err != nil {
		t.Fatal(err)
	}
	st.Close()
	if got := NewUsageStats(dir).m["m1"].Shown; got != 1 {
		t.Fatalf("usage entry lost after a store write: shown=%d", got)
	}
	legacy := t.TempDir()
	if err := os.WriteFile(filepath.Join(legacy, "usage.json"), []byte(`{"m9":{"shown":4,"used":2,"last":""}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := NewUsageStats(legacy).m["m9"].Shown; got != 4 {
		t.Fatalf("legacy usage.json not read: shown=%d", got)
	}
}
