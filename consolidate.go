package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

const (
	// defaultConsolidate is how often ultra runs the slow path. Long enough that
	// the batch is worth an LLM call, short enough that contradiction cleanup is
	// not days behind the conversation that caused it.
	defaultConsolidate = 6 * time.Hour

	// consolidateTimeout bounds one pass. Longer than a single extraction
	// because it reasons over a whole batch of facts.
	consolidateTimeout = 10 * time.Minute

	// defaultBatch caps facts per consolidation call so the prompt cannot grow
	// without bound as memory accumulates.
	defaultBatch = 200
)

// Consolidation is the slow path. Extraction reasons about ONE turn; this
// reasons ACROSS many turns and many sessions, which is the only way a memory
// system can notice that two facts contradict each other, that a preference
// repeated eleven times is a pattern rather than an event, or that the last
// three weeks of work form a single arc.
//
// It is what separates ultra from pro: same per-write extraction, plus this.

// Merge is one fact that replaces several. Supersedes holds the memory IDs the
// consolidated text absorbs, so the originals can be pruned rather than left to
// compete with their own replacement at retrieval time.
type Merge struct {
	Text       string   `json:"text"`
	Layer      string   `json:"layer,omitempty"`
	Supersedes []string `json:"supersedes"`
}

// Consolidation is the JSON contract for one consolidation call.
type Consolidation struct {
	Merges  []Merge  `json:"merges"`
	Drops   []string `json:"drops"`
	Schemas []Schema `json:"schemas"`
	// Arc is the cross-session narrative: a longer summary synthesised from
	// many L4 summaries rather than from any single turn.
	Arc *string `json:"arc,omitempty"`
}

// Report is what one consolidation pass actually did. Returned by the endpoint
// and logged by the worker, so "the slow path ran" is checkable rather than
// assumed — the previous digest handler returned a hardcoded success.
type Report struct {
	FactsIn    int      `json:"facts_in"`
	Merged     int      `json:"merged"`
	Dropped    int      `json:"dropped"`
	Schemas    int      `json:"schemas"`
	Arc        bool     `json:"arc"`
	PrunedRaw  int      `json:"pruned_raw"`
	Protected  int      `json:"protected_raw"`
	DurationMs int64    `json:"duration_ms"`
	Errors     []string `json:"errors,omitempty"`
}

// Consolidator owns the slow path.
type Consolidator struct {
	store *MemoryStore
	llm   *LLMClient
	every time.Duration

	// retention is how long an unreferenced L2 raw memory survives before the
	// decay pass removes it. Zero means never delete raw history.
	retention time.Duration

	// batch caps how many facts one consolidation call sees, so the prompt
	// cannot grow without bound as memory accumulates.
	batch int

	mu     sync.Mutex
	last   *Report
	lastAt time.Time
	ran    int
}

// NewConsolidator wires the slow path. retention <= 0 disables raw decay.
func NewConsolidator(store *MemoryStore, llm *LLMClient, every, retention time.Duration, batch int) *Consolidator {
	if batch <= 0 {
		batch = 200
	}
	return &Consolidator{store: store, llm: llm, every: every, retention: retention, batch: batch}
}

// Stats is the /api/v1/status view of the slow path.
func (c *Consolidator) Stats() (runs int, last *Report, at time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ran, c.last, c.lastAt
}

// Run drives the ticker until ctx is cancelled. Called as a goroutine only in
// ultra mode; pro and lite never start it, so they make no background calls.
func (c *Consolidator) Run(ctx context.Context) {
	if c.every <= 0 {
		return
	}
	t := time.NewTicker(c.every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			// A bounded context so one stuck call cannot wedge every later tick.
			run, cancel := context.WithTimeout(ctx, consolidateTimeout)
			rep, err := c.Once(run)
			cancel()
			if err != nil {
				log.Printf("consolidate: %v", err)
				continue
			}
			log.Printf("consolidate: %+v", rep)
		}
	}
}

