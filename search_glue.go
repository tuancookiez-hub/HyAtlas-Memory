package main

import "sort"

// clauseHitsOwners runs clauseHits for each of the search's owners and merges the
// results, so a search over a group of aliased IDs finds clauses under any of them.
// An empty users list searches every owner (agentID still applies when set).
func (s *MemoryStore) clauseHitsOwners(qv []float32, users []string, agentID string, limit int) []SearchHit {
	if len(users) == 0 {
		var where map[string]string
		if agentID != "" {
			where = map[string]string{"agent_id": agentID}
		}
		return s.clauseHits(qv, where, limit)
	}
	var out []SearchHit
	for _, u := range users {
		where := map[string]string{"user_id": u}
		if agentID != "" {
			where["agent_id"] = agentID
		}
		out = mergeBest(out, s.clauseHits(qv, where, limit))
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
