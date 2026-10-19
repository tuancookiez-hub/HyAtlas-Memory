package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// aliasUU2 makes "u" and "u2" one person on the store.
func aliasUU2(s *MemoryStore) {
	s.SetAliases(aliasMap(parseUserAliases("u,u2")))
}

// The store answers who is who: owners lists the user first, canonicalOwner is the
// first ID of the group for every member, and without aliases both are the user.
func TestStoreOwnersAndCanonicalOwner(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return `{"facts":[]}` })
	st := srv.store
	if got := st.owners("u"); len(got) != 1 || got[0] != "u" {
		t.Fatalf("owners without aliases = %v, want [u]", got)
	}
	if got := st.canonicalOwner("u2"); got != "u2" {
		t.Fatalf("canonicalOwner without aliases = %q, want u2", got)
	}
	aliasUU2(st)
	if got := st.owners("u2"); len(got) != 2 || got[0] != "u2" || got[1] != "u" {
		t.Fatalf("owners(u2) = %v, want [u2 u]", got)
	}
	if st.canonicalOwner("u") != "u" || st.canonicalOwner("u2") != "u" {
		t.Fatalf("canonical owners = %q, %q; want u for both", st.canonicalOwner("u"), st.canonicalOwner("u2"))
	}
	if got := srv.expandOwners([]string{"u2", "x"}); strings.Join(got, ",") != "u2,u,x" {
		t.Fatalf("expandOwners = %v, want [u2 u x]", got)
	}
}

// A fact stored under u2 shows in the extraction context of u.
func TestAliasFactShowsInExtractionContext(t *testing.T) {
	srv, rec := newCtxServer(t, func() string { return `{"facts":[]}` })
	aliasUU2(srv.store)
	now := time.Now()
	ctxAdd(t, srv.store, memory.L3Fact, "fact-alias", modelFact, "u2", now.Add(-48*time.Hour))
	ctxAdd(t, srv.store, memory.L3Fact, "fact-other", modelFact, "other", now)
	ctxAdd(t, srv.store, memory.L7Intention, "int-alias", "wait for maintainer review", "u2", now.Add(-24*time.Hour))
	ctxRaw(t, srv.store, "raw1", modelFact)

	if err := srv.extract(modelFact, "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	got := rec.last()
	for _, want := range []string{"fact-alias", "int-alias"} {
		if !strings.Contains(got, want) {
			t.Errorf("prompt lacks the alias owner's %q", want)
		}
	}
	if strings.Contains(got, "fact-other") {
		t.Error("prompt leaks another person's fact")
	}
}

// A fact written under u dedupes against an identical fact stored under u2.
func TestAliasWriteDedupesAcrossAliases(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return `{"facts":[]}` })
	aliasUU2(srv.store)
	ctxAdd(t, srv.store, memory.L3Fact, "old-alias", modelFact, "u2", time.Now().Add(-time.Hour))

	ex := &Extraction{Facts: []Fact{{Data: modelFact, Layer: "project_state"}}}
	ids := promoteExtractionCtx(srv.store, ex, ExtractContext{}, "u", "a", "src", defaultDedupeScore)
	if len(ids) != 1 {
		t.Fatalf("facts written = %d, want 1", len(ids))
	}
	if gone, by := ctxSuperseded(t, srv.store, "old-alias"); !gone || by != ids[0] {
		t.Fatalf("alias fact superseded = %v by %q; want true by %q", gone, by, ids[0])
	}
}

// A Now page refreshed through u2 is keyed by the canonical ID, so u reads it, and a
// page stored earlier under a non-canonical ID is still found.
func TestAliasNowPageSharedAcrossAliases(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return "Active:\n- ship it" })
	aliasUU2(srv.store)
	nowAddRow(t, srv.store, memory.L7Intention, "i1", "ship it", "u2", time.Hour)

	n := NewNowPages(t.TempDir(), srv.store, srv.llm, time.Hour)
	if err := n.refresh(context.Background(), "u2", "a"); err != nil {
		t.Fatal(err)
	}
	if _, ok := n.pages[nowOwnerKey("u", "a")]; !ok {
		t.Fatalf("page keys = %v, want the canonical u key", n.pages)
	}
	for _, who := range []string{"u", "u2"} {
		if p, ok := n.Get(who, "a"); !ok || !strings.Contains(p.Text, "ship it") {
			t.Fatalf("Get(%s) = %+v ok=%v, want the shared page", who, p, ok)
		}
	}
	w := httptest.NewRecorder()
	n.HandleNow(w, httptest.NewRequest(http.MethodGet, "/api/v1/now?user_id=u2&agent_id=a", nil))
	if !strings.Contains(w.Body.String(), "ship it") {
		t.Fatalf("HandleNow(u2) = %s, want the page", w.Body.String())
	}

	// A page written before the alias was configured sits under the raw ID.
	n.pages[nowOwnerKey("v2", "a")] = NowPage{Text: "old page"}
	srv.store.SetAliases(aliasMap(parseUserAliases("v,v2")))
	if p, ok := n.Get("v", "a"); !ok || p.Text != "old page" {
		t.Fatalf("legacy page via canonical ID = %+v ok=%v, want the raw-key page", p, ok)
	}
}

