package main

import (
	"math"
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// legacyBM25Rank is the keyword ranking as it was before the inverted index: it
// tokenised and stemmed every document on every call. It is kept here only as the
// reference the index must agree with, score for score.
func legacyBM25Rank(query []string, docs []DocIndex) []keywordScore {
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

// keywordCorpus is a small mixed corpus: repeats, stemmed forms, text with no search
// terms at all, and rows of different lengths.
var keywordCorpus = []struct{ id, user, text string }{
	{"k01", "alice", "HYATLAS_SYNC_EXTRACT=on makes the write block until extraction is done"},
	{"k02", "alice", "the sync button is blue"},
	{"k03", "bob", "HYATLAS_SYNC_EXTRACT is a server knob"},
	{"k04", "alice", "Alice libraries are stored in the library folder; the library is large"},
	{"k05", "alice", "stopping the server while stopped sessions are running"},
	{"k06", "bob", "nvda dip strategy"},
	{"k07", "alice", "nvda dip strategy plus a long tail of unrelated filler words about many other separate topics"},
	{"k08", "bob", "the a an of to"},
	{"k09", "alice", "deploy pipeline failed on port 19528 and the pipeline was fixed by restarting"},
	{"k10", "alice", "meeting notes: roadmap review, budget review, hiring review"},
	{"k11", "bob", "roadmap budget hiring port deploy sessions library notes"},
	{"k12", "alice", "went to the market and bought apples"},
}

var keywordQueries = []string{
	"HYATLAS_SYNC_EXTRACT", "sync", "library sessions", "nvda dip", "review", "pipeline port deploy fixed restarting",
	"roadmap budget hiring port deploy sessions library notes", "stop stopped stopping", "apple market went",
	"nothing matches here", "the a an", "review review review",
}

// matchIDs lists the document ids of ranked results, in order.
func matchIDs(ms []keywordScore) []string {
	out := make([]string, len(ms))
	for i, m := range ms {
		out[i] = m.Doc.ID
	}
	return out
}

// compareKeywordRanks fails when the store's index and the legacy ranking differ in
// order, matched count or score, for every query and owner scope.
func compareKeywordRanks(t *testing.T, s *MemoryStore, label string) {
	t.Helper()
	for _, owners := range [][]string{nil, {"alice"}, {"bob"}, {"alice", "bob"}} {
		var docs []DocIndex
		var ids []string
		s.mu.RLock()
		for _, d := range s.index {
			if isSuperseded(d) || d.Layer == string(memory.L2Raw) {
				continue
			}
			if len(owners) > 0 && !containsID(owners, d.UserID) {
				continue
			}
			docs = append(docs, d)
			ids = append(ids, d.ID)
		}
		s.mu.RUnlock()
		for _, q := range keywordQueries {
			tokens := keywordTokens(q)
			want := legacyBM25Rank(tokens, docs)
			s.mu.RLock()
			got := s.kw.rank(tokens, ids)
			s.mu.RUnlock()
			if len(got) != len(want) {
				t.Fatalf("%s: owners %v query %q: %d results, legacy %d", label, owners, q, len(got), len(want))
			}
			for i := range want {
				if got[i].ID != want[i].Doc.ID || got[i].Matched != want[i].Matched || got[i].Score != want[i].Score {
					t.Fatalf("%s: owners %v query %q: result %d is %+v, legacy %s %v (%d matched)",
						label, owners, q, i, got[i], want[i].Doc.ID, want[i].Score, want[i].Matched)
				}
			}
			// The wrapper that indexes on the spot agrees too.
			if direct := bm25Rank(tokens, docs); !reflect.DeepEqual(matchIDs(direct), matchIDs(want)) {
				t.Fatalf("%s: owners %v query %q: bm25Rank order %v, legacy %v", label, owners, q, matchIDs(direct), matchIDs(want))
			}
		}
	}
}

// The inverted index must score exactly as the per-query tokenising ranking did, and
// must stay right when rows are replaced, superseded, deleted and when the store is
// reopened (which rebuilds the index from disk).
func TestKeywordIndexMatchesLegacyScoring(t *testing.T) {
	dir := t.TempDir()
	s, err := NewMemoryStore(ctxForTest(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	for _, c := range keywordCorpus {
		if err := s.Add(memory.L3Fact, c.id, c.text, map[string]string{"user_id": c.user, "agent_id": "a1"}); err != nil {
			t.Fatal(err)
		}
	}
	// A raw row is in the keyword index too (lite mode searches it); the comparison below leaves it out of the ids it ranks.
	if err := s.Add(memory.L2Raw, "raw1", "HYATLAS_SYNC_EXTRACT raw transcript sync review", map[string]string{"user_id": "alice", "agent_id": "a1"}); err != nil {
		t.Fatal(err)
	}
	s.mu.RLock()
	_, rawIndexed := s.kw.docs["raw1"]
	s.mu.RUnlock()
	if !rawIndexed {
		t.Error("a raw row was not put in the keyword index")
	}
	compareKeywordRanks(t, s, "fresh")

	// Replacing a row's text (a same-id write) must re-index it.
	s.mu.Lock()
	d := s.index["k02"]
	d.Content = "the sync button is now green and the library is closed"
	s.putLocked(d)
	s.mu.Unlock()
	compareKeywordRanks(t, s, "replaced")

	// A flag change that leaves the text alone keeps the row indexed.
	if err := s.SetExtracted("k01", true); err != nil {
		t.Fatal(err)
	}
	compareKeywordRanks(t, s, "extracted flag")

	// Superseded rows leave the index; deleted rows too.
	if _, err := s.Supersede([]string{"k04", "k05"}, "k09"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Delete([]string{"k10"}, "", "", ""); err != nil {
		t.Fatal(err)
	}
	compareKeywordRanks(t, s, "superseded and deleted")
	s.mu.RLock()
	for _, gone := range []string{"k04", "k05", "k10"} {
		if _, ok := s.kw.docs[gone]; ok {
			t.Errorf("%s is still in the keyword index", gone)
		}
	}
	for term, ids := range s.kw.post {
		for id := range ids {
			if _, ok := s.kw.docs[id]; !ok {
				t.Errorf("posting list %q names unindexed %s", term, id)
			}
		}
	}
	s.mu.RUnlock()

	// A store opened on the same directory rebuilds the index and agrees.
	s.Close()
	again, err := NewMemoryStore(ctxForTest(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	defer again.Close()
	compareKeywordRanks(t, again, "reopened")
}
