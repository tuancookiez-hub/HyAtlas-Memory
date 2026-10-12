package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

type backlogEnv struct {
	job   *BacklogJob
	store *MemoryStore
	dir   string
	calls *int32
	admin *atomic.Bool
}

// newBacklogEnv builds a job whose LLM is a fake endpoint answering reply(body).
func newBacklogEnv(t *testing.T, reply func(body string) string) *backlogEnv {
	t.Helper()
	store, dir := newPagesStore(t)
	var calls int32
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		atomic.AddInt32(&calls, 1)
		out, _ := json.Marshal(map[string]any{"choices": []map[string]any{
			{"message": map[string]string{"content": reply(string(b))}}}})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	}))
	t.Cleanup(mock.Close)
	llm := NewLLMClient(mock.URL, "k", "test-model")
	cons := NewConsolidator(store, llm, 0, 0, 0)
	admin := &atomic.Bool{}
	admin.Store(true)
	job := NewBacklogJob(dir, store, cons, llm, admin.Load)
	return &backlogEnv{job: job, store: store, dir: dir, calls: &calls, admin: admin}
}

func (e *backlogEnv) post(t *testing.T, dry bool) *httptest.ResponseRecorder {
	t.Helper()
	body, _ := json.Marshal(map[string]any{"dry_run": dry})
	rec := httptest.NewRecorder()
	e.job.HandleBacklog(rec, httptest.NewRequest(http.MethodPost, "/api/v1/admin/backlog", bytes.NewReader(body)))
	return rec
}

func (e *backlogEnv) get() BacklogStatus {
	rec := httptest.NewRecorder()
	e.job.HandleBacklog(rec, httptest.NewRequest(http.MethodGet, "/api/v1/admin/backlog", nil))
	var st BacklogStatus
	_ = json.Unmarshal(rec.Body.Bytes(), &st)
	return st
}

func (e *backlogEnv) seed(t *testing.T) {
	t.Helper()
	now := time.Now()
	ctxAdd(t, e.store, memory.L3Fact, "old1", "HyAtlas extraction model is poolside and returns 403", "u", now.Add(-72*time.Hour))
	ctxAdd(t, e.store, memory.L3Fact, "new1", "HyAtlas extraction model is now stepfun", "u", now)
	ctxAdd(t, e.store, memory.L3Fact, "v1", "Other owner fact one", "v", now.Add(-24*time.Hour))
	ctxAdd(t, e.store, memory.L3Fact, "v2", "Other owner fact two", "v", now)
}

// backlogReply answers owner u's cluster with a valid pair plus junk pairs, and
// owner v's with a pair that points at u's facts.
func backlogReply(body string) string {
	if strings.Contains(body, "id=v1") {
		return `{"outdated":[{"id":"old1","by":"new1","reason":"cross owner"}],"pages":[]}`
	}
	return "```json\n" + `{"outdated":[
		{"id":"old1","by":"new1","reason":"model changed"},
		{"id":"ghost","by":"new1","reason":"unknown id"},
		{"id":"new1","by":"old1","reason":"reversed dates"},
		{"id":"old1","by":"v2","reason":"cross cluster"}],
	"pages":[{"page":"pg-x","name":"HyAtlas","kind":"project","set":[{"attr":"extraction model","value":"stepfun"}],"event":"switched extraction model"}]}` + "\n```"
}

func (e *backlogEnv) waitDone(t *testing.T) {
	t.Helper()
	e.job.wg.Wait()
}

func TestBacklogDryRunWritesPlanOnly(t *testing.T) {
	e := newBacklogEnv(t, backlogReply)
	e.seed(t)
	if rec := e.post(t, true); rec.Code != 202 {
		t.Fatalf("dry run POST = %d %s", rec.Code, rec.Body)
	}
	e.waitDone(t)
	st := e.get()
	if st.State != "done" || st.ClustersTotal != 2 || st.ClustersDone != 2 {
		t.Fatalf("status = %+v", st)
	}
	raw, err := os.ReadFile(filepath.Join(e.dir, backlogPlanFile))
	if err != nil {
		t.Fatal(err)
	}
	var plan backlogPlan
	if err := json.Unmarshal(raw, &plan); err != nil {
		t.Fatal(err)
	}
	if len(plan.Owners) != 2 {
		t.Fatalf("owners = %d", len(plan.Owners))
	}
	for _, po := range plan.Owners {
		switch po.User {
		case "u":
			if len(po.Outdated) != 1 || po.Outdated[0].ID != "old1" || po.Outdated[0].By != "new1" ||
				!strings.Contains(po.Outdated[0].Text, "poolside") || !strings.Contains(po.Outdated[0].ByText, "stepfun") {
				t.Errorf("u outdated = %+v", po.Outdated)
			}
			if len(po.Pages) != 1 || po.Pages[0].Name != "HyAtlas" || po.Pages[0].Page != "new" {
				t.Errorf("u pages = %+v", po.Pages)
			}
		case "v":
			if len(po.Outdated) != 0 {
				t.Errorf("cross-owner pair kept: %+v", po.Outdated)
			}
		}
	}
	if n := st.Summary["facts_to_supersede"]; n != float64(1) {
		t.Errorf("summary = %+v", st.Summary)
	}
	for _, id := range []string{"old1", "new1"} {
		if sup, _ := ctxSuperseded(t, e.store, id); sup {
			t.Errorf("dry run superseded %s", id)
		}
	}
	if n := len(e.store.pages.List("u", "a")); n != 0 {
		t.Errorf("dry run created %d pages", n)
	}
}

