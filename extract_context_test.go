package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// ctxLLM is a fake chat endpoint that records every request body and answers
// with whatever reply returns for it.
type ctxLLM struct {
	mu     sync.Mutex
	bodies []string
}

func (c *ctxLLM) last() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.bodies) == 0 {
		return ""
	}
	return c.bodies[len(c.bodies)-1]
}

// newCtxServer builds a test server whose LLM client talks to a fake endpoint.
func newCtxServer(t *testing.T, reply func() string) (*Server, *ctxLLM) {
	t.Helper()
	rec := &ctxLLM{}
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		rec.mu.Lock()
		rec.bodies = append(rec.bodies, string(b))
		rec.mu.Unlock()
		out, _ := json.Marshal(map[string]any{"choices": []map[string]any{
			{"message": map[string]string{"content": reply()}}}})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	}))
	t.Cleanup(mock.Close)
	srv := newTestServer(t, "test-model", mock.URL)
	srv.mode = ModePro
	srv.llm = NewLLMClient(mock.URL, "k", "test-model")
	return srv, rec
}

func ctxAdd(t *testing.T, s *MemoryStore, layer memory.Layer, id, text, user string, ts time.Time) {
	t.Helper()
	if err := s.Add(layer, id, text, map[string]string{
		"user_id": user, "agent_id": "a", "ts": ts.UTC().Format(time.RFC3339), "source_id": "src-" + id,
	}); err != nil {
		t.Fatal(err)
	}
}

func ctxRaw(t *testing.T, s *MemoryStore, id, text string) {
	t.Helper()
	ctxAdd(t, s, memory.L2Raw, id, text, "u", time.Now())
}

func ctxSuperseded(t *testing.T, s *MemoryStore, id string) (bool, string) {
	t.Helper()
	d := s.GetMany([]string{id})
	if len(d) == 0 {
		t.Fatalf("row %s missing", id)
	}
	return isSuperseded(d[0]), d[0].Meta["superseded_by"]
}

const modelFact = "HyAtlas extraction model is poolside and returns 403"

