package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// TestSetExtractedSurvivesRestart guards the doc-index persistence contract:
// SetExtracted must survive a store reopen. Before loadIndex existed, startup
// always rebuilt the index from chromem metadata — which never carries the
// extracted flag — silently resetting every doc to unextracted.
func TestSetExtractedSurvivesRestart(t *testing.T) {
	dir := t.TempDir()
	em := NewLocalEmbedder(384)
	graphPath := filepath.Join(dir, "graph.json")

	s1, err := NewMemoryStore(context.Background(), dir, em, graphPath)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	if err := s1.Add(memory.L2Raw, "doc-extract-1", "raw memory text", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": "2026-10-06T00:00:00Z",
	}); err != nil {
		t.Fatalf("add: %v", err)
	}
	if err := s1.SetExtracted("doc-extract-1", true); err != nil {
		t.Fatalf("set extracted: %v", err)
	}
	s1.Close()

	s2, err := NewMemoryStore(context.Background(), dir, em, graphPath)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	t.Cleanup(s2.Close)

	docs, _ := s2.List(memory.L2Raw, "", "", 10, 0, false)
	if len(docs) != 1 {
		t.Fatalf("reopened store: want 1 raw doc, got %d", len(docs))
	}
	if !docs[0].Extracted {
		t.Error("extracted flag did not survive restart: want true, got false")
	}
}

// TestLoadIndexFallsBackToRebuild checks the size-mismatch guard: a stale or
// corrupt doc_index.json must trigger a chromem rebuild, not serve a wrong index.
func TestLoadIndexFallsBackToRebuild(t *testing.T) {
	dir := t.TempDir()
	em := NewLocalEmbedder(384)
	graphPath := filepath.Join(dir, "graph.json")

	s1, err := NewMemoryStore(context.Background(), dir, em, graphPath)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	for i, id := range []string{"doc-a", "doc-b", "doc-c"} {
		if err := s1.Add(memory.L2Raw, id, "text "+id, map[string]string{
			"user_id": "u", "agent_id": "a", "ts": "2026-10-0" + string(rune('1'+i)) + "T00:00:00Z",
		}); err != nil {
			t.Fatalf("add %s: %v", id, err)
		}
	}
	s1.Close()

	// Corrupt the index: keep only one of three docs — size mismatch forces rebuild.
	if err := os.WriteFile(filepath.Join(dir, "doc_index.json"),
		[]byte(`{"doc-a":{"id":"doc-a","layer":"l2_raw","content":"stale"}}`), 0o644); err != nil {
		t.Fatalf("corrupt index: %v", err)
	}

	s2, err := NewMemoryStore(context.Background(), dir, em, graphPath)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	t.Cleanup(s2.Close)

	docs, total := s2.List(memory.L2Raw, "", "", 10, 0, false)
	if total != 3 || len(docs) != 3 {
		t.Errorf("stale index not rebuilt: want 3 docs, got %d (total %d)", len(docs), total)
	}
}

// TestDeleteAllGuard covers the unscoped-wipe guard and both scoping styles.
func TestDeleteAllGuard(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	for _, u := range []string{"keep-user", "drop-user"} {
		if err := srv.store.Add(memory.L2Raw, "doc-"+u, "text", map[string]string{
			"user_id": u, "agent_id": "a", "ts": "2026-10-06T00:00:00Z",
		}); err != nil {
			t.Fatalf("add: %v", err)
		}
	}

	// 1. Unscoped call → refused with 400, nothing deleted.
	w := httptest.NewRecorder()
	srv.handleDelete(w, httptest.NewRequest("POST", "/api/v1/delete_all", nil))
	if w.Code != http.StatusBadRequest {
		t.Errorf("unscoped delete: want 400, got %d", w.Code)
	}
	var refused map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &refused); err != nil {
		t.Fatal(err)
	}
	if refused["deleted_count"] != float64(0) {
		t.Errorf("unscoped delete deleted %v docs", refused["deleted_count"])
	}
	if srv.store.TotalMemories() != 2 {
		t.Errorf("store changed by refused delete: %d docs", srv.store.TotalMemories())
	}

	// 2. Scoped via JSON body (plugin client style) → deletes only that scope.
	w = httptest.NewRecorder()
	srv.handleDelete(w, httptest.NewRequest("POST", "/api/v1/delete_all",
		strings.NewReader(`{"user_id":"drop-user"}`)))
	if w.Code != http.StatusOK {
		t.Fatalf("body-scoped delete: want 200, got %d", w.Code)
	}
	var bodyRes map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &bodyRes); err != nil {
		t.Fatal(err)
	}
	if bodyRes["deleted_count"] != float64(1) {
		t.Errorf("body-scoped delete: want 1 deleted, got %v", bodyRes["deleted_count"])
	}

	// 3. Scoped via query param (curl style) → deletes that scope.
	w = httptest.NewRecorder()
	srv.handleDelete(w, httptest.NewRequest("POST", "/api/v1/delete_all?user_id=keep-user", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("query-scoped delete: want 200, got %d", w.Code)
	}
	if srv.store.TotalMemories() != 0 {
		t.Errorf("after both scoped deletes: want 0 docs, got %d", srv.store.TotalMemories())
	}

	// 4. Explicit wipe-all opt-in on an empty store → allowed, 200.
	w = httptest.NewRecorder()
	srv.handleDelete(w, httptest.NewRequest("POST", "/api/v1/delete_all?confirm=wipe-all", nil))
	if w.Code != http.StatusOK {
		t.Errorf("confirm=wipe-all: want 200, got %d", w.Code)
	}
}

