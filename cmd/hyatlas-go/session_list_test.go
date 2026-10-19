package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The plugin sends session_id at the top level of /api/v1/add. handleAdd used to
// decode it and drop it, so /api/v1/list showed an empty session_id for every
// plugin write.
func TestAddStoresSessionIDForList(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	h := srv.routes()

	post := func(body string) {
		t.Helper()
		w := httptest.NewRecorder()
		h.ServeHTTP(w, localRequest("POST", "/api/v1/add", strings.NewReader(body)))
		if w.Code != http.StatusOK {
			t.Fatalf("add %s: status %d: %s", body, w.Code, w.Body.String())
		}
	}
	post(`{"text":"from plugin","user_id":"u","agent_id":"a","session_id":"sess-1"}`)
	// An explicit metadata.session_id is kept, not replaced by the top-level one.
	post(`{"text":"explicit meta","user_id":"u","agent_id":"a","session_id":"sess-2",` +
		`"metadata":{"session_id":"meta-wins"}}`)
	post(`{"text":"no session","user_id":"u","agent_id":"a"}`)

	w := httptest.NewRecorder()
	h.ServeHTTP(w, localRequest("GET", "/api/v1/list?layer=l2_raw", nil))
	var out struct {
		Memories []map[string]any `json:"memories"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, m := range out.Memories {
		got[m["content"].(string)] = m["session_id"].(string)
	}
	want := map[string]string{
		"from plugin":   "sess-1",
		"explicit meta": "meta-wins",
		"no session":    "",
	}
	for content, sid := range want {
		if got[content] != sid {
			t.Errorf("list session_id for %q = %q, want %q", content, got[content], sid)
		}
	}
}
