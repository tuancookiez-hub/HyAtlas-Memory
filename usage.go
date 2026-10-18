package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// usageEntry is what the server knows about one memory's recall history: how many
// times it was handed to an agent, how many times the agent's reply drew on it, and
// when it last did.
type usageEntry struct {
	Shown int    `json:"shown"`
	Used  int    `json:"used"`
	Last  string `json:"last"` // RFC3339 of the last time it was used
}

// UsageStats counts how often each memory is recalled and how often the reply used
// it. The counts feed a small ranking nudge, so memory that gets used rises and
// memory that is only ever shown sinks. It is persisted next to the store so the
// signal survives restarts.
type UsageStats struct {
	mu   sync.RWMutex
	m    map[string]usageEntry
	path string
	// dirty is set when the counts changed since the last write, and gen counts those
	// changes so a write can tell whether a change landed while it was running.
	dirty bool
	gen   uint64
	// flushArmed is set while the timer that writes the counts is waiting. fileMu keeps
	// two writes from racing on the same temp file.
	flushArmed bool
	fileMu     sync.Mutex
}

// usageMinShown is how many recalls a memory needs before its used/shown ratio is
// trusted. Below it a memory keeps its plain relevance rank, because one or two
// recalls say nothing about whether it helps.
const usageMinShown = 3

// usageMaxEntries caps how many memories usage.json tracks. Every recalled id gains
// an entry and nothing else removes one, so without a cap the file grows forever.
// Pruning to 90% of the cap leaves headroom, so it does not run on every write.
const usageMaxEntries = 20000

// usageLearningFile is where usage learning is persisted. Before 4.8.0 it shared
// usage.json with the store's write and search counters, so each overwrote the
// other and neither survived a restart reliably.
const usageLearningFile = "usage_learning.json"

// usageFlushEvery is the least time between two writes of the counts. Changes in the
// window are written together by one timer, so a busy server does not rewrite the
// whole file on every recall.
const usageFlushEvery = 2 * time.Second

// NewUsageStats loads <dataDir>/usage_learning.json if it exists. Without it, a
// usage.json from before 4.8.0 is read once if it holds per-memory entries rather
// than the store's counters. A missing or unreadable file starts empty, because
// usage counts are advisory and must never block startup. An empty dataDir keeps
// the counts in memory only.
func NewUsageStats(dataDir string) *UsageStats {
	u := &UsageStats{m: map[string]usageEntry{}}
	if dataDir == "" {
		return u
	}
	u.path = filepath.Join(dataDir, usageLearningFile)
	b, err := os.ReadFile(u.path)
	if err != nil {
		b, err = os.ReadFile(filepath.Join(dataDir, "usage.json"))
	}
	if err == nil {
		var m map[string]usageEntry
		if json.Unmarshal(b, &m) == nil && m != nil {
			u.m = m
		}
	}
	return u
}

// Record counts one recall: every id in recalled was shown; the ones whose text the
// reply evidently used are counted as used. contents maps id -> memory text.
// It marks the counts for a write, which Flush or the flush timer makes later, and
// returns how many were used. The write never happens while the caller holds the lock.
//
// Why: the plugin sends what it recalled and what the assistant then said. A memory
// the agent keeps getting and never uses is noise; one it uses matters. This is that
// signal, read without an LLM call.
func (u *UsageStats) Record(recalled []string, contents map[string]string, reply, query string) int {
	if u == nil {
		return 0
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	now := time.Now().UTC().Format(time.RFC3339)
	seen := make(map[string]bool, len(recalled))
	used := 0
	for _, id := range recalled {
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		e := u.m[id]
		e.Shown++
		if usedInReply(contents[id], reply, query) {
			e.Used++
			e.Last = now
			used++
		}
		u.m[id] = e
	}
	if len(u.m) > usageMaxEntries {
		u.pruneLocked()
	}
	if len(seen) > 0 {
		u.markDirtyLocked()
	}
	return used
}

// markDirtyLocked records that the counts changed and starts the flush timer if none
// is waiting. Stats with no file path are memory only and never write. The caller
// must hold u.mu for writing.
func (u *UsageStats) markDirtyLocked() {
	if u.path == "" {
		return
	}
	u.gen++
	u.dirty = true
	if !u.flushArmed {
		u.flushArmed = true
		time.AfterFunc(usageFlushEvery, u.flushTimer)
	}
}

// flushTimer runs when the flush timer ends. It clears the armed flag and writes the
// counts. A failed write is logged, because usage counts are advisory.
func (u *UsageStats) flushTimer() {
	u.mu.Lock()
	u.flushArmed = false
	u.mu.Unlock()
	if err := u.Flush(); err != nil {
		log.Printf("usage: cannot save %s: %v", u.path, err)
	}
}

// Flush writes the counts to usage_learning.json now if they changed since the last
// write, and returns nil when nothing changed or the stats have no file. The server
// should call it on shutdown, so counts recorded in the last two seconds are kept. A
// nil UsageStats does nothing.
//
// The map is marshalled under the read lock and the file is written outside it, so
// searches that read the counts are never held up by the disk.
func (u *UsageStats) Flush() error {
	if u == nil || u.path == "" {
		return nil
	}
	u.fileMu.Lock()
	defer u.fileMu.Unlock()

	u.mu.RLock()
	if !u.dirty {
		u.mu.RUnlock()
		return nil
	}
	gen := u.gen
	b, err := json.Marshal(u.m)
	u.mu.RUnlock()
	if err != nil {
		return err
	}

	if err := os.MkdirAll(filepath.Dir(u.path), 0o755); err != nil {
		return err
	}
	tmp := u.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, u.path); err != nil {
		return err
	}

	u.mu.Lock()
	// A change that landed during the write keeps the counts dirty for the next flush.
	if u.gen == gen {
		u.dirty = false
	}
	u.mu.Unlock()
	return nil
}

