package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
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
	// without bound as memory accumulates. 50, not 200: a free reasoning model took
	// 4 to 10+ minutes on 200 facts and often missed consolidateTimeout, while 50
	// came back in about two and a half. A larger owner is cut into more clusters.
	defaultBatch = 50

	// maxPromptSchemas caps how many of an owner's existing schemas are shown to
	// the model, so the refinement list cannot grow the prompt without bound.
	maxPromptSchemas = 20

	// maxPromptPageNames caps how many entity page names are offered to the model as
	// canonical spellings for knowledge endpoints.
	maxPromptPageNames = 40

	// consolidateStateFile holds the per-owner clusters and the rotation cursor.
	// It sits next to doc_index.json, in the data dir.
	consolidateStateFile = "consolidate_state.json"

	// defaultCatchup is how soon the next pass starts when one left due clusters
	// unreached (HYATLAS_CONSOLIDATE_CATCHUP).
	defaultCatchup = 20 * time.Minute

	// defaultParallel and maxParallel bound how many clusters one pass sends to the
	// model at once (HYATLAS_CONSOLIDATE_PARALLEL). A free model rate-limits hard, so
	// the ceiling is low.
	defaultParallel = 2
	maxParallel     = 4
)

// minCallBudget is the floor of callBudget: the least time that must remain before
// a pass's deadline for a new call to start, before any call time has been measured.
// A variable so tests can shorten it.
var minCallBudget = 3 * time.Minute

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

// Drop is one fact the model judged stale or trivially obvious, with its reason. A
// drop without a reason is not applied: the reason is what makes a drop auditable,
// and it is stored on the dropped row as drop_reason.
type Drop struct {
	ID     string `json:"id"`
	Reason string `json:"reason"`
}

// UnmarshalJSON accepts a drop object or, from a model that ignores the format, a
// bare id string, which carries no reason and so is not applied.
func (d *Drop) UnmarshalJSON(b []byte) error {
	var id string
	if err := json.Unmarshal(b, &id); err == nil {
		*d = Drop{ID: id}
		return nil
	}
	type plain Drop
	return json.Unmarshal(b, (*plain)(d))
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
	Drops   []Drop               `json:"drops"`
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
	// OwnersRun counts owners that had at least one cluster sent to the model this pass.
	OwnersRun int `json:"owners_run"`
	// OwnersUnchanged counts owners with no due cluster: nothing had changed since
	// their clusters last ran, so nothing was sent.
	OwnersUnchanged int `json:"owners_unchanged"`
	// ClustersRun counts clusters whose call finished this pass, successful or not.
	ClustersRun int `json:"clusters_run"`
	// ClustersPending counts due clusters the pass did not reach, because its context
	// or time budget ran out. Run schedules the next pass early while this is above zero.
	ClustersPending int `json:"clusters_pending"`
	// SkippedOwners names owners that still had a due cluster that was not reached.
	// The next pass starts from them.
	SkippedOwners []string `json:"skipped_owners,omitempty"`
	Merged        int      `json:"merged"`
	// Edges counts distinct L5 edges this pass created. Re-citing an edge that
	// already exists adds evidence to it and is not counted.
	Edges int `json:"edges"`
	// Absorbed counts facts a merge folded into its new fact. They are superseded,
	// not lost: their content lives on in the merged fact.
	Absorbed int `json:"absorbed"`
	// Dropped counts facts the model dropped as stale or obvious, each with a
	// reason. It used to include Absorbed, which made merges look like data loss.
	Dropped    int      `json:"dropped"`
	Schemas    int      `json:"schemas"`
	Arc        bool     `json:"arc"`
	PrunedRaw  int      `json:"pruned_raw"`
	Protected  int      `json:"protected_raw"`
	DurationMs int64    `json:"duration_ms"`
	Errors     []string `json:"errors,omitempty"`
}

// consolidateState is what survives a restart: the stable topic clusters of each
// owner (keyed by scopeKey.String()) and the rotation cursor. A state file from
// before clusters were stored has only the old watermark keys, which are ignored;
// its owners simply get their clusters built on the next pass.
type consolidateState struct {
	Cursor   int                       `json:"cursor"`
	Clusters map[string]*ownerClusters `json:"clusters,omitempty"`
}

// ownerClusters is one owner's stored clustering. Built is when the clusters were
// last cut from scratch (RFC 3339), which HYATLAS_CONSOLIDATE_RECLUSTER measures
// against; NextID numbers new clusters so an ID is never reused.
type ownerClusters struct {
	Built    string          `json:"built,omitempty"`
	NextID   int             `json:"next_id"`
	Clusters []*clusterState `json:"clusters"`
}

// clusterState is one stable cluster: a set of fact IDs that is sent to the model
// as one call. Membership only changes when facts die (superseded or deleted) or
// new facts are assigned to it, so one new fact dirties one cluster instead of
// reshuffling the owner. DoneFP is the fingerprint of FactIDs after the cluster's
// last successful call; the cluster is due when the two differ. Fails counts
// consecutive failed calls on the membership FailFP.
type clusterState struct {
	ID      string   `json:"id"`
	Topic   bool     `json:"topic,omitempty"`
	FactIDs []string `json:"fact_ids"`
	DoneFP  string   `json:"done_fp,omitempty"`
	Fails   int      `json:"fails,omitempty"`
	FailFP  string   `json:"fail_fp,omitempty"`
	// FailedAt is when the last counted failure happened (RFC 3339). A cluster left
	// alone after maxWindowFails is tried again once failCooldown has passed.
	FailedAt string `json:"failed_at,omitempty"`
}

