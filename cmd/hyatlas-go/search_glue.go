package main

import "sort"

// clauseHitsOwners finds the clause hits of the search's owners, so a search over a
// group of aliased IDs finds clauses under any of them. An empty users list searches
// every owner (agentID still applies when set). For several owners it runs one clause
// query with no owner filter, wide enough for limit parents per owner, and keeps the
// hits whose owner is in users, instead of one query per owner.
func (s *MemoryStore) clauseHitsOwners(qv []float32, users []string, agentID string, limit int) []SearchHit {
	switch len(users) {
	case 0:
		return s.clauseHits(qv, ownerWhere("", agentID), limit)
	case 1:
		return s.clauseHits(qv, ownerWhere(users[0], agentID), limit)
	}
	want := make(map[string]bool, len(users))
	for _, u := range users {
		want[u] = true
	}
	if limit <= 0 {
		limit = 5
	}
	var out []SearchHit
	for _, h := range s.clauseHits(qv, ownerWhere("", agentID), limit*len(users)) {
		if want[h.Meta["user_id"]] {
			out = append(out, h)
		}
	}
	return out
}

// mergeBest merges two vector hit lists by id, keeping each hit's higher score, and
// returns them best first. A long fact found both by its own vector and by one of
// its clauses counts once, at the better of the two scores.
func mergeBest(a, b []SearchHit) []SearchHit {
	if len(b) == 0 {
		return a
	}
	byID := make(map[string]int, len(a)+len(b))
	out := make([]SearchHit, 0, len(a)+len(b))
	for _, list := range [][]SearchHit{a, b} {
		for _, h := range list {
			if i, ok := byID[h.ID]; ok {
				if h.Score > out[i].Score {
					out[i].Score = h.Score
				}
				continue
			}
			byID[h.ID] = len(out)
			out = append(out, h)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Score > out[j].Score })
	return out
}
