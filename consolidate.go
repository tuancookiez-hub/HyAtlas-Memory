package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
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

	// maxPromptSchemas caps how many of an owner's existing schemas are shown to
	// the model, so the refinement list cannot grow the prompt without bound.
	maxPromptSchemas = 20

	// consolidateStateFile holds the per-owner watermarks and the rotation cursor.
	// It sits next to doc_index.json, in the data dir.
	consolidateStateFile = "consolidate_state.json"
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

// ConsolidatedSchema is a schema the slow path proposes. Supersedes names the
// owner's existing schemas that this one refines; they are retired once it is written.
type ConsolidatedSchema struct {
	Pattern    string   `json:"pattern"`
	Context    string   `json:"context,omitempty"`
	Supersedes []string `json:"supersedes,omitempty"`
}

// Consolidation is the JSON contract for one consolidation call.
type Consolidation struct {
	Merges  []Merge              `json:"merges"`
	Drops   []string             `json:"drops"`
	Schemas []ConsolidatedSchema `json:"schemas"`
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
	FactsIn int `json:"facts_in"`
	// OwnersRun counts owners that were sent to the model this pass.
	OwnersRun int `json:"owners_run"`
	// OwnersUnchanged counts owners whose facts had not changed since their last
	// successful pass, so they were not sent again.
	OwnersUnchanged int `json:"owners_unchanged"`
	// SkippedOwners names owners that still had work but were not reached because
	// the pass's context ended first. The next pass starts from them.
	SkippedOwners []string `json:"skipped_owners,omitempty"`
	Merged        int      `json:"merged"`
	// Edges counts distinct L5 edges this pass created. Re-citing an edge that
	// already exists adds evidence to it and is not counted.
	Edges      int      `json:"edges"`
	Dropped    int      `json:"dropped"`
	Schemas    int      `json:"schemas"`
	Arc        bool     `json:"arc"`
	PrunedRaw  int      `json:"pruned_raw"`
	Protected  int      `json:"protected_raw"`
	DurationMs int64    `json:"duration_ms"`
	Errors     []string `json:"errors,omitempty"`
}

// consolidateState is what survives a restart: which owners are already
// consolidated, at which facts fingerprint, and where the next pass starts.
type consolidateState struct {
	Cursor int               `json:"cursor"`
	Owners map[string]string `json:"owners"`
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

	// done maps an owner (scopeKey.String) to the fingerprint of its live facts
	// after its last successful pass. cursor is where the next pass starts in
	// the owner order. Both are read and written only under gate, and are
	// persisted to statePath.
	done      map[string]string
	cursor    int
	statePath string

	mu     sync.Mutex
	last   *Report
	lastAt time.Time
	ran    int
}

// errBusy reports that another pass holds the consolidator. Callers decide what
// that means for them: the ticker skips the tick, the digest endpoint says so.
var errBusy = errors.New("a consolidation pass is already running")