// Once runs a single consolidation pass and reports exactly what it changed.
func (c *Consolidator) Once(ctx context.Context) (*Report, error) {
	start := time.Now()
	rep := &Report{}
	if c.llm == nil {
		return nil, fmt.Errorf("consolidation needs an LLM; none configured")
	}

	facts, total := c.store.List(memory.L3Fact, "", "", c.batch, 0, false)
	rep.FactsIn = total
	if len(facts) < 2 {
		// Nothing to reconcile. Report the pass as run, with zero changes, so
		// status distinguishes "ran and found nothing" from "never ran".
		return c.finish(rep, start), nil
	}

	cons, err := c.ask(ctx, facts)
	if err != nil {
		rep.Errors = append(rep.Errors, err.Error())
		return c.finish(rep, start), nil
	}

	live := liveIDs(facts)
	now := time.Now().UTC().Format(time.RFC3339)

	// Apply merges first: write the replacement, then prune what it absorbed.
	// Pruning only IDs that were actually in the input set means a hallucinated
	// ID in the model's reply cannot delete anything.
	for _, m := range cons.Merges {
		text := strings.TrimSpace(m.Text)
		if text == "" {
			continue
		}
		owner, agent := "", ""
		absorbed := make([]string, 0, len(m.Supersedes))
		for _, id := range m.Supersedes {
			if !live[id] {
				continue
			}
			absorbed = append(absorbed, id)
			if owner == "" {
				owner, agent = ownerOf(facts, id)
			}
		}
		if len(absorbed) < 2 {
			// A "merge" of one fact is a rewrite, not a consolidation. Dropping
			// it avoids losing the original's provenance for no gain.
			continue
		}
		label := m.Layer
		if label == "" {
			label = "consolidated"
		}
		if err := c.store.Add(memory.L3Fact, newID(), text, map[string]string{
			"user_id": owner, "agent_id": agent,
			"source_layer_label": label, "ts": now, "consolidated": "true",
		}); err != nil {
			rep.Errors = append(rep.Errors, "merge add: "+err.Error())
			continue
		}
		n, err := c.store.Delete(absorbed, "", "", "")
		if err != nil {
			rep.Errors = append(rep.Errors, "merge prune: "+err.Error())
			continue
		}
		rep.Merged++
		rep.Dropped += n
		for _, id := range absorbed {
			delete(live, id)
		}
	}

	// Explicit drops: facts the model judged stale or superseded. Same
	// live-set guard, so only facts it was actually shown can be removed.
	if len(cons.Drops) > 0 {
		ids := make([]string, 0, len(cons.Drops))
		for _, id := range cons.Drops {
			if live[id] {
				ids = append(ids, id)
				delete(live, id)
			}
		}
		if len(ids) > 0 {
			n, err := c.store.Delete(ids, "", "", "")
			if err != nil {
				rep.Errors = append(rep.Errors, "drop: "+err.Error())
			} else {
				rep.Dropped += n
			}
		}
	}

	// Generalised schemas: patterns that only become visible across many turns.
	for _, sc := range cons.Schemas {
		if strings.TrimSpace(sc.Pattern) == "" {
			continue
		}
		owner, agent := dominantOwner(facts)
		if err := c.store.Add(memory.L6Schema, newID(), sc.Pattern, map[string]string{
			"user_id": owner, "agent_id": agent,
			"context": sc.Context, "ts": now, "consolidated": "true",
		}); err != nil {
			rep.Errors = append(rep.Errors, "schema: "+err.Error())
			continue
		}
		rep.Schemas++
	}

	// The cross-session arc: one L4 summary that spans many, which no single
	// turn's extraction could have produced.
	if cons.Arc != nil && strings.TrimSpace(*cons.Arc) != "" {
		owner, agent := dominantOwner(facts)
		if err := c.store.Add(memory.L4Summary, newID(), *cons.Arc, map[string]string{
			"user_id": owner, "agent_id": agent,
			"ts": now, "consolidated": "true",
		}); err != nil {
			rep.Errors = append(rep.Errors, "arc: "+err.Error())
		} else {
			rep.Arc = true
		}
	}

	pruned, protected := c.decayRaw()
	rep.PrunedRaw, rep.Protected = pruned, protected
	return c.finish(rep, start), nil
}

func (c *Consolidator) finish(rep *Report, start time.Time) *Report {
	rep.DurationMs = time.Since(start).Milliseconds()
	c.mu.Lock()
	c.last, c.lastAt, c.ran = rep, time.Now().UTC(), c.ran+1
	c.mu.Unlock()
	return rep
}

