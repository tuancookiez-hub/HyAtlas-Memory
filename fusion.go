package main

import (
	"math"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// rrfK is the reciprocal rank fusion constant. 60 is the usual default: it keeps one
// list's top hit from swamping the other list's.
const rrfK = 60

// searchReader is which rankings a search uses. The dashboard's Explore tabs send it
// as "reader": "legacy" (Semantic), "hybrid_tag" (Keyword) and "hybrid_v2" (Hybrid).
type searchReader int

const (
	readHybrid  searchReader = iota // vector and keyword, fused (the default)
	readVector                      // vector only
	readKeyword                     // keyword only
)

// parseReader maps the request's "reader" field to a searchReader. Anything it does
// not name, including an empty value, is hybrid: the Hermes plugin sends none.
func parseReader(v string) searchReader {
	switch v {
	case "legacy", "semantic", "vector":
		return readVector
	case "hybrid_tag", "keyword":
		return readKeyword
	}
	return readHybrid
}

// fuseHits merges a vector ranking and a keyword ranking with reciprocal rank fusion,
// then drops repeated text and keeps the best limit (see refineHits).
//
// The score floor applies to vector hits only. A keyword hit already had to contain
// most of the query's terms (keywordRequired), and the identifiers keyword search is
// for, such as HYATLAS_SYNC_EXTRACT or a port number, are exactly what an embedding
// scores low. Each hit keeps its cosine Score; only the order comes from the fusion.
// Both inputs must be sorted best first.
func fuseHits(vec, kw []SearchHit, minScore float64, limit int) []SearchHit {
	type fused struct {
		hit  SearchHit
		rank float64
	}
	byID := map[string]*fused{}
	var order []string
	add := func(h SearchHit, rank int) {
		f, ok := byID[h.ID]
		if !ok {
			f = &fused{hit: h}
			byID[h.ID] = f
			order = append(order, h.ID)
		} else if h.Score > f.hit.Score {
			f.hit.Score = h.Score
		}
		f.rank += 1.0 / float64(rrfK+rank)
	}
	rank := 0
	for _, h := range vec {
		if float64(h.Score) < minScore {
			continue
		}
		rank++
		add(h, rank)
	}
	for i, h := range kw {
		add(h, i+1)
	}
	out := make([]fused, 0, len(order))
	for _, id := range order {
		out = append(out, *byID[id])
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].rank != out[j].rank {
			return out[i].rank > out[j].rank
		}
		return out[i].hit.Score > out[j].hit.Score
	})
	hits := make([]SearchHit, len(out))
	for i, f := range out {
		hits[i] = f.hit
	}
	return refineHits(hits, 0, limit)
}

// identifierTerms returns the keyword terms of the identifier-like words in a short
// query, or nil when the query is not one. A word is identifier-like when it holds a
// digit, an underscore, or punctuation between letters and digits: RSI(2), Reg-T,
// 19528, HYATLAS_SYNC_EXTRACT, discord-tps. Only queries of up to three words count;
// in a longer sentence such a word is one part of a question, not the whole of it.
func identifierTerms(query string) []string {
	words := strings.Fields(query)
	if len(words) == 0 || len(words) > 3 {
		return nil
	}
	var terms []string
	for _, w := range words {
		if !looksLikeIdentifier(w) {
			continue
		}
		terms = append(terms, keywordTokens(w)...)
	}
	return terms
}

// looksLikeIdentifier reports whether a single word reads as a name or code rather
// than plain prose: it contains a digit or an underscore, or punctuation with a
// letter or digit on both sides.
func looksLikeIdentifier(w string) bool {
	rs := []rune(strings.Trim(w, ".,;:!?\"'()[]{}"))
	for i, r := range rs {
		if unicode.IsDigit(r) || r == '_' {
			return true
		}
		if i > 0 && i < len(rs)-1 && r != '\'' && r != '\u2019' && !unicode.IsLetter(r) && !unicode.IsDigit(r) && !unicode.IsSpace(r) &&
			(unicode.IsLetter(rs[i-1]) || unicode.IsDigit(rs[i-1])) && (unicode.IsLetter(rs[i+1]) || unicode.IsDigit(rs[i+1])) {
			return true
		}
	}
	return false
}