// NewConsolidator wires the slow path. retention <= 0 disables raw decay. The
// per-owner watermarks are loaded from the data dir, so a restart does not
// re-run owners whose facts have not changed.
func NewConsolidator(store *MemoryStore, llm *LLMClient, every, retention time.Duration, batch int) *Consolidator {
	if batch <= 0 {
		batch = 200
	}
	c := &Consolidator{store: store, llm: llm, every: every, retention: retention, batch: batch,
		done: map[string]string{}}
	if store != nil && store.indexPath != "" {
		c.statePath = filepath.Join(filepath.Dir(store.indexPath), consolidateStateFile)
	}
	c.loadState()
	return c
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
//
// Owners are visited from a cursor that moves between passes, so a pass cut
// short by its deadline does not always stop at the same owners. An owner whose
// facts have not changed since its last successful pass is not sent again.
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

	byOwner, owners := groupByOwner(facts)
	// A single fact has nothing to reconcile against, so its owner sits this pass out.
	var work []scopeKey
	for _, k := range owners {
		if len(byOwner[k]) >= 2 {
			work = append(work, k)
		}
	}

	n := len(work)
	first := 0
	if n > 0 {
		first = c.cursor % n
	}
	next := first + 1 // a pass that reaches every owner starts one owner later next time
	ran := map[scopeKey]bool{}
	for i := 0; i < n; i++ {
		k := work[(first+i)%n]
		if err := ctx.Err(); err != nil {
			next = first + i
			var skipped []string
			for j := i; j < n; j++ {
				sk := work[(first+j)%n]
				if c.done[sk.String()] != factsFingerprint(byOwner[sk]) {
					skipped = append(skipped, sk.String())
				}
			}
			if len(skipped) > 0 {
				rep.SkippedOwners = skipped
				rep.Errors = append(rep.Errors, fmt.Sprintf(
					"pass stopped (%v) before %d owner(s) were consolidated; they run next pass", err, len(skipped)))
			}
			break
		}
		fp := factsFingerprint(byOwner[k])
		if c.done[k.String()] == fp {
			rep.OwnersUnchanged++
			continue
		}
		scope := byOwner[k]
		if len(scope) > c.batch {
			scope = scope[:c.batch]
		}
		rep.OwnersRun++
		if c.consolidateScope(ctx, k, scope, rep) {
			ran[k] = true
		}
	}
	if n > 0 {
		c.cursor = next % n
	}

	// Record each owner that completed, at the fingerprint its facts have now,
	// which includes this pass's own merges. Owners that failed keep their old
	// watermark, so they are retried next pass.
	if len(ran) > 0 {
		post, _ := groupByOwner(c.listAllLive(memory.L3Fact))
		for k := range ran {
			c.done[k.String()] = factsFingerprint(post[k])
		}
	}
	present := map[string]bool{}
	for _, k := range owners {
		present[k.String()] = true
	}
	for key := range c.done {
		if !present[key] {
			delete(c.done, key)
		}
	}
	if err := c.saveState(); err != nil {
		rep.Errors = append(rep.Errors, "state: "+err.Error())
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
// model invented, fails the live-set guard and changes nothing. It reports
// whether the model answered, which is what makes the owner's watermark advance.
func (c *Consolidator) consolidateScope(ctx context.Context, owner scopeKey, facts []DocIndex, rep *Report) bool {
	fail := func(msg string) {
		rep.Errors = append(rep.Errors, owner.String()+": "+msg)
	}
	schemas := c.liveOf(memory.L6Schema, owner)
	cons, err := c.ask(ctx, facts, schemas)
	if err != nil {
		fail(err.Error())
		return false
	}

	live := liveIDs(facts)
	// batch is the immutable membership of what the LLM was shown. Evidence is
	// resolved against this rather than `live`, because merges delete IDs from
	// `live` — two facts merged into one would otherwise stop corroborating the
	// edge that cited them, silently dropping it.
	batch := liveIDs(facts)
	byID := byIDOf(facts)
	now := time.Now().UTC().Format(time.RFC3339)

	// Apply merges first. Only IDs in this owner's batch count, so a hallucinated
	// ID changes nothing.
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
		c.applyMerge(owner, text, label, absorbed, now, live, rep, fail)
	}

	// Explicit drops: facts the model judged stale or superseded. Same
	// live-set guard, so only facts it was actually shown can be superseded.
	var drops []string
	dropped := map[string]bool{}
	for _, id := range cons.Drops {
		if live[id] && !dropped[id] {
			dropped[id] = true
			drops = append(drops, id)
		}
	}
	if len(drops) > 0 {
		// An empty mergedID means dropped rather than replaced.
		_, err := c.store.Supersede(drops, "")
		marked := c.supersededOf(drops)
		for _, id := range marked {
			delete(live, id)
		}
		rep.Dropped += len(marked)
		if err := c.retireMirrors(c.store.GetMany(marked), ""); err != nil {
			fail("drop mirror: " + err.Error())
		}
		if err != nil {
			fail("drop: " + err.Error())
		}
	}

	// L5 knowledge graph. The slow path owns this layer: a relation worth
	// keeping is one corroborated by more than one memory, and only this pass
	// can see more than one. Edges are cited against the L2 raw rows behind the
	// evidence, not the fact rows, so decayRaw's citation guard still protects
	// the conversations the graph depends on. Edges belong to this owner, so the
	// same entity name under another owner stays a separate node.
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
		var cites []string
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
		// One call per relation, carrying every corroborating conversation, so the
		// edge keeps all of its citations and is counted once.
		created, err := c.store.Graph().AddEdgeWithSources(owner.user, owner.agent, rel.From, rel.Relation, rel.To, cites)
		if err != nil {
			fail("edge: " + err.Error())
			continue
		}
		if created {
			rep.Edges++
		}
	}

	// Generalised schemas: patterns that only become visible across many turns.
	// A pattern whose normalised text matches a live schema of this owner is
	// already known and is not written again. A schema may refine existing ones,
	// which it then supersedes.
	known := map[string]bool{}
	existing := map[string]bool{}
	for _, sc := range schemas {
		known[normalizeText(sc.Content)] = true
		existing[sc.ID] = true
	}
	for _, sc := range cons.Schemas {
		pattern := strings.TrimSpace(sc.Pattern)
		key := normalizeText(pattern)
		if pattern == "" || known[key] {
			continue
		}
		known[key] = true
		id := newID()
		if err := c.store.Add(memory.L6Schema, id, pattern, map[string]string{
			"user_id": owner.user, "agent_id": owner.agent,
			"context": sc.Context, "ts": now, "consolidated": "true",
		}); err != nil {
			fail("schema: " + err.Error())
			continue
		}
		rep.Schemas++
		var retire []string
		for _, old := range sc.Supersedes {
			if existing[old] && old != id && !containsID(retire, old) {
				retire = append(retire, old)
			}
		}
		if len(retire) > 0 {
			if _, err := c.store.Supersede(retire, id); err != nil {
				fail("schema supersede: " + err.Error())
			}
		}
	}

	// The cross-session arc: one L4 summary that spans many, which no single
	// turn's extraction could have produced. A new arc replaces this owner's
	// previous consolidation arc, so arcs do not accumulate one per pass.
	if cons.Arc != nil && strings.TrimSpace(*cons.Arc) != "" {
		var prevArcs []string
		for _, d := range c.liveOf(memory.L4Summary, owner) {
			if d.Meta["kind"] == "arc" {
				prevArcs = append(prevArcs, d.ID)
			}
		}
		arcID := newID()
		if err := c.store.Add(memory.L4Summary, arcID, strings.TrimSpace(*cons.Arc), map[string]string{
			"user_id": owner.user, "agent_id": owner.agent,
			"ts": now, "consolidated": "true", "kind": "arc",
		}); err != nil {
			fail("arc: " + err.Error())
		} else {
			rep.Arc = true
			if len(prevArcs) > 0 {
				if _, err := c.store.Supersede(prevArcs, arcID); err != nil {
					fail("arc supersede: " + err.Error())
				}
			}
		}
	}
	return true
}