func TestExtractPromptShowsOwnerContext(t *testing.T) {
	srv, rec := newCtxServer(t, func() string { return `{"facts":[]}` })
	now := time.Now()
	ctxAdd(t, srv.store, memory.L3Fact, "fact-mine", modelFact, "u", now.Add(-48*time.Hour))
	ctxAdd(t, srv.store, memory.L3Fact, "fact-theirs", modelFact, "other", now)
	ctxAdd(t, srv.store, memory.L7Intention, "int-mine", "wait for maintainer review", "u", now.Add(-24*time.Hour))
	ctxAdd(t, srv.store, memory.L7Intention, "int-theirs", "someone else goal", "other", now)
	ctxRaw(t, srv.store, "raw1", modelFact)

	if err := srv.extract(modelFact, "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	got := rec.last()
	for _, want := range []string{"Related facts already stored", "fact-mine", "Open intentions", "int-mine",
		"intentions_done", "wait for maintainer review"} {
		if !strings.Contains(got, want) {
			t.Errorf("prompt lacks %q", want)
		}
	}
	for _, bad := range []string{"fact-theirs", "int-theirs", "someone else goal"} {
		if strings.Contains(got, bad) {
			t.Errorf("prompt leaks another owner's memory %q", bad)
		}
	}
}

func TestExtractPromptUnchangedWithoutContext(t *testing.T) {
	srv, rec := newCtxServer(t, func() string { return `{"facts":[]}` })
	ctxRaw(t, srv.store, "raw1", "hello there")
	if err := srv.extract("hello there", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	got := rec.last()
	for _, bad := range []string{"intentions_done", "replaces", "Related facts", "Open intentions"} {
		if strings.Contains(got, bad) {
			t.Errorf("context-free prompt mentions %q", bad)
		}
	}
	if !strings.Contains(got, "Input: hello there") {
		t.Error("input missing from prompt")
	}
}

func TestExtractReplacesSupersedesShownFactAndMirror(t *testing.T) {
	var newText = "HyAtlas extraction model is poolside and now returns 403 consistently"
	srv, _ := newCtxServer(t, func() string {
		return `{"facts":[{"data":"` + newText + `","layer":"decision","replaces":["fact-old","never-shown"]}]}`
	})
	now := time.Now()
	ctxAdd(t, srv.store, memory.L3Fact, "fact-old", modelFact, "u", now.Add(-72*time.Hour))
	ctxAdd(t, srv.store, memory.L1Profile, "mirror-old", modelFact, "u", now.Add(-72*time.Hour))
	// Live but never shown to this owner's extraction (another owner's row): the
	// model names it anyway.
	ctxAdd(t, srv.store, memory.L3Fact, "never-shown", modelFact, "other", now)
	// Mirror shares source_id and content with fact-old (ctxAdd sets src-<id>).
	if err := srv.store.Add(memory.L1Profile, "mirror-old2", modelFact, map[string]string{
		"user_id": "u", "agent_id": "a", "ts": now.Format(time.RFC3339), "source_id": "src-fact-old"}); err != nil {
		t.Fatal(err)
	}
	ctxRaw(t, srv.store, "raw1", modelFact)

	if err := srv.extract(modelFact, "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	if dead, by := ctxSuperseded(t, srv.store, "fact-old"); !dead || by == "" {
		t.Errorf("shown related fact not superseded (dead=%v by=%q)", dead, by)
	}
	if dead, _ := ctxSuperseded(t, srv.store, "mirror-old2"); !dead {
		t.Error("L1 mirror of the replaced fact stayed live")
	}
	if dead, _ := ctxSuperseded(t, srv.store, "never-shown"); dead {
		t.Error("an id that was not shown was superseded")
	}
	live, _ := srv.store.List(memory.L3Fact, "u", "a", 50, 0, false)
	found := false
	for _, d := range live {
		if d.ID == "fact-old" {
			t.Error("replaced fact still listed live")
		}
		if d.Content == newText {
			found = true
		}
	}
	if !found {
		t.Error("new fact was not written")
	}
}

func TestExtractIntentionDoneAndReplaces(t *testing.T) {
	srv, _ := newCtxServer(t, func() string {
		return `{"facts":[],"intention":{"goal":"tag v4.6.0 release","replaces":["int-a","int-ghost"]},"intentions_done":["int-b","int-ghost"]}`
	})
	now := time.Now()
	ctxAdd(t, srv.store, memory.L7Intention, "int-a", "waiting for maintainer review", "u", now.Add(-5*time.Hour))
	ctxAdd(t, srv.store, memory.L7Intention, "int-b", "merge the pull request", "u", now.Add(-4*time.Hour))
	ctxAdd(t, srv.store, memory.L7Intention, "int-c", "unrelated open goal", "u", now.Add(-3*time.Hour))
	ctxRaw(t, srv.store, "raw1", "merged and tagging next")

	if err := srv.extract("merged and tagging next", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	live, _ := srv.store.List(memory.L7Intention, "u", "a", 50, 0, false)
	var newID string
	for _, d := range live {
		if d.Content == "tag v4.6.0 release" {
			newID = d.ID
		}
	}
	if newID == "" {
		t.Fatal("new intention not written")
	}
	if dead, by := ctxSuperseded(t, srv.store, "int-a"); !dead || by != newID {
		t.Errorf("int-a: dead=%v by=%q, want superseded by %q", dead, by, newID)
	}
	if dead, by := ctxSuperseded(t, srv.store, "int-b"); !dead || by != "raw1" {
		t.Errorf("int-b: dead=%v by=%q, want superseded by raw1", dead, by)
	}
	if dead, _ := ctxSuperseded(t, srv.store, "int-c"); dead {
		t.Error("an intention the reply did not mention was closed")
	}
}

func TestExtractIntentionsDoneWithoutNewIntention(t *testing.T) {
	srv, _ := newCtxServer(t, func() string {
		return `{"facts":[],"intention":null,"intentions_done":["int-a"]}`
	})
	ctxAdd(t, srv.store, memory.L7Intention, "int-a", "waiting for review", "u", time.Now().Add(-time.Hour))
	ctxRaw(t, srv.store, "raw1", "it merged")
	if err := srv.extract("it merged", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	if dead, by := ctxSuperseded(t, srv.store, "int-a"); !dead || by != "raw1" {
		t.Errorf("dead=%v by=%q", dead, by)
	}
}

func TestExtractIntentionOutsideWindowNotShown(t *testing.T) {
	srv, rec := newCtxServer(t, func() string {
		return `{"facts":[],"intentions_done":["int-old"]}`
	})
	ctxAdd(t, srv.store, memory.L7Intention, "int-old", "ancient goal from long ago", "u", time.Now().Add(-extractIntentionWindow-24*time.Hour))
	ctxAdd(t, srv.store, memory.L7Intention, "int-new", "fresh goal", "u", time.Now().Add(-time.Hour))
	ctxRaw(t, srv.store, "raw1", "anything")
	if err := srv.extract("anything", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	if got := rec.last(); strings.Contains(got, "int-old") || !strings.Contains(got, "int-new") {
		t.Errorf("window not applied to the prompt: %s", got)
	}
	if dead, _ := ctxSuperseded(t, srv.store, "int-old"); dead {
		t.Error("an intention that was not shown was closed")
	}
}

// A reply with none of the new keys must write exactly what it always did.
func TestExtractWithoutNewKeysRegression(t *testing.T) {
	srv, _ := newCtxServer(t, func() string {
		return `{"facts":[{"data":"Cats are great","layer":"user_preferences"}],"summary":{"text":"s"},"intention":{"goal":"pet the cat"}}`
	})
	ctxAdd(t, srv.store, memory.L3Fact, "fact-x", "Cats are great", "u", time.Now().Add(-time.Hour))
	ctxAdd(t, srv.store, memory.L7Intention, "int-x", "open goal", "u", time.Now().Add(-time.Hour))
	ctxRaw(t, srv.store, "raw1", "Cats are great")
	if err := srv.extract("Cats are great", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	if dead, _ := ctxSuperseded(t, srv.store, "fact-x"); dead {
		t.Error("fact superseded without a replaces key")
	}
	if dead, _ := ctxSuperseded(t, srv.store, "int-x"); dead {
		t.Error("intention closed without intentions_done")
	}
	// A summary the model sends anyway is ignored: summaries come from episodes.
	if _, n := srv.store.List(memory.L4Summary, "u", "a", 50, 0, false); n != 0 {
		t.Errorf("per-write L4 summary written (%d rows)", n)
	}
	for layer, want := range map[memory.Layer]string{memory.L3Fact: "Cats are great", memory.L1Profile: "Cats are great",
		memory.L7Intention: "pet the cat"} {
		items, _ := srv.store.List(layer, "u", "a", 50, 0, false)
		ok := false
		for _, d := range items {
			ok = ok || d.Content == want
		}
		if !ok {
			t.Errorf("layer %s lacks %q", layer, want)
		}
	}
}

func TestParseExtractionTolerantIDs(t *testing.T) {
	ex, err := parseExtraction(`{"facts":[
		{"data":"a","layer":"decision","replaces":"abc"},
		{"data":"b","layer":"decision","replaces":["x","y",7,{"k":1}]},
		{"data":"c","layer":"decision","replaces":{"nope":true}},
		{"data":"d","layer":"decision","replaces":null},
		{"data":"e","layer":"decision","replaces":42}],
		"intention":{"goal":"g","replaces":"i1, i2"},
		"intentions_done":"d1"}`)
	if err != nil {
		t.Fatal(err)
	}
	want := [][]string{{"abc"}, {"x", "y"}, nil, nil, nil}
	for i, f := range ex.Facts {
		if strings.Join(f.Replaces, ",") != strings.Join(want[i], ",") {
			t.Errorf("fact %d replaces = %v, want %v", i, f.Replaces, want[i])
		}
	}
	if got := strings.Join(ex.Intention.Replaces, ","); got != "i1,i2" {
		t.Errorf("intention replaces = %q", got)
	}
	if len(ex.IntentionsDone) != 1 || ex.IntentionsDone[0] != "d1" {
		t.Errorf("intentions_done = %v", ex.IntentionsDone)
	}

	// A bare-string intention still parses, with no replaces.
	ex, err = parseExtraction(`{"facts":[],"intention":"just a goal","intentions_done":{"bad":1}}`)
	if err != nil || ex.Intention.Goal != "just a goal" || len(ex.Intention.Replaces) != 0 || len(ex.IntentionsDone) != 0 {
		t.Errorf("string intention: %+v err=%v", ex, err)
	}
}

// seedPage creates a page for owner u/a and returns it.
func seedPage(t *testing.T, s *MemoryStore, name string, sets ...PageSet) Page {
	t.Helper()
	if _, _, err := s.pages.Apply("u", "a", "seed", []PageUpdate{{Name: name, Kind: "project", Set: sets}}, nil); err != nil {
		t.Fatal(err)
	}
	for _, pg := range s.pages.List("u", "a") {
		if pg.Name == name {
			return pg
		}
	}
	t.Fatal("seeded page missing")
	return Page{}
}

func TestExtractPromptShowsRelatedPages(t *testing.T) {
	srv, rec := newCtxServer(t, func() string { return `{"facts":[]}` })
	mine := seedPage(t, srv.store, "HyAtlas", PageSet{Attr: "extraction model", Value: "stepfun step-5-preview"})
	if _, _, err := srv.store.pages.Apply("other", "a", "seed", []PageUpdate{{Name: "HyAtlas", Set: []PageSet{{Attr: "extraction model", Value: "secret"}}}}, nil); err != nil {
		t.Fatal(err)
	}
	ctxRaw(t, srv.store, "raw1", "x")
	if err := srv.extract(pageText(mine), "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	got := rec.last()
	for _, want := range []string{"Entity pages (id | name | current state):",
		mine.ID + " | HyAtlas | extraction model: stepfun step-5-preview", `\"pages\"`, "id of a listed page"} {
		if !strings.Contains(got, want) {
			t.Errorf("prompt lacks %q", want)
		}
	}
	if strings.Contains(got, "secret") {
		t.Error("prompt leaks another owner's page")
	}
}

func TestExtractContextFreePromptHasPagesButNoIDs(t *testing.T) {
	srv, rec := newCtxServer(t, func() string { return `{"facts":[]}` })
	ctxRaw(t, srv.store, "raw1", "hello there")
	if err := srv.extract("hello there", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	got := rec.last()
	if !strings.Contains(got, `\"pages\"`) || !strings.Contains(got, `\"page\": \"new\"`) {
		t.Error("context-free prompt lacks the pages contract")
	}
	for _, bad := range []string{"id of a listed page", "Entity pages (id", "replaces"} {
		if strings.Contains(got, bad) {
			t.Errorf("context-free prompt mentions %q", bad)
		}
	}
}

func TestExtractReplyUpdatesShownPageAndCreatesNew(t *testing.T) {
	var pageID string
	srv, _ := newCtxServer(t, func() string {
		return `{"facts":[],"pages":[` +
			`{"page":"` + pageID + `","name":"HyAtlas","set":[{"attr":"extraction model","value":"gemini"}],"event":"switched model"},` +
			`{"page":"new","name":"Hermes","kind":"tool","set":[{"attr":"role","value":"agent host"}]}]}`
	})
	pg := seedPage(t, srv.store, "HyAtlas", PageSet{Attr: "extraction model", Value: "stepfun"})
	pageID = pg.ID
	ctxRaw(t, srv.store, "raw1", "x")
	if err := srv.extract(pageText(pg), "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	if len(srv.store.pages.List("u", "a")) != 2 {
		t.Fatalf("want 2 pages, got %d", len(srv.store.pages.List("u", "a")))
	}
	got, _ := srv.store.pages.Get(pg.ID)
	if got.State[0].Value != "gemini" || len(got.History) != 2 ||
		got.History[0].Text != "extraction model: stepfun → gemini" || got.History[1].Text != "switched model" {
		t.Errorf("page not updated as asked: %+v", got)
	}
}

func TestExtractReplyWithoutPagesLeavesPagesAlone(t *testing.T) {
	srv, _ := newCtxServer(t, func() string {
		return `{"facts":[{"data":"the build uses go 1.26 toolchain","layer":"decision"}]}`
	})
	pg := seedPage(t, srv.store, "HyAtlas", PageSet{Attr: "version", Value: "4.6.0"})
	ctxRaw(t, srv.store, "raw1", "x")
	if err := srv.extract(pageText(pg), "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	after, _ := srv.store.pages.Get(pg.ID)
	if after.Updated != pg.Updated || len(after.History) != 0 || len(srv.store.pages.List("u", "a")) != 1 {
		t.Errorf("page changed without a pages key: %+v", after)
	}
	live, _ := srv.store.List(memory.L3Fact, "u", "a", 10, 0, false)
	if len(live) != 1 {
		t.Errorf("facts still written as before? got %d", len(live))
	}
}

func TestExtractGarbagePagesDoNotFailTheWrite(t *testing.T) {
	srv, _ := newCtxServer(t, func() string {
		return `{"facts":[{"data":"a durable fact about the setup","layer":"decision"}],"pages":"oops"}`
	})
	ctxRaw(t, srv.store, "raw1", "x")
	if err := srv.extract("some input", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	if live, _ := srv.store.List(memory.L3Fact, "u", "a", 10, 0, false); len(live) != 1 {
		t.Errorf("fact lost to a garbage pages field: %d", len(live))
	}
	if n := len(srv.store.pages.List("u", "a")); n != 0 {
		t.Errorf("garbage created %d pages", n)
	}
}

// A fact the model says replaces a shown fact retires it as "replaced", one it says
// updates a shown fact retires it as "updated", and an id in both lists counts as
// replaced. The new fact keeps a valid "when" as event_at and drops an invalid one.
func TestExtractRecordsHowFactsWereSuperseded(t *testing.T) {
	srv, _ := newCtxServer(t, func() string {
		return `{"facts":[` +
			`{"data":"HyAtlas extraction now uses stepfun","layer":"decision","when":"2026-10-05","replaces":["f-model"],"updates":["f-runs","f-model"]},` +
			`{"data":"The user tested twice","layer":"project_state","when":"soon"}]}`
	})
	now := time.Now()
	ctxAdd(t, srv.store, memory.L3Fact, "f-model", "HyAtlas extraction uses poolside", "u", now.Add(-time.Hour))
	ctxAdd(t, srv.store, memory.L3Fact, "f-runs", "HyAtlas extraction ran on poolside once", "u", now.Add(-time.Hour))
	ctxRaw(t, srv.store, "raw1", "HyAtlas extraction now uses stepfun")
	if err := srv.extract("HyAtlas extraction now uses stepfun", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	how := func(id string) string { return srv.store.GetMany([]string{id})[0].Meta[metaSupersededHow] }
	if got := how("f-model"); got != howReplaced {
		t.Errorf("f-model superseded_how = %q, want %q", got, howReplaced)
	}
	if got := how("f-runs"); got != howUpdated {
		t.Errorf("f-runs superseded_how = %q, want %q", got, howUpdated)
	}
	live, _ := srv.store.List(memory.L3Fact, "u", "a", 50, 0, false)
	byText := map[string]DocIndex{}
	for _, d := range live {
		byText[d.Content] = d
	}
	if d := byText["HyAtlas extraction now uses stepfun"]; d.Meta[metaEventAt] != "2026-10-05" {
		t.Errorf("event_at = %q, want 2026-10-05", d.Meta[metaEventAt])
	}
	if d, ok := byText["The user tested twice"]; !ok || d.Meta[metaEventAt] != "" {
		t.Errorf("invalid when stored as event_at %q (found %v)", d.Meta[metaEventAt], ok)
	}
}

// The extraction prompt carries today's date so relative dates can be resolved, and
// an intention's valid due date is stored as due_at.
func TestExtractTodayLineAndIntentionDue(t *testing.T) {
	srv, rec := newCtxServer(t, func() string {
		return `{"facts":[],"intention":{"goal":"finish the report","due":"2026-11-01"}}`
	})
	ctxRaw(t, srv.store, "raw1", "I need to finish the report by November 1")
	if err := srv.extract("I need to finish the report by November 1", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(rec.last(), "Today: "+time.Now().Format("2006-01-02")) {
		t.Error("extraction prompt lacks today's date")
	}
	items, _ := srv.store.List(memory.L7Intention, "u", "a", 10, 0, false)
	if len(items) != 1 || items[0].Meta[metaDueAt] != "2026-11-01" {
		t.Fatalf("intention due_at missing: %+v", items)
	}
}

// A date on a preference is dropped: preferences are timeless.
func TestExtractDropsEventDateOnPreference(t *testing.T) {
	srv, _ := newCtxServer(t, func() string {
		return `{"facts":[{"data":"Alex only drinks dark roast coffee","layer":"user_preferences","when":"2026-10-17"}]}`
	})
	ctxRaw(t, srv.store, "raw1", "My friend Alex only drinks dark roast coffee")
	if err := srv.extract("My friend Alex only drinks dark roast coffee", "u", "a", "raw1"); err != nil {
		t.Fatal(err)
	}
	live, _ := srv.store.List(memory.L3Fact, "u", "a", 10, 0, false)
	if len(live) != 1 || live[0].Meta[metaEventAt] != "" {
		t.Fatalf("preference kept event_at: %+v", live)
	}
}

// TestExtractContextHidesExperiences checks that a task experience, even the nearest
// live row to the input, is never shown as a related fact, while an ordinary fact
// near the input is still shown.
func TestExtractContextHidesExperiences(t *testing.T) {
	const input = "how to fix the build on windows"
	const fact = "The build runs on a Windows host"
	st := guardStore(t, vecEmbedder{
		input: {1, 0, 0},
		fact:  simTo(0.9),
	})
	expID := newID()
	if err := st.Add(memory.L3Fact, expID, input, map[string]string{
		"user_id": "u", "agent_id": "a", "source_layer_label": experienceLabel,
		"ts": "2026-10-18T00:00:00Z",
	}); err != nil {
		t.Fatal(err)
	}
	factID := addRow(t, st, memory.L3Fact, fact, "2026-10-18T00:00:00Z")

	ec := st.extractContext(input, "u", "a", time.Now().UTC())
	sawFact := false
	for _, it := range ec.Related {
		if it.ID == expID {
			t.Errorf("experience %s shown as a related fact", expID)
		}
		if it.ID == factID {
			sawFact = true
		}
	}
	if !sawFact {
		t.Errorf("ordinary related fact %s not shown: %+v", factID, ec.Related)
	}
}
