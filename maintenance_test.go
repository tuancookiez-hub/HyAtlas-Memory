package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

func TestCompactTurnText(t *testing.T) {
	cases := []struct{ name, in, want string }{
		{"drops tool output and empty messages",
			"USER: hi\n\nASSISTANT: \n\nTOOL: big dump\n\nmore of the dump\n\nASSISTANT: done",
			"USER: hi\n\nASSISTANT: done"},
		{"drops the compaction summary",
			"USER: [CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted\n\nUSER: q\n\nASSISTANT: a",
			"USER: q\n\nASSISTANT: a"},
		{"unlabelled text is only capped", "a plain memory-tool note", "a plain memory-tool note"},
		{"only tool output leaves a note", "TOOL: x\n\nSYSTEM: y",
			"[turn compacted: it held only tool output or a context summary]"},
	}
	for _, c := range cases {
		if got := compactTurnText(c.in, 4000, 12000); got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
	long := compactTurnText("USER: "+strings.Repeat("x", 10000)+"\n\nASSISTANT: "+strings.Repeat("y", 10000), 100, 150)
	if len(long) > 160 {
		t.Errorf("capped turn is %d bytes, want about 150", len(long))
	}
}

func postJSON(t *testing.T, h func(w *httptest.ResponseRecorder)) map[string]any {
	t.Helper()
	w := httptest.NewRecorder()
	h(w)
	if w.Code != 200 {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestCompactRawDryRunThenApply(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	bloated := "USER: the question\n\nTOOL: " + strings.Repeat("dump ", 5000) + "\n\nASSISTANT: the answer"
	bigID, smallID := newID(), newID()
	meta := func() map[string]string {
		return map[string]string{"user_id": "alice", "agent_id": "a1", "session_id": "s9", "extracted": "false"}
	}
	if err := srv.store.Add(memory.L2Raw, bigID, bloated, meta()); err != nil {
		t.Fatal(err)
	}
	if err := srv.store.Add(memory.L2Raw, smallID, "USER: short\n\nASSISTANT: fine", meta()); err != nil {
		t.Fatal(err)
	}
	call := func(body string) map[string]any {
		return postJSON(t, func(w *httptest.ResponseRecorder) {
			srv.handleCompactRaw(w, httptest.NewRequest("POST", "/api/v1/admin/compact_raw", strings.NewReader(body)))
		})
	}

	// Extraction flips the index flag only (not the metadata); a rewrite must keep it.
	if err := srv.store.SetExtracted(bigID, true); err != nil {
		t.Fatal(err)
	}
	dry := call(`{}`)
	if dry["dry_run"] != true || dry["rows_changed"] != float64(1) {
		t.Fatalf("dry run = %v, want dry_run true and 1 row to change", dry)
	}
	if got := srv.store.GetMany([]string{bigID}); got[0].Content != bloated {
		t.Fatal("a dry run changed the row")
	}

	applied := call(`{"dry_run": false}`)
	if applied["rows_rewritten"] != float64(1) {
		t.Fatalf("apply = %v, want 1 row rewritten", applied)
	}
	got := srv.store.GetMany([]string{bigID, smallID})
	byID := map[string]DocIndex{got[0].ID: got[0], got[1].ID: got[1]}
	if c := byID[bigID].Content; c != "USER: the question\n\nASSISTANT: the answer" {
		t.Errorf("compacted content = %q", c)
	}
	if m := byID[bigID].Meta; m["user_id"] != "alice" || m["session_id"] != "s9" || !byID[bigID].Extracted {
		t.Errorf("metadata not kept: %v (extracted %v)", m, byID[bigID].Extracted)
	}
	if byID[smallID].Content != "USER: short\n\nASSISTANT: fine" {
		t.Error("a row that needed no change was rewritten")
	}
}

func TestDedupeFactsKeepsNewestPerOwner(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	add := func(user, ts, text string) string {
		id := newID()
		if err := srv.store.Add(memory.L3Fact, id, text, map[string]string{
			"user_id": user, "agent_id": "a1", "ts": ts, "source_id": "src-" + id}); err != nil {
			t.Fatal(err)
		}
		return id
	}
	older := add("alice", "2026-01-01T00:00:00Z", "alice prefers dark mode")
	newer := add("alice", "2026-02-01T00:00:00Z", "alice prefers dark mode")
	bobs := add("bob", "2026-03-01T00:00:00Z", "alice prefers dark mode")
	call := func(body string) map[string]any {
		return postJSON(t, func(w *httptest.ResponseRecorder) {
			srv.handleDedupeFacts(w, httptest.NewRequest("POST", "/api/v1/admin/dedupe_facts", strings.NewReader(body)))
		})
	}

	dry := call(`{"threshold": 0.999}`)
	if dry["duplicates"] != float64(1) {
		t.Fatalf("dry run = %v, want exactly 1 duplicate (bob's copy is another owner)", dry)
	}
	if live, _ := srv.store.List(memory.L3Fact, "alice", "a1", 10, 0, false); len(live) != 2 {
		t.Fatalf("a dry run changed the store: %d live", len(live))
	}

	call(`{"threshold": 0.999, "dry_run": false}`)
	live, _ := srv.store.List(memory.L3Fact, "alice", "a1", 10, 0, false)
	if len(live) != 1 || live[0].ID != newer {
		t.Fatalf("alice live = %v, want only the newer fact %s", live, newer)
	}
	all, _ := srv.store.ListAll(memory.L3Fact, "alice", "a1", 10, 0, false)
	for _, d := range all {
		if d.ID == older && d.Meta["superseded_by"] != newer {
			t.Errorf("older fact superseded_by = %q, want %q", d.Meta["superseded_by"], newer)
		}
	}
	if b, _ := srv.store.List(memory.L3Fact, "bob", "a1", 10, 0, false); len(b) != 1 || b[0].ID != bobs {
		t.Error("bob's fact was touched")
	}
}

func TestMaintenanceRequiresPost(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	w := httptest.NewRecorder()
	srv.handleCompactRaw(w, httptest.NewRequest("GET", "/api/v1/admin/compact_raw", nil))
	if w.Code != 405 {
		t.Errorf("GET compact_raw = %d, want 405", w.Code)
	}
}
