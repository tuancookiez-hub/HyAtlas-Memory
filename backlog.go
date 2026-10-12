package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Write-time logic (4.6.0 and later) only keeps new writes consistent. A store that
// already holds thousands of facts from before still has older facts that a newer
// one contradicts, and no entity pages at all. The backlog pass cleans that up once:
// a dry run asks the model, topic cluster by topic cluster, which facts are
// outdated and which durable things the facts are about, and saves that as a plan
// to read; apply then carries out the saved plan without asking the model again.

const (
	backlogPlanFile    = "backlog_plan.json"
	backlogAppliedFile = "backlog_plan.applied.json"

	// backlogCallTimeout bounds one cluster's LLM call. A free reasoning model can
	// take minutes on a full window; a stuck call must not hold the job forever.
	backlogCallTimeout = 6 * time.Minute

	// backlogPagesMax caps the pages taken from one cluster, as the prompt asks.
	backlogPagesMax = 5

	// backlogSamples is how many outdated pairs the summary shows with their texts.
	backlogSamples = 10
)

const backlogSystem = `You are cleaning a personal memory store. Below are stored facts about related topics, each with its id and date. Output a JSON object with EXACTLY these keys:
{
  "outdated": [{"id": "<older fact id>", "by": "<newer fact id>", "reason": "<one short sentence>"}],
  "pages": [{"page": "new", "name": "<canonical name>", "kind": "project|tool|person|place|org|concept|other", "set": [{"attr": "<short attribute>", "value": "<current value>"}], "event": "<optional notable change, one sentence>"}]
}
Rules:
- outdated: ONLY when a newer fact (later date) states a different value or state for the same subject, so the older one is no longer true. "by" must be that newer fact. A fact is never outdated just for being old, and never by a fact about a different subject. When unsure, leave it out.
- pages: 0-5 durable things these facts are about (projects, tools, people, places, organisations), each with its CURRENT state taken from the newest facts. Do not make pages for one-off events.
- Use only ids from the list. Never invent an id.
Return ONLY valid JSON, no prose, no markdown fences.`

// BacklogOutdated is one planned supersede: fact ID is no longer true because the
// newer fact By says otherwise. The texts are kept so the plan can be read.
type BacklogOutdated struct {
	ID     string `json:"id"`
	By     string `json:"by"`
	Reason string `json:"reason"`
	Text   string `json:"text"`
	ByText string `json:"by_text"`
}

// backlogOwner is the plan for one owner.
type backlogOwner struct {
	User     string            `json:"user"`
	Agent    string            `json:"agent"`
	Pages    []PageUpdate      `json:"pages"`
	Outdated []BacklogOutdated `json:"outdated"`
}

// backlogPlan is what the dry run saves and apply reads.
type backlogPlan struct {
	Created string         `json:"created"`
	Owners  []backlogOwner `json:"owners"`
}

// BacklogStatus is the GET view of the job.
type BacklogStatus struct {
	State         string         `json:"state"` // idle|running|done|failed
	DryRun        bool           `json:"dry_run"`
	Started       string         `json:"started,omitempty"`
	Finished      string         `json:"finished,omitempty"`
	ClustersTotal int            `json:"clusters_total"`
	ClustersDone  int            `json:"clusters_done"`
	Errors        []string       `json:"errors,omitempty"`
	PlanPath      string         `json:"plan_path,omitempty"`
	Summary       map[string]any `json:"summary,omitempty"`
}

// BacklogJob runs the one-time backlog pass, one job at a time.
type BacklogJob struct {
	dataDir string
	store   *MemoryStore
	cons    *Consolidator
	llm     *LLMClient
	admin   func() bool

	wg sync.WaitGroup // lets tests wait for the background goroutine

	mu     sync.Mutex
	status BacklogStatus
}

// NewBacklogJob builds an idle job. admin reports whether the server runs with
// HYATLAS_ADMIN=on; it is a func so the orchestrator can pass the server's flag.
func NewBacklogJob(dataDir string, store *MemoryStore, cons *Consolidator, llm *LLMClient, admin func() bool) *BacklogJob {
	return &BacklogJob{dataDir: dataDir, store: store, cons: cons, llm: llm, admin: admin,
		status: BacklogStatus{State: "idle"}}
}

func (b *BacklogJob) planPath() string { return filepath.Join(b.dataDir, backlogPlanFile) }

// snapshot returns a copy of the status safe to encode while the job runs.
func (b *BacklogJob) snapshot() BacklogStatus {
	b.mu.Lock()
	defer b.mu.Unlock()
	s := b.status
	s.Errors = append([]string(nil), s.Errors...)
	if s.Summary != nil {
		m := make(map[string]any, len(s.Summary))
		for k, v := range s.Summary {
			m[k] = v
		}
		s.Summary = m
	}
	return s
}

