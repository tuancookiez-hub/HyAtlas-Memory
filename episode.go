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
	"strings"
	"sync"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// episodesFile is the name of the file, inside the data directory, that holds the
// open and pending episodes so that they survive a restart.
const episodesFile = "episodes.json"

// defaultEpisodeGap is how long an owner may pause before a run of writes counts
// as a new episode. Thirty minutes ends a sitting without splitting a single task.
const defaultEpisodeGap = 30 * time.Minute

// episodeMaxTries is how many failed summary attempts an episode gets before it is
// dropped, so one broken model does not keep an episode pending forever.
const episodeMaxTries = 3

// episodeMaxFacts is how many of the newest facts are sent to the model for one
// episode. A very long episode is summarised from its recent end.
const episodeMaxFacts = 40

// episodeSummaryMax is the number of bytes kept from the model's reply.
const episodeSummaryMax = 600

// episodeFactChars is the number of bytes kept from each fact sent to the model.
const episodeFactChars = 300

// episodeSystemPrompt is the system message for an episode summary. It asks for
// prose that only restates the given facts, so the summary adds no invented detail.
const episodeSystemPrompt = "You summarise one episode of work from the memory facts it produced. Write 1-3 plain sentences: what was worked on, what was decided or found, and what is still open. Use only the facts given. No lists, no headings, no preamble."

// episode is one owner's run of writes with no gap longer than Episodes.gap.
type episode struct {
	User    string   `json:"user"`
	Agent   string   `json:"agent"`
	Start   string   `json:"start"` // RFC3339 UTC, first write
	Last    string   `json:"last"`  // RFC3339 UTC, latest write
	FactIDs []string `json:"fact_ids"`
	Tries   int      `json:"tries,omitempty"` // failed summary attempts
}

// key is the owner key that keys an open episode: the user and agent joined by a bar.
func (ep *episode) key() string {
	return ep.User + "|" + ep.Agent
}

// episodesState is the on-disk shape of episodes.json.
type episodesState struct {
	Open    map[string]*episode `json:"open"`
	Pending []*episode          `json:"pending"`
}

// Episodes groups each owner's consecutive writes into episodes and writes one L4
// summary per episode once it ends. Cutting episodes needs no model; only the
// summary does. A nil *Episodes is valid and does nothing, so callers need not
// check whether episodes are enabled.
type Episodes struct {
	mu      sync.Mutex
	path    string // empty means memory only
	store   *MemoryStore
	llm     *LLMClient
	gap     time.Duration
	open    map[string]*episode // one open episode per owner key
	pending []*episode          // ended episodes waiting for a summary
}

// resolveEpisodeGap reads HYATLAS_EPISODE_GAP and returns the gap between writes
// that ends an episode. "0", "off" and "false" return 0, which disables episodes.
// An unset value returns the default, and an invalid value logs and also returns
// the default, so a typo never silently turns the feature off.
func resolveEpisodeGap() time.Duration {
	raw := strings.TrimSpace(os.Getenv("HYATLAS_EPISODE_GAP"))
	if raw == "" {
		return defaultEpisodeGap
	}
	switch strings.ToLower(raw) {
	case "0", "off", "false":
		return 0
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		log.Printf("episodes: HYATLAS_EPISODE_GAP=%q is not a duration; using 30m", raw)
		return defaultEpisodeGap
	}
	return d
}

// NewEpisodes returns an Episodes that cuts episodes with the given gap and
// summarises them with llm. It returns nil when there is no model or the gap is
// not positive, because episodes cannot run then. A non-empty dataDir persists the
// open and pending episodes to episodes.json and reloads them here; an empty
// dataDir keeps them in memory only.
func NewEpisodes(dataDir string, store *MemoryStore, llm *LLMClient, gap time.Duration) *Episodes {
	if llm == nil || gap <= 0 {
		return nil
	}
	e := &Episodes{store: store, llm: llm, gap: gap, open: map[string]*episode{}}
	if dataDir != "" {
		e.path = filepath.Join(dataDir, episodesFile)
		e.load()
	}
	return e
}

// load reads episodes.json into memory. A missing or unreadable file leaves the
// episodes empty and logs once, so a bad file cannot stop the server from starting.
func (e *Episodes) load() {
	raw, err := os.ReadFile(e.path)
	if err != nil {
		log.Printf("episodes: cannot read %s, starting empty: %v", e.path, err)
		return
	}
	var st episodesState
	if err := json.Unmarshal(raw, &st); err != nil {
		log.Printf("episodes: cannot parse %s, starting empty: %v", e.path, err)
		return
	}
	for key, ep := range st.Open {
		if ep != nil {
			e.open[key] = ep
		}
	}
	for _, ep := range st.Pending {
		if ep != nil {
			e.pending = append(e.pending, ep)
		}
	}
}