// applyMerge writes one consolidated fact and retires the facts it absorbed.
//
// The replacement is written before anything is superseded, so a failed write
// touches no original. Supersede marks the originals as replaced by the new ID.
// Only the originals the store reports as marked are counted and removed from
// live. If some were not marked, the replacement is retracted, so no live
// original is left beside it.
func (c *Consolidator) applyMerge(owner scopeKey, text, label string, absorbed []DocIndex, now string,
	live map[string]bool, rep *Report, fail func(string)) {
	srcs := distinctSources(absorbed)
	// The merged fact inherits the conversations it absorbed, so L5
	// corroboration and the raw-decay citation guard still see them.
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
		return
	}
	ids := make([]string, len(absorbed))
	for i, d := range absorbed {
		ids[i] = d.ID
	}
	_, supErr := c.store.Supersede(ids, mergedID)

	// Count what the store marked, not what was asked for.
	marked := c.supersededOf(ids)
	for _, id := range marked {
		delete(live, id)
	}
	rep.Dropped += len(marked)
	if len(marked) < len(ids) {
		_, retractErr := c.store.Supersede([]string{mergedID}, "")
		msg := fmt.Sprintf("merge supersede: %d of %d absorbed facts marked; replacement retracted", len(marked), len(ids))
		if retractErr != nil {
			msg += ": retract: " + retractErr.Error()
		}
		fail(msg)
		return
	}
	if supErr != nil {
		fail("merge supersede: " + supErr.Error())
	}
	rep.Merged++

	// The L1 profile mirror of a superseded preference is superseded with it, so
	// the profile does not keep an old phrasing. A preference that survives the
	// merge is mirrored again under the new text.
	if err := c.retireMirrors(c.store.GetMany(marked), mergedID); err != nil {
		fail("merge mirror: " + err.Error())
	}
	if label == "user_preferences" && len(srcs) > 0 {
		if err := c.store.Add(memory.L1Profile, newID(), text, map[string]string{
			"user_id": owner.user, "agent_id": owner.agent,
			"source_id": srcs[0], "ts": now,
		}); err != nil {
			fail("merge mirror add: " + err.Error())
		}
	}
}