// pruneLocked drops entries until the map holds usageMaxEntries*9/10. It goes first
// for memories never used, fewest shows first, then for the oldest last use. The
// caller must hold u.mu.
//
// Why: a memory that was never used and was rarely shown carries the least signal,
// so it is the cheapest to forget. Used memories are kept, and among them the ones
// not used for longest go first.
func (u *UsageStats) pruneLocked() {
	target := usageMaxEntries * 9 / 10
	drop := len(u.m) - target
	if drop <= 0 {
		return
	}
	type cand struct {
		id string
		e  usageEntry
	}
	var unused, rest []cand
	for id, e := range u.m {
		if e.Used == 0 {
			unused = append(unused, cand{id, e})
		} else {
			rest = append(rest, cand{id, e})
		}
	}
	sort.Slice(unused, func(i, j int) bool {
		if unused[i].e.Shown != unused[j].e.Shown {
			return unused[i].e.Shown < unused[j].e.Shown
		}
		return unused[i].id < unused[j].id
	})
	for _, c := range unused {
		if drop <= 0 {
			return
		}
		delete(u.m, c.id)
		drop--
	}
	sort.Slice(rest, func(i, j int) bool {
		if rest[i].e.Last != rest[j].e.Last {
			return rest[i].e.Last < rest[j].e.Last
		}
		return rest[i].id < rest[j].id
	})
	for _, c := range rest {
		if drop <= 0 {
			return
		}
		delete(u.m, c.id)
		drop--
	}
}

// Factor is the ranking multiplier for a memory: 1.0 until it has been shown at
// least usageMinShown times, then 0.94 + 0.12*(used+1)/(shown+2). That keeps the
// range within about 6% either way, so a hit moves only a few places and relevance
// still decides the order.
func (u *UsageStats) Factor(id string) float64 {
	if u == nil {
		return 1.0
	}
	u.mu.RLock()
	defer u.mu.RUnlock()
	e, ok := u.m[id]
	if !ok || e.Shown < usageMinShown {
		return 1.0
	}
	return 0.94 + 0.12*float64(e.Used+1)/float64(e.Shown+2)
}

// LastUsed returns when memory id was last used in a reply. It returns the zero time
// when the memory has no usage record, the record has no readable timestamp, or u is
// nil.
//
// Why: recency ranking measures age from the later of creation and last use, so a
// memory the agent keeps relying on does not sink just because it was written long ago.
func (u *UsageStats) LastUsed(id string) time.Time {
	if u == nil {
		return time.Time{}
	}
	u.mu.RLock()
	defer u.mu.RUnlock()
	e, ok := u.m[id]
	if !ok {
		return time.Time{}
	}
	t, err := time.Parse(time.RFC3339, e.Last)
	if err != nil {
		return time.Time{}
	}
	return t
}

// rerankUsage reorders hits by position weight 1/(rrfK+i+1) times Factor (stable
// sort, new slice, Score untouched). nil UsageStats returns hits unchanged.
//
// Why: the position weight is the same decay reciprocal rank fusion uses, so the
// usage factor is compared on the same scale as relevance. A memory that is used
// well can overtake a neighbour it barely outranks, but not one far above it.
func rerankUsage(hits []SearchHit, u *UsageStats) []SearchHit {
	if u == nil || len(hits) == 0 {
		return hits
	}
	return rerankByFactor(hits, func(_ int, h SearchHit) float64 { return u.Factor(h.ID) })
}