func (b *BacklogJob) update(f func(s *BacklogStatus)) {
	b.mu.Lock()
	defer b.mu.Unlock()
	f(&b.status)
}

// HandleBacklog is /api/v1/admin/backlog: GET reports the job, POST starts it.
// {"dry_run": false} applies the saved plan; anything else plans. Both are refused
// unless the server runs with HYATLAS_ADMIN=on.
func (b *BacklogJob) HandleBacklog(w http.ResponseWriter, r *http.Request) {
	if b.admin == nil || !b.admin() {
		jsonResponse(w, 403, map[string]any{"error": "maintenance endpoints are off: restart the server with HYATLAS_ADMIN=on"})
		return
	}
	switch r.Method {
	case http.MethodGet:
		jsonResponse(w, 200, b.snapshot())
	case http.MethodPost:
		var body struct {
			DryRun *bool `json:"dry_run"`
		}
		if r.Body != nil {
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil && err.Error() != "EOF" {
				jsonResponse(w, 400, map[string]any{"error": "bad body"})
				return
			}
		}
		dry := body.DryRun == nil || *body.DryRun
		code, err := b.start(dry)
		if err != nil {
			jsonResponse(w, code, map[string]any{"error": err.Error(), "status": b.snapshot()})
			return
		}
		jsonResponse(w, 202, b.snapshot())
	default:
		w.Header().Set("Allow", "GET, POST")
		jsonResponse(w, 405, map[string]any{"error": "method not allowed: use GET or POST"})
	}
}

// start validates the request and launches the job; the HTTP code is returned
// with any refusal.
func (b *BacklogJob) start(dry bool) (int, error) {
	b.mu.Lock()
	if b.status.State == "running" {
		b.mu.Unlock()
		return 409, fmt.Errorf("a backlog job is already running")
	}
	var plan *backlogPlan
	if dry {
		if b.cons == nil || b.llm == nil || !b.llm.Configured() {
			b.mu.Unlock()
			return 400, fmt.Errorf("the backlog dry run needs an LLM; none configured")
		}
	} else {
		p, err := b.readPlan()
		if err != nil {
			b.mu.Unlock()
			return 400, err
		}
		plan = p
	}
	b.status = BacklogStatus{State: "running", DryRun: dry, Started: time.Now().UTC().Format(time.RFC3339),
		PlanPath: b.planPath()}
	b.mu.Unlock()

	b.wg.Add(1)
	go func() {
		defer b.wg.Done()
		var err error
		if dry {
			err = b.runDry(context.Background())
		} else {
			err = b.runApply(plan)
		}
		b.update(func(s *BacklogStatus) {
			s.Finished = time.Now().UTC().Format(time.RFC3339)
			s.State = "done"
			if err != nil {
				s.State = "failed"
				s.Errors = append(s.Errors, err.Error())
			}
		})
	}()
	return 202, nil
}

// readPlan loads the saved plan. A missing plan is the "already applied or never
// planned" case, since apply renames the file.
func (b *BacklogJob) readPlan() (*backlogPlan, error) {
	raw, err := os.ReadFile(b.planPath())
	if os.IsNotExist(err) {
		return nil, fmt.Errorf("no backlog plan to apply: run a dry run first (a plan is applied only once)")
	}
	if err != nil {
		return nil, err
	}
	var plan backlogPlan
	if err := json.Unmarshal(raw, &plan); err != nil {
		return nil, fmt.Errorf("backlog plan is unreadable: %w", err)
	}
	return &plan, nil
}

// ownerWork is one owner's clusters, cut before any LLM call so progress has a total.
type ownerWork struct {
	key      scopeKey
	clusters [][]DocIndex
}

