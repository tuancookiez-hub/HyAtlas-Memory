package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"

	"github.com/philippgille/chromem-go"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// One-off maintenance for stores written before 4.4.1. The Hermes plugin used to
// store each turn with every tool result and, after context compaction, the
// compaction summary, so raw rows reached hundreds of thousands of characters; and
// facts re-extracted from that repeated text piled up as near-duplicates. These two
// endpoints repair what is already stored. Both default to a dry run.

const (
	// compactMessageMax and compactTurnMax match the plugin's per-message and
	// per-turn caps, so a compacted old row looks like a row written today.
	compactMessageMax = 4000
	compactTurnMax    = 12000
)

// turnRolePrefixes are the speaker labels the plugin writes at the start of each
// message of a turn, each message separated by a blank line.
var turnRolePrefixes = []string{"USER: ", "ASSISTANT: ", "TOOL: ", "SYSTEM: ", "FUNCTION: "}

// compactTurnText rewrites a stored turn to what the plugin stores now: user and
// assistant messages only, compaction summaries left out, each message capped at
// perMsg bytes and the whole at total. Text with no speaker labels (a memory-tool
// write, for one) is only capped. If nothing but tool output and summaries is left,
// a short note says so, so the row is never empty.
func compactTurnText(text string, perMsg, total int) string {
	type segment struct{ role, body string }
	var starts []int
	for i := 0; i < len(text); i++ {
		if i != 0 && !strings.HasSuffix(text[:i], "\n\n") && !strings.HasSuffix(text[:i], "\n\r\n") {
			continue
		}
		for _, p := range turnRolePrefixes {
			if strings.HasPrefix(text[i:], p) {
				starts = append(starts, i)
				break
			}
		}
	}
	if len(starts) == 0 {
		return utf8Trunc(text, total)
	}
	var segs []segment
	if starts[0] > 0 {
		segs = append(segs, segment{body: text[:starts[0]]})
	}
	for n, st := range starts {
		end := len(text)
		if n+1 < len(starts) {
			end = starts[n+1]
		}
		seg := text[st:end]
		for _, p := range turnRolePrefixes {
			if strings.HasPrefix(seg, p) {
				segs = append(segs, segment{role: strings.TrimSuffix(p, ": "), body: seg[len(p):]})
				break
			}
		}
	}
	var parts []string
	for _, sg := range segs {
		body := strings.TrimSpace(sg.body)
		if body == "" || isCompactionText(body) {
			continue
		}
		switch sg.role {
		case "USER", "ASSISTANT":
			parts = append(parts, sg.role+": "+utf8Trunc(body, perMsg))
		case "":
			parts = append(parts, utf8Trunc(body, perMsg))
		}
	}
	if len(parts) == 0 {
		return "[turn compacted: it held only tool output or a context summary]"
	}
	return utf8Trunc(strings.Join(parts, "\n\n"), total)
}

// isCompactionText reports whether a message is Hermes' context-compaction summary,
// detected as Hermes does: a marker near the start.
func isCompactionText(s string) bool {
	head := s
	if len(head) > 200 {
		head = head[:200]
	}
	return strings.Contains(head, "CONTEXT COMPACTION") || strings.Contains(head, "Conversation Summary")
}

