package main

import (
	"context"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

const (
	// crossSchemaEveryKey is the environment variable that sets how often the
	// cross-topic pass may run for one owner. 0 or off disables the pass.
	crossSchemaEveryKey = "HYATLAS_CROSS_SCHEMA_EVERY"

	// defaultCrossSchemaEvery is the least time between two cross-topic passes for one
	// owner. One extra call a day is enough: schemas change slowly.
	defaultCrossSchemaEvery = 24 * time.Hour

	// crossSchemaMin is how many live schemas an owner needs before the pass is worth
	// a call; fewer cannot hold a pattern that spans two others.
	crossSchemaMin = 4

	// crossSchemaMaxShown caps how many of an owner's schemas the pass shows the
	// model, newest first, so the prompt cannot grow without bound.
	crossSchemaMaxShown = 40

	// crossSchemaMaxNew caps how many patterns one pass may write for an owner.
	crossSchemaMaxNew = 3
)

// crossPattern is one pattern the cross-topic pass proposes: its text and the ids of
// the schemas it generalises.
type crossPattern struct {
	Pattern  string `json:"pattern"`
	Evidence idList `json:"evidence"`
}

// crossReply is the model's answer to the cross-topic prompt.
type crossReply struct {
	Patterns []crossPattern `json:"patterns"`
}

// schemaTopics maps each fact ID of the owner's clusters to its cluster ID, so a
// schema can be placed in the topic its evidence facts come from.
func (c *Consolidator) schemaTopics(owner scopeKey) map[string]string {
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	out := map[string]string{}
	if oc := c.clusters[owner.String()]; oc != nil {
		for _, cs := range oc.Clusters {
			for _, id := range cs.FactIDs {
				out[id] = cs.ID
			}
		}
	}
	return out
}

// schemaTopic is the cluster key a schema belongs to: the cluster holding most of
// its evidence facts (the lowest cluster ID on a tie), or "" when none of its
// evidence is in a cluster, which means the topic is unknown.
func schemaTopic(d DocIndex, topics map[string]string) string {
	counts := map[string]int{}
	for _, id := range strings.Split(d.Meta["evidence"], ",") {
		if t := topics[strings.TrimSpace(id)]; t != "" {
			counts[t]++
		}
	}
	best, bestN := "", 0
	for t, n := range counts {
		if n > bestN || (n == bestN && t < best) {
			best, bestN = t, n
		}
	}
	return best
}

// crossSchemaDue reports whether the owner's cross-topic pass may run now: the pass
// is on and the last run was at least crossEvery ago (or never).
func (c *Consolidator) crossSchemaDue(owner scopeKey) bool {
	if c.crossEvery <= 0 {
		return false
	}
	c.stateMu.Lock()
	last := c.crossRan[owner.String()]
	c.stateMu.Unlock()
	at, err := time.Parse(time.RFC3339, last)
	return err != nil || time.Since(at) >= c.crossEvery
}

// markCrossRan records that the owner's cross-topic pass ran now and saves the state,
// so a restart does not run it again early.
func (c *Consolidator) markCrossRan(owner scopeKey, rep *Report) {
	c.stateMu.Lock()
	if c.crossRan == nil {
		c.crossRan = map[string]string{}
	}
	c.crossRan[owner.String()] = time.Now().UTC().Format(time.RFC3339)
	err := c.saveState()
	c.stateMu.Unlock()
	if err != nil {
		rep.Errors = append(rep.Errors, "state: "+err.Error())
	}
}

// crossSchemaPass runs the cross-topic pass for each owner that is due: it shows the
// model the owner's live schemas and writes the patterns that hold across two or more
// of them, as new L6 schemas. It never retires or edits an existing schema. It costs
// one LLM call per owner at most once per crossEvery, and only for an owner with at
// least crossSchemaMin schemas from at least two topics. Failures are soft: they are
// reported in rep.Errors and change nothing.
func (c *Consolidator) crossSchemaPass(ctx context.Context, owners []scopeKey, rep *Report) {
	if c.crossEvery <= 0 || c.llm == nil {
		return
	}
	for _, owner := range owners {
		if ctx.Err() != nil {
			return
		}
		if !c.crossSchemaDue(owner) {
			continue
		}
		candidates, ok := c.crossCandidates(owner)
		if !ok {
			continue
		}
		n, err := c.crossSchemaOwner(ctx, owner, candidates)
		if ctx.Err() != nil {
			return
		}
		// An attempt counts even when it failed, so a broken model costs one call a
		// day for an owner, not one on every pass.
		c.markCrossRan(owner, rep)
		if err != nil {
			rep.Errors = append(rep.Errors, owner.String()+": cross-topic: "+err.Error())
		}
		rep.CrossSchemas += n
		log.Printf("consolidate: cross-topic pass for %s wrote %d schema(s)", owner, n)
	}
}

// crossCandidates returns the schemas to show for an owner, newest first and at most
// crossSchemaMaxShown, and whether they are enough: at least crossSchemaMin from at
// least two known topics. Schemas this pass wrote earlier (cross_topic) are left out,
// so it does not generalise its own output.
func (c *Consolidator) crossCandidates(owner scopeKey) ([]DocIndex, bool) {
	var schemas []DocIndex
	for _, d := range c.liveOf(memory.L6Schema, owner) {
		if d.Meta["cross_topic"] != "true" {
			schemas = append(schemas, d)
		}
	}
	if len(schemas) < crossSchemaMin {
		return nil, false
	}
	sortNewestFirst(schemas)
	if len(schemas) > crossSchemaMaxShown {
		schemas = schemas[:crossSchemaMaxShown]
	}
	topics := c.schemaTopics(owner)
	seen := map[string]bool{}
	for _, d := range schemas {
		if t := schemaTopic(d, topics); t != "" {
			seen[t] = true
		}
	}
	return schemas, len(seen) >= 2
}

// crossSchemaOwner makes the one call for an owner and writes what it returns. It
// returns how many schemas were written.
func (c *Consolidator) crossSchemaOwner(ctx context.Context, owner scopeKey, schemas []DocIndex) (int, error) {
	var b strings.Builder
	for _, d := range schemas {
		fmt.Fprintf(&b, "- %s | %s\n", d.ID, utf8Trunc(oneLine(d.Content), 300))
	}
	system := fmt.Sprintf(`You look for patterns that cross topics. Below are one person's stored schemas (schema_id | pattern), each found inside a single topic. Return at most %d NEW patterns that hold across two or more of them, from different topics, and are not just one of them reworded. Each pattern is one full sentence that says what it is and when it applies.

Return ONLY valid JSON, no prose, no markdown fences:
{"patterns": [{"pattern": "<the cross-topic pattern>", "evidence": ["<schema_id>", "<schema_id>"]}]}
"evidence" lists the ids of the schemas below that the pattern generalises, at least two. Return {"patterns": []} if there is none.

Schemas:
%s`, crossSchemaMaxNew, b.String())
	content, err := c.llm.chat(ctx, []map[string]string{
		{"role": "system", "content": system},
		{"role": "user", "content": "Find the cross-topic patterns."},
	}, 0.1)
	if err != nil {
		return 0, err
	}
	var reply crossReply
	if err := decodeLenient(content, &reply); err != nil {
		return 0, fmt.Errorf("reply not understood: %w", err)
	}

	byID := byIDOf(schemas)
	topics := c.schemaTopics(owner)
	now := time.Now().UTC().Format(time.RFC3339)

	// Writes are serialised with the per-cluster schema writes, and the owner's
	// schemas are read again under the lock for the duplicate check.
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	known := map[string]bool{}
	for _, d := range c.liveOf(memory.L6Schema, owner) {
		known[normalizeText(d.Content)] = true
	}
	written := 0
	for _, p := range reply.Patterns {
		if written >= crossSchemaMaxNew {
			break
		}
		pattern := strings.TrimSpace(p.Pattern)
		key := normalizeText(pattern)
		if pattern == "" || known[key] {
			continue
		}
		cited := shown(p.Evidence, func(id string) bool { _, ok := byID[id]; return ok })
		// A pattern needs two schemas from two different topics to be cross-topic.
		cites := map[string]bool{}
		for _, id := range cited {
			if t := schemaTopic(byID[id], topics); t != "" {
				cites[t] = true
			}
		}
		if len(cited) < 2 || len(cites) < 2 {
			continue
		}
		known[key] = true
		dup, vec := c.schemaKnown(owner, pattern, nil)
		if dup {
			continue
		}
		meta := map[string]string{
			"user_id": owner.user, "agent_id": owner.agent,
			"ts": now, "consolidated": "true", "cross_topic": "true",
			"generalises": strings.Join(cited, ","),
		}
		// Search finds a schema through the facts it cites, so the evidence is the
		// facts behind the schemas this pattern generalises.
		var facts []string
		seen := map[string]bool{}
		for _, id := range cited {
			for _, f := range strings.Split(byID[id].Meta["evidence"], ",") {
				if f = strings.TrimSpace(f); f != "" && !seen[f] {
					seen[f] = true
					facts = append(facts, f)
				}
			}
		}
		sort.Strings(facts)
		if len(facts) >= 2 {
			meta["evidence"] = strings.Join(facts, ",")
		}
		if err := c.store.addWithVector(memory.L6Schema, newID(), pattern, meta, vec); err != nil {
			return written, fmt.Errorf("write: %w", err)
		}
		written++
	}
	return written, nil
}
