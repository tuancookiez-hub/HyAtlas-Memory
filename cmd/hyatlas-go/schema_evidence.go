package main

import (
	"maps"
	"sort"
	"strings"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// schemaEvidenceMax caps how many schemas a search adds through evidence.
const schemaEvidenceMax = 2

// schemasSupporting returns live L6 schemas of the owners (users, agentID with the
// same empty-means-any rule as windowCandidates) whose "evidence" metadata names at
// least one of factIDs, ordered by how many of factIDs they cite (most first; ties:
// newest first), at most limit. Each returned SearchHit has Layer L6, Content the
// pattern, Score 0, and Meta a copy of the row's metadata.
//
// It is a reverse lookup from facts to the patterns they support, so a search that
// finds facts can also show the schemas behind them. It reads only the index and
// never calls the embedder, so it is cheap enough to run on every search.
func (s *MemoryStore) schemasSupporting(factIDs []string, users []string, agentID string, limit int) []SearchHit {
	if len(factIDs) == 0 || limit <= 0 {
		return nil
	}
	want := make(map[string]bool, len(factIDs))
	for _, id := range factIDs {
		want[id] = true
	}

	type scored struct {
		doc   DocIndex
		cites int
	}
	var found []scored

	for _, d := range s.liveDocs(memory.L6Schema, users, agentID) {
		cites := 0
		seen := map[string]bool{}
		for _, id := range strings.Split(d.Meta["evidence"], ",") {
			id = strings.TrimSpace(id)
			if id == "" || seen[id] {
				continue
			}
			seen[id] = true
			if want[id] {
				cites++
			}
		}
		if cites == 0 {
			continue
		}
		found = append(found, scored{doc: d, cites: cites})
	}

	// Most cited first; equal counts fall back to newest first.
	sort.SliceStable(found, func(a, b int) bool {
		if found[a].cites != found[b].cites {
			return found[a].cites > found[b].cites
		}
		return newerFirst(found[a].doc, found[b].doc)
	})
	if len(found) > limit {
		found = found[:limit]
	}

	hits := make([]SearchHit, 0, len(found))
	for _, f := range found {
		meta := maps.Clone(f.doc.Meta)
		hits = append(hits, SearchHit{
			ID:      f.doc.ID,
			Content: f.doc.Content,
			Score:   0,
			Layer:   memory.L6Schema,
			Meta:    meta,
		})
	}
	return hits
}
