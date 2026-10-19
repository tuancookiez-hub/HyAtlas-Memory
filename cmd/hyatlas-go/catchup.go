package main

import (
	"context"
	"errors"
	"log"
	"strconv"
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
	// catchupMaxAgeKey is the environment variable that sets how old a raw row may be
	// and still be caught up. A duration such as 72h, or "off" for no limit.
	catchupMaxAgeKey = "HYATLAS_CATCHUP_MAX_AGE"
	// catchupMaxAgeDefault is the age limit when the variable is unset. Older rows are
	// left alone, so a long outage does not turn into a re-extraction of old history.
	catchupMaxAgeDefault = 7 * 24 * time.Hour
	// catchupMaxTries is how many parse failures a row gets from the loop before it is
	// given up on. /api/v1/reprocess with explicit ids still extracts such a row.
	catchupMaxTries = 3
	// metaExtractWanted is the metadata key a write sets on its raw row when the mode
	// extracts. Only rows that carry it are caught up or counted as waiting.
	metaExtractWanted = "extract_wanted"
	// metaCatchupTries is the metadata key that counts the loop's parse failures on a row.
	metaCatchupTries = "catchup_tries"
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
	// maxAge is how old a raw row may be and still be caught up; 0 means no limit.
	maxAge time.Duration
}

// CatchupResult is what one catch-up run did.
type CatchupResult struct {
	// Extracted is how many rows went through a model call and succeeded.
	Extracted int
	// Marked is how many rows already had derived rows and were only flagged as
	// extracted, with no model call.
	Marked int
	// Failed is how many rows hit a model error. The run stops at the first chat error
	// (the endpoint is likely down) and goes on past a reply that could not be parsed.
	Failed int
	// Skipped is how many rows were too new to touch yet, are being extracted, or have
	// used up their tries.
	Skipped int
}

// NewCatchup builds the loop for srv. An interval of zero or less turns it off and
// returns nil, which every method accepts.
func NewCatchup(srv *Server, every time.Duration) *Catchup {
	if every <= 0 {
		return nil
	}
	return &Catchup{srv: srv, every: every, kick: make(chan struct{}, 1), now: time.Now,
		maxAge: envDuration(catchupMaxAgeKey, catchupMaxAgeDefault)}
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

// Start runs the loop in a goroutine that the server's background group tracks, so a
// shutdown waits for it. It does nothing on a nil Catchup.
func (c *Catchup) Start(ctx context.Context) {
	if c == nil {
		return
	}
	c.srv.bg.Add(1)
	go func() {
		defer c.srv.bg.Done()
		c.Run(ctx)
	}()
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

// Once runs one pass and reports what it did; ran is false when another run was in
// progress or the server cannot extract right now (lite mode, or no usable key), in
// which case nothing was tried. Up to catchupBatch rows that were written to be
// extracted and are not yet, and are younger than the age limit, are taken oldest
// first, through the same extraction as a normal write. A row that already has
// derived rows is flagged as extracted instead of extracted again. A reply that
// cannot be parsed counts a try on that row and the pass goes on with the next one; a
// row with catchupMaxTries tries is left alone. Any other model error stops the pass,
// since the endpoint is then likely still down. The pass is counted in the server's
// background group, so a shutdown waits for it.
func (c *Catchup) Once(ctx context.Context) (res CatchupResult, ran bool) {
	if c == nil || !c.running.CompareAndSwap(false, true) {
		return res, false
	}
	defer c.running.Store(false)
	s := c.srv
	s.bg.Add(1)
	s.bgRunning.Add(1)
	defer s.bgRunning.Add(-1)
	defer s.bg.Done()
	if !s.mode.UsesLLM() || s.llm == nil || !s.llm.Configured() {
		return res, false
	}
	now := c.now()
	rows, exhausted := s.store.catchupRows(catchupBatch, c.maxAge, now)
	res.Skipped += exhausted
	if len(rows) == 0 {
		return res, true
	}
	derived := s.store.derivedSources()
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
			if errors.Is(err, errParse) {
				c.noteTry(it)
				continue
			}
			break
		}
		res.Extracted++
	}
	log.Printf("catch-up: %d unextracted row(s) looked at: %d extracted, %d already had facts, %d failed, %d skipped",
		len(rows), res.Extracted, res.Marked, res.Failed, res.Skipped)
	return res, true
}

// noteTry records one more failed try on the raw row it, so a row the model never
// answers with JSON stops being retried after catchupMaxTries passes.
func (c *Catchup) noteTry(it DocIndex) {
	tries := catchupTries(it) + 1
	if err := c.srv.store.updateMeta(it.ID, map[string]string{metaCatchupTries: strconv.Itoa(tries)}); err != nil {
		log.Printf("catch-up: cannot record a try on %s: %v", it.ID, err)
		return
	}
	if tries >= catchupMaxTries {
		log.Printf("catch-up: giving up on %s after %d unparsable replies; /api/v1/reprocess can still extract it", it.ID, tries)
	}
}

// catchupTries is how many parse failures the loop has recorded on the row d.
func catchupTries(d DocIndex) int {
	n, _ := strconv.Atoi(d.Meta[metaCatchupTries])
	return n
}

// catchupRows returns up to max raw rows the loop may extract, oldest first: live,
// written to be extracted (extract_wanted), not extracted, with fewer than
// catchupMaxTries tries, and younger than maxAge (0 means any age) at now. Rows
// without the marker are never taken. The second result is how many rows would
// qualify but have used up their tries; they are left out before the cut so they
// cannot fill the batch and starve newer rows.
func (s *MemoryStore) catchupRows(max int, maxAge time.Duration, now time.Time) (rows []DocIndex, exhausted int) {
	// Newest first, so walking it backwards gives the oldest first order and the
	// walk can stop as soon as max rows are found.
	all := s.liveNewestFirst(memory.L2Raw)
	for i := len(all) - 1; i >= 0; i-- {
		d := all[i]
		if !awaitsExtraction(d) {
			continue
		}
		if at := docTime(d.Ts); maxAge > 0 && !at.IsZero() && now.Sub(at) > maxAge {
			continue
		}
		if catchupTries(d) >= catchupMaxTries {
			exhausted++
			continue
		}
		if len(rows) < max {
			rows = append(rows, d)
		}
	}
	return rows, exhausted
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

// unextractedCount is how many live raw rows are waiting for extraction: written to
// be extracted and not yet. It reads the store's counter and applies no age limit, so
// it can include rows the loop no longer picks up. Lite mode extracts nothing, so it
// reports 0 there.
func (s *Server) unextractedCount() int {
	if !s.mode.UsesLLM() {
		return 0
	}
	return s.store.awaitingExtraction()
}