// TestExtractErrConcurrent exercises the mutex around lastExtractErr. Run with
// -race to catch regressions: extraction goroutines write while status reads.
func TestExtractErrConcurrent(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(2)
		go func(n int) {
			defer wg.Done()
			srv.setExtractErr("err")
			srv.setExtractErr("")
		}(i)
		go func() {
			defer wg.Done()
			_ = srv.extractErr()
			w := httptest.NewRecorder()
			srv.handleStatus(w, httptest.NewRequest("GET", "/api/v1/status", nil))
		}()
	}
	wg.Wait()
}

// TestReprocessByIds covers the backfill contract: explicit ids are processed
// even when the extracted flag says otherwise (caller already chose the rows),
// and the response reports reprocessed/failed/skipped.
func TestReprocessByIds(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	var calls atomic.Int32
	mock := mockExtractionServer(t, &calls)
	defer mock.Close()
	srv.llm = NewLLMClient(mock.URL, "test-key", "mock-model")

	for _, id := range []string{"raw-1", "raw-2"} {
		if err := srv.store.Add(memory.L2Raw, id, "text "+id, map[string]string{
			"user_id": "u", "agent_id": "a", "ts": "2026-10-06T00:00:00Z",
		}); err != nil {
			t.Fatalf("add: %v", err)
		}
	}
	// Mark one extracted — with explicit ids it must still be reprocessed.
	if err := srv.store.SetExtracted("raw-1", true); err != nil {
		t.Fatalf("set extracted: %v", err)
	}

	w := httptest.NewRecorder()
	srv.handleReprocess(w, httptest.NewRequest("POST", "/api/v1/reprocess",
		strings.NewReader(`{"ids":["raw-1","raw-2"]}`)))
	if w.Code != http.StatusOK {
		t.Fatalf("reprocess: want 200, got %d", w.Code)
	}
	var res map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if res["reprocessed"] != float64(2) {
		t.Errorf("reprocessed: want 2 (ids bypass the extracted skip), got %v", res["reprocessed"])
	}
	if res["skipped"] != float64(0) {
		t.Errorf("skipped: want 0 for explicit ids, got %v", res["skipped"])
	}
	// Promoted facts landed in L3.
	docs, _ := srv.store.List(memory.L3Fact, "", "", 10, 0, false)
	if len(docs) != 2 {
		t.Errorf("l3 facts after reprocess: want 2, got %d", len(docs))
	}
	// And the rows are now flagged extracted.
	for _, id := range []string{"raw-1", "raw-2"} {
		if d := srv.store.GetMany([]string{id}); len(d) != 1 || !d[0].Extracted {
			t.Errorf("%s: extracted flag not set after reprocess", id)
		}
	}
}

// delete_all is destructive, so only POST and DELETE may reach it. A GET from a
// link prefetcher must get 405 and must not touch the store, even when it
// carries a scope that would otherwise be valid.
func TestDeleteAllRejectsNonDeleteMethods(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	if err := srv.store.Add(memory.L2Raw, "doc-1", "text", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": "2026-10-06T00:00:00Z",
	}); err != nil {
		t.Fatalf("add: %v", err)
	}
	h := srv.routes()
	for _, method := range []string{"GET", "PUT", "PATCH"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, localRequest(method, "/api/v1/delete_all?all=true", nil))
		if w.Code != http.StatusMethodNotAllowed {
			t.Errorf("%s delete_all: want 405, got %d", method, w.Code)
		}
		if w.Header().Get("Allow") == "" {
			t.Errorf("%s delete_all: 405 without an Allow header", method)
		}
	}
	if srv.store.TotalMemories() != 1 {
		t.Errorf("refused methods changed the store: %d docs", srv.store.TotalMemories())
	}
	// DELETE with a real scope is allowed.
	w := httptest.NewRecorder()
	h.ServeHTTP(w, localRequest("DELETE", "/api/v1/delete_all?user_id=u", nil))
	if w.Code != http.StatusOK || srv.store.TotalMemories() != 0 {
		t.Errorf("DELETE scoped: code %d, docs %d; want 200 and 0", w.Code, srv.store.TotalMemories())
	}
}

