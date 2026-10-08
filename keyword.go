package main

import (
	"math"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// keywordStopwords are words too common to say anything about a memory. They are
// dropped from both queries and documents.
var keywordStopwords = map[string]bool{
	"a": true, "an": true, "the": true, "and": true, "or": true, "of": true, "to": true,
	"in": true, "on": true, "for": true, "with": true, "is": true, "are": true, "was": true,
	"were": true, "be": true, "been": true, "being": true, "it": true, "its": true, "this": true,
	"that": true, "these": true, "those": true, "what": true, "which": true, "who": true,
	"whom": true, "how": true, "do": true, "does": true, "did": true, "can": true, "could": true,
	"should": true, "would": true, "will": true, "shall": true, "may": true, "might": true,
	"must": true, "i": true, "you": true, "he": true, "she": true, "we": true, "they": true,
	"me": true, "my": true, "your": true, "our": true, "us": true, "them": true, "their": true,
	"his": true, "her": true, "at": true, "by": true, "from": true, "as": true, "about": true,
	"into": true, "over": true, "not": true, "no": true, "yes": true, "so": true, "if": true,
	"then": true, "than": true, "there": true, "here": true, "when": true, "where": true,
	"why": true, "just": true, "also": true, "any": true, "all": true, "some": true, "more": true,
	"most": true, "very": true, "use": true, "using": true, "used": true, "get": true, "got": true,
	"have": true, "has": true, "had": true, "please": true, "want": true, "need": true,
	"make": true, "made": true, "like": true, "one": true, "two": true,
}

// keywordTokens splits text into lower-case search terms: runs of letters or digits.
// Everything else separates terms, so HYATLAS_SYNC_EXTRACT is hyatlas, sync, extract.
// Terms shorter than two characters and stopwords are dropped. Order is kept and
// repeats stay, because document term counts need them.
func keywordTokens(text string) []string {
	fields := strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	})
	out := make([]string, 0, len(fields))
	for _, t := range fields {
		if utf8.RuneCountInString(t) < 2 || keywordStopwords[t] {
			continue
		}
		out = append(out, t)
	}
	return out
}

// keywordScore is one document's BM25 result: its score and how many distinct query
// terms it contains.
type keywordScore struct {
	Doc     DocIndex
	Score   float64
	Matched int
}

// keywordRequired is how many distinct query terms a document must contain to count
// as a keyword match: all of them for a query of up to three terms, three quarters
// (rounded up) beyond that. Requiring most terms is what keeps a stray common word
// from returning an unrelated memory.
func keywordRequired(n int) int {
	if n <= 3 {
		return n
	}
	return int(math.Ceil(0.75 * float64(n)))
}

// bm25Rank scores docs against the distinct query terms with BM25 (k1 1.2, b 0.75),
// keeps the documents that contain at least keywordRequired of them, and returns
// them best first, ties broken by ID.
func bm25Rank(query []string, docs []DocIndex) []keywordScore {
	seen := make(map[string]bool, len(query))
	uniq := make([]string, 0, len(query))
	for _, t := range query {
		if !seen[t] {
			seen[t] = true
			uniq = append(uniq, t)
		}
	}
	if len(uniq) == 0 || len(docs) == 0 {
		return nil
	}

	const k1, b = 1.2, 0.75
	N := len(docs)
	tokens := make([][]string, N)
	tfs := make([]map[string]int, N)
	df := make(map[string]int)
	totalLen := 0
	for i, d := range docs {
		tokens[i] = keywordTokens(d.Content)
		totalLen += len(tokens[i])
		tf := make(map[string]int, len(tokens[i]))
		for _, t := range tokens[i] {
			tf[t]++
		}
		tfs[i] = tf
		for _, t := range uniq {
			if tf[t] > 0 {
				df[t]++
			}
		}
	}
	avgdl := float64(totalLen) / float64(N)
	if avgdl == 0 {
		avgdl = 1
	}
	idf := make(map[string]float64, len(uniq))
	for _, t := range uniq {
		d := float64(df[t])
		idf[t] = math.Log(1 + (float64(N)-d+0.5)/(d+0.5))
	}

	need := keywordRequired(len(uniq))
	var out []keywordScore
	for i, d := range docs {
		dl := float64(len(tokens[i]))
		score := 0.0
		matched := 0
		for _, t := range uniq {
			tf := float64(tfs[i][t])
			if tf == 0 {
				continue
			}
			matched++
			score += idf[t] * (tf * (k1 + 1)) / (tf + k1*(1-b+b*dl/avgdl))
		}
		if matched >= need {
			out = append(out, keywordScore{Doc: d, Score: score, Matched: matched})
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		return out[i].Doc.ID < out[j].Doc.ID
	})
	return out
}

