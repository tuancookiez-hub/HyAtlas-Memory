package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// forgetReq posts one /api/v1/forget body through the server's handler and returns
// the status code and the decoded response.
func forgetReq(t *testing.T, srv *Server, body string) (int, map[string]any) {
	t.Helper()
	w := httptest.NewRecorder()
	srv.handleForget(w, httptest.NewRequest(http.MethodPost, "/api/v1/forget", strings.NewReader(body)))
	var resp map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	return w.Code, resp
}

// A memory-tool raw row, a mirror in L1 sourced from it, and a fact that lists it
// among its source_ids are all retired as forgotten, with the reason recorded.
func TestForgetRetiresRawAndDerivedRows(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	st := srv.store
	raw := newID()
	if err := st.Add(memory.L2Raw, raw, "prefers tea", map[string]string{
		"user_id": "u", "agent_id": "a", "write_origin": "memory_tool", "ts": nowTS(),
	}); err != nil {
		t.Fatal(err)
	}
	mirror := addRow(t, st, memory.L1Profile, "prefers tea", nowTS())
	if err := st.Add(memory.L3Fact, newID(), "fact that absorbed it", map[string]string{
		"user_id": "u", "agent_id": "a", "source_ids": "other," + raw, "ts": nowTS(),
	}); err != nil {
		t.Fatal(err)
	}
	// The mirror is sourced from the raw row, so its source_id names it.
	if err := st.Add(memory.L1Profile, newID(), "prefers tea too", map[string]string{
		"user_id": "u", "agent_id": "a", "source_id": raw, "ts": nowTS(),
	}); err != nil {
		t.Fatal(err)
	}
	code, resp := forgetReq(t, srv, `{"text":"prefers tea","user_id":"u","agent_id":"a","reason":"user changed it"}`)
	if code != 200 {
		t.Fatalf("status = %d, want 200", code)
	}
	if resp["forgotten"] != float64(1) || resp["derived"] != float64(2) {
		t.Fatalf("response = %v, want forgotten 1 and derived 2", resp)
	}
	if m := metaOf(t, st, raw); m[metaSupersededHow] != howForgotten || m[metaDropReason] != "user changed it" {
		t.Errorf("raw row meta = %v", m)
	}
	if m := metaOf(t, st, mirror); m[metaSupersededHow] == howForgotten {
		t.Errorf("mirror with no source link was forgotten: %v", m)
	}
	// Restore brings the forgotten rows back, as the admin restore does.
	if n, err := st.Restore([]string{raw}); err != nil || n != 1 {
		t.Fatalf("restore = %d, %v", n, err)
	}
	if isSuperseded(st.GetMany([]string{raw})[0]) {
		t.Error("restored raw row is still superseded")
	}
}

// Forgetting with the default reason records the Hermes memory tool reason.
func TestForgetDefaultReason(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	st := srv.store
	raw := newID()
	if err := st.Add(memory.L2Raw, raw, "likes green", map[string]string{
		"user_id": "u", "agent_id": "a", "write_origin": "memory_tool", "ts": nowTS(),
	}); err != nil {
		t.Fatal(err)
	}
	if code, _ := forgetReq(t, srv, `{"text":"likes green","user_id":"u","agent_id":"a"}`); code != 200 {
		t.Fatalf("status = %d", code)
	}
	if m := metaOf(t, st, raw); m[metaDropReason] != defaultForgetReason {
		t.Errorf("drop_reason = %q, want %q", m[metaDropReason], defaultForgetReason)
	}
}

// A raw row with the same text but no memory-tool origin, and another owner's
// identical memory-tool row, are both left live. Surrounding space in the request
// and the row is ignored.
func TestForgetLeavesOtherRowsAlone(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	st := srv.store
	ts := nowTS()
	forged := newID()
	if err := st.Add(memory.L2Raw, forged, "  owns a cat \n", map[string]string{
		"user_id": "u", "agent_id": "a", "write_origin": "memory_tool", "ts": ts,
	}); err != nil {
		t.Fatal(err)
	}
	noOrigin := newID()
	if err := st.Add(memory.L2Raw, noOrigin, "owns a cat", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": ts,
	}); err != nil {
		t.Fatal(err)
	}
	otherOwner := newID()
	if err := st.Add(memory.L2Raw, otherOwner, "owns a cat", map[string]string{
		"user_id": "v", "agent_id": "a", "write_origin": "memory_tool", "ts": ts,
	}); err != nil {
		t.Fatal(err)
	}
	code, resp := forgetReq(t, srv, `{"text":"owns a cat","user_id":"u","agent_id":"a"}`)
	if code != 200 || resp["forgotten"] != float64(1) || resp["derived"] != float64(0) {
		t.Fatalf("status %d, response %v", code, resp)
	}
	if isSuperseded(st.GetMany([]string{noOrigin})[0]) {
		t.Error("row without write_origin was forgotten")
	}
	if isSuperseded(st.GetMany([]string{otherOwner})[0]) {
		t.Error("another owner's row was forgotten")
	}
	if !isSuperseded(st.GetMany([]string{forged})[0]) {
		t.Error("matching memory-tool row was not forgotten")
	}
}

// Empty text is a 400, and any method other than POST is a 405.
func TestForgetRejectsBadRequests(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	if code, _ := forgetReq(t, srv, `{"text":"   "}`); code != 400 {
		t.Errorf("empty text: status = %d, want 400", code)
	}
	w := httptest.NewRecorder()
	srv.handleForget(w, httptest.NewRequest(http.MethodGet, "/api/v1/forget", nil))
	if w.Code != 405 {
		t.Errorf("GET: status = %d, want 405", w.Code)
	}
}
