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
	for layer, want := range map[memory.Layer]string{memory.L3Fact: "Cats are great", memory.L1Profile: "Cats are great",
		memory.L4Summary: "s", memory.L7Intention: "pet the cat"} {
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
