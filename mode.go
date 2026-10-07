package main

import (
	"fmt"
	"strings"
	"time"
)

// Mode selects how much work a write does. All three run the same store and the
// same local embeddings; they differ in whether an extraction LLM is called, and
// whether the caller waits for it.
//
//	lite  — raw + embeddings only. No LLM call, so nothing leaves the machine.
//	pro   — one synchronous LLM extraction; the write blocks until it returns.
//	ultra — the same extraction on a background goroutine (default).
type Mode string

const (
	ModeLite  Mode = "lite"
	ModePro   Mode = "pro"
	ModeUltra Mode = "ultra"

	// defaultMode is what a server with no HYATLAS_MODE set runs.
	defaultMode = ModeUltra

	// extractTimeout bounds one extraction LLM call. Shared by the synchronous
	// (pro) and background (ultra) paths so the two cannot drift apart.
	extractTimeout = 180 * time.Second
)

// validModes is the accepted set, in the order used by help text.
var validModes = []Mode{ModeLite, ModePro, ModeUltra}

// ParseMode validates HYATLAS_MODE. Empty means the default.
//
// An unrecognised value is an error rather than a silent fallback: a user who
// typos "lite" and quietly gets ultra would have their conversation text sent to
// an extraction LLM they believed they had turned off.
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
			return m
		}
	}
	return defaultMode
}

// UsesLLM reports whether this mode calls an extraction LLM at all. Lite does
// not, which is the only mode where conversation text never leaves the machine.
func (m Mode) UsesLLM() bool { return m.OrDefault() != ModeLite }

// Sync reports whether extraction blocks the write request.
func (m Mode) Sync() bool { return m.OrDefault() == ModePro }

// Describe is the startup-log and status spelling of a mode.
func (m Mode) Describe() string {
	switch m.OrDefault() {
	case ModeLite:
		return "lite (raw + local embeddings, no LLM extraction)"
	case ModePro:
		return "pro (synchronous LLM extraction, no background worker)"
	default:
		return "ultra (background LLM extraction, all 7 layers)"
	}
}

// LayersActive is the count of the 7-layer model this mode populates. Lite stops
// at the raw trace; the other two fill all seven through extraction.
func (m Mode) LayersActive() int {
	if m.OrDefault() == ModeLite {
		return 1
	}
	return 7
}
