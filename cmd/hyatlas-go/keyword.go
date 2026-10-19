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
// Terms shorter than two characters and stopwords are dropped, and each kept term is
// passed through stemTerm. Order is kept and repeats stay, because document term counts
// need them. Every caller tokenises with this function, so the stemming is symmetric:
// a query for "libraries" matches a memory that says "library".
func keywordTokens(text string) []string {
	fields := strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	})
	out := make([]string, 0, len(fields))
	for _, t := range fields {
		if utf8.RuneCountInString(t) < 2 || keywordStopwords[t] {
			continue
		}
		out = append(out, stemTerm(t))
	}
	return out
}

// irregularStems maps common irregular verb forms to their base form. It is checked
// before the length gate, because "ran", "went" and "made" are too short for the
// suffix rules and would otherwise never be reached.
var irregularStems = map[string]string{
	"broken":  "break",
	"broke":   "break",
	"built":   "build",
	"made":    "make",
	"went":    "go",
	"ran":     "run",
	"running": "run",
	"written": "write",
	"wrote":   "write",
	"chosen":  "choose",
	"chose":   "choose",
	"taken":   "take",
	"took":    "take",
	"given":   "give",
	"gave":    "give",
}

// stemTerm reduces an English term to a rough base form, so that "sessions" and
// "session" share one index term. It handles irregular verbs first, then the first
// suffix rule that applies, in this order: ies to y; ing and ed, with a doubled final
// consonant reduced to one (stopping to stop) except ll, ss and zz; es, only after ss,
// x, ch or sh; and s, unless the word ends in ss, us or is. Every rule keeps a stem of
// at least three letters. Terms with a digit or a non-ASCII letter, and terms shorter
// than five letters, are returned unchanged.
//
// Why so crude: a full stemmer would be a dependency for a small gain, and it is
// deliberately not exact (embedding and keyword search only need matching forms to
// meet, not the linguistically right stem). The same function runs on documents and
// queries, so a mistake such as "embedded" to "embed" still matches itself.
func stemTerm(t string) string {
	if s, ok := irregularStems[t]; ok {
		return s
	}
	if len(t) < 5 || !isLowerASCII(t) {
		return t
	}
	if strings.HasSuffix(t, "ies") && len(t)-3 >= 3 {
		return t[:len(t)-3] + "y"
	}
	if strings.HasSuffix(t, "ing") && len(t)-3 >= 3 {
		return undoubleConsonant(t[:len(t)-3])
	}
	if strings.HasSuffix(t, "ed") && len(t)-2 >= 3 {
		return undoubleConsonant(t[:len(t)-2])
	}
	if strings.HasSuffix(t, "es") && len(t)-2 >= 3 {
		stem := t[:len(t)-2]
		if strings.HasSuffix(stem, "ss") || strings.HasSuffix(stem, "x") ||
			strings.HasSuffix(stem, "ch") || strings.HasSuffix(stem, "sh") {
			return stem
		}
	}
	if strings.HasSuffix(t, "s") && len(t)-1 >= 3 &&
		!strings.HasSuffix(t, "ss") && !strings.HasSuffix(t, "us") && !strings.HasSuffix(t, "is") {
		return t[:len(t)-1]
	}
	return t
}

// undoubleConsonant drops one letter when stem ends in a doubled consonant other than
// ll, ss or zz, so "stopp" becomes "stop" but "fail" and "pass" keep their letters.
// It is the step that undoes consonant doubling before ing and ed.
func undoubleConsonant(stem string) string {
	n := len(stem)
	if n < 2 {
		return stem
	}
	last := stem[n-1]
	if stem[n-2] != last || strings.IndexByte("aeiou", last) >= 0 || last < 'a' || last > 'z' {
		return stem
	}
	switch stem[n-2:] {
	case "ll", "ss", "zz":
		return stem
	}
	return stem[:n-1]
}

// isLowerASCII reports whether s holds only the letters a to z.
func isLowerASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < 'a' || s[i] > 'z' {
			return false
		}
	}
	return true
}

// sameNumbers reports whether a and b contain the same set of numbers (runs of
// digits). Embeddings barely see numbers, so "open PR #101722" and "open PR #96783"
// score 0.99 alike. De-duplication requires the numbers to match, because keeping
// two near-identical rows costs little and wrongly merging two true ones loses one.
func sameNumbers(a, b string) bool {
	na, nb := digitRuns(a), digitRuns(b)
	if len(na) != len(nb) {
		return false
	}
	for n := range na {
		if !nb[n] {
			return false
		}
	}
	return true
}