// usedInReply reports whether reply evidently drew on memory text. Words of the
// query that led to the recall do not count: a reply naturally repeats the question
// ("hyatlas", "extraction"), so every on-topic memory would look used. Only what the
// memory added beyond the question is evidence. query may be empty.
//
// Why: an exact citation is rare, so the test is term overlap. A memory with enough
// distinctive terms counts as used when a third of them come back in the reply. A
// short memory cannot be judged on overlap, so an identifier it carries (a port
// number, an env var, a function name) is the signal: the reply repeating it means
// the agent actually relied on the memory. No LLM call is involved.
func usedInReply(memory, reply, query string) bool {
	if strings.TrimSpace(memory) == "" || strings.TrimSpace(reply) == "" {
		return false
	}
	replyTerms := map[string]bool{}
	for _, t := range keywordTokens(reply) {
		replyTerms[t] = true
	}
	queryTerms := map[string]bool{}
	for _, t := range keywordTokens(query) {
		queryTerms[t] = true
	}

	memTerms := map[string]bool{}
	var ordered []string
	for _, t := range keywordTokens(memory) {
		if utf8.RuneCountInString(t) < 4 || memTerms[t] || queryTerms[t] {
			continue
		}
		memTerms[t] = true
		ordered = append(ordered, t)
	}
	if len(ordered) >= 3 {
		hit := 0
		for _, t := range ordered {
			if replyTerms[t] {
				hit++
			}
		}
		if hit*10 >= len(ordered)*3 {
			return true
		}
	}

	for _, w := range strings.Fields(memory) {
		if !looksLikeIdentifier(w) {
			continue
		}
		toks := keywordTokens(w)
		if len(toks) == 0 {
			continue
		}
		asked := true
		for _, t := range toks {
			if !queryTerms[t] {
				asked = false
				break
			}
		}
		if asked {
			continue // the question named this identifier; repeating it proves nothing
		}
		all := true
		for _, t := range toks {
			if !replyTerms[t] {
				all = false
				break
			}
		}
		if all {
			return true
		}
	}
	return false
}

// usageFeedbackMaxIDs caps how many recalled ids one feedback call may count.
const usageFeedbackMaxIDs = 50

// usageFeedbackMaxReply caps the reply bytes kept for matching.
const usageFeedbackMaxReply = 20000

// usageFeedbackRequest is the body of POST /api/v1/feedback.
type usageFeedbackRequest struct {
	RecalledIDs []string `json:"recalled_ids"`
	Reply       string   `json:"reply"`
	Query       string   `json:"query"`
	UserID      string   `json:"user_id"`
	AgentID     string   `json:"agent_id"`
}

// HandleFeedback serves POST /api/v1/feedback with body
// {"recalled_ids": [...], "reply": "...", "user_id": "...", "agent_id": "..."}.
// It looks up the memories with store.GetMany, ignores ids that are not found or
// belong to another owner (when user_id is given, compare DocIndex.UserID; an empty
// row owner is allowed), calls Record and answers {"ok": true, "shown": n, "used": m}.
// 400 on a bad body; at most 50 ids are considered; reply is cut to 20000 bytes.
//
// Why the owner check: a caller may only move the counts of memories it could have
// been shown. A row with no owner is shared, so it stays countable.
//
// expand maps the caller's user ID to every ID of the same person
// (HYATLAS_USER_ALIASES), so a memory written under an alias still counts; nil
// means no aliases.
func (u *UsageStats) HandleFeedback(store *MemoryStore, expand func([]string) []string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body usageFeedbackRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			jsonResponse(w, 400, map[string]string{"error": "bad json body"})
			return
		}
		ids := body.RecalledIDs
		if len(ids) > usageFeedbackMaxIDs {
			ids = ids[:usageFeedbackMaxIDs]
		}
		reply := body.Reply
		if len(reply) > usageFeedbackMaxReply {
			cut := usageFeedbackMaxReply
			// Back up to a rune boundary so the kept text stays valid UTF-8.
			for cut > 0 && !utf8.RuneStart(reply[cut]) {
				cut--
			}
			reply = reply[:cut]
		}

		owners := map[string]bool{}
		if body.UserID != "" {
			group := []string{body.UserID}
			if expand != nil {
				group = expand(group)
			}
			for _, id := range group {
				owners[id] = true
			}
		}
		var kept []string
		contents := map[string]string{}
		if store != nil {
			for _, d := range store.GetMany(ids) {
				if len(owners) > 0 && d.UserID != "" && !owners[d.UserID] {
					continue
				}
				if _, dup := contents[d.ID]; dup {
					continue
				}
				kept = append(kept, d.ID)
				contents[d.ID] = d.Content
			}
		}
		used := u.Record(kept, contents, reply, body.Query)
		jsonResponse(w, 200, map[string]any{"ok": true, "shown": len(kept), "used": used})
	}
}