// A call with no filter is refused, and only an explicit all=true wipes.
func TestDeleteAllRequiresFilterOrAll(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	if err := srv.store.Add(memory.L2Raw, "doc-1", "text", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": "2026-10-06T00:00:00Z",
	}); err != nil {
		t.Fatalf("add: %v", err)
	}
	h := srv.routes()

	// layer=* is "everything" and must not bypass the guard.
	for _, target := range []string{"/api/v1/delete_all", "/api/v1/delete_all?layer=*"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, localRequest("POST", target, nil))
		if w.Code != http.StatusBadRequest {
			t.Errorf("POST %s: want 400, got %d", target, w.Code)
		}
	}
	if srv.store.TotalMemories() != 1 {
		t.Fatalf("refused unscoped delete removed docs: %d left", srv.store.TotalMemories())
	}

	// all=true in the body is the explicit wipe.
	w := httptest.NewRecorder()
	h.ServeHTTP(w, localRequest("POST", "/api/v1/delete_all", strings.NewReader(`{"all":true}`)))
	if w.Code != http.StatusOK {
		t.Errorf("body all=true: want 200, got %d", w.Code)
	}
	if srv.store.TotalMemories() != 0 {
		t.Errorf("body all=true left %d docs", srv.store.TotalMemories())
	}
}

// Request bodies are capped. A body over the limit is refused before it is
// decoded, so nothing is stored.
func TestRequestBodyIsBounded(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	big := `{"text":"` + strings.Repeat("a", maxRequestBody) + `"}`
	w := httptest.NewRecorder()
	srv.routes().ServeHTTP(w, localRequest("POST", "/api/v1/add", strings.NewReader(big)))
	if w.Code != http.StatusBadRequest {
		t.Errorf("oversized add: want 400, got %d", w.Code)
	}
	if srv.store.TotalMemories() != 0 {
		t.Errorf("oversized add stored %d docs", srv.store.TotalMemories())
	}
}

// The dashboard status must report the configured mode and the same LLM state
// as /api/v1/status, not a hardcoded "ok".
func TestDashStatusReportsModeAndLLMState(t *testing.T) {
	cases := []struct {
		mode    Mode
		withKey bool
		wantLLM string
	}{
		{ModeLite, true, "unused"},
		{ModeUltra, false, "unconfigured"},
		{ModePro, true, "ok"},
	}
	for _, c := range cases {
		srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
		srv.mode = c.mode
		if c.withKey {
			srv.llm = NewLLMClient("http://127.0.0.1:1/v1", "k", "m")
		}
		w := httptest.NewRecorder()
		srv.handleDashStatus(w, httptest.NewRequest("GET", "/api/status", nil))
		var st map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &st); err != nil {
			t.Fatal(err)
		}
		if st["mode"] != string(c.mode) {
			t.Errorf("%s: dash mode = %v", c.mode, st["mode"])
		}
		if st["llm"] != c.wantLLM {
			t.Errorf("%s: dash llm = %v, want %q", c.mode, st["llm"], c.wantLLM)
		}
	}
}

// The dashboard status reads the extraction error that background goroutines
// write. Run with -race: this must not read the field unsynchronised.
func TestDashStatusConcurrentWithExtractErr(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(2)
		go func() {
			defer wg.Done()
			srv.setExtractErr("boom")
			srv.setExtractErr("")
		}()
		go func() {
			defer wg.Done()
			w := httptest.NewRecorder()
			srv.handleDashStatus(w, httptest.NewRequest("GET", "/api/status", nil))
		}()
	}
	wg.Wait()
}

// Concurrent Adds all persist the same doc index. The index file must stay
// valid JSON and every add must succeed.
func TestConcurrentAddsKeepIndexValid(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	var wg sync.WaitGroup
	errs := make(chan error, 80)
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 10; i++ {
				id := "doc-" + string(rune('a'+g)) + "-" + string(rune('a'+i))
				if err := srv.store.Add(memory.L2Raw, id, "text", map[string]string{
					"user_id": "u", "ts": "2026-10-06T00:00:00Z",
				}); err != nil {
					errs <- err
				}
			}
		}(g)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Errorf("concurrent add: %v", err)
	}
	b, err := os.ReadFile(filepath.Join(srv.store.indexPath))
	if err != nil {
		t.Fatal(err)
	}
	var idx map[string]DocIndex
	if err := json.Unmarshal(b, &idx); err != nil {
		t.Fatalf("doc_index.json is not valid after concurrent writes: %v", err)
	}
	if len(idx) != 80 {
		t.Errorf("index has %d docs on disk, want 80", len(idx))
	}
}

