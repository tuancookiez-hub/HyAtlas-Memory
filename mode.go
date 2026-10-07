package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Mode selects how much reasoning the server does. All three share the same
// store, the same local embeddings and the same seven layers; they differ in
// whether an LLM is called at all, and in how widely it reasons.
//
// The three modes form a real ladder — each is strictly more capable than the
// one before it:
//
//	lite  — no reasoning. Raw text plus local embeddings only. No LLM call is
//	        ever made, so conversation text never leaves the machine.
//	pro   — reasons WITHIN one turn. One extraction call per write fills all
//	        seven layers from that turn. Nothing runs after the write returns.
//	ultra — reasons ACROSS time. Everything pro does, plus the slow path: a
//	        periodic consolidation pass that merges contradicting facts,
//	        generalises schemas visible only across many turns, synthesises a
//	        cross-session arc, and decays raw history that no graph edge still
//	        cites.
//
// Ultra is the only mode that can notice two facts contradict each other, or
// that a preference repeated across sessions is a pattern rather than an event,
// because neither conclusion is available from any single turn.
//
// Whether a write BLOCKS on its extraction is a separate question from how much
// reasoning happens — see Sync. Conflating the two produced an earlier design
// where pro and ultra were identical apart from latency.
type Mode string

const (
	ModeLite  Mode = "lite"
	ModePro   Mode = "pro"
	ModeUltra Mode = "ultra"

	// defaultMode is what a server with no HYATLAS_MODE set runs.
	defaultMode = ModeUltra

	// extractTimeout bounds one extraction LLM call. Shared by the blocking and
	// background paths so the two cannot drift apart.
	extractTimeout = 180 * time.Second

	// syncKey is the separate knob for whether a write waits on extraction.
	syncKey = "HYATLAS_SYNC_EXTRACT"
)

// validModes is the accepted set, in ladder order — each entry is strictly more
// capable than the one before it.
var validModes = []Mode{ModeLite, ModePro, ModeUltra}

// ParseMode validates HYATLAS_MODE. Empty means the default.
//
// An unrecognised value is an error rather than a silent fallback: a user who
// typos "lite" and quietly gets ultra would have their conversation text sent to
// an LLM they believed they had turned off.
func ParseMode(raw string) (Mode, error) {
	v := strings.ToLower(strings.TrimSpace(raw))
	if v == "" {
		return defaultMode, nil
	}
	for _, m := range validModes {
		if v == string(m) {
			return m, nil
		}
	}
	return "", fmt.Errorf("unknown HYATLAS_MODE %q (valid: %s)", raw, strings.Join(modeNames(), ", "))
}

func modeNames() []string {
	out := make([]string, len(validModes))
	for i, m := range validModes {
		out[i] = string(m)
	}
	return out
}

// OrDefault canonicalises the zero value. A Server literal built without a mode
// (tests, or any construction site that forgets the field) must behave as the
// documented default rather than as a fourth unnamed mode that reports "" on the
// wire.
func (m Mode) OrDefault() Mode {
	for _, v := range validModes {
		if m == v {
			return v
		}
	}
	return defaultMode
}

// UsesLLM reports whether this mode calls an LLM at all. Lite does not, which is
// the only mode where conversation text never leaves the machine.
func (m Mode) UsesLLM() bool { return m.OrDefault() != ModeLite }

// Consolidates reports whether this mode runs the slow path. This — not blocking
// versus background — is what ultra adds over pro.
func (m Mode) Consolidates() bool { return m.OrDefault() == ModeUltra }

// Rank orders the modes so "each tier is at least as capable as the last" is
// assertable in a test rather than merely claimed in prose. Capability is
// monotonic: everything a lower rank does, the next rank also does.
func (m Mode) Rank() int {
	switch m.OrDefault() {
	case ModeLite:
		return 0
	case ModePro:
		return 1
	default:
		return 2
	}
}

// LayersActive is the count of the 7-layer model this mode populates.
//
// The counts differ per tier because the two systems own different layers:
//
//	lite   1 — L2 raw only, nothing extracted
//	pro    5 — System1 fills L1, L2, L3, L4, L7 from each turn
//	ultra  7 — System1 plus the slow path, which adds L5 knowledge and L6
//	           schemas. Those two are System2 products: a knowledge relation
//	           worth keeping is corroborated by more than one turn, and a schema
//	           is a *recurring* pattern, so neither can be produced from a single
//	           turn.
func (m Mode) LayersActive() int {
	switch m.OrDefault() {
	case ModeLite:
		return 1
	case ModePro:
		return 5
	default:
		return 7
	}
}

// System1Layers are the layers the per-turn pass owns, for any mode that calls
// an LLM. L5 and L6 are excluded on purpose — see LayersActive.
func System1Layers() []memory.Layer {
	return []memory.Layer{
		memory.L1Profile, memory.L2Raw, memory.L3Fact, memory.L4Summary, memory.L7Intention,
	}
}

// System2Layers are the layers only the slow path produces.
func System2Layers() []memory.Layer {
	return []memory.Layer{memory.L5Knowledge, memory.L6Schema}
}

// Describe is the startup-log and status spelling of a mode.
func (m Mode) Describe() string {
	switch m.OrDefault() {
	case ModeLite:
		return "lite (raw + local embeddings, no LLM call, nothing leaves the machine)"
	case ModePro:
		return "pro (per-write extraction: L1-L4 + L7, no slow path)"
	default:
		return "ultra (per-write extraction + slow path adding L5 knowledge and L6 schemas)"
	}
}

// Sync is the separate knob: whether a write blocks on its extraction.
//
// Pro defaults to blocking — a mode whose whole point is one call within the
// write should let the caller see the outcome. Ultra defaults to background, so
// the write returns immediately while extraction and the slow path run behind
// it. Lite is unaffected because it never extracts.
type Sync string

const (
	SyncAuto Sync = ""
	SyncOn   Sync = "on"
	SyncOff  Sync = "off"
)

// ParseSync validates the sync knob. Empty means "follow the mode's default".
//
// Like ParseMode this rejects unknown values instead of defaulting, because a
// silently-ignored knob reads as working while doing nothing.
func ParseSync(raw string) (Sync, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "":
		return SyncAuto, nil
	case "on", "true", "1", "yes":
		return SyncOn, nil
	case "off", "false", "0", "no":
		return SyncOff, nil
	default:
		return "", fmt.Errorf("unknown %s %q (valid: on, off)", syncKey, raw)
	}
}

// Blocks resolves the knob against a mode's default.
func (s Sync) Blocks(m Mode) bool {
	if !m.UsesLLM() {
		return false
	}
	switch s {
	case SyncOn:
		return true
	case SyncOff:
		return false
	default:
		return m.OrDefault() == ModePro
	}
}

// Describe renders the resolved knob for logs and status.
func (s Sync) Describe(m Mode) string {
	if !m.UsesLLM() {
		return "none (lite makes no extraction call)"
	}
	if s.Blocks(m) {
		return "blocking (the write waits for extraction)"
	}
	return "background (the write returns immediately)"
}