// retireMirrors supersedes the L1 Profile rows that mirror superseded
// user_preferences facts. promoteExtraction writes such a fact to L1 with the
// same source_id and content, which is how the mirror is found.
func (c *Consolidator) retireMirrors(docs []DocIndex, by string) error {
	var ids []string
	seen := map[string]bool{}
	for _, d := range docs {
		if d.Meta["source_layer_label"] != "user_preferences" {
			continue
		}
		for _, id := range c.store.MirrorsOf(memory.L1Profile, d.Meta["source_id"], d.Content) {
			if !seen[id] {
				seen[id] = true
				ids = append(ids, id)
			}
		}
	}
	if len(ids) == 0 {
		return nil
	}
	_, err := c.store.Supersede(ids, by)
	return err
}

// supersededOf returns those of ids that the store now reports as superseded.
func (c *Consolidator) supersededOf(ids []string) []string {
	var out []string
	for _, d := range c.store.GetMany(ids) {
		if isSuperseded(d) {
			out = append(out, d.ID)
		}
	}
	return out
}

// liveOf returns the live docs in layer that belong to exactly this owner, newest
// first. List's filters cannot be used: an empty user or agent means "any" there,
// which would pull in other owners.
func (c *Consolidator) liveOf(layer memory.Layer, owner scopeKey) []DocIndex {
	var out []DocIndex
	for _, d := range c.listAllLive(layer) {
		if d.UserID == owner.user && d.AgentID == owner.agent {
			out = append(out, d)
		}
	}
	return out
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

// loadState restores the watermarks. A missing or unreadable file starts clean:
// the cost is one extra pass over each owner, not lost data.
func (c *Consolidator) loadState() {
	if c.statePath == "" {
		return
	}
	b, err := os.ReadFile(c.statePath)
	if err != nil || len(b) == 0 {
		return
	}
	var st consolidateState
	if err := json.Unmarshal(b, &st); err != nil {
		log.Printf("consolidate: ignoring unreadable %s: %v", c.statePath, err)
		return
	}
	if st.Owners != nil {
		c.done = st.Owners
	}
	c.cursor = st.Cursor
}

// saveState persists the watermarks with a write-then-rename, as the doc index does.
func (c *Consolidator) saveState() error {
	if c.statePath == "" {
		return nil
	}
	b, err := json.MarshalIndent(consolidateState{Cursor: c.cursor, Owners: c.done}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(c.statePath), 0o755); err != nil {
		return err
	}
	tmp := c.statePath + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, c.statePath)
}

