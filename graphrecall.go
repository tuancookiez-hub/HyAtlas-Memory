package main

import (
	"maps"
	"slices"
	"strings"

	"github.com/tuancookiez-hub/hyatlas-v4/graph"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// metaVia is the Meta key of a search hit that records how the hit got into the
// result when it was not found by the query itself; admitLinked sets it to viaLink.
const metaVia = "via"

// viaLink is the metaVia value of a fact that came in through a link.
const viaLink = "link"

// metaLinks is the metadata key holding a fact's links: comma-separated id:relation.
const metaLinks = "links"

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

// graphAdmitWithin runs graphAdmit and then places the graph-linked extras inside the
// visible part of the result, which is the first limit hits. Graph-linked facts take at
// most a third of the visible slots, at the end of the visible part. The query's own
// best hits keep the top limit - n places, so a graph link can never push one of them
// down. When limit is zero or less, the result is graphAdmit's result unchanged. The
// returned slice is new, the inputs are not modified, and scores are unchanged.
func graphAdmitWithin(candidates, admitted []SearchHit, neighbour []string, floor float64, max, limit int) []SearchHit {
	out := graphAdmit(candidates, admitted, neighbour, floor, max)
	return placeWithin(admitted, out[len(admitted):], limit)
}

// placeWithin puts extras inside the visible part of admitted, which is its first
// limit hits: at most a third of the visible slots, at the end of the visible part,
// so the query's own best hits keep the top places. Extras that do not fit follow the
// admitted hits. When limit is zero or less, the extras simply follow admitted. The
// returned slice is new and the inputs are not modified.
func placeWithin(admitted, extras []SearchHit, limit int) []SearchHit {
	if limit <= 0 {
		out := make([]SearchHit, 0, len(admitted)+len(extras))
		return append(append(out, admitted...), extras...)
	}
	room := limit / 3
	if room < 1 {
		room = 1
	}
	n := min(len(extras), room)
	pos := min(len(admitted), limit-n)
	if pos < 0 {
		pos = 0
	}
	res := make([]SearchHit, 0, len(admitted)+len(extras))
	res = append(res, admitted[:pos]...)
	res = append(res, extras[:n]...)
	res = append(res, admitted[pos:]...)
	res = append(res, extras[n:]...)
	return res
}

// linkedMax is how many facts the links of the top hits may add to one search.
const linkedMax = 2

// factLink is one link a fact carries to another fact: the other fact's id and how
// the two relate.
type factLink struct {
	ID       string
	Relation string
}

// linkRelations are the relations a fact link may name.
var linkRelations = map[string]bool{"related": true, "contrasts": true, "same_series": true}

// parseLinks reads the links metadata, a comma-separated list of id:relation. It is
// tolerant: an item with no colon, an empty id, or an unknown relation is skipped, and
// so is a repeated id.
func parseLinks(s string) []factLink {
	var out []factLink
	seen := map[string]bool{}
	for _, item := range strings.Split(s, ",") {
		id, rel, ok := strings.Cut(strings.TrimSpace(item), ":")
		id, rel = strings.TrimSpace(id), strings.ToLower(strings.TrimSpace(rel))
		if !ok || id == "" || !linkRelations[rel] || seen[id] {
			continue
		}
		seen[id] = true
		out = append(out, factLink{ID: id, Relation: rel})
	}
	return out
}

// linkedFacts returns the live L3 facts with the given ids that belong to one of the
// owners in users (any owner when users is empty) and to agentID when that is set. A
// superseded row, a missing row and another owner's row are left out. The result keeps
// the order of ids, each hit unscored (admitLinked scores it) and each Meta a copy.
func (s *MemoryStore) linkedFacts(ids []string, users []string, agentID string) []SearchHit {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var out []SearchHit
	for _, id := range ids {
		d, ok := s.index[id]
		if !ok || d.Layer != string(memory.L3Fact) || isSuperseded(d) {
			continue
		}
		if _, dead := s.superseded[id]; dead {
			continue
		}
		if len(users) > 0 && !slices.Contains(users, d.UserID) {
			continue
		}
		if agentID != "" && d.AgentID != agentID {
			continue
		}
		out = append(out, SearchHit{ID: d.ID, Content: d.Content, Layer: memory.L3Fact, Meta: maps.Clone(d.Meta)})
	}
	return out
}

// admitLinked adds up to linkedMax facts that the top limit hits link to and the query
// itself missed. A linked fact needs no score floor, because the model linked it, and
// it takes at most a third of the visible slots (placeWithin), after the query's own
// best hits. Each reports its own cosine similarity to the query vector qv (0 when qv is
// empty or the fact has no stored vector), never the score of the hit that links to it,
// and is marked with metaVia = viaLink. It ranks in link order.
func admitLinked(s *MemoryStore, hits []SearchHit, users []string, agentID string, limit int, qv []float32) []SearchHit {
	if limit <= 0 {
		return hits
	}
	have := make(map[string]bool, len(hits))
	for _, h := range hits {
		have[h.ID] = true
	}
	var extras []SearchHit
	for i, h := range hits {
		if i >= limit || len(extras) >= linkedMax {
			break
		}
		var ids []string
		for _, l := range parseLinks(h.Meta[metaLinks]) {
			if !have[l.ID] {
				ids = append(ids, l.ID)
			}
		}
		for _, e := range s.linkedFacts(ids, users, agentID) {
			if len(extras) >= linkedMax || have[e.ID] {
				continue
			}
			have[e.ID] = true
			if e.Meta == nil {
				e.Meta = map[string]string{}
			}
			e.Meta[metaVia] = viaLink
			if len(qv) > 0 {
				e.Score = float32(cosineSim(qv, s.vectors([]string{e.ID})[e.ID]))
			}
			extras = append(extras, e)
		}
	}
	if len(extras) == 0 {
		return hits
	}
	return placeWithin(hits, extras, limit)
}
