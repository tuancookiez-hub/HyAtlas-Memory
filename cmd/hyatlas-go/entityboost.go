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
	var toks []string
	for _, w := range nameTokens(name) {
		toks = append(toks, keywordTokens(w)...)
	}
	return tokensMatch(set, toks)
}

// tokensMatch reports whether toks is not empty and every token is in the set. It is
// nameMatches for a name whose tokens are already known.
func tokensMatch(set map[string]bool, toks []string) bool {
	if len(toks) == 0 {
		return false
	}
	for _, t := range toks {
		if !set[t] {
			return false
		}
	}
	return true
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
		for _, en := range s.pages.entityNames(u, agentID) {
			if en.Key == "" || seen[en.Key] || !tokensMatch(qset, en.Tokens) {
				continue
			}
			seen[en.Key] = true
			out = append(out, en.Name)
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
	return rerankByFactor(hits, func(_ int, h SearchHit) float64 {
		set := entityTokenSet(h.Content)
		for _, n := range names {
			if nameMatches(set, n) {
				return entityBoost
			}
		}
		return 1
	})
}
