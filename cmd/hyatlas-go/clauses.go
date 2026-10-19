package main

import (
	"fmt"
	"log"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/philippgille/chromem-go"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Clause indexing gives a long fact several search-only rows, one per clause. A
// consolidation merge can join many topics into one fact, and one embedding of the
// whole text averages them, so a query about one part scores it low. Each clause
// is embedded alone, a search that matches a clause finds the fact, and the fact
// itself is what the search returns. Clause rows live in their own collection, so
// counts, lists, the dashboard and normal search never see them.

const (
	// clauseCollection is the chromem collection that holds the clause rows.
	clauseCollection = "l3_clause"
	// clauseMinChars is the length (in characters) above which a live L3 fact is
	// split into clauses. Shorter facts are one embedding and need no clauses.
	clauseMinChars = 300
	// clauseMax caps the clauses of one fact. Overflow is merged into the last one.
	clauseMax = 6
	// clausePieceMin is the shortest piece kept as a clause. A shorter piece is
	// merged into the piece after it, so a fragment never stands alone.
	clausePieceMin = 40
)

// clauseEligible reports whether a row should have clause rows: a live L3 fact
// longer than clauseMinChars.
func clauseEligible(d DocIndex) bool {
	return d.Layer == string(memory.L3Fact) && !isSuperseded(d) &&
		utf8.RuneCountInString(d.Content) > clauseMinChars
}

// clauseID is the id of the n-th clause row of a parent fact.
func clauseID(parent string, n int) string {
	return parent + "#" + strconv.Itoa(n)
}

// splitClauses cuts a fact into clauses at sentence ends (". ", "; ") and at
// newlines. A piece shorter than clausePieceMin is merged into the piece after it,
// and at most clauseMax clauses are returned: any overflow joins the last one, so
// no text is lost. A blank text gives no clauses.
func splitClauses(text string) []string {
	var raw []string
	start := 0
	for i := 0; i < len(text); i++ {
		c := text[i]
		switch {
		case c == '\n':
			raw = append(raw, text[start:i])
			start = i + 1
		case (c == '.' || c == ';') && i+1 < len(text) && text[i+1] == ' ':
			raw = append(raw, text[start:i+1])
			start = i + 2
			i++
		}
	}
	raw = append(raw, text[start:])

	var out []string
	carry := ""
	for _, p := range raw {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		if carry != "" {
			p = carry + " " + p
			carry = ""
		}
		if utf8.RuneCountInString(p) < clausePieceMin {
			carry = p
			continue
		}
		out = append(out, p)
	}
	if carry != "" {
		if len(out) > 0 {
			out[len(out)-1] += " " + carry
		} else {
			out = append(out, carry)
		}
	}
	if len(out) > clauseMax {
		last := strings.Join(out[clauseMax-1:], " ")
		out = append(out[:clauseMax-1], last)
	}
	return out
}

// indexClauses writes the clause rows of d when d is an eligible fact. It is
// best-effort: a failure is logged, and the parent write that called it has already
// succeeded and is not affected.
func (s *MemoryStore) indexClauses(d DocIndex) {
	if !clauseEligible(d) {
		return
	}
	if _, err := s.writeClauses(d); err != nil {
		log.Printf("clauses: index %s: %v", d.ID, err)
	}
}

// writeClauses embeds the clauses of d and replaces the clause rows of d with them.
// It embeds before touching chromem, outside every store lock, and it writes only
// while d is still live. It returns how many clauses were written, which is 0 when d
// is not live. A failure drops any old clause rows of d, so none are left stale.
func (s *MemoryStore) writeClauses(d DocIndex) (int, error) {
	s.mu.RLock()
	cur, ok := s.index[d.ID]
	live := ok && !isSuperseded(cur)
	s.mu.RUnlock()
	if !live {
		return 0, nil
	}
	// A row re-added under the same id may be shorter now: old clauses would map a
	// stale sentence to the new text, so they go first.
	s.dropClauses(d.ID)
	pieces := splitClauses(d.Content)
	// One piece would only duplicate the parent's own vector, so it is not written.
	if len(pieces) < 2 {
		return 0, nil
	}
	docs := make([]chromem.Document, 0, len(pieces))
	for i, p := range pieces {
		emb, err := s.embed.Embed(s.ctx, p)
		if err != nil {
			s.dropClauses(d.ID)
			return 0, fmt.Errorf("couldn't embed clause %d: %w", i, err)
		}
		docs = append(docs, chromem.Document{
			ID:        clauseID(d.ID, i),
			Content:   p,
			Embedding: emb,
			Metadata:  map[string]string{"parent": d.ID, "user_id": d.UserID, "agent_id": d.AgentID},
		})
	}
	// Write the new rows. A supersede or delete that lands while the rows are written
	// drops them under s.mu; the check below catches a supersede that marked d before
	// this write became visible.
	for _, doc := range docs {
		if err := s.clauses.AddDocument(s.ctx, doc); err != nil {
			s.dropClauses(d.ID)
			return 0, fmt.Errorf("couldn't write clause row: %w", err)
		}
	}
	s.mu.RLock()
	cur, ok = s.index[d.ID]
	live = ok && !isSuperseded(cur)
	s.mu.RUnlock()
	if !live {
		s.dropClauses(d.ID)
		return 0, nil
	}
	return len(docs), nil
}

// dropClauses deletes the clause rows of the parent fact id. It takes no store lock,
// so it may run under s.mu. A failure is logged: a stale clause row only makes a
// search skip its parent, because clauseHits checks that the parent is live.
func (s *MemoryStore) dropClauses(id string) {
	ids := make([]string, clauseMax)
	for i := range ids {
		ids[i] = clauseID(id, i)
	}
	if err := s.clauses.Delete(s.ctx, nil, nil, ids...); err != nil {
		log.Printf("clauses: drop %s: %v", id, err)
	}
}

// hasClauses reports whether the parent fact id already has clause rows. Every
// clause write creates the first row, so its presence is enough.
func (s *MemoryStore) hasClauses(id string) bool {
	_, err := s.clauses.GetByID(s.ctx, clauseID(id, 0))
	return err == nil
}

// clauseHits searches the clause rows with the query embedding qv, filtered by where
// as searchWhere filters the layers, and returns the parent facts they belong to.
// Each hit is the parent, with the score of its best clause. A parent that is gone
// or superseded is skipped, and a parent with several matching clauses appears once.
// At most limit hits are returned.
func (s *MemoryStore) clauseHits(qv []float32, where map[string]string, limit int) []SearchHit {
	if limit <= 0 {
		limit = 5
	}
	n := s.clauses.Count()
	// Several clauses can belong to one parent, and some hits are skipped, so ask
	// for more clauses than parents wanted.
	k := limit * clauseMax
	if k > n {
		k = n
	}
	if k <= 0 {
		return nil
	}
	if len(where) == 0 {
		where = nil
	}
	res, err := s.clauses.QueryEmbedding(s.ctx, qv, k, where, nil)
	if err != nil {
		log.Printf("clauses: query: %v", err)
		return nil
	}
	var out []SearchHit
	seen := map[string]bool{}
	for _, r := range res {
		parent := r.Metadata["parent"]
		if parent == "" || seen[parent] {
			continue
		}
		s.mu.RLock()
		d, ok := s.index[parent]
		s.mu.RUnlock()
		if !ok || isSuperseded(d) {
			continue
		}
		// Results come best first, so the first clause seen for a parent is its best.
		seen[parent] = true
		out = append(out, SearchHit{ID: d.ID, Content: d.Content, Score: r.Similarity,
			Layer: memory.Layer(d.Layer), Meta: d.Meta})
		if len(out) == limit {
			break
		}
	}
	return out
}

// backfillClauses writes clause rows for every live long L3 fact that has none yet,
// so facts stored before clause indexing are searchable by clause too. It embeds
// only, and a second call writes nothing for facts it already covered. It logs and
// returns the number of facts it indexed.
func (s *MemoryStore) backfillClauses() int {
	s.mu.RLock()
	var todo []DocIndex
	for _, d := range s.index {
		if clauseEligible(d) {
			todo = append(todo, d)
		}
	}
	s.mu.RUnlock()
	n := 0
	for _, d := range todo {
		if s.hasClauses(d.ID) {
			continue
		}
		written, err := s.writeClauses(d)
		if err != nil {
			log.Printf("clauses: backfill %s: %v", d.ID, err)
			continue
		}
		if written > 0 {
			n++
		}
	}
	log.Printf("clauses: backfilled %d long facts", n)
	return n
}