// decayRaw removes old L2 raw memories, but never one that a live L5 edge
// cites. Every graph edge stores its source memory ID as evidence, so deleting
// a cited row would leave the knowledge graph pointing at a memory that no
// longer exists — a dangling citation is worse than the disk space it saves.
func (c *Consolidator) decayRaw() (pruned, protected int) {
	if c.retention <= 0 {
		return 0, 0
	}
	cutoff := time.Now().UTC().Add(-c.retention)

	cited := map[string]bool{}
	if _, edges := c.store.Graph().Snapshot(0); len(edges) > 0 {
		for _, e := range edges {
			if e.Source != "" {
				cited[e.Source] = true
			}
		}
	}

	// List's limit is a page size, not "all": 0 returns an empty slice rather
	// than every row. Read the total first, then fetch that many, so the decay
	// pass actually sees the history it is supposed to age out.
	_, total := c.store.List(memory.L2Raw, "", "", 1, 0, false)
	if total == 0 {
		return 0, 0
	}
	raw, _ := c.store.List(memory.L2Raw, "", "", total, 0, false)
	old := make([]string, 0, 64)
	for _, d := range raw {
		ts, err := time.Parse(time.RFC3339, d.Ts)
		if err != nil || ts.After(cutoff) {
			continue
		}
		if cited[d.ID] {
			protected++
			continue
		}
		old = append(old, d.ID)
	}
	if len(old) == 0 {
		return 0, protected
	}
	n, err := c.store.Delete(old, "", "", "")
	if err != nil {
		return 0, protected
	}
	return n, protected
}

// ask sends the current fact set to the LLM and parses a Consolidation.
func (c *Consolidator) ask(ctx context.Context, facts []DocIndex) (*Consolidation, error) {
	var b strings.Builder
	for _, f := range facts {
		fmt.Fprintf(&b, "- id=%s | %s\n", f.ID, truncate(f.Content, 400))
	}
	system := `You are a memory consolidation engine. Below are durable facts already stored, each with its id. Reason ACROSS them — not about any single one — and output a JSON object with EXACTLY these keys:
{
  "merges": [{"text": "<one fact that replaces several>", "layer": "user_preferences|project_state|technical_lesson|decision|negative_knowledge", "supersedes": ["<id>", "<id>"]}],
  "drops": ["<id>"],
  "schemas": [{"pattern": "<a recurring pattern only visible across many facts>", "context": "<when it applies>"}],
  "arc": "<1-3 sentences: what these facts say about the user's work over time>"
}
Rules:
- merges: ONLY combine facts that genuinely say the same thing or contradict each other. For a contradiction, keep the newer statement and supersede the older. supersedes MUST list at least 2 ids. Use only ids from the input.
- drops: ids of facts that are stale, trivially obvious, or fully absorbed by a merge. Be conservative — deleting memory is irreversible.
- schemas: 0-3 patterns that generalise beyond the individual facts.
- arc: null if the facts are too few or too unrelated to synthesise.
- Never invent an id. Never reference a fact not listed.
Return ONLY valid JSON, no prose, no markdown fences.

Facts:
` + b.String()

	messages := []map[string]string{
		{"role": "system", "content": system},
		{"role": "user", "content": "Consolidate the facts above."},
	}
	content, err := c.llm.chat(ctx, messages, 0.1)
	if err != nil {
		return nil, err
	}
	return parseConsolidation(content)
}

// parseConsolidation tolerantly extracts the JSON object, mirroring the
// extraction parser: small models wrap objects in prose or fences.
func parseConsolidation(content string) (*Consolidation, error) {
	content = trimFences(content)
	var cons Consolidation
	if err := json.Unmarshal([]byte(content), &cons); err == nil {
		return &cons, nil
	}
	start, end := strings.Index(content, "{"), strings.LastIndex(content, "}")
	if start == -1 || end <= start {
		return nil, fmt.Errorf("consolidation parse failed (raw %.80s)", content)
	}
	if err := json.Unmarshal([]byte(content[start:end+1]), &cons); err != nil {
		return nil, err
	}
	return &cons, nil
}

func liveIDs(facts []DocIndex) map[string]bool {
	m := make(map[string]bool, len(facts))
	for _, f := range facts {
		m[f.ID] = true
	}
	return m
}

func ownerOf(facts []DocIndex, id string) (user, agent string) {
	for _, f := range facts {
		if f.ID == id {
			return f.UserID, f.AgentID
		}
	}
	return "", ""
}

// dominantOwner scopes consolidated output to whoever owns most of the input,
// so a merge cannot land in the wrong user's retrieval scope.
func dominantOwner(facts []DocIndex) (user, agent string) {
	uc, ac := map[string]int{}, map[string]int{}
	for _, f := range facts {
		uc[f.UserID]++
		ac[f.AgentID]++
	}
	return top(uc), top(ac)
}

func top(m map[string]int) string {
	best, n := "", -1
	for k, v := range m {
		if v > n {
			best, n = k, v
		}
	}
	return best
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// parseDuration reads an env var as a duration, returning def when unset or
// invalid. A typo must not silently disable the slow path.
func parseDuration(key string, def time.Duration) time.Duration {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	if n, err := strconv.Atoi(v); err == nil {
		return time.Duration(n) * time.Second
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		log.Printf("%s=%q is not a duration; using %s", key, v, def)
		return def
	}
	return d
}
