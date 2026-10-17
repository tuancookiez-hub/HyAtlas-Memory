package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Limits on the candidates shown to the reconcile model. Each new fact is compared
// with at most reconcilePerFact stored facts, and the whole write sees at most
// reconcileTotal, so one long extraction cannot make the prompt grow without bound.
const (
	reconcileMinScore = 0.70
	reconcilePerFact  = 8
	reconcileTotal    = 20
)

// reconcileSystemPrompt tells the model when to merge, when to retire and when to
// leave a fact alone. The rules follow Hunyuan's write path: a repeat or a fuller
// statement is an UPDATE, and a fact that is no longer true is a SUPERSEDE.
const reconcileSystemPrompt = `You keep a memory store free of repeats and of facts that are no longer true.
You are given NEW facts, just extracted from a conversation, and CANDIDATES, facts already stored for the same user.
For each new fact choose one operation:
- ADD: the new fact is new information, or only related to a candidate. Make no op for it.
- UPDATE: the new fact says the same thing as a candidate with more detail, or repeats a candidate with a count. Give the merged text, which keeps everything both said, in "text", and the candidate ids in "targets".
- SUPERSEDE: the new fact shows that a candidate is no longer true, such as a changed job, address or plan. Give the candidate ids in "targets" and a short "reason".
Use only the ids shown. Never invent an id. Two facts that merely share a topic are ADD.
Reply with JSON only, in this form:
{"ops": [{"op": "ADD|UPDATE|SUPERSEDE", "fact": "<new fact id>", "targets": ["<candidate id>"], "text": "<UPDATE only>", "reason": "<SUPERSEDE only>"}]}
Reply {"ops": []} when nothing applies.`

// reconcileOp is one decision from the reconcile model. Fact is the id of the new
// fact it is about. Targets are stored fact ids that were shown as candidates.
type reconcileOp struct {
	Op      string   `json:"op"`      // ADD | UPDATE | SUPERSEDE
	Fact    string   `json:"fact"`    // the new fact id this op is about
	Targets []string `json:"targets"` // stored fact ids (UPDATE, SUPERSEDE)
	Text    string   `json:"text"`    // UPDATE only: the merged text
	Reason  string   `json:"reason"`  // SUPERSEDE only
}

// reconcileReply is the model's JSON answer. Ops are decoded through reconcileOpWire,
// so a bare string or a comma-separated string as targets does not reject the reply.
type reconcileReply struct {
	Ops []reconcileOpWire `json:"ops"`
}

// reconcileOpWire is reconcileOp as the model may send it. Targets uses idList,
// which accepts the odd shapes small models produce for a list of ids.
type reconcileOpWire struct {
	Op      string `json:"op"`
	Fact    string `json:"fact"`
	Targets idList `json:"targets"`
	Text    string `json:"text"`
	Reason  string `json:"reason"`
}

// reconcileEnabled reports whether the second reconcile call is switched on. It is
// off by default because it costs one extra LLM call per write. It suits a local
// model and is too costly on a hosted free tier.
func reconcileEnabled() bool {
	return envOn("HYATLAS_RECONCILE")
}

// reconcileCandidates returns the live L3 facts of the owners that are at least
// reconcileMinScore similar to f, most similar first, at most reconcilePerFact of
// them. f itself is never a candidate. The search runs per owner, so a person known
// by several user IDs is compared across all of them.
func (s *MemoryStore) reconcileCandidates(f DocIndex, users []string, agentID string) []DocIndex {
	qv, err := s.embed.Embed(s.ctx, f.Content)
	if err != nil {
		log.Printf("reconcile: embed fact %s: %v", f.ID, err)
		return nil
	}
	var hits []SearchHit
	seen := map[string]bool{f.ID: true}
	for _, u := range users {
		if u == "" {
			continue
		}
		where := map[string]string{"user_id": u}
		if agentID != "" {
			where["agent_id"] = agentID
		}
		found, err := s.searchWhere(qv, reconcilePerFact, memory.L3Fact, where)
		if err != nil {
			log.Printf("reconcile: search for fact %s: %v", f.ID, err)
			continue
		}
		for _, h := range found {
			if seen[h.ID] || isExperience(h) || float64(h.Score) < reconcileMinScore {
				continue
			}
			seen[h.ID] = true
			hits = append(hits, h)
		}
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].Score > hits[j].Score })
	if len(hits) > reconcilePerFact {
		hits = hits[:reconcilePerFact]
	}
	ids := make([]string, 0, len(hits))
	for _, h := range hits {
		ids = append(ids, h.ID)
	}
	return s.GetMany(ids)
}

