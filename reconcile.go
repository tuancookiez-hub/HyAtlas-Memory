package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sort"
	"strconv"
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

// Metadata keys a recurring event writes on a row: the count and the dates of the
// event. Links use metaLinks (graphrecall.go).
const (
	metaSeriesCount = "series_count"
	metaSeriesDates = "series_dates"
)

// Limits on the links of a row and the dates of a series. A row keeps its newest
// linksMax links, and a series keeps its newest seriesMaxDates dates.
const (
	linksMax       = 8
	seriesMaxDates = 20
)

// reconcileSystemPrompt tells the model when to merge, when to retire, when to link and
// when to leave a fact alone. The rules follow Hunyuan's write path: a repeat or a fuller
// statement is an UPDATE, and a fact that is no longer true is a SUPERSEDE.
const reconcileSystemPrompt = `You keep a memory store free of repeats and of facts that are no longer true.
You are given NEW facts, just extracted from a conversation, and CANDIDATES, facts already stored for the same user.
For each new fact choose one operation:
- ADD: the new fact is new information, or only related to a candidate. Make no op for it.
- UPDATE: the new fact says the same thing as a candidate with more detail, or repeats a candidate with a count. Give the merged text, which keeps everything both said, in "text", and the candidate ids in "targets". If the merged fact is the same kind of event happening again, also give "count", how many times it has happened so far, and "dates", its dates as YYYY-MM-DD.
- SUPERSEDE: the new fact shows that a candidate is no longer true, such as a changed job, address or plan. Give the candidate ids in "targets" and a short "reason".
- LINK: the new fact and a candidate are connected, but neither is retired. Give the candidate ids in "targets" and one "relation": "related" when both stay true about the same subject, "contrasts" when they differ in a way worth seeing together, such as a preference in two contexts, or "same_series" when they are the same kind of event at different times.
Use only the ids shown. Never invent an id. Two facts that merely share a topic are ADD, unless one of the LINK relations fits.
Reply with JSON only, in this form:
{"ops": [{"op": "ADD|UPDATE|SUPERSEDE|LINK", "fact": "<new fact id>", "targets": ["<candidate id>"], "text": "<UPDATE only>", "count": "<UPDATE only, optional>", "dates": ["<UPDATE only, optional>"], "reason": "<SUPERSEDE only>", "relation": "<LINK only>"}]}
Reply {"ops": []} when nothing applies.`

// reconcileOp is one decision from the reconcile model. Fact is the id of the new
// fact it is about. Targets are stored fact ids that were shown as candidates. Targets
// uses idList, which accepts the odd shapes small models produce for a list of ids.
// Count and Dates are kept raw, because a small model may send them in any shape, and
// they are read tolerantly by seriesCount and seriesDates.
type reconcileOp struct {
	Op       string          `json:"op"`       // ADD | UPDATE | SUPERSEDE | LINK
	Fact     string          `json:"fact"`     // the new fact id this op is about
	Targets  idList          `json:"targets"`  // stored fact ids (UPDATE, SUPERSEDE, LINK)
	Text     string          `json:"text"`     // UPDATE only: the merged text
	Reason   string          `json:"reason"`   // SUPERSEDE only
	Relation string          `json:"relation"` // LINK only: related | contrasts | same_series
	Count    json.RawMessage `json:"count"`    // UPDATE only: how many times a recurring event has happened
	Dates    json.RawMessage `json:"dates"`    // UPDATE only: the event's dates
}