// fp is the fingerprint of the cluster's current membership.
func (cs *clusterState) fp() string { return idsFingerprint(cs.FactIDs) }

// due reports whether the cluster needs a call: it holds at least two facts (one
// fact has nothing to be reconciled against), its membership differs from the last
// successful call, and it has not failed maxWindowFails times on this membership
// within the last failCooldown.
func (cs *clusterState) due() bool {
	if len(cs.FactIDs) < 2 {
		return false
	}
	fp := cs.fp()
	if fp == cs.DoneFP {
		return false
	}
	if cs.Fails >= maxWindowFails && cs.FailFP == fp {
		// Left alone, but not forever: a quiet cluster's membership may never change,
		// and the failures may have been the model's bad day rather than the cluster.
		at, err := time.Parse(time.RFC3339, cs.FailedAt)
		return err == nil && time.Since(at) >= failCooldown
	}
	return true
}

// maxWindowFails is how many consecutive failed calls on one unchanged cluster are
// tolerated before it is left alone (until its membership changes, or failCooldown
// passes), so one bad cluster cannot re-spend an LLM call on every pass.
const maxWindowFails = 3

// failCooldown is how long a cluster left alone after maxWindowFails waits before
// it is tried again with the same members.
const failCooldown = 24 * time.Hour

// Consolidator owns the slow path.
type Consolidator struct {
	store *MemoryStore
	llm   *LLMClient
	every time.Duration

	// catchup is the delay before the next pass when a pass left due clusters
	// unreached, so a backlog drains in hours instead of waiting a full every.
	catchup time.Duration

	// recluster, when positive, is how long an owner's clusters live before they
	// are cut again from scratch. Zero keeps them until facts change them.
	recluster time.Duration

	// parallel is how many clusters one pass sends to the model at once.
	parallel int

	// timeout bounds one pass; zero means consolidateTimeout.
	timeout time.Duration

	// retention is how long an unreferenced L2 raw memory survives before the
	// decay pass removes it. Zero means never delete raw history.
	retention time.Duration

	// batch caps how many facts one consolidation call sees, so the prompt
	// cannot grow without bound as memory accumulates.
	batch int

	// graph is whether a pass asks for and writes L5 knowledge edges and the
	// cross-session arc. It is on by default; the server turns it off only when
	// HYATLAS_CONSOLIDATE_GRAPH is off, which leaves ultra with six layers.
	graph bool

	// gate makes a pass single-flight. The ticker and a manual POST /digest can
	// both ask for one, and two passes over the same batch would duplicate the
	// LLM spend and race each other's merges and edge writes.
	gate sync.Mutex

	// clusters holds each owner's stable clusters, and cursor is where the next
	// pass starts in the owner order. They are changed by the pass that holds gate;
	// while its calls run in parallel, stateMu guards them. Both are persisted to
	// statePath.
	clusters  map[string]*ownerClusters
	cursor    int
	stateMu   sync.Mutex
	statePath string

	// writeMu serialises the schema and arc writes of concurrent calls, so two
	// calls for one owner cannot both write the same schema or leave two live arcs.
	writeMu sync.Mutex

	// aliases maps a user ID to every ID of the same person (HYATLAS_USER_ALIASES),
	// as the server's search does. A person's facts are consolidated as one owner,
	// so a contradiction between facts written under two of their IDs meets in one
	// call. Set once with SetAliases before the first pass; nil means no aliases.
	aliases map[string][]string

	// durations holds the most recent successful call times (guarded by stateMu),
	// so callBudget can tell how long a call really takes on this model.
	durations []time.Duration

	mu     sync.Mutex
	last   *Report
	lastAt time.Time
	ran    int
}

// errBusy reports that another pass holds the consolidator. Callers decide what
// that means for them: the ticker skips the tick, the digest endpoint says so.
var errBusy = errors.New("a consolidation pass is already running")

// NewConsolidator wires the slow path. retention <= 0 disables raw decay. The
// clusters are loaded from the data dir, so a restart does not re-run clusters
// whose facts have not changed. HYATLAS_CONSOLIDATE_PARALLEL, _CATCHUP and
// _RECLUSTER are read here so the server needs no wiring for them.
func NewConsolidator(store *MemoryStore, llm *LLMClient, every, retention time.Duration, batch int) *Consolidator {
	if batch <= 0 {
		batch = defaultBatch
	}
	catchup := parseDuration("HYATLAS_CONSOLIDATE_CATCHUP", defaultCatchup)
	if catchup <= 0 {
		log.Printf("HYATLAS_CONSOLIDATE_CATCHUP must be positive; using %s", defaultCatchup)
		catchup = defaultCatchup
	}
	recluster := parseDuration("HYATLAS_CONSOLIDATE_RECLUSTER", 0)
	if recluster < 0 {
		recluster = 0
	}
	parallel := envInt("HYATLAS_CONSOLIDATE_PARALLEL", defaultParallel)
	if parallel > maxParallel {
		parallel = maxParallel
	}
	c := &Consolidator{store: store, llm: llm, every: every, retention: retention, batch: batch,
		graph: true, clusters: map[string]*ownerClusters{},
		catchup: catchup, recluster: recluster, parallel: parallel}
	if store != nil && store.indexPath != "" {
		c.statePath = filepath.Join(filepath.Dir(store.indexPath), consolidateStateFile)
	}
	c.loadState()
	return c
}

