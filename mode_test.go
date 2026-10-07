package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// The mode selector is a privacy boundary, not a cosmetic setting: lite is the
// only mode where conversation text never leaves the machine. These tests pin
// both the parsing and the behaviour, because a mode that parses but does not
// change what the server does would be worse than no selector at all.

func TestParseMode(t *testing.T) {
	cases := []struct {
		in      string
		want    Mode
		wantErr bool
	}{
		{"", ModeUltra, false},       // unset -> documented default
		{"lite", ModeLite, false},    //
		{"LITE", ModeLite, false},    // case-insensitive
		{"  pro  ", ModePro, false},  // whitespace trimmed
		{"Pro", ModePro, false},      //
		{"ultra", ModeUltra, false},  //
		{"Ultra", ModeUltra, false},  //
		{"turbo", "", true},          // typo must not silently fall back
		{"lite ", ModeLite, false},   //
		{"ULTRA ", ModeUltra, false}, //
		{"system1", "", true},        // v3.5-era naming is not a mode
		{"0", "", true},              //
		{"true", "", true},           //
	}
	for _, c := range cases {
		got, err := ParseMode(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("ParseMode(%q) = %q, want an error", c.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("ParseMode(%q) unexpected error: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("ParseMode(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// A rejected mode must name the valid set, or the user cannot fix their typo.
func TestParseModeErrorListsValidModes(t *testing.T) {
	_, err := ParseMode("turbo")
	if err == nil {
		t.Fatal("expected an error for an unknown mode")
	}
	for _, want := range []string{"turbo", "lite", "pro", "ultra"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q does not mention %q", err.Error(), want)
		}
	}
}

// The zero value must behave as ultra AND report "ultra", never "". A Server
// literal built without the mode field would otherwise emit an unnamed mode.
func TestModeZeroValueIsUltra(t *testing.T) {
	var m Mode
	if m.OrDefault() != ModeUltra {
		t.Errorf("OrDefault() = %q, want ultra", m.OrDefault())
	}
	if !m.UsesLLM() {
		t.Error("zero value disabled the LLM; it silently behaves like lite")
	}
	// The zero value IS ultra, so it must do everything ultra does — including
	// the slow path — and must not block on extraction.
	if SyncAuto.Blocks(m) {
		t.Error("zero value blocks on extraction; it silently behaves like pro")
	}
	if !m.Consolidates() {
		t.Error("zero value skipped the slow path; it no longer behaves like ultra")
	}
	if m.Rank() != 2 {
		t.Errorf("Rank() = %d, want 2 (ultra)", m.Rank())
	}
	if m.Describe() != ModeUltra.Describe() {
		t.Errorf("zero value Describe()=%q, want the ultra spelling %q", m.Describe(), ModeUltra.Describe())
	}
	if m.LayersActive() != 7 {
		t.Errorf("LayersActive() = %d, want 7", m.LayersActive())
	}
}

func TestModeSemantics(t *testing.T) {
	cases := []struct {
		mode        Mode
		usesLLM     bool
		consolidate bool
		layers      int
		rank        int
		blocksByDef bool
		describHas  string
	}{
		{ModeLite, false, false, 1, 0, false, "no LLM call"},
		{ModePro, true, false, 5, 1, true, "no slow path"},
		{ModeUltra, true, true, 7, 2, false, "slow path"},
	}
	for _, c := range cases {
		if got := c.mode.UsesLLM(); got != c.usesLLM {
			t.Errorf("%s UsesLLM() = %v, want %v", c.mode, got, c.usesLLM)
		}
		if got := c.mode.Consolidates(); got != c.consolidate {
			t.Errorf("%s Consolidates() = %v, want %v", c.mode, got, c.consolidate)
		}
		if got := c.mode.LayersActive(); got != c.layers {
			t.Errorf("%s LayersActive() = %d, want %d", c.mode, got, c.layers)
		}
		if got := c.mode.Rank(); got != c.rank {
			t.Errorf("%s Rank() = %d, want %d", c.mode, got, c.rank)
		}
		if got := SyncAuto.Blocks(c.mode); got != c.blocksByDef {
			t.Errorf("%s default Blocks() = %v, want %v", c.mode, got, c.blocksByDef)
		}
		if d := c.mode.Describe(); !strings.Contains(d, c.describHas) {
			t.Errorf("%s Describe() = %q, want it to mention %q", c.mode, d, c.describHas)
		}
	}
}

// The modes must form a real ladder: each tier does everything the one below it
// does, plus more. Without this the tiers are just three unrelated settings and
// "ultra is better than pro" is marketing rather than a property.
func TestModesFormAMonotonicLadder(t *testing.T) {
	for i := 1; i < len(validModes); i++ {
		lo, hi := validModes[i-1], validModes[i]
		if hi.Rank() <= lo.Rank() {
			t.Errorf("%s.Rank()=%d not above %s.Rank()=%d", hi, hi.Rank(), lo, lo.Rank())
		}
		if hi.LayersActive() < lo.LayersActive() {
			t.Errorf("%s populates fewer layers (%d) than %s (%d)",
				hi, hi.LayersActive(), lo, lo.LayersActive())
		}
		// Capability must be monotonic: anything the lower tier does, the higher
		// one does too. Lite makes no LLM call; the two above it both do.
		if lo.UsesLLM() && !hi.UsesLLM() {
			t.Errorf("%s uses an LLM but %s does not — capability regresses", lo, hi)
		}
		if lo.Consolidates() && !hi.Consolidates() {
			t.Errorf("%s consolidates but %s does not — capability regresses", lo, hi)
		}
	}
	// Ultra must be strictly more capable than pro, not merely different.
	if ModeUltra.Consolidates() == ModePro.Consolidates() {
		t.Error("ultra and pro both do or both skip consolidation; they are then the same tier")
	}
}

// The sync knob is orthogonal to the mode: any mode that extracts can be forced
// to block or not block, so pro and ultra differ in capability rather than in
// latency alone.
func TestSyncKnobIsIndependentOfMode(t *testing.T) {
	cases := []struct {
		sync Sync
		mode Mode
		want bool
	}{
		{SyncAuto, ModeLite, false},  // never blocks: nothing to wait for
		{SyncOn, ModeLite, false},    // forcing on cannot make lite call an LLM
		{SyncAuto, ModePro, true},    // pro defaults to blocking
		{SyncOff, ModePro, false},    // but can be made background
		{SyncAuto, ModeUltra, false}, // ultra defaults to background
		{SyncOn, ModeUltra, true},    // but can be made blocking
	}
	for _, c := range cases {
		if got := c.sync.Blocks(c.mode); got != c.want {
			t.Errorf("sync=%q mode=%s Blocks() = %v, want %v", c.sync, c.mode, got, c.want)
		}
	}
}

// Forcing ultra to block must not silently disable consolidation — that would
// turn ultra into pro and misreport what the server does.
func TestSyncKnobDoesNotChangeCapability(t *testing.T) {
	for _, s := range []Sync{SyncAuto, SyncOn, SyncOff} {
		if !ModeUltra.Consolidates() {
			t.Errorf("ultra stopped consolidating under sync=%q", s)
		}
		if ModePro.Consolidates() {
			t.Errorf("pro gained consolidation under sync=%q", s)
		}
	}
}

func TestParseSync(t *testing.T) {
	for in, want := range map[string]Sync{
		"": SyncAuto, "on": SyncOn, "ON": SyncOn, "true": SyncOn, "1": SyncOn, "yes": SyncOn,
		"  off ": SyncOff, "false": SyncOff, "0": SyncOff, "no": SyncOff,
	} {
		got, err := ParseSync(in)
		if err != nil {
			t.Errorf("ParseSync(%q) unexpected error: %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("ParseSync(%q) = %q, want %q", in, got, want)
		}
	}
	for _, bad := range []string{"maybe", "sync", "2", "on/off"} {
		if _, err := ParseSync(bad); err == nil {
			t.Errorf("ParseSync(%q) should reject an unknown value", bad)
		}
	}
}

// Describe must reflect the resolved combination, so logs and status cannot
// claim a write blocks when it does not.
func TestSyncDescribeMatchesBlocks(t *testing.T) {
	for _, m := range validModes {
		for _, s := range []Sync{SyncAuto, SyncOn, SyncOff} {
			d, b := s.Describe(m), s.Blocks(m)
			if b && !strings.Contains(d, "blocking") {
				t.Errorf("mode=%s sync=%q blocks but Describe()=%q", m, s, d)
			}
			if !b && strings.Contains(d, "blocking") {
				t.Errorf("mode=%s sync=%q does not block but Describe()=%q", m, s, d)
			}
		}
	}
}

// Lite is the privacy guarantee: no LLM call, so no text leaves the machine.
func TestExtractForModeLiteMakesNoLLMCall(t *testing.T) {
	calls := 0
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{}"}}]}`))
	}))
	defer mock.Close()

	srv := newTestServer(t, "test-model", mock.URL)
	srv.mode = ModeLite
	srv.llm = NewLLMClient(mock.URL, "k", "test-model")

	if got := srv.extractForMode("secret conversation text", "u", "a", "id1"); got != "skipped" {
		t.Errorf("extraction_status = %q, want \"skipped\"", got)
	}
	if calls != 0 {
		t.Errorf("lite mode made %d LLM call(s); conversation text left the machine", calls)
	}
}

// Ultra extracts on a background goroutine and answers immediately.
func TestExtractForModeUltraIsAsync(t *testing.T) {
	release := make(chan struct{})
	called := make(chan struct{}, 1)
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called <- struct{}{}
		// Hold the LLM open so a synchronous call would be observably slow.
		// Capped: if a regression makes this mode block, the handler returns
		// instead of deadlocking the whole test binary until the package
		// timeout (180s), so the failure is fast and names the real problem.
		select {
		case <-release:
		case <-time.After(3 * time.Second):
			http.Error(w, "held too long", http.StatusGatewayTimeout)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{}"}}]}`))
	}))
	defer mock.Close()
	defer close(release)

	srv := newTestServer(t, "test-model", mock.URL)
	srv.mode = ModeUltra
	srv.llm = NewLLMClient(mock.URL, "k", "test-model")

	start := time.Now()
	status := srv.extractForMode("text", "u", "a", "id1")
	elapsed := time.Since(start)

	if status != "pending" {
		t.Errorf("extraction_status = %q, want \"pending\"", status)
	}
	if elapsed > 500*time.Millisecond {
		t.Errorf("ultra blocked the caller for %v; extraction should be backgrounded", elapsed)
	}
	if status == "failed" && elapsed > 2*time.Second {
		t.Error("extraction ran inline and waited on the LLM; ultra must not block the write")
	}
	select {
	case <-called:
	case <-time.After(2 * time.Second):
		t.Error("background extraction never called the LLM")
	}
}

// Pro blocks until extraction finishes and reports the outcome, so the response
// tells the truth instead of leaving the caller to poll.
func TestExtractForModeProIsSynchronous(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"facts\":[],\"summary\":{\"text\":\"s\"},\"knowledge\":[],\"schemas\":[],\"intention\":null}"}}]}`))
	}))
	defer mock.Close()

	srv := newTestServer(t, "test-model", mock.URL)
	srv.mode = ModePro
	srv.llm = NewLLMClient(mock.URL, "k", "test-model")

	if got := srv.extractForMode("text", "u", "a", "id1"); got != "done" {
		t.Errorf("extraction_status = %q, want \"done\"", got)
	}
}

// A failing synchronous extraction must report failed, not pretend it worked.
func TestExtractForModeProReportsFailure(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer mock.Close()

	srv := newTestServer(t, "test-model", mock.URL)
	srv.mode = ModePro
	srv.llm = NewLLMClient(mock.URL, "k", "test-model")

	if got := srv.extractForMode("text", "u", "a", "id1"); got != "failed" {
		t.Errorf("extraction_status = %q, want \"failed\"", got)
	}
	if srv.extractErr() == "" {
		t.Error("failure was not recorded for /api/v1/status to surface")
	}
}

// A mode that needs an LLM but cannot make the call must say so in a way that
// names the problem. The old spelling, "unavailable", did not distinguish "no
// client at all" from "client present, no credential" — and the latter is the
// state a fresh install lands in, so the message could not tell the user what to
// do about it.
func TestExtractForModeReportsUnconfigured(t *testing.T) {
	// No client at all.
	srv := newTestServer(t, "test-model", "http://127.0.0.1:1/v1")
	srv.mode = ModeUltra
	srv.llm = nil
	if got := srv.extractForMode("text", "u", "a", "id1"); got != "unconfigured" {
		t.Errorf("extraction_status = %q, want \"unconfigured\"", got)
	}

	// Client present but no credential — the fresh-install case.
	srv2 := newTestServer(t, "test-model", "http://127.0.0.1:1/v1")
	srv2.mode = ModePro
	srv2.llm = NewLLMClient("http://127.0.0.1:1/v1", "", "test-model")
	if got := srv2.extractForMode("text", "u", "a", "id2"); got != "unconfigured" {
		t.Errorf("with an empty key extraction_status = %q, want \"unconfigured\"", got)
	}

	// A client WITH a credential must not report unconfigured (it will fail on
	// the unreachable endpoint instead, which is a different and honest state).
	srv3 := newTestServer(t, "test-model", "http://127.0.0.1:1/v1")
	srv3.mode = ModePro
	srv3.llm = NewLLMClient("http://127.0.0.1:1/v1", "k", "test-model")
	if got := srv3.extractForMode("text", "u", "a", "id3"); got == "unconfigured" {
		t.Error("a configured key reported \"unconfigured\"")
	}

	// Lite never calls an LLM, so the question does not arise.
	srv4 := newTestServer(t, "test-model", "http://127.0.0.1:1/v1")
	srv4.mode = ModeLite
	srv4.llm = nil
	if got := srv4.extractForMode("text", "u", "a", "id4"); got != "skipped" {
		t.Errorf("lite extraction_status = %q, want \"skipped\"", got)
	}
}

// /api/v1/status must report the configured mode, and must not claim llm=ok in
// lite where no LLM call is ever made.
// /api/v1/status must report the configured mode, and must be honest about the
// LLM: lite never calls one, and a mode that needs one but has no credential
// must not claim "ok". That false-healthy state is what made a fresh install
// look fine and then fail every write.
func TestStatusReportsMode(t *testing.T) {
	for _, c := range []struct {
		name    string
		mode    Mode
		key     string // credential handed to the LLM client; "" means no client
		wantLLM string
	}{
		{"lite ignores the LLM entirely", ModeLite, "", "unused"},
		{"lite with a key still does not use it", ModeLite, "k", "unused"},
		{"pro with no client", ModePro, "", "unconfigured"},
		{"ultra with no client", ModeUltra, "", "unconfigured"},
		{"pro with a credential", ModePro, "k", "ok"},
		{"ultra with a credential", ModeUltra, "k", "ok"},
	} {
		srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
		srv.mode = c.mode
		if c.key != "" {
			srv.llm = NewLLMClient("http://127.0.0.1:1/v1", c.key, "m")
		}

		w := httptest.NewRecorder()
		srv.handleStatus(w, httptest.NewRequest("GET", "/api/v1/status", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("%s: status code %d", c.name, w.Code)
		}
		var st map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &st); err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		if got := st["mode"]; got != string(c.mode) {
			t.Errorf("%s: mode = %v, want %q", c.name, got, c.mode)
		}
		if got := st["llm"]; got != c.wantLLM {
			t.Errorf("%s: llm = %v, want %q", c.name, got, c.wantLLM)
		}
		wantUses := c.mode != ModeLite
		if got := st["uses_llm"]; got != wantUses {
			t.Errorf("%s: uses_llm = %v, want %v", c.name, got, wantUses)
		}
		if d, _ := st["mode_detail"].(string); !strings.Contains(d, string(c.mode)) {
			t.Errorf("%s: mode_detail = %q, want it to name the mode", c.name, d)
		}
	}
}

// The startup warning is the only thing that reaches a user who never reads
// status: it must fire exactly when a mode needs an LLM and has no credential,
// and must not fire in lite or when one is configured.
func TestStartupWarningFiresOnlyWhenUnconfigured(t *testing.T) {
	base := runtimeCfg{LLMBase: "http://api/v1", LLMModel: "m"}

	cases := []struct {
		name    string
		mode    Mode
		key     string
		keyFile string
		want    bool
	}{
		{"lite never warns", ModeLite, "", "", false},
		{"pro without a key warns", ModePro, "", "", true},
		{"ultra without a key warns", ModeUltra, "", "", true},
		{"pro with a key is quiet", ModePro, "k", "", false},
		{"ultra with a key file is quiet", ModeUltra, "", "/path/auth.json", false},
	}
	for _, c := range cases {
		t.Setenv("HYATLAS_LLM_KEY", c.key)
		t.Setenv("HYATLAS_LLM_KEY_FILE", c.keyFile)
		rt := base
		rt.Mode = c.mode
		w := startupWarning(rt)
		if got := w != ""; got != c.want {
			t.Errorf("%s: warning present = %v, want %v (got %q)", c.name, got, c.want, w)
		}
		if c.want {
			// The warning must be actionable: name the mode, the variables, and the
			// offline escape hatch.
			for _, need := range []string{string(c.mode), "HYATLAS_LLM_BASE",
				"HYATLAS_LLM_KEY", "HYATLAS_MODE=lite", "hermes memory setup"} {
				if !strings.Contains(w, need) {
					t.Errorf("%s: warning does not mention %q", c.name, need)
				}
			}
		}
	}
}

// The dashboard's mode must be the configured one, not the hardcoded "ultra" it
// used to report regardless of configuration.
func TestDashInfoReportsConfiguredMode(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	srv.mode = ModeLite

	w := httptest.NewRecorder()
	srv.handleDashInfo(w, httptest.NewRequest("GET", "/api/info", nil))

	var got map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got["mode"] != "lite" {
		t.Errorf("dashboard mode = %v, want \"lite\" (it used to be hardcoded ultra)", got["mode"])
	}
}

// HYATLAS_MODE must reach the resolved runtime config.
func TestResolveRuntimeReadsMode(t *testing.T) {
	for _, v := range []string{"lite", "pro", "ultra"} {
		t.Setenv("HYATLAS_MODE", v)
		if got := resolveRuntime().Mode; string(got) != v {
			t.Errorf("HYATLAS_MODE=%s resolved to %q", v, got)
		}
	}
	t.Setenv("HYATLAS_MODE", "")
	if got := resolveRuntime().Mode; got != ModeUltra {
		t.Errorf("unset HYATLAS_MODE resolved to %q, want ultra", got)
	}
}

// The startup banner must name the mode, so an operator can see at a glance
// whether their server is sending text to an LLM.
func TestListeningLineReportsMode(t *testing.T) {
	for _, v := range []Mode{ModeLite, ModePro, ModeUltra} {
		os.Setenv("HYATLAS_MODE", string(v))
		line := listeningLine(resolveRuntime())
		if !strings.Contains(line, "mode="+string(v)) {
			t.Errorf("%s: banner %q does not name the mode", v, line)
		}
	}
	os.Unsetenv("HYATLAS_MODE")
}

// Reprocessing in lite must not attempt extraction.
func TestReprocessSkippedInLiteMode(t *testing.T) {
	calls := 0
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{}"}}]}`))
	}))
	defer mock.Close()

	srv := newTestServer(t, "test-model", mock.URL)
	srv.mode = ModeLite
	srv.llm = NewLLMClient(mock.URL, "k", "test-model")

	w := httptest.NewRecorder()
	srv.handleReprocess(w, httptest.NewRequest("POST", "/api/v1/reprocess",
		strings.NewReader(`{"max":5}`)))

	var got map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if calls != 0 {
		t.Errorf("lite reprocess made %d LLM call(s)", calls)
	}
	if note, _ := got["note"].(string); !strings.Contains(note, "lite") {
		t.Errorf("reprocess note = %q, want it to explain why nothing ran", note)
	}
}