// reconcileReply is the model's JSON answer.
type reconcileReply struct {
	Ops []reconcileOp `json:"ops"`
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
	// The fact was embedded when it was stored; reading that vector back saves a
	// second embedding. Only a fact whose vector cannot be read is embedded here.
	qv := s.vectors([]string{f.ID})[f.ID]
	if len(qv) == 0 {
		var err error
		if qv, err = s.embed.Embed(s.ctx, f.Content); err != nil {
			log.Printf("reconcile: embed fact %s: %v", f.ID, err)
			return nil
		}
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
		found, err := s.searchWhereOpts(qv, reconcilePerFact, memory.L3Fact, where,
			searchOpts{excludeLabel: experienceLabel})
		if err != nil {
			log.Printf("reconcile: search for fact %s: %v", f.ID, err)
			continue
		}
		for _, h := range found {
			if seen[h.ID] || float64(h.Score) < reconcileMinScore {
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

// reconcileOwners compares the facts just stored by one write with the similar stored
// facts of users, in one LLM call, and applies the model's UPDATE and SUPERSEDE ops.
// users are every user ID of the write's owner (the write's own ID plus its aliases),
// so a fact is compared with the same person's facts under any of their IDs. When
// users is empty it is treated as []string{userID}. userID stays the owner of a merged
// UPDATE fact. newFacts are the ids of the facts the write stored. It returns how many
// ops were applied. The facts are already stored when this runs, so a failure is
// logged and returned, and it never undoes the write. No candidates means no LLM call.
func (s *MemoryStore) reconcileOwners(ctx context.Context, llm *LLMClient, newFacts []string, users []string, userID, agentID string) (int, error) {
	if len(newFacts) == 0 {
		return 0, nil
	}
	if len(users) == 0 {
		users = []string{userID}
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
		for _, c := range s.reconcileCandidates(f, users, agentID) {
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
	var rep reconcileReply
	if err := decodeLenient(content, &rep); err != nil {
		return nil, fmt.Errorf("parse reply: %w", err)
	}
	if rep.Ops == nil {
		return []reconcileOp{}, nil
	}
	return rep.Ops, nil
}

// applyReconcile applies ops that name only shown ids. An id the model invented is
// dropped, and so is an op left with nothing to act on. An id is used by at most one
// op, so a second op cannot retire a row the first one already retired. A SUPERSEDE
// is skipped when its new fact was already merged and retired by an earlier UPDATE,
// so no op runs through a retired fact. After a SUPERSEDE, its new fact is marked used,
// so a later UPDATE cannot retire the fact that now stands in for the targets. A LINK
// is skipped when its new fact or any of its targets is used by an earlier op, and it
// never marks anything used, because it retires nothing. It returns how many ops were
// applied.
func (s *MemoryStore) applyReconcile(ops []reconcileOp, newSet, candSet map[string]bool, userID, agentID string) int {
	used := map[string]bool{}
	applied := 0
	for _, op := range ops {
		fact := strings.TrimSpace(op.Fact)
		if !newSet[fact] {
			continue
		}
		targets := shown(op.Targets, func(id string) bool { return candSet[id] && !used[id] })
		switch strings.ToUpper(strings.TrimSpace(op.Op)) {
		case "LINK":
			relation := strings.ToLower(strings.TrimSpace(op.Relation))
			all := shown(op.Targets, func(id string) bool { return candSet[id] })
			if !validRelation(relation) || len(all) == 0 || used[fact] || anyUsed(all, used) {
				continue
			}
			if s.applyLink(fact, all, relation) {
				applied++
			}
		case "UPDATE":
			text := strings.TrimSpace(op.Text)
			if text == "" || len(targets) == 0 || used[fact] {
				continue
			}
			if s.applyUpdate(fact, targets, text, userID, agentID, hintOf(op)) {
				used[fact] = true
				for _, t := range targets {
					used[t] = true
				}
				applied++
			}
		case "SUPERSEDE":
			if len(targets) == 0 || used[fact] {
				continue
			}
			reason := strings.TrimSpace(op.Reason)
			if reason == "" {
				reason = reasonNotRecorded
			}
			if _, err := s.retire(targets, Retire{By: fact, How: howReplaced, Reason: reason}); err != nil {
				log.Printf("reconcile: supersede %v by %s: %v", targets, fact, err)
				continue
			}
			used[fact] = true
			for _, t := range targets {
				used[t] = true
			}
			applied++
		}
		// ADD, and any op the model made up, changes nothing.
	}
	return applied
}

// applyUpdate writes the merged text as one new fact and retires the new fact and
// its targets as updated. The merged fact takes the label of the new fact, and the
// event date of the newest row it involves. When hint is not nil the merged fact also
// carries the series of a recurring event, see mergeSeries. It reports whether the
// update was made.
func (s *MemoryStore) applyUpdate(fact string, targets []string, text, userID, agentID string, hint *seriesHint) bool {
	involved := append([]string{fact}, targets...)
	var newest, newFact DocIndex
	var newestAt time.Time
	var targetRows []DocIndex
	for _, d := range s.GetMany(involved) {
		if d.ID == fact {
			newFact = d
		} else {
			targetRows = append(targetRows, d)
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
	if hint != nil {
		for k, v := range mergeSeries(*hint, targetRows, newFact) {
			meta[k] = v
		}
	}
	mergedID := newID()
	if err := s.Add(memory.L3Fact, mergedID, text, meta); err != nil {
		log.Printf("reconcile: add merged fact: %v", err)
		return false
	}
	if _, err := s.retire(involved, Retire{By: mergedID, How: howUpdated}); err != nil {
		log.Printf("reconcile: supersede %v by %s: %v", involved, mergedID, err)
		return false
	}
	return true
}

// validRelation reports whether relation is one a LINK op may name: related, contrasts
// or same_series. Any other relation makes the op a no-op.
func validRelation(relation string) bool {
	switch relation {
	case "related", "contrasts", "same_series":
		return true
	}
	return false
}

// anyUsed reports whether any id in ids is marked in used.
func anyUsed(ids []string, used map[string]bool) bool {
	for _, id := range ids {
		if used[id] {
			return true
		}
	}
	return false
}

// applyLink writes a link between the new fact and each target, on both rows, with
// the given relation. It retires nothing. It reports whether any row's links changed.
func (s *MemoryStore) applyLink(fact string, targets []string, relation string) bool {
	written := false
	for _, t := range targets {
		if s.addLink(fact, t+":"+relation) {
			written = true
		}
		if s.addLink(t, fact+":"+relation) {
			written = true
		}
	}
	return written
}

// addLink appends entry, an "id:relation" pair, to the links of the live row id. An
// entry the row already has is not added again, and when the row would hold more than
// linksMax links the oldest are dropped. It reports whether the row changed.
func (s *MemoryStore) addLink(id, entry string) bool {
	rows := s.GetMany([]string{id})
	if len(rows) == 0 {
		return false
	}
	links := splitLinks(rows[0].Meta[metaLinks])
	for _, l := range links {
		if l == entry {
			return false
		}
	}
	links = append(links, entry)
	if len(links) > linksMax {
		links = links[len(links)-linksMax:]
	}
	if err := s.updateMeta(id, map[string]string{metaLinks: strings.Join(links, ",")}); err != nil {
		log.Printf("reconcile: link %s to %s: %v", id, entry, err)
		return false
	}
	return true
}

// splitLinks splits the stored links of a row into its raw "id:relation" entries, in
// the order they were written, and drops empty ones. Unlike parseLinks (graphrecall.go)
// it keeps entries as written, so a rewrite never changes links it does not add.
func splitLinks(v string) []string {
	var out []string
	for _, l := range strings.Split(v, ",") {
		if l = strings.TrimSpace(l); l != "" {
			out = append(out, l)
		}
	}
	return out
}

// seriesHint is the series an UPDATE gave for a recurring event: the count of its
// occurrences and the dates it named. Either part may be zero or empty.
type seriesHint struct {
	count int
	dates []string
}

// hintOf returns the series an UPDATE op gave, or nil when it gave no usable count and
// no usable date. Garbage in count or dates is ignored, so it never fails the reply.
func hintOf(op reconcileOp) *seriesHint {
	h := seriesHint{count: seriesCount(op.Count), dates: seriesDates(op.Dates)}
	if h.count == 0 && len(h.dates) == 0 {
		return nil
	}
	return &h
}

// seriesCount reads the count of a series, which a small model may send as a number,
// a float or a numeric string. It returns 0 for anything else.
func seriesCount(raw json.RawMessage) int {
	var n float64
	if err := json.Unmarshal(raw, &n); err == nil {
		if n > 0 && n < 1e6 {
			return int(n)
		}
		return 0
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		if f, err := strconv.ParseFloat(strings.TrimSpace(s), 64); err == nil && f > 0 && f < 1e6 {
			return int(f)
		}
	}
	return 0
}

// seriesDates reads the dates of a series, which a small model may send as one string
// of dates or as a list. Each date kept is a full YYYY-MM-DD day, and anything else is
// dropped.
func seriesDates(raw json.RawMessage) []string {
	var parts []string
	var one string
	var many []any
	if err := json.Unmarshal(raw, &one); err == nil {
		parts = strings.FieldsFunc(one, func(r rune) bool { return r == ',' || r == ';' || r == ' ' })
	} else if err := json.Unmarshal(raw, &many); err == nil {
		for _, v := range many {
			if s, ok := v.(string); ok {
				parts = append(parts, s)
			}
		}
	}
	var out []string
	for _, p := range parts {
		if day := fullDay(p); day != "" {
			out = append(out, day)
		}
	}
	return out
}

// fullDay returns the YYYY-MM-DD day of an event date, or "" when s is not a full day.
// A YYYY-MM date or a timeless value gives "".
func fullDay(s string) string {
	if day := normalizeEventDate(s); len(day) == 10 {
		return day
	}
	return ""
}

// mergeSeries returns the series meta of a merged fact. The count is the largest of the
// hint's count, one more than the largest series count of a target, and 2. The dates are
// the sorted, de-duplicated union of the targets' series dates and event dates, the new
// fact's event date and the hint's dates, and the newest seriesMaxDates of them are kept.
func mergeSeries(h seriesHint, targets []DocIndex, fact DocIndex) map[string]string {
	count := h.count
	if count < 2 {
		count = 2
	}
	dates := map[string]bool{}
	for _, d := range targets {
		if c, err := strconv.Atoi(d.Meta[metaSeriesCount]); err == nil && c+1 > count {
			count = c + 1
		}
		for _, day := range strings.Split(d.Meta[metaSeriesDates], ",") {
			if day = fullDay(day); day != "" {
				dates[day] = true
			}
		}
		if day := fullDay(d.Meta[metaEventAt]); day != "" {
			dates[day] = true
		}
	}
	if day := fullDay(fact.Meta[metaEventAt]); day != "" {
		dates[day] = true
	}
	for _, day := range h.dates {
		dates[day] = true
	}
	list := make([]string, 0, len(dates))
	for day := range dates {
		list = append(list, day)
	}
	sort.Strings(list)
	if len(list) > seriesMaxDates {
		list = list[len(list)-seriesMaxDates:]
	}
	return map[string]string{
		metaSeriesCount: strconv.Itoa(count),
		metaSeriesDates: strings.Join(list, ","),
	}
}