// digitRuns is the set of digit runs in s, with leading zeros kept.
func digitRuns(s string) map[string]bool {
	out := map[string]bool{}
	start := -1
	for i, r := range s + " " {
		if r >= '0' && r <= '9' {
			if start < 0 {
				start = i
			}
			continue
		}
		if start >= 0 {
			out[s[start:i]] = true
			start = -1
		}
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

// kwDoc is one indexed document: how often each term occurs in it, and how many terms
// it has in all, which is the document length BM25 needs.
type kwDoc struct {
	content string
	tf      map[string]int
	n       int
}

// kwIndex is an inverted keyword index: for every term, the documents that contain it,
// and for every document its term counts and length. The store keeps one over its live
// rows, raw ones only when it is told to (see SetKeywordRaw), updated as rows are
// written, replaced and deleted, so a search reads
// the posting lists of its query terms instead of tokenising and stemming every row.
// It is not safe for concurrent use; the store guards it with its own lock.
type kwIndex struct {
	docs map[string]kwDoc
	post map[string]map[string]struct{}
}

// newKwIndex returns an empty keyword index.
func newKwIndex() *kwIndex {
	return &kwIndex{docs: map[string]kwDoc{}, post: map[string]map[string]struct{}{}}
}

// put indexes content under id, replacing what id held before. Content that is
// unchanged since the last put is not tokenised again.
func (k *kwIndex) put(id, content string) {
	if cur, ok := k.docs[id]; ok {
		if cur.content == content {
			return
		}
		k.remove(id)
	}
	tokens := keywordTokens(content)
	tf := make(map[string]int, len(tokens))
	for _, t := range tokens {
		tf[t]++
	}
	k.docs[id] = kwDoc{content: content, tf: tf, n: len(tokens)}
	for t := range tf {
		ids := k.post[t]
		if ids == nil {
			ids = map[string]struct{}{}
			k.post[t] = ids
		}
		ids[id] = struct{}{}
	}
}

// remove drops id from the index. An id that is not indexed is ignored.
func (k *kwIndex) remove(id string) {
	cur, ok := k.docs[id]
	if !ok {
		return
	}
	delete(k.docs, id)
	for t := range cur.tf {
		if ids := k.post[t]; ids != nil {
			delete(ids, id)
			if len(ids) == 0 {
				delete(k.post, t)
			}
		}
	}
}

// kwMatch is one ranked document of kwIndex.rank: its id, BM25 score, and how many
// distinct query terms it contains.
type kwMatch struct {
	ID      string
	Score   float64
	Matched int
}

// rank scores the documents named by ids against the distinct query terms with BM25
// (k1 1.2, b 0.75), keeps those that contain at least keywordRequired of them, and
// returns them best first, ties broken by ID. The collection statistics (document
// count, average length and each term's document frequency) are taken over ids alone,
// so a search scoped to one owner scores as if the store held only that owner's rows.
// Ids the index does not hold count as empty documents.
func (k *kwIndex) rank(query []string, ids []string) []kwMatch {
	seen := make(map[string]bool, len(query))
	uniq := make([]string, 0, len(query))
	for _, t := range query {
		if !seen[t] {
			seen[t] = true
			uniq = append(uniq, t)
		}
	}
	if len(uniq) == 0 || len(ids) == 0 {
		return nil
	}

	const k1, b = 1.2, 0.75
	N := len(ids)
	pos := make(map[string]int, N)
	totalLen := 0
	for i, id := range ids {
		pos[id] = i
		totalLen += k.docs[id].n
	}
	avgdl := float64(totalLen) / float64(N)
	if avgdl == 0 {
		avgdl = 1
	}
	// Only the posting lists of the query terms are read.
	df := make(map[string]int, len(uniq))
	matched := make(map[string]int)
	for _, t := range uniq {
		for id := range k.post[t] {
			if _, in := pos[id]; in {
				df[t]++
				matched[id]++
			}
		}
	}
	idf := make(map[string]float64, len(uniq))
	for _, t := range uniq {
		d := float64(df[t])
		idf[t] = math.Log(1 + (float64(N)-d+0.5)/(d+0.5))
	}

	need := keywordRequired(len(uniq))
	var out []kwMatch
	for id, m := range matched {
		if m < need {
			continue
		}
		doc := k.docs[id]
		dl := float64(doc.n)
		score := 0.0
		for _, t := range uniq {
			tf := float64(doc.tf[t])
			if tf == 0 {
				continue
			}
			score += idf[t] * (tf * (k1 + 1)) / (tf + k1*(1-b+b*dl/avgdl))
		}
		out = append(out, kwMatch{ID: id, Score: score, Matched: m})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		return out[i].ID < out[j].ID
	})
	return out
}

// bm25Rank scores docs against the query with BM25 and keeps the documents that
// contain at least keywordRequired of its distinct terms, best first, ties broken by
// ID. It indexes docs on the spot, so it suits tests and one-off ranking of a small
// set of rows; a search over the store's live rows uses the store's own index.
func bm25Rank(query []string, docs []DocIndex) []keywordScore {
	idx := newKwIndex()
	ids := make([]string, 0, len(docs))
	byID := make(map[string]DocIndex, len(docs))
	for _, d := range docs {
		idx.put(d.ID, d.Content)
		ids = append(ids, d.ID)
		byID[d.ID] = d
	}
	matches := idx.rank(query, ids)
	out := make([]keywordScore, 0, len(matches))
	for _, m := range matches {
		out = append(out, keywordScore{Doc: byID[m.ID], Score: m.Score, Matched: m.Matched})
	}
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
// unless layer asks for them (a server that extracts passes includeRaw false; see
// keywordSearch). userIDs, when given, restricts to those owners plus
// rows with no owner at all, and agentID, when set, to that agent. Each hit's Score is its cosine similarity to the
// query, so it reads like a vector hit; it is 0 when that cannot be computed. It does
// not count toward the search counter, because the caller's search already does.
func (s *MemoryStore) KeywordSearch(query string, limit int, layer memory.Layer, userIDs []string, agentID string) ([]SearchHit, error) {
	if len(keywordTokens(query)) == 0 {
		return nil, nil
	}
	qv, err := s.embed.Embed(s.ctx, query)
	if err != nil {
		qv = nil
	}
	return s.KeywordSearchVec(query, qv, limit, layer, userIDs, agentID)
}

// KeywordSearchVec is KeywordSearch with the query already embedded, so a caller that
// holds the query vector does not embed it again. A nil qv gives every hit a score of 0.
func (s *MemoryStore) KeywordSearchVec(query string, qv []float32, limit int, layer memory.Layer, userIDs []string, agentID string) ([]SearchHit, error) {
	return s.keywordSearch(query, qv, limit, layer, userIDs, agentID, false)
}

// keywordSearch is KeywordSearchVec with a switch for raw rows. With includeRaw true,
// a search over all layers ranks L2 raw rows with the rest; a server in lite mode sets
// it, because raw rows are all lite stores and an identifier would otherwise find
// nothing. With it false a search over all layers leaves them out, so the rankings of
// the modes that extract do not change.
func (s *MemoryStore) keywordSearch(query string, qv []float32, limit int, layer memory.Layer, userIDs []string, agentID string, includeRaw bool) ([]SearchHit, error) {
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

	// Rows are chosen under the read lock without tokenising any of them: the ids that
	// pass the layer, superseded and owner filters are ranked from the inverted index.
	var ranked []keywordScore
	s.mu.RLock()
	var ids []string
	// Raw rows the store keeps out of its keyword index (keywordRaw false) are ranked
	// on the fly below, as before the index existed.
	var rawDocs []DocIndex
	for _, d := range s.index {
		if isSuperseded(d) {
			continue
		}
		if layer != "" {
			if d.Layer != string(layer) {
				continue
			}
		} else if d.Layer == string(memory.L2Raw) && !includeRaw {
			continue
		}
		// A row with no owner at all predates owner tracking; under an owner filter
		// it is visible, as in SearchOwners and the graph endpoints.
		ownerless := d.UserID == "" && d.AgentID == ""
		if len(owners) > 0 && !owners[d.UserID] && !ownerless {
			continue
		}
		if agentID != "" && d.AgentID != agentID && !(len(owners) > 0 && ownerless) {
			continue
		}
		if d.Layer == string(memory.L2Raw) && !s.keywordRaw {
			rawDocs = append(rawDocs, d)
			continue
		}
		ids = append(ids, d.ID)
	}
	matches := s.kw.rank(q, ids)
	if len(rawDocs) > 0 {
		for _, r := range bm25Rank(q, rawDocs) {
			matches = append(matches, kwMatch{ID: r.Doc.ID, Score: r.Score, Matched: r.Matched})
		}
		sort.Slice(matches, func(i, j int) bool {
			if matches[i].Score != matches[j].Score {
				return matches[i].Score > matches[j].Score
			}
			return matches[i].ID < matches[j].ID
		})
	}
	if len(matches) > limit {
		matches = matches[:limit]
	}
	for _, m := range matches {
		ranked = append(ranked, keywordScore{Doc: s.index[m.ID], Score: m.Score, Matched: m.Matched})
	}
	s.mu.RUnlock()
	if len(ranked) > limit {
		ranked = ranked[:limit]
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
