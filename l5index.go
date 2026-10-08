package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
	"time"

	"github.com/philippgille/chromem-go"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// L5 knowledge lives in the graph (graph.json), which is what the dashboard and
// the Desktop starmap draw. Search reads the vector collections, so without a
// copy there an L5 relation could never be recalled. Each live graph edge is
// therefore also stored as one L5 document, "From relation To", with the edge's
// owner and sources. The document ID is derived from the edge, so indexing the
// same edge twice is a no-op.

// l5DocID is the stable document ID for an edge: its owner, endpoints and relation,
// compared case-insensitively.
func l5DocID(userID, agentID, from, rel, to string) string {
	key := strings.ToLower(strings.Join([]string{userID, agentID,
		strings.TrimSpace(from), strings.TrimSpace(rel), strings.TrimSpace(to)}, "\x1f"))
	sum := sha256.Sum256([]byte(key))
	return "l5-" + hex.EncodeToString(sum[:12])
}

// l5Text is how an edge reads as a memory: "HyAtlas runs on port 19528".
func l5Text(from, rel, to string) string {
	return strings.TrimSpace(from) + " " + strings.ReplaceAll(strings.TrimSpace(rel), "_", " ") + " " + strings.TrimSpace(to)
}

// l5Edge is one relation to index, with its labels already resolved.
type l5Edge struct {
	user, agent, from, rel, to string
	sources                    []string
	recordedAt                 int64
}

// indexL5 stores each edge that is not yet in the L5 collection as an L5 document
// and writes the doc index once at the end. Edges already indexed are skipped, so
// it is safe to call with every edge on every start. It returns how many were added.
func (s *MemoryStore) indexL5(edges []l5Edge) (int, error) {
	col := s.cols[memory.L5Knowledge]
	if col == nil {
		return 0, fmt.Errorf("no collection for layer %s", memory.L5Knowledge)
	}
	added := 0
	var firstErr error
	for _, e := range edges {
		if strings.TrimSpace(e.from) == "" || strings.TrimSpace(e.rel) == "" || strings.TrimSpace(e.to) == "" {
			continue
		}
		id := l5DocID(e.user, e.agent, e.from, e.rel, e.to)
		s.mu.RLock()
		_, exists := s.index[id]
		s.mu.RUnlock()
		if exists {
			continue
		}
		text := l5Text(e.from, e.rel, e.to)
		emb, err := s.embed.Embed(s.ctx, text)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		// A relation that restates one already indexed for the same owner ("HyAtlas
		// server at 127.0.0.1:19528" beside "HyAtlas runs on 127.0.0.1:19528") is
		// not indexed again; the edge stays in the graph either way.
		if s.l5Dedupe > 0 {
			near, err := s.searchWhere(emb, 1, memory.L5Knowledge,
				map[string]string{"user_id": e.user, "agent_id": e.agent})
			if err == nil && len(near) > 0 && float64(near[0].Score) >= s.l5Dedupe && sameNumbers(near[0].Content, text) {
				continue
			}
		}
		ts := time.Now().UTC()
		if e.recordedAt > 0 {
			ts = time.Unix(e.recordedAt, 0).UTC()
		}
		meta := map[string]string{
			"user_id": e.user, "agent_id": e.agent, "ts": ts.Format(time.RFC3339),
			"layer": string(memory.L5Knowledge), "kind": "edge",
			"from": e.from, "relation": e.rel, "to": e.to,
		}
		if len(e.sources) > 0 {
			meta["source_id"] = e.sources[0]
			meta["sources"] = strings.Join(e.sources, ",")
		}
		s.rowMu.Lock()
		err = col.AddDocument(s.ctx, chromem.Document{ID: id, Content: text, Embedding: emb, Metadata: meta})
		if err == nil {
			s.mu.Lock()
			s.putLocked(docIndexFrom(id, string(memory.L5Knowledge), text, meta))
			s.mu.Unlock()
			added++
		} else if firstErr == nil {
			firstErr = err
		}
		s.rowMu.Unlock()
	}
	if added > 0 {
		s.mu.Lock()
		if err := s.persistIndexLocked(); err != nil && firstErr == nil {
			firstErr = err
		}
		s.mu.Unlock()
	}
	return added, firstErr
}

// BackfillL5 indexes every live graph edge that has no L5 document yet. Graphs
// written before 4.5.0 have edges and no L5 documents; this makes them searchable.
// It is idempotent, so the server runs it at every start.
func (s *MemoryStore) BackfillL5() (int, error) {
	nodes, edges := s.g.Snapshot(0)
	label := make(map[string]string, len(nodes))
	for _, n := range nodes {
		label[n.ID] = n.Label
	}
	var todo []l5Edge
	for _, e := range edges {
		if e.InvalidatedAt != 0 {
			continue
		}
		from, to := label[e.From], label[e.To]
		if from == "" || to == "" {
			continue
		}
		todo = append(todo, l5Edge{user: e.UserID, agent: e.AgentID, from: from, rel: e.Relation, to: to,
			sources: e.Sources, recordedAt: e.RecordedAt})
	}
	return s.indexL5(todo)
}