// runDry asks the model about every cluster and saves the plan. A cluster that
// fails is recorded and skipped; the job continues.
func (b *BacklogJob) runDry(ctx context.Context) error {
	facts := b.cons.listAllLive(memory.L3Fact)
	byOwner, owners := groupByOwner(facts)
	var work []ownerWork
	total := 0
	for _, k := range owners {
		fs := byOwner[k]
		// A single fact has nothing to be contradicted by or reconciled against.
		if len(fs) < 2 {
			continue
		}
		sortNewestFirst(fs)
		cl := b.cons.clusterWindows(fs)
		work = append(work, ownerWork{key: k, clusters: cl})
		total += len(cl)
	}
	b.update(func(s *BacklogStatus) { s.ClustersTotal = total })

	plan := backlogPlan{Created: time.Now().UTC().Format(time.RFC3339), Owners: []backlogOwner{}}
	for _, ow := range work {
		po := backlogOwner{User: ow.key.user, Agent: ow.key.agent, Pages: []PageUpdate{}, Outdated: []BacklogOutdated{}}
		seenPair := map[string]bool{}
		pageAt := map[string]int{} // normalized page name -> index in po.Pages
		for _, cluster := range ow.clusters {
			cluster = dedupFacts(cluster)
			out, err := b.askCluster(ctx, cluster)
			if err != nil {
				msg := fmt.Sprintf("%s: %v", ow.key, err)
				log.Printf("backlog: %s", msg)
				b.update(func(s *BacklogStatus) { s.Errors = append(s.Errors, msg); s.ClustersDone++ })
				continue
			}
			for _, o := range out.outdated {
				// A fact in two overlapping clusters can yield the same pair twice, and
				// one fact is made outdated once.
				if seenPair[o.ID] {
					continue
				}
				seenPair[o.ID] = true
				po.Outdated = append(po.Outdated, o)
			}
			for _, up := range out.pages {
				key := normalizeName(up.Name)
				if key == "" {
					continue
				}
				if i, ok := pageAt[key]; ok {
					po.Pages[i] = mergePageUpdate(po.Pages[i], up)
					continue
				}
				pageAt[key] = len(po.Pages)
				po.Pages = append(po.Pages, up)
			}
			b.update(func(s *BacklogStatus) { s.ClustersDone++ })
		}
		plan.Owners = append(plan.Owners, po)
	}

	if err := writeJSONAtomic(b.planPath(), plan); err != nil {
		return fmt.Errorf("write plan: %w", err)
	}
	pages, outdated := 0, 0
	var sample []BacklogOutdated
	for _, po := range plan.Owners {
		pages += len(po.Pages)
		outdated += len(po.Outdated)
		for _, o := range po.Outdated {
			if len(sample) < backlogSamples {
				sample = append(sample, o)
			}
		}
	}
	b.update(func(s *BacklogStatus) {
		s.Summary = map[string]any{"owners": len(plan.Owners), "clusters": total, "pages_proposed": pages,
			"facts_to_supersede": outdated, "sample_outdated": sample}
	})
	return nil
}

// dedupFacts drops repeated ids from a cluster. Trailing time-ordered windows can
// overlap by a few facts, so the same fact may be listed twice.
func dedupFacts(fs []DocIndex) []DocIndex {
	seen := make(map[string]bool, len(fs))
	out := make([]DocIndex, 0, len(fs))
	for _, f := range fs {
		if !seen[f.ID] {
			seen[f.ID] = true
			out = append(out, f)
		}
	}
	return out
}

// mergePageUpdate folds a later cluster's proposal for the same page into the
// first. Clusters are walked newest topic first, so the first value of an
// attribute wins and a later cluster only adds what is missing.
func mergePageUpdate(a, b PageUpdate) PageUpdate {
	have := map[string]bool{}
	for _, s := range a.Set {
		have[strings.ToLower(s.Attr)] = true
	}
	for _, s := range b.Set {
		if !have[strings.ToLower(s.Attr)] {
			a.Set = append(a.Set, s)
		}
	}
	if a.Event == "" {
		a.Event = b.Event
	}
	if a.Kind == "" {
		a.Kind = b.Kind
	}
	return a
}

type clusterAnswer struct {
	outdated []BacklogOutdated
	pages    []PageUpdate
}

// askCluster makes the one bounded LLM call for a cluster and validates the reply
// against that cluster alone.
func (b *BacklogJob) askCluster(ctx context.Context, cluster []DocIndex) (clusterAnswer, error) {
	var sb strings.Builder
	sb.WriteString("Facts:\n")
	for _, f := range cluster {
		fmt.Fprintf(&sb, "- id=%s | date=%s | %s\n", f.ID, factDate(f.Ts), truncate(f.Content, 400))
	}
	cctx, cancel := context.WithTimeout(ctx, backlogCallTimeout)
	defer cancel()
	content, err := b.llm.chat(cctx, []map[string]string{
		{"role": "system", "content": backlogSystem},
		{"role": "user", "content": sb.String()},
	}, 0.1)
	if err != nil {
		return clusterAnswer{}, err
	}
	return parseBacklogReply(content, cluster)
}