// Note records the facts one extraction wrote for an owner. It appends them to the
// owner's open episode while the gap allows, and otherwise closes that episode and
// starts a new one. A call with no facts never starts an episode, because an
// episode with nothing in it has nothing to summarise.
func (e *Episodes) Note(user, agent string, factIDs []string, at time.Time) {
	if e == nil {
		return
	}
	at = at.UTC()
	stamp := at.Format(time.RFC3339)
	key := user + "|" + agent

	e.mu.Lock()
	defer e.mu.Unlock()

	if ep, ok := e.open[key]; ok {
		if at.Sub(episodeTime(ep.Last)) <= e.gap {
			ep.FactIDs = appendUniqueIDs(ep.FactIDs, factIDs)
			ep.Last = stamp
			e.persistLocked()
			return
		}
		delete(e.open, key)
		e.pending = append(e.pending, ep)
		log.Printf("episodes: closed %s (%d facts)", key, len(ep.FactIDs))
	}
	if len(factIDs) == 0 {
		e.persistLocked()
		return
	}
	e.open[key] = &episode{
		User:    user,
		Agent:   agent,
		Start:   stamp,
		Last:    stamp,
		FactIDs: appendUniqueIDs(nil, factIDs),
	}
	e.persistLocked()
}

// Run closes ended episodes and summarises them until ctx is done. It runs one
// tick at once, so episodes left open before a restart are closed, and then one
// tick every gap/3, kept between one and five minutes.
func (e *Episodes) Run(ctx context.Context) {
	if e == nil {
		return
	}
	interval := e.gap / 3
	if interval < time.Minute {
		interval = time.Minute
	}
	if interval > 5*time.Minute {
		interval = 5 * time.Minute
	}
	e.tick(ctx, time.Now())
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			e.tick(ctx, time.Now())
		}
	}
}

// tick does one pass. It first moves every open episode whose last write is older
// than the gap to pending. Then it summarises each pending episode in turn. The
// lock is not held during a model call, so Note keeps working while a summary is
// in flight. Anything that changed is persisted at the end.
func (e *Episodes) tick(ctx context.Context, now time.Time) {
	if e == nil {
		return
	}
	e.mu.Lock()
	changed := false
	for key, ep := range e.open {
		if now.Sub(episodeTime(ep.Last)) > e.gap {
			delete(e.open, key)
			e.pending = append(e.pending, ep)
			changed = true
			log.Printf("episodes: closed %s (%d facts)", key, len(ep.FactIDs))
		}
	}
	batch := append([]*episode(nil), e.pending...)
	e.mu.Unlock()

	for _, ep := range batch {
		if ctx.Err() != nil {
			break
		}
		e.process(ctx, ep)
		changed = true
	}

	if changed {
		e.mu.Lock()
		e.persistLocked()
		e.mu.Unlock()
	}
}

// process summarises one pending episode. It skips the model when fewer than two
// facts are still live, because a summary of one fact only restates it. A failed
// attempt counts as a try, and the episode is dropped after episodeMaxTries.
func (e *Episodes) process(ctx context.Context, ep *episode) {
	key := ep.key()
	facts := e.liveFacts(ep)
	if len(facts) < 2 {
		log.Printf("episodes: skipped %s: %d live facts", key, len(facts))
		e.mu.Lock()
		e.removePendingLocked(ep)
		e.mu.Unlock()
		return
	}

	start := time.Now()
	text, err := e.summarise(ctx, ep, facts)
	if err == nil {
		err = e.writeSummary(ep, text)
	}
	if err != nil {
		e.mu.Lock()
		ep.Tries++
		tries := ep.Tries
		if tries >= episodeMaxTries {
			e.removePendingLocked(ep)
		}
		e.mu.Unlock()
		log.Printf("episodes: summary of %s failed (try %d): %v", key, tries, err)
		if tries >= episodeMaxTries {
			log.Printf("episodes: dropped %s after %d tries", key, tries)
		}
		return
	}

	e.mu.Lock()
	e.removePendingLocked(ep)
	e.mu.Unlock()
	log.Printf("episodes: summarised %s (%d facts) in %s", key, len(facts), time.Since(start).Round(time.Millisecond))
}

