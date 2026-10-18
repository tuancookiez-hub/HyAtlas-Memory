package main

import (
	"context"
	"log"
	"sync/atomic"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// A write made while the server has no key, or whose extraction call failed, is
// stored as a raw trace and nothing more. Without the catch-up loop only the manual
// /api/v1/reprocess call fixed it, so a week of notes taken before the first chat of
// a session, or during a provider outage, stayed unsearchable by their facts for good.

const (
	// catchupEveryKey is the environment variable that sets how often the catch-up
	// loop runs. A duration such as 10m, or "off" to turn the loop off.
	catchupEveryKey = "HYATLAS_CATCHUP_EVERY"
	// catchupEveryDefault is how often the loop runs when the variable is unset.
	catchupEveryDefault = 10 * time.Minute
	// catchupBatch is the most raw rows one run looks at, oldest first. A run that
	// finds more leaves the rest for the next one, so a large backlog is worked off
	// at a pace a hosted free tier can take.
	catchupBatch = 20
	// catchupMinAge is how old a raw row must be before the loop touches it. A
	// younger row may still have its own background extraction running, and a second
	// extraction of the same text would store its facts twice.
	catchupMinAge = 2 * time.Minute
)

// Catchup re-extracts raw memories that extraction never reached. It runs when a key
// arrives (Kick) and on a timer, one run at a time.
type Catchup struct {
	srv   *Server
	every time.Duration
	// kick wakes the loop early. It holds at most one pending wake-up, because two
	// keys arriving close together need only one run.
	kick chan struct{}
	// running is set while a run is in progress, so a timer tick and a key push
	// that coincide do not extract the same rows twice.
	running atomic.Bool
	// now is the clock; tests replace it.
	now func() time.Time
}

// CatchupResult is what one catch-up run did.
type CatchupResult struct {
	// Extracted is how many rows went through a model call and succeeded.
	Extracted int
	// Marked is how many rows already had derived rows and were only flagged as
	// extracted, with no model call.
	Marked int
	// Failed is how many rows hit a model error; the run stops at the first.
	Failed int
	// Skipped is how many rows were too new to touch yet or are being extracted.
	Skipped int
}

// NewCatchup builds the loop for srv. An interval of zero or less turns it off and
// returns nil, which every method accepts.
func NewCatchup(srv *Server, every time.Duration) *Catchup {
	if every <= 0 {
		return nil
	}
	return &Catchup{srv: srv, every: every, kick: make(chan struct{}, 1), now: time.Now}
}

// Kick asks the loop to run now. It never blocks and does nothing on a nil Catchup.
func (c *Catchup) Kick() {
	if c == nil {
		return
	}
	select {
	case c.kick <- struct{}{}:
	default:
	}
}

// Run does one pass whenever the timer fires or Kick is called, until ctx is done.
func (c *Catchup) Run(ctx context.Context) {
	if c == nil {
		return
	}
	t := time.NewTicker(c.every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		case <-c.kick:
		}
		c.Once(ctx)
	}
}

// Wait blocks until a running pass has finished or timeout passes, so a shutdown does
// not close the store under an extraction the loop started. It does nothing on a nil
// Catchup.
func (c *Catchup) Wait(timeout time.Duration) {
	if c == nil {
		return
	}
	deadline := time.Now().Add(timeout)
	for c.running.Load() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
}

// Once runs one pass and reports what it did; ran is false when another run was in
// progress or the server cannot extract right now (lite mode, or no usable key), in
// which case nothing was tried. Up to catchupBatch unextracted raw rows are taken
// oldest first, through the same extraction as a normal write. A row that already
// has derived rows is flagged as extracted instead of extracted again. The pass stops
// at the first model error, since the endpoint is then likely still down.
func (c *Catchup) Once(ctx context.Context) (res CatchupResult, ran bool) {
	if c == nil || !c.running.CompareAndSwap(false, true) {
		return res, false
	}
	defer c.running.Store(false)
	s := c.srv
	if !s.mode.UsesLLM() || s.llm == nil || !s.llm.Configured() {
		return res, false
	}
	rows := s.unextractedRaw(catchupBatch)
	if len(rows) == 0 {
		return res, true
	}
	derived := s.store.derivedSources()
	now := c.now()
	for _, it := range rows {
		if ctx.Err() != nil {
			break
		}
		if at := docTime(it.Ts); !at.IsZero() && now.Sub(at) < catchupMinAge {
			res.Skipped++
			continue
		}
		// A write's own extraction may still be running, however old the row is (a
		// slow provider can take longer than catchupMinAge); a second one would store
		// its facts twice.
		if s.extractionRunning(it.ID) {
			res.Skipped++
			continue
		}
		if derived[it.ID] {
			_ = s.store.SetExtracted(it.ID, true)
			res.Marked++
			continue
		}
		if err := s.extract(it.Content, it.UserID, it.AgentID, it.ID); err != nil {
			res.Failed++
			break
		}
		res.Extracted++
	}
	log.Printf("catch-up: %d unextracted row(s) looked at: %d extracted, %d already had facts, %d failed, %d too new",
		len(rows), res.Extracted, res.Marked, res.Failed, res.Skipped)
	return res, true
}

// derivedSources is the set of raw memory ids that any other row names as a source,
// live or superseded. A raw row in this set has been extracted, whatever its
// Extracted flag says: the flag lives only in the doc index, which a hard kill can
// force to be rebuilt from the collections without it.
func (s *MemoryStore) derivedSources() map[string]bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := map[string]bool{}
	for _, d := range s.index {
		if d.Layer == string(memory.L2Raw) {
			continue
		}
		for _, src := range sourcesOf(d) {
			if src != "" {
				out[src] = true
			}
		}
	}
	return out
}

// unextractedCount is how many live raw rows extraction has not reached. Lite mode
// extracts nothing, so it reports 0 there instead of counting every raw row.
func (s *Server) unextractedCount() int {
	if !s.mode.UsesLLM() {
		return 0
	}
	s.store.mu.RLock()
	defer s.store.mu.RUnlock()
	n := 0
	for _, d := range s.store.index {
		if d.Layer == string(memory.L2Raw) && !d.Extracted && !isSuperseded(d) {
			n++
		}
	}
	return n
}
