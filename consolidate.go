package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"sort"
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

// CitedRelation is an L5 edge plus the fact IDs that evidence it. Unlike the
// per-turn triple, it must be corroborated by more than one memory — that is
// what makes it a System2 product rather than a restatement of one turn.
type CitedRelation struct {
	From     string   `json:"from"`
	Relation string   `json:"relation"`
	To       string   `json:"to"`
	Evidence []string `json:"evidence"`
}

// Consolidation is the JSON contract for one consolidation call.
type Consolidation struct {
	Merges  []Merge  `json:"merges"`
	Drops   []string `json:"drops"`
	Schemas []Schema `json:"schemas"`
	// Knowledge are L5 graph edges. The slow path owns L5: a relation worth
	// keeping is one corroborated across memories, and only this pass can see
	// more than one. Each triple carries the fact IDs that evidence it, so the
	// edge cites real provenance instead of a single turn's guess.
	Knowledge []CitedRelation `json:"knowledge"`
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
	Edges      int      `json:"edges"`
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

	// gate makes a pass single-flight. The ticker and a manual POST /digest can
	// both ask for one, and two passes over the same batch would duplicate the
	// LLM spend and race each other's merges and edge writes.
	gate sync.Mutex

	mu     sync.Mutex
	last   *Report
	lastAt time.Time
	ran    int
}

// errBusy reports that another pass holds the consolidator. Callers decide what
// that means for them: the ticker skips the tick, the digest endpoint says so.
var errBusy = errors.New("a consolidation pass is already running")

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
			if errors.Is(err, errBusy) {
				// A manual pass is in flight; it covers this tick's work.
				continue
			}
			if err != nil {
				log.Printf("consolidate: %v", err)
				continue
			}
			log.Printf("consolidate: %+v", rep)
		}
	}
}

// scopeKey is the owner a consolidation pass works within. Facts from different
// owners are never reasoned over together.
type scopeKey struct{ user, agent string }

func (k scopeKey) String() string { return fmt.Sprintf("user_id=%q agent_id=%q", k.user, k.agent) }

// Once runs a single consolidation pass and reports exactly what it changed.
//
// Live facts are grouped by owner (user_id, agent_id), and each owner with at
// least two facts is consolidated on its own. The model never sees two people's
// memories in one prompt, so no merge, L5 edge, schema or arc can mix them.
func (c *Consolidator) Once(ctx context.Context) (*Report, error) {
	// Single flight: the loser is told so rather than queued, because the work
	// it would have done is covered by the pass already in flight.
	if !c.gate.TryLock() {
		return nil, errBusy
	}
	defer c.gate.Unlock()

	start := time.Now()
	rep := &Report{}
	if c.llm == nil {
		return nil, fmt.Errorf("consolidation needs an LLM; none configured")
	}

	facts := c.listAllLive(memory.L3Fact)
	rep.FactsIn = len(facts)

	// Group under each owner in List's newest-first order, then visit owners in a
	// fixed order so a pass is reproducible.
	byOwner := map[scopeKey][]DocIndex{}
	var owners []scopeKey
	for _, f := range facts {
		k := scopeKey{user: f.UserID, agent: f.AgentID}
		if _, seen := byOwner[k]; !seen {
			owners = append(owners, k)
		}
		byOwner[k] = append(byOwner[k], f)
	}
	sort.Slice(owners, func(i, j int) bool {
		if owners[i].user != owners[j].user {
			return owners[i].user < owners[j].user
		}
		return owners[i].agent < owners[j].agent
	})
	for _, k := range owners {
		scope := byOwner[k]
		// A single fact has nothing to reconcile against, so its owner sits this pass out.
		if len(scope) < 2 {
			continue
		}
		if len(scope) > c.batch {
			scope = scope[:c.batch]
		}
		c.consolidateScope(ctx, k, scope, rep)
	}

	// Raw decay runs once per pass. Its citation guard already reads every
	// owner's edges and facts, so it is not split by scope.
	pruned, protected := c.decayRaw()
	rep.PrunedRaw, rep.Protected = pruned, protected
	return c.finish(rep, start), nil
}