// requireTerms keeps the hits whose text contains every term. For an identifier
// query, a hit that does not mention the identifier is an embedding look-alike
// ("hy_memory renamed to hyatlas" for HYATLAS_SYNC_EXTRACT), not an answer.
func requireTerms(hits []SearchHit, terms []string) []SearchHit {
	out := hits[:0:0]
	for _, h := range hits {
		have := map[string]bool{}
		for _, t := range keywordTokens(h.Content) {
			have[t] = true
		}
		ok := true
		for _, t := range terms {
			if !have[t] {
				ok = false
				break
			}
		}
		if ok {
			out = append(out, h)
		}
	}
	return out
}

// defaultIntentionTTL is how long an L7 intention stays in search results. 7 days:
// extraction records one for nearly every turn, so most are short-lived steps, and a
// goal that is still alive is restated (and replaced) by later turns.
const defaultIntentionTTL = 7 * 24 * time.Hour

// recencyHalfLife returns how fast a hit's ranking weight fades with age, or 0 when it
// does not fade. Durable memories (who the user is, standing schemas) never fade. Goals
// go stale within days, lessons and decisions keep their value for months, and
// everything else, including project state, fades within a month because it describes
// what is true now and is quickly overtaken.
func recencyHalfLife(h SearchHit) time.Duration {
	switch h.Layer {
	case memory.L1Profile, memory.L6Schema:
		return 0
	case memory.L7Intention:
		return 3 * 24 * time.Hour
	case memory.L3Fact:
		switch h.Meta["source_layer_label"] {
		case "user_preferences", "decision", "technical_lesson", "negative_knowledge":
			return 180 * 24 * time.Hour
		}
	}
	return 30 * 24 * time.Hour
}

// recencyFloor is the least share of its ranking weight an old memory keeps. Adjacent
// reciprocal-rank weights near the top differ by under 2% (1/61 against 1/62), so the
// floor sets how far age can move a hit: at 0.92, about five places at most. A lower
// floor let a fresh hit jump twenty-odd places, which made recency, not relevance,
// decide the results.
const recencyFloor = 0.92

// rerankRecency reorders hits so newer memories win close calls. Each hit keeps its
// reciprocal-rank weight from its incoming position and is scaled by a recency factor
// between recencyFloor and 1.0, so age only decides between hits that are already
// close in rank. The half-lives
// follow how fast each kind of memory goes stale. Hits with no timestamp, a timestamp
// in the future, or a durable layer keep factor 1.0. The input slice and the Score
// fields are not changed.
func rerankRecency(hits []SearchHit, now time.Time) []SearchHit {
	if len(hits) == 0 {
		return hits
	}
	weights := make([]float64, len(hits))
	order := make([]int, len(hits))
	for i, h := range hits {
		w := 1.0 / float64(rrfK+i+1)
		if half := recencyHalfLife(h); half > 0 {
			if c := gmtCreated(h.Meta["ts"]); c > 0 {
				created := time.Unix(c, 0)
				if created.Before(now) {
					age := now.Sub(created).Hours() / half.Hours()
					w *= recencyFloor + (1-recencyFloor)*math.Pow(0.5, age)
				}
			}
		}
		weights[i] = w
		order[i] = i
	}
	sort.SliceStable(order, func(a, b int) bool {
		return weights[order[a]] > weights[order[b]]
	})
	out := make([]SearchHit, len(hits))
	for i, idx := range order {
		out[i] = hits[idx]
	}
	return out
}

// dropStaleIntentions removes L7 intentions older than ttl. An intention is "what the
// user is trying to do right now"; weeks later it is history, and recall presenting it
// as current misleads the agent. The row is not deleted, only left out of search. An
// intention with an unknown timestamp is kept, because there is no evidence it is old.
// A ttl of zero or less keeps everything. Order is unchanged.
func dropStaleIntentions(hits []SearchHit, ttl time.Duration, now time.Time) []SearchHit {
	if ttl <= 0 {
		return hits
	}
	out := make([]SearchHit, 0, len(hits))
	for _, h := range hits {
		if h.Layer == memory.L7Intention {
			if c := gmtCreated(h.Meta["ts"]); c > 0 && now.Sub(time.Unix(c, 0)) > ttl {
				continue
			}
		}
		out = append(out, h)
	}
	return out
}
