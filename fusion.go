package main

import (
	"sort"
	"strings"
	"unicode"
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