// reconcile compares the facts just stored by one write with the owner's similar
// stored facts, in one LLM call, and applies the model's UPDATE and SUPERSEDE ops.
// newFacts are the ids of the facts the write stored. It returns how many ops were
// applied. The facts are already stored when this runs, so a failure is logged and
// returned, and it never undoes the write. No candidates means no LLM call.
func (s *MemoryStore) reconcile(ctx context.Context, llm *LLMClient, newFacts []string, userID, agentID string) (int, error) {
	if len(newFacts) == 0 {
		return 0, nil
	}
	news := s.GetMany(newFacts)
	newSet := make(map[string]bool, len(news))
	for _, f := range news {
		newSet[f.ID] = true
	}

	// Collect candidates for all new facts, once each, up to the write's total.
	var cands []DocIndex
	candSet := map[string]bool{}
	for _, f := range news {
		if len(cands) >= reconcileTotal {
			break
		}
		for _, c := range s.reconcileCandidates(f, []string{userID}, agentID) {
			if len(cands) >= reconcileTotal {
				break
			}
			if newSet[c.ID] || candSet[c.ID] {
				continue
			}
			candSet[c.ID] = true
			cands = append(cands, c)
		}
	}
	if len(news) == 0 || len(cands) == 0 {
		return 0, nil
	}

	reply, err := llm.chat(ctx, reconcileMessages(news, cands), 0)
	if err != nil {
		err = fmt.Errorf("reconcile: llm: %w", err)
		log.Printf("%v", err)
		return 0, err
	}
	ops, err := parseReconcile(reply)
	if err != nil {
		err = fmt.Errorf("reconcile: %w", err)
		log.Printf("%v", err)
		return 0, err
	}
	return s.applyReconcile(ops, newSet, candSet, userID, agentID), nil
}

// reconcileMessages builds the chat for one reconcile call. It lists the new facts
// as "id | text" and the candidates as "id | date | text", the same line style the
// extraction prompt uses.
func reconcileMessages(news, cands []DocIndex) []map[string]string {
	var b strings.Builder
	b.WriteString("NEW facts (id | text):\n")
	for _, f := range news {
		fmt.Fprintf(&b, "%s | %s\n", f.ID, oneLine(f.Content))
	}
	b.WriteString("\nCANDIDATES (id | date | text):\n")
	for _, c := range cands {
		fmt.Fprintf(&b, "%s | %s | %s\n", c.ID, dateOf(c.Ts), oneLine(c.Content))
	}
	return []map[string]string{
		{"role": "system", "content": reconcileSystemPrompt},
		{"role": "user", "content": b.String()},
	}
}

// parseReconcile decodes the model's reply into its ops. It tolerates code fences
// and text around the object, as parseExtraction does.
func parseReconcile(content string) ([]reconcileOp, error) {
	content = trimFences(content)
	var rep reconcileReply
	if err := json.Unmarshal([]byte(content), &rep); err != nil {
		start := strings.Index(content, "{")
		end := strings.LastIndex(content, "}")
		if start == -1 || end == -1 || end <= start {
			return nil, fmt.Errorf("parse reply: %w (raw %.80s)", err, content)
		}
		if err := json.Unmarshal([]byte(content[start:end+1]), &rep); err != nil {
			return nil, fmt.Errorf("parse reply: %w", err)
		}
	}
	out := make([]reconcileOp, 0, len(rep.Ops))
	for _, w := range rep.Ops {
		out = append(out, reconcileOp{Op: w.Op, Fact: w.Fact, Targets: w.Targets,
			Text: w.Text, Reason: w.Reason})
	}
	return out, nil
}

