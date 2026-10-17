package main

import (
	"strings"
	"time"
)

// Metadata keys and values shared by the write path, the background passes and the
// read path. They are defined once here so every writer and reader agrees.
const (
	// metaEventAt is the date a fact's event happened or is due (YYYY-MM-DD or
	// YYYY-MM), as resolved by extraction. Absent for timeless facts.
	metaEventAt = "event_at"
	// metaDueAt is the date an intention is due (YYYY-MM-DD or YYYY-MM).
	metaDueAt = "due_at"
	// metaSupersededHow says how a superseded row was retired; see the how* values.
	metaSupersededHow = "superseded_how"
	// kindEpisode marks an L4 summary written for one episode of work, in the
	// "kind" key that already marks consolidation arcs ("arc").
	kindEpisode = "episode"
)

// How a row was superseded. Only howReplaced means the old row stopped being true;
// history on read shows those.
const (
	howReplaced = "replaced" // contradicted by a newer fact
	howUpdated  = "updated"  // extended by a newer fact that keeps everything it said
	howRestated = "restated" // a near-duplicate folded into a newer phrasing
	howMerged   = "merged"   // absorbed by a consolidation merge
)

// normalizeEventDate returns s as YYYY-MM-DD or YYYY-MM when it is a valid date in
// one of those forms (surrounding space and a trailing time are ignored), and "" for
// anything else. A model's date is model output, so it is checked before it is
// stored.
func normalizeEventDate(s string) string {
	s = strings.TrimSpace(s)
	if len(s) >= 10 {
		if t, err := time.Parse("2006-01-02", s[:10]); err == nil {
			return t.Format("2006-01-02")
		}
	}
	if len(s) == 7 {
		if t, err := time.Parse("2006-01", s); err == nil {
			return t.Format("2006-01")
		}
	}
	return ""
}

// eventDateRange returns the span a normalized event date covers: one day for
// YYYY-MM-DD, the whole month for YYYY-MM, in loc. ok is false for anything else.
func eventDateRange(s string, loc *time.Location) (from, to time.Time, ok bool) {
	if t, err := time.ParseInLocation("2006-01-02", s, loc); err == nil && len(s) == 10 {
		return t, t.AddDate(0, 0, 1), true
	}
	if t, err := time.ParseInLocation("2006-01", s, loc); err == nil && len(s) == 7 {
		return t, t.AddDate(0, 1, 0), true
	}
	return time.Time{}, time.Time{}, false
}