// cosineSim is the cosine similarity of a and b, or 0 when either is empty, zero,
// or their lengths differ.
func cosineSim(a, b []float32) float64 {
	if len(a) == 0 || len(b) == 0 || len(a) != len(b) {
		return 0
	}
	var dot, na, nb float64
	for i := range a {
		x, y := float64(a[i]), float64(b[i])
		dot += x * y
		na += x * x
		nb += y * y
	}
	if na == 0 || nb == 0 {
		return 0
	}
	return dot / (math.Sqrt(na) * math.Sqrt(nb))
}

// KeywordSearch finds live memories by their words rather than their meaning, which
// catches exact names and identifiers an embedding blurs. Raw (L2) rows are left out
// unless layer asks for them. userIDs, when given, restricts to those owners, and
// agentID, when set, to that agent. Each hit's Score is its cosine similarity to the
// query, so it reads like a vector hit; it is 0 when that cannot be computed. It does
// not count toward the search counter, because the caller's search already does.
func (s *MemoryStore) KeywordSearch(query string, limit int, layer memory.Layer, userIDs []string, agentID string) ([]SearchHit, error) {
	if limit <= 0 {
		limit = 5
	}
	q := keywordTokens(query)
	if len(q) == 0 {
		return nil, nil
	}

	owners := make(map[string]bool, len(userIDs))
	for _, u := range userIDs {
		if u != "" {
			owners[u] = true
		}
	}

	var docs []DocIndex
	s.mu.RLock()
	for _, d := range s.index {
		if isSuperseded(d) {
			continue
		}
		if layer != "" {
			if d.Layer != string(layer) {
				continue
			}
		} else if d.Layer == string(memory.L2Raw) {
			continue
		}
		if len(owners) > 0 && !owners[d.UserID] {
			continue
		}
		if agentID != "" && d.AgentID != agentID {
			continue
		}
		docs = append(docs, d)
	}
	s.mu.RUnlock()

	ranked := bm25Rank(q, docs)
	if len(ranked) > limit {
		ranked = ranked[:limit]
	}

	qv, err := s.embed.Embed(s.ctx, query)
	if err != nil {
		qv = nil
	}

	hits := make([]SearchHit, 0, len(ranked))
	for _, r := range ranked {
		d := r.Doc
		var score float64
		if qv != nil {
			if col := s.cols[memory.Layer(d.Layer)]; col != nil {
				if doc, err := col.GetByID(s.ctx, d.ID); err == nil {
					score = cosineSim(qv, doc.Embedding)
				}
			}
		}
		meta := make(map[string]string, len(d.Meta)+3)
		for k, v := range d.Meta {
			meta[k] = v
		}
		if meta["user_id"] == "" {
			meta["user_id"] = d.UserID
		}
		if meta["agent_id"] == "" {
			meta["agent_id"] = d.AgentID
		}
		if meta["ts"] == "" {
			meta["ts"] = d.Ts
		}
		hits = append(hits, SearchHit{
			ID:      d.ID,
			Content: d.Content,
			Score:   float32(score),
			Layer:   memory.Layer(d.Layer),
			Meta:    meta,
		})
	}
	return hits, nil
}