// parseBacklogReply tolerantly reads the reply and keeps only what holds up: both
// ids belong to this cluster, differ, and the newer fact is not older than the one
// it replaces. A missing key is an empty list.
func parseBacklogReply(content string, cluster []DocIndex) (clusterAnswer, error) {
	content = trimFences(content)
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(content), &raw); err != nil {
		start, end := strings.Index(content, "{"), strings.LastIndex(content, "}")
		if start == -1 || end <= start {
			return clusterAnswer{}, fmt.Errorf("backlog reply parse failed (raw %.80s)", content)
		}
		if err := json.Unmarshal([]byte(content[start:end+1]), &raw); err != nil {
			return clusterAnswer{}, err
		}
	}
	byID := byIDOf(cluster)
	var ans clusterAnswer

	var items []json.RawMessage
	if json.Unmarshal(raw["outdated"], &items) == nil {
		seen := map[string]bool{}
		for _, it := range items {
			var o struct {
				ID     string `json:"id"`
				By     string `json:"by"`
				Reason string `json:"reason"`
			}
			if json.Unmarshal(it, &o) != nil {
				continue
			}
			old, okOld := byID[o.ID]
			newer, okNew := byID[o.By]
			if !okOld || !okNew || o.ID == o.By || seen[o.ID] {
				continue
			}
			od, nd := factDate(old.Ts), factDate(newer.Ts)
			if od == "unknown" || nd == "unknown" || nd < od {
				continue
			}
			seen[o.ID] = true
			ans.outdated = append(ans.outdated, BacklogOutdated{ID: o.ID, By: o.By,
				Reason: truncate(strings.TrimSpace(o.Reason), 200),
				Text:   truncate(old.Content, 400), ByText: truncate(newer.Content, 400)})
		}
	}

	var pages pageUpdates
	if len(raw["pages"]) > 0 {
		_ = json.Unmarshal(raw["pages"], &pages)
	}
	for _, up := range pages {
		if normalizeName(up.Name) == "" {
			continue
		}
		up.Page = "new" // ids in a plan are never trusted; Apply matches by name
		ans.pages = append(ans.pages, up)
		if len(ans.pages) == backlogPagesMax {
			break
		}
	}
	return ans, nil
}

// runApply carries out the saved plan without any LLM call. Every planned
// supersede is re-checked against the live store first, because the store may have
// changed since the dry run.
func (b *BacklogJob) runApply(plan *backlogPlan) error {
	superseded, skipped, created, updated := 0, 0, 0, 0
	var errs []string
	for _, po := range plan.Owners {
		// Liveness is judged once, before this owner's first supersede, so a fact that
		// is both outdated and the replacement for an older one does not make that
		// older one look dead.
		var ids []string
		for _, o := range po.Outdated {
			ids = append(ids, o.ID, o.By)
		}
		docs := byIDOf(b.store.GetMany(ids))
		liveOwned := func(id string) bool {
			d, ok := docs[id]
			return ok && d.Layer == string(memory.L3Fact) && !isSuperseded(d) &&
				d.UserID == po.User && d.AgentID == po.Agent
		}
		replacedBy := map[string]string{}
		for _, o := range po.Outdated {
			// Liveness was judged up front, so two facts that name each other (same-day
			// dates pass the date check) would both be superseded and neither survive.
			if o.ID == o.By || !liveOwned(o.ID) || !liveOwned(o.By) || replacedBy[o.By] == o.ID {
				skipped++
				continue
			}
			replacedBy[o.ID] = o.By
			n, err := b.store.Supersede([]string{o.ID}, o.By)
			if err != nil {
				errs = append(errs, fmt.Sprintf("supersede %s: %v", o.ID, err))
			}
			if n == 0 {
				skipped++
				continue
			}
			superseded += n
		}
		for i := 0; i < len(po.Pages); i += pageUpdatesMax {
			end := i + pageUpdatesMax
			if end > len(po.Pages) {
				end = len(po.Pages)
			}
			c, u, err := b.store.pages.Apply(po.User, po.Agent, "backlog", po.Pages[i:end], nil)
			created += c
			updated += u
			if err != nil {
				errs = append(errs, fmt.Sprintf("pages: %v", err))
			}
		}
	}
	// Renamed last so a plan is applied once; an apply that dies early can be rerun,
	// since already-superseded facts are skipped and pages match by name.
	if err := os.Rename(b.planPath(), filepath.Join(b.dataDir, backlogAppliedFile)); err != nil {
		errs = append(errs, "rename plan: "+err.Error())
	}
	b.update(func(s *BacklogStatus) {
		s.Errors = append(s.Errors, errs...)
		s.Summary = map[string]any{"superseded": superseded, "pages_created": created,
			"pages_updated": updated, "skipped": skipped}
	})
	return nil
}

// writeJSONAtomic writes v through a temp file and a rename, as the other
// persisted files do, so a crash never leaves half a plan.
func writeJSONAtomic(path string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