// consolidateScope is one LLM pass over a single owner's facts. It applies the
// merges, drops, L5 edges, schemas and arc the model returned. facts is that
// owner's newest batch, and everything written carries the owner's scope.
//
// Only IDs in facts are acted on. An ID that belongs to another owner, or one the
// model invented, fails the live-set guard and changes nothing.
func (c *Consolidator) consolidateScope(ctx context.Context, owner scopeKey, facts []DocIndex, rep *Report) {
	fail := func(msg string) {
		rep.Errors = append(rep.Errors, owner.String()+": "+msg)
	}
	cons, err := c.ask(ctx, facts)
	if err != nil {
		fail(err.Error())
		return
	}

	live := liveIDs(facts)
	// batch is the immutable membership of what the LLM was shown. Evidence is
	// resolved against this rather than `live`, because merges delete IDs from
	// `live` — two facts merged into one would otherwise stop corroborating the
	// edge that cited them, silently dropping it.
	batch := liveIDs(facts)
	byID := byIDOf(facts)
	now := time.Now().UTC().Format(time.RFC3339)

	// Apply merges first: write the replacement, then supersede what it absorbed.
	// Only IDs in this owner's batch count, so a hallucinated ID changes nothing.
	for _, m := range cons.Merges {
		text := strings.TrimSpace(m.Text)
		if text == "" {
			continue
		}
		absorbed := make([]DocIndex, 0, len(m.Supersedes))
		seen := map[string]bool{}
		for _, id := range m.Supersedes {
			if !live[id] || seen[id] {
				continue
			}
			seen[id] = true
			absorbed = append(absorbed, byID[id])
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
		// The merged fact inherits the conversations it absorbed, so L5
		// corroboration and the raw-decay citation guard still see them.
		srcs := distinctSources(absorbed)
		meta := map[string]string{
			"user_id": owner.user, "agent_id": owner.agent,
			"source_layer_label": label, "ts": now, "consolidated": "true",
		}
		if len(srcs) > 0 {
			meta["source_id"] = srcs[0]
			meta["source_ids"] = strings.Join(srcs, ",")
		}
		mergedID := newID()
		if err := c.store.Add(memory.L3Fact, mergedID, text, meta); err != nil {
			fail("merge add: " + err.Error())
			continue
		}
		ids := make([]string, len(absorbed))
		for i, d := range absorbed {
			ids[i] = d.ID
		}
		n, err := c.store.Supersede(ids, mergedID)
		if err != nil {
			fail("merge supersede: " + err.Error())
		}
		rep.Merged++
		rep.Dropped += n
		for _, id := range ids {
			delete(live, id)
		}
	}

	// Explicit drops: facts the model judged stale or superseded. Same
	// live-set guard, so only facts it was actually shown can be superseded.
	var drops []string
	for _, id := range cons.Drops {
		if live[id] {
			drops = append(drops, id)
			delete(live, id)
		}
	}
	if len(drops) > 0 {
		// An empty mergedID means dropped rather than replaced.
		n, err := c.store.Supersede(drops, "")
		if err != nil {
			fail("drop: " + err.Error())
		}
		rep.Dropped += n
	}

	// L5 knowledge graph. The slow path owns this layer: a relation worth
	// keeping is one corroborated by more than one memory, and only this pass
	// can see more than one. Edges are cited against the L2 raw rows behind the
	// evidence, not the fact rows, so decayRaw's citation guard still protects
	// the conversations the graph depends on.
	for _, rel := range cons.Knowledge {
		if strings.TrimSpace(rel.From) == "" || strings.TrimSpace(rel.Relation) == "" ||
			strings.TrimSpace(rel.To) == "" {
			continue
		}
		// Corroboration is the whole reason this is a System2 product. A triple
		// resting on a single fact is just that fact restated, and the per-turn
		// pass already declined to write it.
		// Corroboration is counted in distinct conversations, not distinct fact
		// rows: two facts extracted from one write share a source_id and are one
		// observation. Legacy rows without a source_id count as their own turn.
		turns := map[string]bool{}
		cites := make([]string, 0, len(rel.Evidence))
		for _, id := range rel.Evidence {
			if !batch[id] {
				continue
			}
			// Trace the fact back to the conversations it came from. A merged fact
			// comes from several, and each one counts as its own turn.
			srcs := sourcesOf(byID[id])
			if len(srcs) == 0 {
				srcs = []string{id}
			}
			for _, src := range srcs {
				if turns[src] {
					continue
				}
				turns[src] = true
				cites = append(cites, src)
			}
		}
		if len(turns) < 2 {
			continue
		}
		for _, src := range cites {
			if err := c.store.Graph().AddEdgeWithSource(rel.From, rel.Relation, rel.To, src); err != nil {
				fail("edge: " + err.Error())
				break
			}
			rep.Edges++
		}
	}

	// Generalised schemas: patterns that only become visible across many turns.
	for _, sc := range cons.Schemas {
		if strings.TrimSpace(sc.Pattern) == "" {
			continue
		}
		if err := c.store.Add(memory.L6Schema, newID(), sc.Pattern, map[string]string{
			"user_id": owner.user, "agent_id": owner.agent,
			"context": sc.Context, "ts": now, "consolidated": "true",
		}); err != nil {
			fail("schema: " + err.Error())
			continue
		}
		rep.Schemas++
	}

	// The cross-session arc: one L4 summary that spans many, which no single
	// turn's extraction could have produced.
	if cons.Arc != nil && strings.TrimSpace(*cons.Arc) != "" {
		if err := c.store.Add(memory.L4Summary, newID(), *cons.Arc, map[string]string{
			"user_id": owner.user, "agent_id": owner.agent,
			"ts": now, "consolidated": "true",
		}); err != nil {
			fail("arc: " + err.Error())
		} else {
			rep.Arc = true
		}
	}
}

// listAllLive returns every live doc in a layer, not one page of it.
func (c *Consolidator) listAllLive(layer memory.Layer) []DocIndex {
	_, total := c.store.List(layer, "", "", 1, 0, false)
	docs, _ := c.store.List(layer, "", "", total, 0, false)
	return docs
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
	// A raw row that a live fact was extracted from is still that fact's
	// provenance, so it is protected too. Superseded facts do not count: they
	// are history, and their raw rows age out with them.
	for _, layer := range []memory.Layer{memory.L1Profile, memory.L3Fact} {
		for _, f := range c.listAllLive(layer) {
			for _, src := range sourcesOf(f) {
				cited[src] = true
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
		// turn names the conversation the fact came from, so the model can tell
		// two facts from one write apart from genuine corroboration.
		turn := f.Meta["source_id"]
		if turn == "" {
			turn = f.ID
		}
		fmt.Fprintf(&b, "- id=%s | turn=%s | %s\n", f.ID, turn, truncate(f.Content, 400))
	}
	system := `You are a memory consolidation engine. Below are durable facts already stored, each with its id and the turn it came from. Reason ACROSS them — not about any single one — and output a JSON object with EXACTLY these keys:
{
  "merges": [{"text": "<one fact that replaces several>", "layer": "user_preferences|project_state|technical_lesson|decision|negative_knowledge", "supersedes": ["<id>", "<id>"]}],
  "drops": ["<id>"],
  "schemas": [{"pattern": "<a recurring pattern only visible across many facts>", "context": "<when it applies>"}],
  "knowledge": [{"from": "<entity>", "relation": "<relation>", "to": "<entity>", "evidence": ["<id>", "<id>"]}],
  "arc": "<1-3 sentences: what these facts say about the user's work over time>"
}
Rules:
- merges: ONLY combine facts that genuinely say the same thing or contradict each other. For a contradiction, keep the newer statement and supersede the older. supersedes MUST list at least 2 ids. Use only ids from the input.
- drops: ids of facts that are stale, trivially obvious, or fully absorbed by a merge. Be conservative — deleting memory is irreversible.
- schemas: 0-3 patterns that generalise beyond the individual facts.
- knowledge: 0-5 entity-relation-entity edges, each corroborated in "evidence" by fact ids from AT LEAST 2 different turns. A triple resting on one turn is just that turn restated — do not emit it. Use only ids from the input.
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

// byIDOf indexes the batch so evidence lookups do not rescan it.
func byIDOf(facts []DocIndex) map[string]DocIndex {
	m := make(map[string]DocIndex, len(facts))
	for _, f := range facts {
		m[f.ID] = f
	}
	return m
}

func liveIDs(facts []DocIndex) map[string]bool {
	m := make(map[string]bool, len(facts))
	for _, f := range facts {
		m[f.ID] = true
	}
	return m
}

// sourcesOf lists the L2 conversations behind a fact. A per-turn fact carries
// one in source_id. A consolidated fact carries every absorbed source in
// source_ids. Legacy rows that recorded neither return nil.
func sourcesOf(d DocIndex) []string {
	if all := d.Meta["source_ids"]; all != "" {
		return strings.Split(all, ",")
	}
	if one := d.Meta["source_id"]; one != "" {
		return []string{one}
	}
	return nil
}

// distinctSources is the union of the conversations behind several facts, in
// first-seen order.
func distinctSources(facts []DocIndex) []string {
	var out []string
	seen := map[string]bool{}
	for _, f := range facts {
		for _, s := range sourcesOf(f) {
			if s != "" && !seen[s] {
				seen[s] = true
				out = append(out, s)
			}
		}
	}
	return out
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