// liveFacts loads an episode's facts from the store and keeps the ones that are
// still live L3 facts, so superseded facts never reach the summary. The result is
// in time order, and when there are more than episodeMaxFacts only the newest are
// kept.
func (e *Episodes) liveFacts(ep *episode) []DocIndex {
	var live []DocIndex
	for _, d := range e.store.GetMany(ep.FactIDs) {
		if d.Layer == string(memory.L3Fact) && !isSuperseded(d) {
			live = append(live, d)
		}
	}
	sort.SliceStable(live, func(i, j int) bool {
		return episodeTime(live[i].Ts).Before(episodeTime(live[j].Ts))
	})
	if len(live) > episodeMaxFacts {
		live = live[len(live)-episodeMaxFacts:]
	}
	return live
}

// summarise asks the model for a short summary of one episode. It uses its own
// three-minute timeout, so a slow model cannot hold the tick for long. The reply is
// cleaned and cut to episodeSummaryMax bytes, and an empty reply is an error.
func (e *Episodes) summarise(ctx context.Context, ep *episode, facts []DocIndex) (string, error) {
	cctx, cancel := context.WithTimeout(ctx, 3*time.Minute)
	defer cancel()

	var user strings.Builder
	fmt.Fprintf(&user, "Episode: %s to %s (UTC)\n", episodeStamp(ep.Start), episodeStamp(ep.Last))
	user.WriteString("Facts:\n")
	for _, d := range facts {
		fmt.Fprintf(&user, "- %s\n", utf8Trunc(oneLine(d.Content), episodeFactChars))
	}
	messages := []map[string]string{
		{"role": "system", "content": episodeSystemPrompt},
		{"role": "user", "content": strings.TrimSpace(user.String())},
	}
	reply, err := e.llm.chat(cctx, messages, 0.2)
	if err != nil {
		return "", fmt.Errorf("episode model call: %w", err)
	}
	text := utf8Trunc(strings.TrimSpace(trimFences(reply)), episodeSummaryMax)
	if text == "" {
		return "", errors.New("episode model returned an empty reply")
	}
	return text, nil
}

// writeSummary stores the episode summary as one L4 row. The row's ts is the
// episode's last write, and its metadata records the episode bounds and the facts
// it came from, so the summary stays traceable.
func (e *Episodes) writeSummary(ep *episode, text string) error {
	meta := map[string]string{
		"user_id":       ep.User,
		"agent_id":      ep.Agent,
		"ts":            ep.Last,
		"kind":          kindEpisode,
		"episode_start": ep.Start,
		"episode_end":   ep.Last,
		"source_ids":    strings.Join(ep.FactIDs, ","),
	}
	if err := e.store.Add(memory.L4Summary, newID(), text, meta); err != nil {
		return fmt.Errorf("write episode summary: %w", err)
	}
	return nil
}

// Counts returns how many episodes are open and how many are waiting for a summary.
func (e *Episodes) Counts() (open, pending int) {
	if e == nil {
		return 0, 0
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.open), len(e.pending)
}

// persistLocked writes the open and pending episodes to episodes.json. The caller
// must hold e.mu. A failed write is logged, never returned, because a lost save
// only costs the next restart some episode state.
func (e *Episodes) persistLocked() {
	if e.path == "" {
		return
	}
	if err := os.MkdirAll(filepath.Dir(e.path), 0o755); err != nil {
		log.Printf("episodes: cannot create directory for %s: %v", e.path, err)
		return
	}
	st := episodesState{Open: e.open, Pending: e.pending}
	if err := writeJSONAtomic(e.path, st); err != nil {
		log.Printf("episodes: cannot save %s: %v", e.path, err)
	}
}

// removePendingLocked drops one episode from the pending list by pointer identity.
// The caller must hold e.mu.
func (e *Episodes) removePendingLocked(ep *episode) {
	for i, p := range e.pending {
		if p == ep {
			e.pending = append(e.pending[:i], e.pending[i+1:]...)
			return
		}
	}
}

// appendUniqueIDs appends the ids not already in dst, keeping their order, so an
// episode never lists a fact twice even if a caller retries a write.
func appendUniqueIDs(dst []string, add []string) []string {
	seen := make(map[string]bool, len(dst)+len(add))
	for _, id := range dst {
		seen[id] = true
	}
	for _, id := range add {
		if !seen[id] {
			seen[id] = true
			dst = append(dst, id)
		}
	}
	return dst
}

// episodeTime parses an RFC3339 timestamp. It returns the zero time when the value
// does not parse, so a bad timestamp sorts first and never panics.
func episodeTime(s string) time.Time {
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}
	}
	return t
}

// episodeStamp formats an RFC3339 timestamp as "YYYY-MM-DD HH:MM" in UTC for the
// model prompt. A value that does not parse is returned unchanged.
func episodeStamp(s string) string {
	t := episodeTime(s)
	if t.IsZero() {
		return s
	}
	return t.UTC().Format("2006-01-02 15:04")
}
