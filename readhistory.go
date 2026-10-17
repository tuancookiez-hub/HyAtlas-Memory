package main

import (
	"sort"
	"time"
)

// HistoryEntry is one older version of a memory, shown under the hit that replaced it.
type HistoryEntry struct {
	Content string `json:"content"`
	Date    string `json:"date"` // event_at if set, else the YYYY-MM-DD of ts
	How     string `json:"how"`  // superseded_how of the old row
}

// historyPerHit is how many older versions are shown under each hit.
const historyPerHit = 2

// historyContentMax is the most bytes of each older version that are shown.
const historyContentMax = 200

// nearDupScore is the cosine at which two hits count as the same line.
const nearDupScore = 0.95

// historyRow is one replaced row found by historyOf, with the id that replaced it and
// its timestamp, which orders the versions.
type historyRow struct {
	by    string
	ts    string
	entry HistoryEntry
}

// historyOf returns, for each id in ids, the older versions that the id replaced. A
// row counts only when its superseded_how is howReplaced and its superseded_by is that
// id, because updated, restated and merged rows say nothing the newer row does not.
// Each id keeps its perHit newest versions. Only one step is followed, so a hit shows
// what it replaced and not the whole chain behind it. An id with no such history is
// absent from the map.
func (s *MemoryStore) historyOf(ids []string, perHit int) map[string][]HistoryEntry {
	out := map[string][]HistoryEntry{}
	if len(ids) == 0 || perHit <= 0 {
		return out
	}
	want := make(map[string]bool, len(ids))
	for _, id := range ids {
		want[id] = true
	}

	var rows []historyRow
	s.mu.RLock()
	for id := range s.superseded {
		d, ok := s.index[id]
		if !ok || d.Meta[metaSupersededHow] != howReplaced || !want[d.Meta["superseded_by"]] {
			continue
		}
		date := normalizeEventDate(d.Meta[metaEventAt])
		if date == "" && len(d.Ts) >= 10 {
			date = d.Ts[:10]
		}
		rows = append(rows, historyRow{
			by: d.Meta["superseded_by"],
			ts: d.Ts,
			entry: HistoryEntry{
				Content: utf8Trunc(oneLine(d.Content), historyContentMax),
				Date:    date,
				How:     d.Meta[metaSupersededHow],
			},
		})
	}
	s.mu.RUnlock()

	// Newest first, so the first perHit rows seen for an id are its newest versions.
	sort.SliceStable(rows, func(i, j int) bool { return historyNewer(rows[i].ts, rows[j].ts) })
	for _, r := range rows {
		if len(out[r.by]) < perHit {
			out[r.by] = append(out[r.by], r.entry)
		}
	}
	return out
}

// historyNewer reports whether timestamp a is later than b. Both are compared as
// RFC3339 times when they parse, and as plain strings otherwise, so a malformed
// timestamp still sorts deterministically.
func historyNewer(a, b string) bool {
	ta, errA := time.Parse(time.RFC3339, a)
	tb, errB := time.Parse(time.RFC3339, b)
	if errA == nil && errB == nil {
		return ta.After(tb)
	}
	return a > b
}

// demoteNearDuplicates returns a copy of hits in which a hit that repeats an earlier
// kept hit is moved after all kept hits. A hit repeats an earlier one when both have
// vectors, their cosine is at least threshold, and their numbers match, because
// embeddings barely see numbers and "PR #101" and "PR #96" must not collapse. Hits
// without a vector are kept in place. Nothing is dropped, so the caller cuts to its
// limit afterwards. The input slice is not changed.
func (s *MemoryStore) demoteNearDuplicates(hits []SearchHit, threshold float64) []SearchHit {
	ids := make([]string, 0, len(hits))
	for _, h := range hits {
		ids = append(ids, h.ID)
	}
	vecs := s.vectors(ids)

	var keptHits []SearchHit
	var keptVecs [][]float32
	var demoted []SearchHit
	for _, h := range hits {
		v := vecs[h.ID]
		if len(v) == 0 {
			keptHits = append(keptHits, h)
			keptVecs = append(keptVecs, nil)
			continue
		}
		dup := false
		for i, k := range keptHits {
			if keptVecs[i] == nil {
				continue
			}
			if cosineSim(keptVecs[i], v) >= threshold && sameNumbers(k.Content, h.Content) {
				dup = true
				break
			}
		}
		if dup {
			demoted = append(demoted, h)
			continue
		}
		keptHits = append(keptHits, h)
		keptVecs = append(keptVecs, v)
	}

	out := make([]SearchHit, 0, len(hits))
	out = append(out, keptHits...)
	out = append(out, demoted...)
	return out
}