func TestBacklogApplyAndReapply(t *testing.T) {
	e := newBacklogEnv(t, backlogReply)
	e.seed(t)
	e.post(t, true)
	e.waitDone(t)
	before := atomic.LoadInt32(e.calls)

	if rec := e.post(t, false); rec.Code != 202 {
		t.Fatalf("apply POST = %d %s", rec.Code, rec.Body)
	}
	e.waitDone(t)
	if got := atomic.LoadInt32(e.calls); got != before {
		t.Errorf("apply made %d LLM requests", got-before)
	}
	if sup, by := ctxSuperseded(t, e.store, "old1"); !sup || by != "new1" {
		t.Errorf("old1 superseded=%v by=%q", sup, by)
	}
	if sup, _ := ctxSuperseded(t, e.store, "new1"); sup {
		t.Error("new1 superseded")
	}
	pgs := e.store.pages.List("u", "a")
	if len(pgs) != 1 || pgs[0].Name != "HyAtlas" || len(pgs[0].State) != 1 {
		t.Fatalf("pages = %+v", pgs)
	}
	st := e.get()
	if st.State != "done" || st.Summary["superseded"] != float64(1) || st.Summary["pages_created"] != float64(1) {
		t.Errorf("status = %+v", st)
	}
	if _, err := os.Stat(filepath.Join(e.dir, backlogPlanFile)); !os.IsNotExist(err) {
		t.Error("plan not renamed")
	}
	if _, err := os.Stat(filepath.Join(e.dir, backlogAppliedFile)); err != nil {
		t.Errorf("applied plan missing: %v", err)
	}
	if rec := e.post(t, false); rec.Code != 400 {
		t.Errorf("second apply = %d, want 400", rec.Code)
	}
}

func TestBacklogApplySkipsFactsGoneSinceDryRun(t *testing.T) {
	e := newBacklogEnv(t, backlogReply)
	e.seed(t)
	e.post(t, true)
	e.waitDone(t)
	if _, err := e.store.Supersede([]string{"old1"}, "elsewhere"); err != nil {
		t.Fatal(err)
	}
	e.post(t, false)
	e.waitDone(t)
	st := e.get()
	if st.Summary["superseded"] != float64(0) || st.Summary["skipped"] != float64(1) {
		t.Errorf("summary = %+v", st.Summary)
	}
	if _, by := ctxSuperseded(t, e.store, "old1"); by != "elsewhere" {
		t.Errorf("superseded_by overwritten: %q", by)
	}
}

func TestBacklogAdminOffIs403(t *testing.T) {
	e := newBacklogEnv(t, backlogReply)
	e.admin.Store(false)
	if rec := e.post(t, true); rec.Code != 403 {
		t.Errorf("POST = %d", rec.Code)
	}
	rec := httptest.NewRecorder()
	e.job.HandleBacklog(rec, httptest.NewRequest(http.MethodGet, "/x", nil))
	if rec.Code != 403 {
		t.Errorf("GET = %d", rec.Code)
	}
}

func TestBacklogPostWhileRunningIs409(t *testing.T) {
	release := make(chan struct{})
	var once sync.Once
	e := newBacklogEnv(t, func(body string) string {
		<-release
		return `{}`
	})
	t.Cleanup(func() { once.Do(func() { close(release) }) })
	e.seed(t)
	if rec := e.post(t, true); rec.Code != 202 {
		t.Fatalf("first POST = %d", rec.Code)
	}
	if rec := e.post(t, true); rec.Code != 409 {
		t.Errorf("second POST = %d, want 409", rec.Code)
	}
	st := e.get()
	if st.State != "running" || !st.DryRun || st.Started == "" {
		t.Errorf("running status = %+v", st)
	}
	once.Do(func() { close(release) })
	e.waitDone(t)
	st = e.get()
	if st.State != "done" || st.Finished == "" || st.ClustersTotal != 2 || st.ClustersDone != 2 {
		t.Errorf("final status = %+v", st)
	}
}

func TestBacklogClusterFailureIsRecordedAndSkipped(t *testing.T) {
	e := newBacklogEnv(t, func(body string) string {
		if strings.Contains(body, "id=v1") {
			return "not json at all"
		}
		return backlogReply(body)
	})
	e.seed(t)
	e.post(t, true)
	e.waitDone(t)
	st := e.get()
	if st.State != "done" || len(st.Errors) != 1 || st.ClustersDone != 2 {
		t.Errorf("status = %+v", st)
	}
}

func TestParseBacklogReplyDedupAndTolerance(t *testing.T) {
	cluster := []DocIndex{
		{ID: "a", Content: "x", Ts: "2026-10-01T00:00:00Z"},
		{ID: "b", Content: "y", Ts: "2026-10-05T00:00:00Z"},
	}
	ans, err := parseBacklogReply(`prose {"outdated":[{"id":"a","by":"b"},{"id":"a","by":"b"},"junk"],"pages":"oops"} tail`, cluster)
	if err != nil || len(ans.outdated) != 1 || len(ans.pages) != 0 {
		t.Errorf("ans=%+v err=%v", ans, err)
	}
	if got := dedupFacts(append(cluster, cluster[0])); len(got) != 2 {
		t.Errorf("dedupFacts = %d", len(got))
	}
}