// A web page in the user's browser must not be able to drive the API. The guard
// applies on every bind address: a DNS-name Host is refused (DNS rebinding), a
// cross-origin Origin is refused (including the opaque "null"), and a request the
// browser marks Sec-Fetch-Site: cross-site is refused even with no Origin. The
// plugin and curl send none of these headers, and a same-origin request is fine.
func TestGuardLocalRefusesBrowserOriginsAndRebinding(t *testing.T) {
	srv := newTestServer(t, "test", "test")
	if err := srv.store.Add(memory.L2Raw, "doc-1", "text", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": "2026-10-06T00:00:00Z",
	}); err != nil {
		t.Fatalf("add: %v", err)
	}
	h := srv.routes()

	send := func(method, path, host string, hdr map[string]string) int {
		r := httptest.NewRequest(method, path, nil)
		r.Host = host
		for k, v := range hdr {
			r.Header.Set(k, v)
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w.Code
	}
	const wipe = "/api/v1/delete_all?all=true"

	// Each of these would be a destructive call if it got through.
	refused := []struct {
		name string
		host string
		hdr  map[string]string
	}{
		{"cross-origin page", "127.0.0.1:19528", map[string]string{"Origin": "https://evil.example"}},
		{"Origin null", "127.0.0.1:19528", map[string]string{"Origin": "null"}},
		{"DNS-name Host, no origin", "evil.example:19528", nil},
		{"DNS-name Host, matching origin", "evil.example", map[string]string{"Origin": "http://evil.example"}},
		{"DNS name on the 0.0.0.0 bind", "hyatlas.lan:19528", nil},
		{"other LAN IP as origin", "192.168.1.5:19528", map[string]string{"Origin": "http://192.168.1.9:19528"}},
		{"cross-site fetch metadata, no origin", "127.0.0.1:19528", map[string]string{"Sec-Fetch-Site": "cross-site"}},
	}
	for _, c := range refused {
		if got := send("POST", wipe, c.host, c.hdr); got != http.StatusForbidden {
			t.Errorf("%s (host=%q hdr=%v): want 403, got %d", c.name, c.host, c.hdr, got)
		}
	}
	if srv.store.TotalMemories() != 1 {
		t.Fatalf("a refused request deleted data: %d left", srv.store.TotalMemories())
	}

	// Allowed without any allowlist: loopback names, IP literals, same-origin,
	// the server's own pages, and clients that send no Origin.
	allowed := []struct {
		name   string
		method string
		path   string
		host   string
		hdr    map[string]string
	}{
		{"localhost with its origin", "GET", "/api/v1/status", "localhost:19528",
			map[string]string{"Origin": "http://localhost:19528"}},
		{"bracketed IPv6, no origin", "GET", "/api/v1/status", "[::1]:19528", nil},
		{"trailing-dot localhost", "GET", "/api/v1/status", "localhost.:19528",
			map[string]string{"Origin": "http://localhost.:19528"}},
		{"same-origin POST from a LAN IP", "POST", "/api/v1/list", "192.168.1.5:19528",
			map[string]string{"Origin": "http://192.168.1.5:19528"}},
		{"same-origin fetch metadata", "POST", "/api/v1/list", "localhost:19528",
			map[string]string{"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost:19528"}},
		{"LAN IP, no origin (0.0.0.0 bind)", "GET", "/api/v1/status", "192.168.1.5:19528", nil},
	}
	for _, c := range allowed {
		if got := send(c.method, c.path, c.host, c.hdr); got != http.StatusOK {
			t.Errorf("%s (host=%q): want 200, got %d", c.name, c.host, got)
		}
	}

	// HYATLAS_ALLOWED_HOSTS names extra hostnames. Case and a trailing dot do not
	// matter, and the listed name is accepted as Host and as Origin host. An
	// unlisted name is still refused.
	srv.allowedHosts = parseHostList(" Hyatlas.LAN , other.example. ")
	h = srv.routes()
	for _, c := range []struct {
		name string
		host string
		hdr  map[string]string
		want int
	}{
		{"allowlisted Host", "hyatlas.lan:19528", nil, http.StatusOK},
		{"allowlisted Host, trailing dot and case", "OTHER.example.:19528", nil, http.StatusOK},
		{"allowlisted origin on a loopback Host", "localhost:19528",
			map[string]string{"Origin": "http://other.example"}, http.StatusOK},
		{"unlisted origin on an allowlisted Host", "hyatlas.lan:19528",
			map[string]string{"Origin": "http://notlisted.example"}, http.StatusForbidden},
		{"unlisted Host", "notlisted.example:19528", nil, http.StatusForbidden},
	} {
		if got := send("GET", "/api/v1/status", c.host, c.hdr); got != c.want {
			t.Errorf("%s: host=%q hdr=%v: want %d, got %d", c.name, c.host, c.hdr, c.want, got)
		}
	}
}