// The plugin (Python) and the server (Go) both validate this setting. They must
// agree exactly: a value the Desktop form permits must not be fatal on server
// boot, and one it rejects must not silently work on the server. This table is
// the shared contract — mirror any change in plugins/hyatlas/settings.py.
func TestParseSyncParityWithPlugin(t *testing.T) {
	cases := []struct {
		in   string
		want Sync // "" means "must error"
		err  bool
	}{
		{"", SyncAuto, false},
		{"on", SyncOn, false},
		{"ON", SyncOn, false},
		{"true", SyncOn, false},
		{"1", SyncOn, false},
		{"yes", SyncOn, false},
		{"off", SyncOff, false},
		{"false", SyncOff, false},
		{"0", SyncOff, false},
		{"no", SyncOff, false},
		{"maybe", "", true},
		{"2", "", true},
		{"on/off", "", true},
	}
	for _, c := range cases {
		got, err := ParseSync(c.in)
		if c.err {
			if err == nil {
				t.Errorf("ParseSync(%q) = %q, want an error (the plugin rejects this too)", c.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("ParseSync(%q) unexpected error: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("ParseSync(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// The two systems must partition the layer model exactly. L5 knowledge and L6
// schemas are System2 products: a relation worth keeping is corroborated by more
// than one turn, and a schema is a recurring pattern, so neither can come from a
// single turn. If this partition drifts, pro and ultra look identical again.
func TestSystemPartitionCoversEveryLayerExactlyOnce(t *testing.T) {
	s1, s2 := System1Layers(), System2Layers()

	seen := map[memory.Layer]int{}
	for _, l := range s1 {
		seen[l]++
	}
	for _, l := range s2 {
		seen[l]++
	}

	for _, l := range memory.All() {
		if seen[l] != 1 {
			t.Errorf("layer %s is owned by %d system(s), want exactly 1", l, seen[l])
		}
	}
	for _, l := range s2 {
		if l != memory.L5Knowledge && l != memory.L6Schema {
			t.Errorf("System2 owns %s; it should own only L5 and L6", l)
		}
	}
	if len(s1)+len(s2) != len(memory.All()) {
		t.Errorf("partition covers %d layers, model has %d", len(s1)+len(s2), len(memory.All()))
	}
}

// Each tier must fill exactly the layers its systems own.
func TestLayersActiveMatchesSystemOwnership(t *testing.T) {
	if got := ModeLite.LayersActive(); got != 1 {
		t.Errorf("lite fills %d layers, want 1 (L2 raw only)", got)
	}
	if got, want := ModePro.LayersActive(), len(System1Layers()); got != want {
		t.Errorf("pro fills %d layers, want %d (System1 only)", got, want)
	}
	if got, want := ModeUltra.LayersActive(), len(memory.All()); got != want {
		t.Errorf("ultra fills %d layers, want %d (System1 + System2)", got, want)
	}
}