// SetAliases gives the consolidator the server's alias groups (see aliases). Call
// it before Run starts.
func (c *Consolidator) SetAliases(m map[string][]string) { c.aliases = m }

// groupOf is every user ID of the person behind user, user first when there are no
// aliases.
func (c *Consolidator) groupOf(user string) []string {
	if g := c.aliases[user]; len(g) > 0 {
		return g
	}
	return []string{user}
}

// canonical is the ID a person's facts are consolidated under: the first ID of
// their alias group, the same for every ID in it.
func (c *Consolidator) canonical(user string) string { return c.groupOf(user)[0] }

// groupOwners groups facts by owner like groupByOwner, with each user ID replaced
// by its canonical alias, so one person's IDs form one owner.
func (c *Consolidator) groupOwners(facts []DocIndex) (map[scopeKey][]DocIndex, []scopeKey) {
	return groupByOwnerWith(facts, c.canonical)
}

// recentDurationsMax is how many successful call times callBudget looks at.
const recentDurationsMax = 8

// callBudget is how much time must remain before a pass's deadline for a call to be
// worth starting: 1.25 times the median of recent successful calls, never less than
// minCallBudget, and never more than 80% of a pass so its first calls always start.
// A fixed budget below the model's real call time started calls the deadline then
// cut, spending free-tier quota for nothing.
func (c *Consolidator) callBudget() time.Duration {
	c.stateMu.Lock()
	ds := append([]time.Duration(nil), c.durations...)
	c.stateMu.Unlock()
	budget := minCallBudget
	if len(ds) > 0 {
		sort.Slice(ds, func(i, j int) bool { return ds[i] < ds[j] })
		if m := ds[len(ds)/2] * 5 / 4; m > budget {
			budget = m
		}
	}
	if limit := c.passTimeout() * 4 / 5; budget > limit {
		budget = limit
	}
	return budget
}

// pendingAtStart reports whether the stored state already holds work: no clusters
// yet (they still have to be built and run) or a cluster that is due. Run then
// starts with a catch-up pass instead of waiting a full every after a restart.
func (c *Consolidator) pendingAtStart() bool {
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	if len(c.clusters) == 0 {
		return true
	}
	for _, oc := range c.clusters {
		for _, cs := range oc.Clusters {
			if cs.due() {
				return true
			}
		}
	}
	return false
}

// Stats is the /api/v1/status view of the slow path.
func (c *Consolidator) Stats() (runs int, last *Report, at time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ran, c.last, c.lastAt
}

// Run drives the passes until ctx is cancelled. Called as a goroutine only in
// ultra mode; pro and lite never start it, so they make no background calls.
//
// The first pass comes c.every after start, or c.catchup when the stored state
// already holds work, so a restart during a backlog does not stall it. After a pass
// that left due clusters unreached, the next comes after c.catchup, so a backlog is
// worked off at the pace the model allows rather than one pass per c.every. A tick
// that finds a manual pass running retries after c.catchup, since that pass may
// itself leave work for later.
func (c *Consolidator) Run(ctx context.Context) {
	if c.every <= 0 {
		return
	}
	first := c.every
	if c.pendingAtStart() && c.catchup < first {
		first = c.catchup
	}
	t := time.NewTimer(first)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		next := c.every
		// A bounded context so one stuck call cannot wedge every later tick.
		run, cancel := context.WithTimeout(ctx, c.passTimeout())
		rep, err := c.Once(run)
		cancel()
		switch {
		case errors.Is(err, errBusy):
			// A manual pass is in flight and covers this tick's work, but it may leave
			// clusters pending, so check back at the catch-up pace, not after c.every.
			next = c.catchup
		case err != nil:
			log.Printf("consolidate: %v", err)
		default:
			log.Printf("consolidate: %+v", rep)
			if rep.ClustersPending > 0 {
				next = c.catchup
			}
		}
		t.Reset(next)
	}
}

// passTimeout is how long one pass may run.
func (c *Consolidator) passTimeout() time.Duration {
	if c.timeout > 0 {
		return c.timeout
	}
	return consolidateTimeout
}

// scopeKey is the owner a consolidation pass works within. Facts from different
// owners are never reasoned over together.
type scopeKey struct{ user, agent string }

func (k scopeKey) String() string { return fmt.Sprintf("user_id=%q agent_id=%q", k.user, k.agent) }

