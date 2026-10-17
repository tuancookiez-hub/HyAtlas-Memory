package main

import (
	"strings"

	"github.com/tuancookiez-hub/hyatlas-v4/graph"
)

// graphRecallMax is how many extra hits graph-assisted recall may add to one search.
// The cap keeps a densely connected entity from pushing out the hits the query asked for.
const graphRecallMax = 3

// maxGraphNeighbours caps how many neighbour labels graphNeighbourNames returns.
const maxGraphNeighbours = 10

// sameAnyName reports whether name is the same entity as one of the names already
// collected, using sameEntity so that version labels and word subsets count as one.
func sameAnyName(names []string, name string) bool {
	for _, n := range names {
		if sameEntity(n, name) {
			return true
		}
	}
	return false
}

// mentionsAny reports whether the token set contains every word of at least one of
// the names. It uses nameMatches, so a neighbour name is matched the same way as an
// entity name.
func mentionsAny(set map[string]bool, names []string) bool {
	for _, n := range names {
		if nameMatches(set, n) {
			return true
		}
	}
	return false
}

// graphNeighbourNames returns the labels of the L5 graph neighbours of the given
// entity names, looked up for each owner in users under agentID. Neighbours that are
// the same entity as one already collected are dropped, and the result is capped at
// maxGraphNeighbours. The lookup is scoped per owner, so one owner's graph never leaks
// into another owner's recall. With no owners the result is empty rather than the
// whole graph.
func (s *MemoryStore) graphNeighbourNames(names []string, users []string, agentID string) []string {
	g := s.Graph()
	if g == nil || len(users) == 0 {
		return nil
	}
	var out []string
	for _, n := range names {
		if strings.TrimSpace(n) == "" {
			continue
		}
		for _, u := range users {
			for _, nb := range g.NeighborsScoped(graph.Scope{UserID: u, AgentID: agentID}, n) {
				if nb.Label == "" || sameAnyName(out, nb.Label) {
					continue
				}
				out = append(out, nb.Label)
				if len(out) >= maxGraphNeighbours {
					return out
				}
			}
		}
	}
	return out
}

// graphAdmit returns admitted followed by up to max candidates that name a graph
// neighbour. A candidate qualifies when it is not already admitted, its score is at
// least floor, and its content mentions a neighbour. Admitted extras go after the
// existing hits, in candidate order. Graph recall only fixes misses: the scores of
// the hits are never changed, so a graph link cannot raise a hit above its own
// relevance. The returned slice is new, and the inputs are not modified.
func graphAdmit(candidates []SearchHit, admitted []SearchHit, neighbour []string, floor float64, max int) []SearchHit {
	out := make([]SearchHit, 0, len(admitted)+max)
	out = append(out, admitted...)
	if max <= 0 || len(candidates) == 0 || len(neighbour) == 0 {
		return out
	}
	have := map[string]bool{}
	for _, h := range admitted {
		have[h.ID] = true
	}
	added := 0
	for _, c := range candidates {
		if added >= max {
			break
		}
		if have[c.ID] || float64(c.Score) < floor {
			continue
		}
		if !mentionsAny(entityTokenSet(c.Content), neighbour) {
			continue
		}
		have[c.ID] = true
		out = append(out, c)
		added++
	}
	return out
}