// decayRaw removes old L2 raw memories, but never one that a live L5 edge
// cites. Every graph edge stores its source memory IDs as evidence, so deleting
// a cited row would leave the knowledge graph pointing at a memory that no
// longer exists — a dangling citation is worse than the disk space it saves.
func (c *Consolidator) decayRaw() (pruned, protected int) {
	if c.retention <= 0 {
		return 0, 0
	}
	cutoff := time.Now().UTC().Add(-c.retention)

	cited := map[string]bool{}
	// Every citation of every live edge is protected, not only the first.
	if _, edges := c.store.Graph().Snapshot(0); len(edges) > 0 {
		for _, e := range edges {
			if e.Source != "" {
				cited[e.Source] = true
			}
			for _, src := range e.Sources {
				cited[src] = true
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

// ask sends the current fact set, and the owner's existing schemas, to the LLM
// and parses a Consolidation.
func (c *Consolidator) ask(ctx context.Context, facts, schemas []DocIndex) (*Consolidation, error) {
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
	var existing strings.Builder
	if len(schemas) > 0 {
		existing.WriteString("\nSchemas already stored for these facts (schema_id | pattern). Do not repeat them:\n")
		for i, sc := range schemas {
			if i >= maxPromptSchemas {
				break
			}
			fmt.Fprintf(&existing, "- schema_id=%s | %s\n", sc.ID, truncate(sc.Content, 200))
		}
	}
	system := `You are a memory consolidation engine. Below are durable facts already stored, each with its id and the turn it came from. Reason ACROSS them — not about any single one — and output a JSON object with EXACTLY these keys:
{
  "merges": [{"text": "<one fact that replaces several>", "layer": "user_preferences|project_state|technical_lesson|decision|negative_knowledge", "supersedes": ["<id>", "<id>"]}],
  "drops": ["<id>"],
  "schemas": [{"pattern": "<a recurring pattern only visible across many facts>", "context": "<when it applies>", "supersedes": ["<schema_id>"]}],
  "knowledge": [{"from": "<entity>", "relation": "<relation>", "to": "<entity>", "evidence": ["<id>", "<id>"]}],
  "arc": "<1-3 sentences: what these facts say about the user's work over time>"
}
Rules:
- merges: ONLY combine facts that genuinely say the same thing or contradict each other. For a contradiction, keep the newer statement and supersede the older. supersedes MUST list at least 2 ids. Use only ids from the input.
- drops: ids of facts that are stale, trivially obvious, or fully absorbed by a merge. Be conservative — deleting memory is irreversible.
- schemas: 0-3 patterns that generalise beyond the individual facts. Do not repeat a schema already stored (listed below, if any). To refine a stored schema, return the refined pattern and list the schema_id it replaces in "supersedes"; otherwise leave "supersedes" empty.
- knowledge: 0-5 entity-relation-entity edges, each corroborated in "evidence" by fact ids from AT LEAST 2 different turns. A triple resting on one turn is just that turn restated — do not emit it. Use only ids from the input.
- arc: null if the facts are too few or too unrelated to synthesise.
- Never invent an id. Never reference a fact not listed.
Return ONLY valid JSON, no prose, no markdown fences.

Facts:
` + b.String() + existing.String()

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

// groupByOwner splits facts by owner. The owners come back in a fixed sorted
// order, so the pass order is reproducible before the cursor rotates it.
func groupByOwner(facts []DocIndex) (map[scopeKey][]DocIndex, []scopeKey) {
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
	return byOwner, owners
}

// factsFingerprint identifies an owner's live facts: how many there are and the
// newest timestamp. A new fact, a merge or a drop changes one of the two.
func factsFingerprint(facts []DocIndex) string {
	newest := ""
	for _, f := range facts {
		if f.Ts > newest {
			newest = f.Ts
		}
	}
	return strconv.Itoa(len(facts)) + "@" + newest
}

// normalizeText is the comparison key for schemas: case, runs of whitespace and
// the punctuation at either end do not make a different schema.
func normalizeText(s string) string {
	s = strings.ToLower(strings.Join(strings.Fields(s), " "))
	return strings.Trim(s, " .,;:!?\"'`*-()[]")
}

func containsID(ids []string, id string) bool {
	for _, v := range ids {
		if v == id {
			return true
		}
	}
	return false
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