// clusterJob is one due cluster of one owner, queued for a pass.
type clusterJob struct {
	owner  scopeKey
	pos    int // the owner's index in the pass's work list
	cs     *clusterState
	fp     string   // the membership fingerprint the call is for
	ids    []string // the stored members, used to keep overlapping clusters apart
	facts  []DocIndex
	late   bool // started after an earlier call finished; a deadline cut is then not its fault
	done   bool // finished, or had nothing left to send; not pending
	ran    bool // a call was made and finished
	failed bool // the call finished without success (counted in Once, see countFailures)
}

// Once runs a single consolidation pass and reports exactly what it changed.
//
// Live facts are grouped by owner (user_id, agent_id), and each owner with at
// least two facts is consolidated on its own. The model never sees two people's
// memories in one prompt, so no merge, L5 edge, schema or arc can mix them.
//
// An owner's facts are held in stable topic clusters (see syncClusters). Only the
// clusters whose membership changed since their last successful call are due, so a
// quiet owner costs nothing and a new fact costs one call. The due clusters of all
// owners are interleaved owner by owner, starting from a cursor that moves between
// passes so no owner starves, and run up to c.parallel at a time. A call is not
// started when less than callBudget remains before the pass's deadline; the
// clusters not reached are counted in ClustersPending and the next pass, which Run
// schedules after c.catchup, starts from them.
//
// How long the first coverage takes depends on the model: calls per pass are
// roughly c.parallel × (pass budget ÷ call time). Measured on a free model with
// 25-fact clusters, a pass ran 7 clusters in 9 minutes, so ~150 clusters took about
// half a day of catch-up passes, where one window per owner every six hours took
// weeks. Afterwards only clusters that received new facts run.
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

	byOwner, owners := c.groupOwners(facts)
	for _, k := range owners {
		sortNewestFirst(byOwner[k])
	}
	// A single fact has nothing to reconcile against, so its owner sits this pass out.
	var work []scopeKey
	for _, k := range owners {
		if len(byOwner[k]) >= 2 {
			work = append(work, k)
		}
	}
	present := map[string]bool{}
	for _, k := range owners {
		present[k.String()] = true
	}
	for key := range c.clusters {
		if !present[key] {
			delete(c.clusters, key)
		}
	}

	n := len(work)
	first := 0
	if n > 0 {
		first = c.cursor % n
	}
	queues := make([][]*clusterJob, n)
	for i, k := range work {
		oc := c.syncClusters(k, byOwner[k])
		queues[i] = c.dueJobs(k, i, oc, byOwner[k])
		if len(queues[i]) == 0 {
			rep.OwnersUnchanged++
		}
	}
	// One cluster per owner in turn, so a big owner's backlog cannot hold back the
	// others for the hours it takes to drain.
	var jobs []*clusterJob
	for round := 0; ; round++ {
		added := false
		for i := 0; i < n; i++ {
			if q := queues[(first+i)%n]; round < len(q) {
				jobs = append(jobs, q[round])
				added = true
			}
		}
		if !added {
			break
		}
	}
	// The layout is saved before any call, so a restart mid-backlog keeps the
	// clusters (and what they have finished) instead of cutting them again.
	if err := c.saveState(); err != nil {
		rep.Errors = append(rep.Errors, "state: "+err.Error())
	}

	c.runJobs(ctx, jobs, rep)
	c.countFailures(jobs, rep)

	var pending []*clusterJob
	ranOwner := map[int]bool{}
	for _, j := range jobs {
		switch {
		case !j.done:
			pending = append(pending, j)
		case j.ran:
			rep.ClustersRun++
			ranOwner[j.pos] = true
		}
	}
	rep.OwnersRun = len(ranOwner)
	rep.ClustersPending = len(pending)
	if len(pending) > 0 {
		seen := map[int]bool{}
		for _, j := range pending {
			if !seen[j.pos] {
				seen[j.pos] = true
				rep.SkippedOwners = append(rep.SkippedOwners, j.owner.String())
			}
		}
		if err := ctx.Err(); err != nil {
			rep.Errors = append(rep.Errors, fmt.Sprintf(
				"pass stopped (%v) before %d cluster(s) of %d owner(s) were consolidated; they run next pass",
				err, len(pending), len(rep.SkippedOwners)))
		}
	}
	// A pass that reached every cluster starts one owner later next time; a cut one
	// resumes at the first owner it did not finish.
	if n > 0 {
		if len(pending) > 0 {
			c.cursor = pending[0].pos
		} else {
			c.cursor = (first + 1) % n
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

// countFailures charges this pass's failed calls to their clusters, unless two or
// more calls failed and none succeeded. Then the model or provider was down (an
// outage, an expired key, a WAF block), not the clusters; with catch-up passes every
// few minutes, counting those would let one bad hour use up every cluster's
// maxWindowFails and leave the backlog untouched. A pass with a single call cannot
// tell the two apart, so a lone failure is charged; failCooldown bounds the cost if
// it was an outage after all.
func (c *Consolidator) countFailures(jobs []*clusterJob, rep *Report) {
	var failed []*clusterJob
	succeeded := 0
	for _, j := range jobs {
		switch {
		case j.ran && j.failed:
			failed = append(failed, j)
		case j.ran:
			succeeded++
		}
	}
	if len(failed) == 0 {
		return
	}
	if succeeded == 0 && len(failed) > 1 {
		rep.Errors = append(rep.Errors, fmt.Sprintf(
			"all %d call(s) of this pass failed; not counted against their clusters (the model or provider looks down)", len(failed)))
		return
	}
	now := time.Now().UTC().Format(time.RFC3339)
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	for _, j := range failed {
		cs := j.cs
		if cs.FailFP == j.fp {
			cs.Fails++
		} else {
			cs.Fails, cs.FailFP = 1, j.fp
		}
		cs.FailedAt = now
		if cs.Fails >= maxWindowFails {
			rep.Errors = append(rep.Errors, fmt.Sprintf(
				"%s: cluster %s failed %d passes in a row; skipped until its facts change or %s passes",
				j.owner, cs.ID, cs.Fails, failCooldown))
			log.Printf("consolidate: %s cluster %s failed %d passes; leaving it for %s or until its facts change",
				j.owner, cs.ID, cs.Fails, failCooldown)
		}
	}
	if err := c.saveState(); err != nil {
		rep.Errors = append(rep.Errors, "state: "+err.Error())
	}
}

// dueJobs lists an owner's due clusters, the one holding the newest fact first.
func (c *Consolidator) dueJobs(k scopeKey, pos int, oc *ownerClusters, facts []DocIndex) []*clusterJob {
	ts := make(map[string]string, len(facts))
	for _, f := range facts {
		ts[f.ID] = f.Ts
	}
	newest := func(cs *clusterState) string {
		var m string
		for _, id := range cs.FactIDs {
			if ts[id] > m {
				m = ts[id]
			}
		}
		return m
	}
	var due []*clusterState
	for _, cs := range oc.Clusters {
		if cs.due() {
			due = append(due, cs)
		}
	}
	sort.SliceStable(due, func(i, j int) bool {
		a, b := newest(due[i]), newest(due[j])
		if a != b {
			return a > b
		}
		return due[i].ID < due[j].ID
	})
	jobs := make([]*clusterJob, len(due))
	for i, cs := range due {
		jobs[i] = &clusterJob{owner: k, pos: pos, cs: cs, fp: cs.fp(), ids: append([]string(nil), cs.FactIDs...)}
	}
	return jobs
}

// runJobs sends the jobs to the model, up to c.parallel at a time, in order. A job
// waits while it shares a fact with a running one (time clusters can overlap), and
// none starts once the pass context has ended or less than callBudget remains
// before its deadline. Jobs not started are left not done, which Once reports as
// pending.
func (c *Consolidator) runJobs(ctx context.Context, jobs []*clusterJob, rep *Report) {
	parallel := c.parallel
	if parallel < 1 {
		parallel = 1
	}
	busy := map[string]bool{}
	running := 0
	idle := make(chan struct{}, len(jobs)+1)
	var wg sync.WaitGroup
	started := 0
	budget := c.callBudget()

	blocked := func(j *clusterJob) bool {
		c.stateMu.Lock()
		defer c.stateMu.Unlock()
		if running >= parallel {
			return true
		}
		for _, id := range j.ids {
			if busy[id] {
				return true
			}
		}
		return false
	}

dispatch:
	for _, j := range jobs {
		for blocked(j) {
			if ctx.Err() != nil {
				break dispatch
			}
			select {
			case <-idle:
			case <-ctx.Done():
			}
		}
		if ctx.Err() != nil {
			break
		}
		if dl, ok := ctx.Deadline(); ok && time.Until(dl) < budget {
			break
		}
		// Re-read the members: an earlier cluster may have merged some of them away.
		j.facts = c.liveMembers(j.ids)
		if len(j.facts) < 2 {
			j.done = true // nothing left to reconcile; the next sync drops the dead ids
			continue
		}
		j.late = started >= parallel
		started++
		c.stateMu.Lock()
		running++
		for _, id := range j.ids {
			busy[id] = true
		}
		c.stateMu.Unlock()
		wg.Add(1)
		go func(j *clusterJob) {
			defer wg.Done()
			c.runCluster(ctx, j, rep, func() {
				running--
				for _, id := range j.ids {
					delete(busy, id)
				}
			})
			idle <- struct{}{}
		}(j)
	}
	wg.Wait()
}

// liveMembers returns the live facts among ids, newest first.
func (c *Consolidator) liveMembers(ids []string) []DocIndex {
	var out []DocIndex
	for _, d := range c.store.GetMany(ids) {
		if !isSuperseded(d) {
			out = append(out, d)
		}
	}
	sortNewestFirst(out)
	return out
}

// runCluster makes one cluster's call and records the outcome. release frees the
// job's slot and facts; it runs under stateMu with the rest of the bookkeeping.
//
// On success the cluster's members become the facts it was shown that are still
// live plus the facts the call wrote, and DoneFP follows. A fact that arrived
// during the call is in neither, so it is assigned on the next sync and the
// cluster is due again. On failure Fails counts up on the same membership; at
// maxWindowFails the cluster is left until its membership changes. A call the
// deadline cut off after earlier calls had spent the pass is not the cluster's
// fault, so it counts as nothing and the cluster stays pending.
func (c *Consolidator) runCluster(ctx context.Context, j *clusterJob, rep *Report, release func()) {
	sub := &Report{}
	callStart := time.Now()
	ok, created := c.consolidateScope(ctx, j.owner, j.facts, sub)
	kind := "time"
	if j.cs.Topic {
		kind = "topic"
	}
	log.Printf("consolidate: %s cluster %s (%s, %d facts) ok=%v in %s",
		j.owner, j.cs.ID, kind, len(j.facts), ok, time.Since(callStart).Round(time.Second))

	took := time.Since(callStart)
	cut := !ok && j.late && ctx.Err() != nil
	var keep []string
	if ok {
		// Only the call's own facts and what it wrote are checked, not the owner's
		// whole store; and liveness is by ID, so members written under another of
		// the owner's alias IDs are kept.
		keep = idsOf(c.liveMembers(append(idsOf(j.facts), created...)))
	}

	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	release()
	rep.absorb(sub)
	cs := j.cs
	switch {
	case cut:
		return
	case ok:
		cs.FactIDs = keep
		cs.DoneFP = cs.fp()
		cs.Fails, cs.FailFP, cs.FailedAt = 0, "", ""
		c.durations = append(c.durations, took)
		if len(c.durations) > recentDurationsMax {
			c.durations = c.durations[len(c.durations)-recentDurationsMax:]
		}
	default:
		// Charged to the cluster by countFailures once the pass shows whether the
		// model was working at all.
		j.failed = true
	}
	j.done, j.ran = true, true
	if err := c.saveState(); err != nil {
		rep.Errors = append(rep.Errors, "state: "+err.Error())
	}
}

// absorb adds a call's own report to the pass's.
func (r *Report) absorb(s *Report) {
	r.Merged += s.Merged
	r.Edges += s.Edges
	r.Absorbed += s.Absorbed
	r.Dropped += s.Dropped
	r.Schemas += s.Schemas
	r.Arc = r.Arc || s.Arc
	r.Errors = append(r.Errors, s.Errors...)
}

// consolidateScope is one LLM pass over a window of a single owner's facts. It
// applies the merges, drops, L5 edges, schemas and arc the model returned. facts
// is that window, and everything written carries the owner's scope.
//
// Only IDs in facts are acted on. An ID that belongs to another owner, or one the
// model invented, fails the live-set guard and changes nothing. It reports whether
// the owner's watermark may advance, and returns the IDs of the facts it wrote.
//
// Failures are fatal or soft. Fatal ones (the LLM call or its reply, a merge's
// replacement write, and supersedes of merged or dropped facts) make the owner
// retry, because the facts it was meant to reconcile are still unreconciled. Soft
// ones (arc, schema, L5 edge, and L1 mirrors) are reported in rep.Errors but do not
// hold the owner back: the facts were reconciled and the next pass would not repair
// them.
func (c *Consolidator) consolidateScope(ctx context.Context, owner scopeKey, facts []DocIndex, rep *Report) (bool, []string) {
	failed := false
	fatal := func(msg string) {
		failed = true
		rep.Errors = append(rep.Errors, owner.String()+": "+msg)
	}
	soft := func(msg string) {
		rep.Errors = append(rep.Errors, owner.String()+": "+msg)
	}
	schemas := c.liveOf(memory.L6Schema, owner)
	cons, err := c.ask(ctx, facts, schemas)
	if err != nil {
		fatal(err.Error())
		return false, nil
	}
	if !c.graph {
		// With the graph off the prompt does not ask for edges or an arc, and
		// anything a model sends for them anyway is ignored.
		cons.Knowledge, cons.Arc = nil, nil
	}

	live := liveIDs(facts)
	// batch is the immutable membership of what the LLM was shown. Evidence is
	// resolved against this rather than `live`, because merges delete IDs from
	// `live` — two facts merged into one would otherwise stop corroborating the
	// edge that cited them, silently dropping it.
	batch := liveIDs(facts)
	byID := byIDOf(facts)
	now := time.Now().UTC().Format(time.RFC3339)
	var created []string

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
		if id := c.applyMerge(owner, text, label, absorbed, now, live, rep, fatal, soft); id != "" {
			created = append(created, id)
		}
	}

	// Explicit drops: facts the model judged stale or superseded. Same
	// live-set guard, so only facts it was actually shown can be superseded, and
	// each needs a reason, which is kept on the dropped row.
	var drops []string
	dropped := map[string]bool{}
	var dropErr error
	for _, d := range cons.Drops {
		reason := strings.TrimSpace(d.Reason)
		if !live[d.ID] || dropped[d.ID] {
			continue
		}
		if reason == "" {
			log.Printf("consolidate: %s: drop of %s has no reason; kept", owner, d.ID)
			continue
		}
		dropped[d.ID] = true
		drops = append(drops, d.ID)
		// An empty supersededBy means dropped rather than replaced.
		if _, e := c.store.supersedeWith([]string{d.ID}, "", map[string]string{"drop_reason": truncate(reason, 200)}); e != nil && dropErr == nil {
			dropErr = e
		}
	}
	if len(drops) > 0 {
		marked := c.supersededOf(drops)
		for _, id := range marked {
			delete(live, id)
		}
		rep.Dropped += len(marked)
		if err := c.retireMirrors(c.store.GetMany(marked), ""); err != nil {
			soft("drop mirror: " + err.Error())
		}
		if dropErr != nil {
			fatal("drop: " + dropErr.Error())
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
		// edge keeps all of its citations and is counted once. The edge is in memory
		// even when persisting it fails, so it is counted as created either way.
		made, err := c.store.Graph().AddEdgeWithSources(owner.user, owner.agent, rel.From, rel.Relation, rel.To, cites)
		if made {
			rep.Edges++
		}
		if err != nil {
			soft("edge: " + err.Error())
		}
		// The graph is what the starmap draws; the L5 document is what search finds.
		// A re-cited edge is already indexed and is skipped.
		if _, err := c.store.indexL5([]l5Edge{{user: owner.user, agent: owner.agent,
			from: rel.From, rel: rel.Relation, to: rel.To, sources: cites}}); err != nil {
			soft("edge index: " + err.Error())
		}
	}

	// Schemas and the arc are written one call at a time, and the owner's schemas are
	// read again under the lock: a call running beside this one may have just written
	// the same pattern or retired the schema this one refines.
	c.writeMu.Lock()
	defer c.writeMu.Unlock()
	schemas = c.liveOf(memory.L6Schema, owner)

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
			soft("schema: " + err.Error())
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
				soft("schema supersede: " + err.Error())
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
			soft("arc: " + err.Error())
		} else {
			rep.Arc = true
			if len(prevArcs) > 0 {
				if _, err := c.store.Supersede(prevArcs, arcID); err != nil {
					soft("arc supersede: " + err.Error())
				}
			}
		}
	}
	return !failed, created
}