// rewriteContent replaces the content of live rows in one layer. Each row keeps its
// ID and metadata and is re-embedded; the index file is written once, at the end,
// instead of once per row as Add does. Missing and superseded rows are skipped. It
// does not count as writes. It returns how many rows were rewritten.
func (s *MemoryStore) rewriteContent(layer memory.Layer, updates map[string]string) (int, error) {
	col := s.cols[layer]
	if col == nil {
		return 0, fmt.Errorf("no collection for layer %s", layer)
	}
	ids := make([]string, 0, len(updates))
	for id := range updates {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	n := 0
	var firstErr error
	for _, id := range ids {
		content := updates[id]
		emb, err := s.embed.Embed(s.ctx, content)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		s.rowMu.Lock()
		s.mu.RLock()
		cur, ok := s.index[id]
		s.mu.RUnlock()
		if !ok || isSuperseded(cur) || cur.Layer != string(layer) {
			s.rowMu.Unlock()
			continue
		}
		meta := make(map[string]string, len(cur.Meta)+4)
		for k, v := range cur.Meta {
			meta[k] = v
		}
		// Older index rows may carry the owner only in the fixed fields.
		for k, v := range map[string]string{"user_id": cur.UserID, "agent_id": cur.AgentID, "ts": cur.Ts} {
			if meta[k] == "" && v != "" {
				meta[k] = v
			}
		}
		meta["layer"] = string(layer)
		if err := col.AddDocument(s.ctx, chromem.Document{ID: id, Content: content, Embedding: emb, Metadata: meta}); err != nil {
			s.rowMu.Unlock()
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		s.mu.Lock()
		nd := docIndexFrom(id, string(layer), content, meta)
		// SetExtracted flips only the index field, not the metadata, so keep it
		// or a rewritten row would be extracted a second time.
		nd.Extracted = nd.Extracted || cur.Extracted
		s.putLocked(nd)
		s.mu.Unlock()
		s.rowMu.Unlock()
		n++
	}
	s.mu.Lock()
	if err := s.persistIndexLocked(); err != nil && firstErr == nil {
		firstErr = err
	}
	s.mu.Unlock()
	return n, firstErr
}

// maintenanceRequest is the body both maintenance endpoints take. DryRun defaults to
// true: a request must say "dry_run": false to change anything.
type maintenanceRequest struct {
	DryRun    *bool   `json:"dry_run"`
	Threshold float64 `json:"threshold"`
}

// readMaintenance refuses the request unless the server runs with HYATLAS_ADMIN=on,
// then decodes the body and reports whether this is a dry run. The gate is there
// because compact_raw cannot be undone and any local process can reach the port.
func (s *Server) readMaintenance(w http.ResponseWriter, r *http.Request) (maintenanceRequest, bool, bool) {
	var body maintenanceRequest
	if !s.admin {
		jsonResponse(w, 403, map[string]any{"error": "maintenance endpoints are off: restart the server with HYATLAS_ADMIN=on"})
		return body, true, false
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		jsonResponse(w, 405, map[string]any{"error": "method not allowed: use POST"})
		return body, true, false
	}
	if r.Body != nil {
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil && err.Error() != "EOF" {
			jsonResponse(w, 400, map[string]any{"error": "bad body"})
			return body, true, false
		}
	}
	dry := body.DryRun == nil || *body.DryRun
	return body, dry, true
}

// handleCompactRaw is POST /api/v1/admin/compact_raw: it rewrites every live raw
// (L2) row with compactTurnText. {"dry_run": false} applies it; otherwise it only
// reports what would change.
func (s *Server) handleCompactRaw(w http.ResponseWriter, r *http.Request) {
	_, dry, ok := s.readMaintenance(w, r)
	if !ok {
		return
	}
	rows, _ := s.store.List(memory.L2Raw, "", "", 1<<30, 0, false)
	updates := map[string]string{}
	before, after := 0, 0
	type sample struct {
		ID        string `json:"id"`
		BeforeLen int    `json:"before_len"`
		AfterLen  int    `json:"after_len"`
		AfterHead string `json:"after_head"`
	}
	var samples []sample
	for _, d := range rows {
		c := compactTurnText(d.Content, compactMessageMax, compactTurnMax)
		if c == d.Content || len(c) >= len(d.Content) {
			continue
		}
		updates[d.ID] = c
		before += len(d.Content)
		after += len(c)
		if len(samples) < 3 {
			samples = append(samples, sample{d.ID, len(d.Content), len(c), utf8Trunc(c, 160)})
		}
	}
	resp := map[string]any{"dry_run": dry, "rows_checked": len(rows), "rows_changed": len(updates),
		"bytes_before": before, "bytes_after": after, "sample": samples}
	if !dry && len(updates) > 0 {
		n, err := s.store.rewriteContent(memory.L2Raw, updates)
		resp["rows_rewritten"] = n
		if err != nil {
			resp["error"] = err.Error()
		}
	}
	jsonResponse(w, 200, resp)
}

// handleDedupeFacts is POST /api/v1/admin/dedupe_facts: for each owner, it walks the
// live L3 facts newest first and supersedes every older fact at least threshold
// similar (default HYATLAS_DEDUPE_SCORE) by the newer one, with its L1 Profile
// mirrors. It is the write-time rule applied to facts stored before it existed.
// {"dry_run": false} applies it.
func (s *Server) handleDedupeFacts(w http.ResponseWriter, r *http.Request) {
	body, dry, ok := s.readMaintenance(w, r)
	if !ok {
		return
	}
	thr := body.Threshold
	if thr <= 0 {
		thr = s.dedupeScore
	}
	if thr <= 0 {
		thr = defaultDedupeScore
	}
	facts, _ := s.store.List(memory.L3Fact, "", "", 1<<30, 0, false)
	type owner struct{ user, agent string }
	byOwner := map[owner][]DocIndex{}
	for _, f := range facts {
		k := owner{f.UserID, f.AgentID}
		byOwner[k] = append(byOwner[k], f)
	}
	type pair struct {
		Old     string  `json:"old"`
		New     string  `json:"new"`
		Score   float64 `json:"score"`
		OldText string  `json:"old_text"`
		NewText string  `json:"new_text"`
	}
	var pairs []pair
	oldDoc := map[string]DocIndex{}
	for k, fs := range byOwner {
		sort.SliceStable(fs, func(i, j int) bool {
			if fs[i].Ts != fs[j].Ts {
				return fs[i].Ts > fs[j].Ts
			}
			return fs[i].ID > fs[j].ID
		})
		gone := map[string]bool{}
		visited := map[string]bool{}
		for _, f := range fs {
			if gone[f.ID] {
				continue
			}
			visited[f.ID] = true
			hits, err := s.store.search(f.Content, 6, memory.L3Fact, k.user, k.agent)
			if err != nil {
				continue
			}
			for _, h := range hits {
				if h.ID == f.ID || gone[h.ID] || visited[h.ID] || float64(h.Score) < thr {
					continue
				}
				if h.Meta["user_id"] != k.user || h.Meta["agent_id"] != k.agent {
					continue
				}
				gone[h.ID] = true
				pairs = append(pairs, pair{h.ID, f.ID, float64(h.Score), utf8Trunc(h.Content, 100), utf8Trunc(f.Content, 100)})
				oldDoc[h.ID] = DocIndex{ID: h.ID, Content: h.Content, Meta: h.Meta}
			}
		}
	}
	sort.Slice(pairs, func(i, j int) bool { return pairs[i].Score > pairs[j].Score })
	resp := map[string]any{"dry_run": dry, "threshold": thr, "owners": len(byOwner),
		"facts": len(facts), "duplicates": len(pairs)}
	sample := pairs
	if len(sample) > 10 {
		sample = sample[:10]
	}
	resp["sample"] = sample
	if !dry && len(pairs) > 0 {
		bySurvivor := map[string][]string{}
		var order []string
		for _, p := range pairs {
			if _, seen := bySurvivor[p.New]; !seen {
				order = append(order, p.New)
			}
			o := oldDoc[p.Old]
			bySurvivor[p.New] = append(bySurvivor[p.New], p.Old)
			bySurvivor[p.New] = append(bySurvivor[p.New], s.store.MirrorsOf(memory.L1Profile, o.Meta["source_id"], o.Content)...)
		}
		marked := 0
		var firstErr error
		for _, survivor := range order {
			n, err := s.store.Supersede(bySurvivor[survivor], survivor)
			marked += n
			if err != nil && firstErr == nil {
				firstErr = err
			}
		}
		resp["rows_superseded"] = marked
		if firstErr != nil {
			resp["error"] = firstErr.Error()
		}
	}
	jsonResponse(w, 200, resp)
}
