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

// fuseHitsWeighted merges a vector ranking and a keyword ranking with reciprocal rank
// fusion, then drops repeated text and keeps the best limit (see refineHits). The
// weights set each ranking's contribution, so a query's intent (see classifyQuery)
// can lean on keyword search for identifiers or on meaning for "how" and "why"
// questions. Equal weights of 1 give the plain fusion.
//
// The score floor applies to vector hits only. A keyword hit already had to contain
// most of the query's terms (keywordRequired), and the identifiers keyword search is
// for, such as HYATLAS_SYNC_EXTRACT or a port number, are exactly what an embedding
// scores low. Each hit keeps its cosine Score; only the order comes from the fusion.
// Both inputs must be sorted best first.
func fuseHitsWeighted(vec, kw []SearchHit, minScore float64, limit int, wVec, wKw float64) []SearchHit {
	type fused struct {
		hit  SearchHit
		rank float64
	}
	byID := map[string]*fused{}
	var order []string
	add := func(h SearchHit, rank int, weight float64) {
		f, ok := byID[h.ID]
		if !ok {
			f = &fused{hit: h}
			byID[h.ID] = f
			order = append(order, h.ID)
		} else if h.Score > f.hit.Score {
			f.hit.Score = h.Score
		}
		f.rank += weight / float64(rrfK+rank)
	}
	rank := 0
	for _, h := range vec {
		if float64(h.Score) < minScore {
			continue
		}
		rank++
		add(h, rank, wVec)
	}
	for i, h := range kw {
		add(h, i+1, wKw)
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
	case memory.L1Profile, memory.L6Schema, memory.L8Entity:
		// An entity page is rewritten whenever its thing changes, so its state is
		// current by construction; its age says nothing about staleness.
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

// rerankRecencyUsed reorders hits so newer memories win close calls. Each hit keeps its
// reciprocal-rank weight from its incoming position and is scaled by a recency factor
// between recencyFloor and 1.0, so age only decides between hits that are already
// close in rank. The half-lives follow how fast each kind of memory goes stale. Hits
// with no timestamp, a timestamp in the future, or a durable layer keep factor 1.0. The
// input slice and the Score fields are not changed.
//
// A memory that is still being used is not old in the sense that matters, so its age
// is measured from the later of its creation and the last time lastUsed reports it was
// used. A zero time, a time after now, or a nil lastUsed falls back to the creation
// timestamp alone. Callers with no usage data pass nil.
//
// Why: a fact written months ago that the agent relies on every week is current, but
// a pure creation age sinks it below newer, unused noise. Measuring from the last use
// lets recency reward a memory for staying in use, not only for being new.
func rerankRecencyUsed(hits []SearchHit, now time.Time, lastUsed func(id string) time.Time) []SearchHit {
	if len(hits) == 0 {
		return hits
	}
	return rerankByFactor(hits, func(_ int, h SearchHit) float64 {
		f := 1.0
		if half := recencyHalfLife(h); half > 0 {
			if c := gmtCreated(h.Meta["ts"]); c > 0 {
				ref := time.Unix(c, 0)
				if lastUsed != nil {
					if u := lastUsed(h.ID); !u.IsZero() && !u.After(now) && u.After(ref) {
						ref = u
					}
				}
				if ref.Before(now) {
					age := now.Sub(ref).Hours() / half.Hours()
					f = recencyFloor + (1-recencyFloor)*math.Pow(0.5, age)
				}
			}
		}
		return f
	})
}

// rerankByFactor reorders hits by their position weight, 1/(rrfK+i+1), times
// factor(i, h), best first, with a stable sort so equal weights keep their order. It
// returns a new slice; the input and every Score are left as they were. A factor of 1
// leaves a hit at its place in the weight order, so it is the "no change" value.
func rerankByFactor(hits []SearchHit, factor func(i int, h SearchHit) float64) []SearchHit {
	weights := make([]float64, len(hits))
	order := make([]int, len(hits))
	for i, h := range hits {
		weights[i] = (1.0 / float64(rrfK+i+1)) * factor(i, h)
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

// dropStaleIntentions removes L7 intentions that are no longer current. An intention
// is "what the user is trying to do right now"; recall presenting history as current
// misleads the agent. The row is not deleted, only left out of search.
//
// An intention with a valid due_at (a normalised YYYY-MM-DD or YYYY-MM) is kept until
// the end of its due span plus one day, and is dropped after that regardless of ttl:
// a plan due in three weeks must not vanish after seven days, and one due yesterday is
// history. An intention without a valid due_at is kept while it is younger than ttl. A
// ttl of zero or less turns off only that ttl rule, not a due_at. An intention with an
// unknown timestamp is kept, because there is no evidence it is old. Order is unchanged.
func dropStaleIntentions(hits []SearchHit, ttl time.Duration, now time.Time) []SearchHit {
	out := make([]SearchHit, 0, len(hits))
	for _, h := range hits {
		if h.Layer == memory.L7Intention {
			if _, to, ok := eventDateRange(h.Meta[metaDueAt], now.Location()); ok {
				if !now.Before(to.Add(24 * time.Hour)) {
					continue
				}
			} else if ttl > 0 {
				if c := gmtCreated(h.Meta["ts"]); c > 0 && now.Sub(time.Unix(c, 0)) > ttl {
					continue
				}
			}
		}
		out = append(out, h)
	}
	return out
}