// applyMerge writes one consolidated fact and retires the facts it absorbed. It
// returns the new fact's ID, or "" when no replacement stays live.
//
// The replacement is written before anything is superseded, so a failed write
// touches no original. Supersede marks the originals as replaced by the new ID.
// Only the originals the store reports as marked are counted and removed from
// live. If none were marked the replacement is retracted, since nothing points at
// it. If some were marked, it stays live: retracting it would leave those marked
// originals superseded by a fact that no longer exists. The unmarked originals
// stay live beside it, and the failure is reported so the owner is retried. The
// replacement write and the supersedes are fatal; the L1 mirror writes are soft.
func (c *Consolidator) applyMerge(owner scopeKey, text, label string, absorbed []DocIndex, now string,
	live map[string]bool, rep *Report, fatal, soft func(string)) string {
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
		fatal("merge add: " + err.Error())
		return ""
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
	rep.Absorbed += len(marked)
	if len(marked) == 0 {
		_, retractErr := c.store.Supersede([]string{mergedID}, "")
		msg := fmt.Sprintf("merge supersede: 0 of %d absorbed facts marked; replacement retracted", len(ids))
		if retractErr != nil {
			msg += ": retract: " + retractErr.Error()
		}
		fatal(msg)
		return ""
	}
	if len(marked) < len(ids) || supErr != nil {
		msg := fmt.Sprintf("merge supersede: %d of %d absorbed facts marked; replacement kept", len(marked), len(ids))
		if supErr != nil {
			msg += ": " + supErr.Error()
		}
		fatal(msg)
	}
	rep.Merged++

	// The L1 profile mirror of a superseded preference is superseded with it, so
	// the profile does not keep an old phrasing. A preference that survives the
	// merge is mirrored again under the new text.
	if err := c.retireMirrors(c.store.GetMany(marked), mergedID); err != nil {
		soft("merge mirror: " + err.Error())
	}
	if label == "user_preferences" && len(srcs) > 0 {
		if err := c.store.Add(memory.L1Profile, newID(), text, map[string]string{
			"user_id": owner.user, "agent_id": owner.agent,
			"source_id": srcs[0], "ts": now,
		}); err != nil {
			soft("merge mirror add: " + err.Error())
		}
	}
	return mergedID
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

// loadState restores the clusters. A missing or unreadable file starts clean: the
// cost is one extra pass over each owner, not lost data. A file from before stable
// clusters has no clusters key, so it loads as empty and the clusters are built.
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
	c.cursor = st.Cursor
	for key, oc := range st.Clusters {
		if oc != nil {
			c.clusters[key] = oc
		}
	}
}

