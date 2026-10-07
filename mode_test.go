package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
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
	if m.Sync() {
		t.Error("zero value is synchronous; it silently behaves like pro")
	}
	if m.LayersActive() != 7 {
		t.Errorf("LayersActive() = %d, want 7", m.LayersActive())
	}
}

func TestModeSemantics(t *testing.T) {
	cases := []struct {
		mode       Mode
		usesLLM    bool
		sync       bool
		layers     int
		describHas string
	}{
		{ModeLite, false, false, 1, "no LLM extraction"},
		{ModePro, true, true, 7, "synchronous"},
		{ModeUltra, true, false, 7, "background"},
	}
	for _, c := range cases {
		if got := c.mode.UsesLLM(); got != c.usesLLM {
			t.Errorf("%s UsesLLM() = %v, want %v", c.mode, got, c.usesLLM)
		}
		if got := c.mode.Sync(); got != c.sync {
			t.Errorf("%s Sync() = %v, want %v", c.mode, got, c.sync)
		}
		if got := c.mode.LayersActive(); got != c.layers {
			t.Errorf("%s LayersActive() = %d, want %d", c.mode, got, c.layers)
		}
		if d := c.mode.Describe(); !strings.Contains(d, c.describHas) {
			t.Errorf("%s Describe() = %q, want it to mention %q", c.mode, d, c.describHas)
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
		<-release // hold the LLM so we can prove the handler did not wait
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

// No LLM client configured: report unavailable rather than silently skipping.
func TestExtractForModeWithoutLLMClient(t *testing.T) {
	srv := newTestServer(t, "test-model", "http://127.0.0.1:1/v1")
	srv.mode = ModeUltra
	srv.llm = nil

	if got := srv.extractForMode("text", "u", "a", "id1"); got != "unavailable" {
		t.Errorf("extraction_status = %q, want \"unavailable\"", got)
	}
}

// /api/v1/status must report the configured mode, and must not claim llm=ok in
// lite where no LLM call is ever made.
func TestStatusReportsMode(t *testing.T) {
	for _, c := range []struct {
		mode    Mode
		wantLLM string
	}{
		{ModeLite, "unused"},
		{ModePro, "ok"},
		{ModeUltra, "ok"},
	} {
		srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
		srv.mode = c.mode

		w := httptest.NewRecorder()
		srv.handleStatus(w, httptest.NewRequest("GET", "/api/v1/status", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("%s: status code %d", c.mode, w.Code)
		}
		var st map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &st); err != nil {
			t.Fatalf("%s: %v", c.mode, err)
		}
		if got := st["mode"]; got != string(c.mode) {
			t.Errorf("%s: mode = %v, want %q", c.mode, got, c.mode)
		}
		if got := st["llm"]; got != c.wantLLM {
			t.Errorf("%s: llm = %v, want %q", c.mode, got, c.wantLLM)
		}
		wantUses := c.mode != ModeLite
		if got := st["uses_llm"]; got != wantUses {
			t.Errorf("%s: uses_llm = %v, want %v", c.mode, got, wantUses)
		}
		if d, _ := st["mode_detail"].(string); !strings.Contains(d, string(c.mode)) {
			t.Errorf("%s: mode_detail = %q, want it to name the mode", c.mode, d)
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