// Episodes of two alias IDs are one episode under the canonical ID.
func TestAliasEpisodesShareOneKey(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return `{"facts":[]}` })
	aliasUU2(srv.store)
	_, llm := newEpisodeLLMStub(t, http.StatusOK, "summary")
	e := NewEpisodes("", srv.store, llm, time.Hour)
	e.Note("u2", "a", []string{"f1"}, episodeTestTime)
	e.Note("u", "a", []string{"f2"}, episodeTestTime.Add(time.Minute))
	if len(e.open) != 1 {
		t.Fatalf("open episodes = %d, want 1", len(e.open))
	}
	ep := e.open[episodeKey("u", "a")]
	if ep == nil || len(ep.FactIDs) != 2 {
		t.Fatalf("episode = %+v, want both facts under the canonical u key", ep)
	}
}

// The list for u includes u2's rows; a list for someone else does not.
func TestAliasListIncludesAliasRows(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return `{"facts":[]}` })
	aliasUU2(srv.store)
	now := time.Now()
	ctxAdd(t, srv.store, memory.L3Fact, "f-u", "fact of u", "u", now)
	ctxAdd(t, srv.store, memory.L3Fact, "f-u2", "fact of u2", "u2", now)
	ctxAdd(t, srv.store, memory.L3Fact, "f-x", "fact of x", "x", now)
	total := func(user string) float64 {
		w := httptest.NewRecorder()
		srv.handleList(w, httptest.NewRequest(http.MethodGet,
			"/api/v1/list?layer=l3_fact&agent_id=a&user_id="+user, nil))
		var resp struct {
			Total float64 `json:"total"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatal(err)
		}
		return resp.Total
	}
	if got := total("u"); got != 2 {
		t.Fatalf("list for u total = %v, want u and u2's rows", got)
	}
	if got := total("x"); got != 1 {
		t.Fatalf("list for x total = %v, want 1", got)
	}
}

// Forget under u finds a memory-tool raw row stored under u2.
func TestAliasForgetFindsAliasRow(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return `{"facts":[]}` })
	aliasUU2(srv.store)
	if err := srv.store.Add(memory.L2Raw, "mt1", "likes oat milk", map[string]string{
		"user_id": "u2", "agent_id": "a", "write_origin": originMemoryTool,
		"ts": time.Now().UTC().Format(time.RFC3339),
	}); err != nil {
		t.Fatal(err)
	}
	code, resp := forgetReq(t, srv, `{"text":"likes oat milk","user_id":"u","agent_id":"a"}`)
	if code != http.StatusOK || resp["forgotten"] != float64(1) {
		t.Fatalf("forget = %d %v, want 1 raw row forgotten", code, resp)
	}
}

// The catch-up pass leaves a row alone while its extraction is running, however old
// the row is, and extracts it once that has ended.
func TestCatchupSkipsRowsBeingExtracted(t *testing.T) {
	srv, stub := newCatchupServer(t)
	catchupRaw(t, srv.store, "r1", "an old raw note", 3*time.Hour)
	srv.beginExtract("r1")
	c := NewCatchup(srv, time.Hour)
	res, ran := c.Once(context.Background())
	if !ran || res.Extracted != 0 || res.Skipped != 1 || stub.calls.Load() != 0 {
		t.Fatalf("run while in flight: ran %v, %+v, %d calls; want the row skipped", ran, res, stub.calls.Load())
	}
	srv.endExtract("r1")
	res, _ = c.Once(context.Background())
	if res.Extracted != 1 || stub.calls.Load() != 1 {
		t.Fatalf("run after: %+v, %d calls; want 1 extracted", res, stub.calls.Load())
	}
}

// waitExtractions returns at once when nothing runs, returns after a running
// extraction finishes, and returns after the timeout, reporting the count, when one
// hangs.
func TestWaitExtractions(t *testing.T) {
	srv := &Server{}
	if left := srv.waitExtractions(time.Second); left != 0 {
		t.Fatalf("idle wait left %d, want 0", left)
	}

	srv.bg.Add(1)
	srv.bgRunning.Add(1)
	go func() {
		time.Sleep(50 * time.Millisecond)
		srv.bgRunning.Add(-1)
		srv.bg.Done()
	}()
	if left := srv.waitExtractions(5 * time.Second); left != 0 {
		t.Fatalf("wait for a finishing extraction left %d, want 0", left)
	}

	srv.bg.Add(1)
	srv.bgRunning.Add(1)
	start := time.Now()
	if left := srv.waitExtractions(50 * time.Millisecond); left != 1 {
		t.Fatalf("wait for a hung extraction left %d, want 1", left)
	}
	if time.Since(start) > 2*time.Second {
		t.Fatal("wait did not return at the timeout")
	}
	srv.bgRunning.Add(-1)
	srv.bg.Done()
}

// goExtract marks the row in flight at once and clears it when the extraction ends.
func TestGoExtractTracksInFlight(t *testing.T) {
	srv, _ := newCtxServer(t, func() string { return `{"facts":[]}` })
	ctxRaw(t, srv.store, "raw1", "some note")
	srv.goExtract("some note", "u", "a", "raw1")
	if !srv.extractionRunning("raw1") {
		t.Fatal("row not marked in flight right after goExtract")
	}
	if left := srv.waitExtractions(10 * time.Second); left != 0 {
		t.Fatalf("extraction still running: %d", left)
	}
	if srv.extractionRunning("raw1") {
		t.Fatal("row still marked in flight after the extraction ended")
	}
}