// saveState persists the clusters with a write-then-rename, as the doc index does.
// Callers hold stateMu while calls are running in parallel.
func (c *Consolidator) saveState() error {
	if c.statePath == "" {
		return nil
	}
	b, err := json.MarshalIndent(consolidateState{Cursor: c.cursor, Clusters: c.clusters}, "", "  ")
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
		// date lets the model tell which of two contradicting facts is newer; the
		// rule below says to keep the newer one, and without a date it had to guess.
		fmt.Fprintf(&b, "- id=%s | turn=%s | date=%s | %s\n", f.ID, turn, factDate(f.Ts), truncate(f.Content, 400))
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
	// The graph keys are asked for only when the graph is on, so a pass without it
	// spends no reply tokens on edges or an arc.
	graphKeys, graphRules := "", ""
	// The owner's entity page names, so an edge says "HyAtlas" the way its page
	// does instead of inventing "hyatlas server" or "HyAtlas v4" again.
	var names strings.Builder
	if c.graph && len(facts) > 0 && c.store.pages != nil {
		for i, pg := range c.store.pages.List(facts[0].UserID, facts[0].AgentID) {
			if i >= maxPromptPageNames {
				break
			}
			if i == 0 {
				names.WriteString("\nKnown entity names (use these exact spellings as knowledge endpoints when they apply):\n")
			}
			fmt.Fprintf(&names, "- %s\n", oneLine(pg.Name))
		}
	}
	if c.graph {
		graphKeys = `,
  "knowledge": [{"from": "<entity>", "relation": "<relation>", "to": "<entity>", "evidence": ["<id>", "<id>"]}],
  "arc": "<1-3 sentences: what these facts say about the user's work over time>"`
		graphRules = `
- knowledge: 0-5 entity-relation-entity edges, each corroborated in "evidence" by fact ids from AT LEAST 2 different turns. A triple resting on one turn is just that turn restated — do not emit it. Use only ids from the input. Only durable relations: what something is, uses, depends on, belongs to, or where it lives. Never an event, an amount, a status or a one-off outcome ("trade filled at $244", "server returned 403") — those are facts, not knowledge.
- arc: null if the facts are too few or too unrelated to synthesise.`
	}
	system := `You are a memory consolidation engine. Below are durable facts already stored, each with its id and the turn it came from. Reason ACROSS them — not about any single one — and output a JSON object with EXACTLY these keys:
{
  "merges": [{"text": "<one fact that replaces several>", "layer": "user_preferences|project_state|technical_lesson|decision|negative_knowledge", "supersedes": ["<id>", "<id>"]}],
  "drops": [{"id": "<id>", "reason": "<why this fact is stale or trivially obvious>"}],
  "schemas": [{"pattern": "<a recurring pattern only visible across many facts>", "context": "<when it applies>", "supersedes": ["<schema_id>"]}]` + graphKeys + `
}
Rules:
- merges: ONLY combine facts that genuinely say the same thing or contradict each other. For a contradiction, keep the newer statement (later date) and supersede the older. supersedes MUST list at least 2 ids. Use only ids from the input.
- drops: facts that are stale (a temporary state that has since passed) or trivially obvious, each with a short reason. A durable fact — a preference, a decision, where something lives, how something works — is never stale just because it is old. A drop without a reason is ignored. Be conservative.
- schemas: 0-3 patterns that generalise beyond the individual facts, each a full sentence that says what the pattern is and when it applies (not a 3-word label such as "pilot trade within cap"). Do not repeat a schema already stored (listed below, if any). To refine a stored schema, return the refined pattern and list the schema_id it replaces in "supersedes"; otherwise leave "supersedes" empty.` + graphRules + `
- Never invent an id. Never reference a fact not listed.
Return ONLY valid JSON, no prose, no markdown fences.

Facts:
` + b.String() + existing.String() + names.String()

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

// factDate returns the YYYY-MM-DD part of a stored timestamp for the consolidation
// prompt, or "unknown" when the row has none.
func factDate(ts string) string {
	if len(ts) >= 10 {
		return ts[:10]
	}
	return "unknown"
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
	return groupByOwnerWith(facts, func(user string) string { return user })
}

// groupByOwnerWith is groupByOwner with each user ID mapped through user first, so
// alias IDs of one person can be grouped as one owner.
func groupByOwnerWith(facts []DocIndex, user func(string) string) (map[scopeKey][]DocIndex, []scopeKey) {
	byOwner := map[scopeKey][]DocIndex{}
	var owners []scopeKey
	for _, f := range facts {
		k := scopeKey{user: user(f.UserID), agent: f.AgentID}
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

// factsFingerprint identifies an owner's live facts by the IDs they hold. See
// idsFingerprint.
func factsFingerprint(facts []DocIndex) string {
	return idsFingerprint(idsOf(facts))
}

// idsFingerprint is a hash of a set of fact IDs, taken over the sorted IDs. Any
// add, remove or swap changes it, so a delete and an add in the same second
// cannot collide the way a count and a newest timestamp could. A watermark written
// before this format is a count@timestamp string, which never matches, so its
// owner runs once.
func idsFingerprint(ids []string) string {
	s := append([]string(nil), ids...)
	sort.Strings(s)
	h := sha256.New()
	for _, id := range s {
		h.Write([]byte(id))
		h.Write([]byte{'\n'})
	}
	return hex.EncodeToString(h.Sum(nil))
}

// idsOf lists the IDs of facts.
func idsOf(facts []DocIndex) []string {
	out := make([]string, 0, len(facts))
	for _, f := range facts {
		out = append(out, f.ID)
	}
	return out
}

// idSetOf is the set of IDs in facts.
func idSetOf(facts []DocIndex) map[string]bool {
	m := make(map[string]bool, len(facts))
	for _, f := range facts {
		m[f.ID] = true
	}
	return m
}

// sortNewestFirst orders an owner's facts by timestamp, newest first, then by ID,
// so a window is the same set on every pass.
func sortNewestFirst(facts []DocIndex) {
	sort.Slice(facts, func(i, j int) bool {
		if facts[i].Ts != facts[j].Ts {
			return facts[i].Ts > facts[j].Ts
		}
		return facts[i].ID > facts[j].ID
	})
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