// applyReconcile applies ops that name only shown ids. An id the model invented is
// dropped, and so is an op left with nothing to act on. An id is used by at most one
// op, so a second op cannot retire a row the first one already retired. It returns
// how many ops were applied.
func (s *MemoryStore) applyReconcile(ops []reconcileOp, newSet, candSet map[string]bool, userID, agentID string) int {
	used := map[string]bool{}
	applied := 0
	for _, op := range ops {
		fact := strings.TrimSpace(op.Fact)
		if !newSet[fact] {
			continue
		}
		targets := keepShown(op.Targets, candSet, used)
		switch strings.ToUpper(strings.TrimSpace(op.Op)) {
		case "UPDATE":
			text := strings.TrimSpace(op.Text)
			if text == "" || len(targets) == 0 || used[fact] {
				continue
			}
			if s.applyUpdate(fact, targets, text, userID, agentID) {
				used[fact] = true
				for _, t := range targets {
					used[t] = true
				}
				applied++
			}
		case "SUPERSEDE":
			if len(targets) == 0 {
				continue
			}
			reason := strings.TrimSpace(op.Reason)
			if reason == "" {
				reason = reasonNotRecorded
			}
			if _, err := s.supersedeWith(targets, fact, map[string]string{
				metaSupersededHow: howReplaced, "drop_reason": reason,
			}); err != nil {
				log.Printf("reconcile: supersede %v by %s: %v", targets, fact, err)
				continue
			}
			for _, t := range targets {
				used[t] = true
			}
			applied++
		}
		// ADD, and any op the model made up, changes nothing.
	}
	return applied
}

// keepShown returns the ids that were shown as candidates and are not already used,
// without repeats, in the order the model gave them.
func keepShown(ids []string, shown, used map[string]bool) []string {
	var out []string
	seen := map[string]bool{}
	for _, id := range ids {
		id = strings.TrimSpace(id)
		if !shown[id] || used[id] || seen[id] {
			continue
		}
		seen[id] = true
		out = append(out, id)
	}
	return out
}

// applyUpdate writes the merged text as one new fact and retires the new fact and
// its targets as updated. The merged fact takes the label of the new fact, and the
// event date of the newest row it involves. It reports whether the update was made.
func (s *MemoryStore) applyUpdate(fact string, targets []string, text, userID, agentID string) bool {
	involved := append([]string{fact}, targets...)
	var newest, newFact DocIndex
	var newestAt time.Time
	for _, d := range s.GetMany(involved) {
		if d.ID == fact {
			newFact = d
		}
		at, err := time.Parse(time.RFC3339, d.Ts)
		if err != nil {
			at = time.Time{}
		}
		if newest.ID == "" || at.After(newestAt) {
			newest, newestAt = d, at
		}
	}
	meta := map[string]string{
		"user_id": userID, "agent_id": agentID,
		"ts": time.Now().UTC().Format(time.RFC3339),
	}
	if label := newFact.Meta["source_layer_label"]; label != "" {
		meta["source_layer_label"] = label
	}
	if when := newest.Meta[metaEventAt]; when != "" {
		meta[metaEventAt] = when
	}
	mergedID := newID()
	if err := s.Add(memory.L3Fact, mergedID, text, meta); err != nil {
		log.Printf("reconcile: add merged fact: %v", err)
		return false
	}
	if _, err := s.supersedeWith(involved, mergedID, map[string]string{
		metaSupersededHow: howUpdated,
	}); err != nil {
		log.Printf("reconcile: supersede %v by %s: %v", involved, mergedID, err)
		return false
	}
	return true
}
