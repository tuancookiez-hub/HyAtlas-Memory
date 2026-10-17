package main

import (
	"sort"
)

// entityBoost multiplies the position weight of a hit that names a known entity. At
// 1.06 a hit moves up at most about three places, so a passing mention cannot carry a
// weak hit from the bottom of the list to the top.
const entityBoost = 1.06

// maxQueryEntities caps how many entity names queryEntities returns, so a query that
// touches many pages cannot flood the rerank step.
const maxQueryEntities = 5

// entityTokenSet returns the stemmed search terms of text as a set. It uses
// keywordTokens, so the sets built from a query, a hit and an entity name agree.
func entityTokenSet(text string) map[string]bool {
	set := map[string]bool{}
	for _, t := range keywordTokens(text) {
		set[t] = true
	}
	return set
}

// nameMatches reports whether every word of an entity name appears in the token set.
// Each word is tokenised the same way as the text, so stemming and punctuation agree.
// A name with no searchable word matches nothing, so an empty or stopword-only name
// never boosts a hit.
func nameMatches(set map[string]bool, name string) bool {
	found := false
	for _, w := range nameTokens(name) {
		for _, t := range keywordTokens(w) {
			found = true
			if !set[t] {
				return false
			}
		}
	}
	return found
}

// queryEntities returns the names, aliases included, of the entity pages owned by
// users under agentID whose words all appear in the query. A query that says
// "how is the hyatlas server doing" therefore names the page "HyAtlas". The result is
// deduplicated, longest name first, and capped at maxQueryEntities, so the caller
// gets the most specific entities without scanning every page on each search.
func (s *MemoryStore) queryEntities(query string, users []string, agentID string) []string {
	if s.pages == nil || len(users) == 0 {
		return nil
	}
	qset := entityTokenSet(query)
	if len(qset) == 0 {
		return nil
	}
	seen := map[string]bool{}
	var out []string
	for _, u := range users {
		for _, pg := range s.pages.List(u, agentID) {
			for _, name := range append([]string{pg.Name}, pg.Aliases...) {
				key := normalizeName(name)
				if key == "" || seen[key] || !nameMatches(qset, name) {
					continue
				}
				seen[key] = true
				out = append(out, name)
			}
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return len(out[i]) > len(out[j]) })
	if len(out) > maxQueryEntities {
		out = out[:maxQueryEntities]
	}
	return out
}

// rerankEntities orders hits by their position weight, 1/(rrfK+i+1), with the weight
// of each hit that names an entity multiplied by entityBoost. A boosted hit moves up
// only a few places, because the boost is small next to the gap between neighbouring
// positions, so an entity mention helps without overriding the existing ranking. The
// Score fields are not changed, and the input slice is left as it was. With no names
// the input is returned unchanged.
func rerankEntities(hits []SearchHit, names []string) []SearchHit {
	if len(names) == 0 || len(hits) == 0 {
		return hits
	}
	weights := make([]float64, len(hits))
	order := make([]int, len(hits))
	for i, h := range hits {
		w := 1.0 / float64(rrfK+i+1)
		set := entityTokenSet(h.Content)
		for _, n := range names {
			if nameMatches(set, n) {
				w *= entityBoost
				break
			}
		}
		weights[i] = w
		order[i] = i
	}
	sort.SliceStable(order, func(a, b int) bool { return weights[order[a]] > weights[order[b]] })
	out := make([]SearchHit, len(hits))
	for k, i := range order {
		out[k] = hits[i]
	}
	return out
}
