package main

import (
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

func newPagesStore(t *testing.T) (*MemoryStore, string) {
	t.Helper()
	dir := t.TempDir()
	store, err := NewMemoryStore(ctxForTest(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	t.Cleanup(func() { store.Close() })
	return store, dir
}

func set(attr, value string) PageSet { return PageSet{Attr: attr, Value: value} }

func TestPagesApplyCreates(t *testing.T) {
	store, _ := newPagesStore(t)
	c, u, err := store.pages.Apply("u", "a", "src1", []PageUpdate{{
		Page: "new", Name: "HyAtlas", Kind: "project",
		Set: []PageSet{set("version", "4.6.0")}, Event: "released 4.6.0",
	}}, nil)
	if err != nil || c != 1 || u != 0 {
		t.Fatalf("created=%d updated=%d err=%v", c, u, err)
	}
	pgs := store.pages.List("u", "a")
	if len(pgs) != 1 {
		t.Fatalf("want 1 page, got %d", len(pgs))
	}
	pg := pgs[0]
	if !strings.HasPrefix(pg.ID, "pg-") || pg.Name != "HyAtlas" || pg.Kind != "project" ||
		len(pg.State) != 1 || pg.State[0].Value != "4.6.0" || pg.State[0].Source != "src1" ||
		len(pg.History) != 1 || pg.History[0].Text != "released 4.6.0" {
		t.Errorf("unexpected page: %+v", pg)
	}
	// A nameless "new" update creates nothing.
	if c, u, _ := store.pages.Apply("u", "a", "s", []PageUpdate{{Page: "new", Set: []PageSet{set("x", "y")}}}, nil); c+u != 0 {
		t.Errorf("nameless update changed %d pages", c+u)
	}
}

func TestPagesSetChangeWritesHistory(t *testing.T) {
	store, _ := newPagesStore(t)
	_, _, _ = store.pages.Apply("u", "a", "s1", []PageUpdate{{Name: "HyAtlas", Set: []PageSet{set("Extraction model", "poolside")}}}, nil)
	id := store.pages.List("u", "a")[0].ID

	// Same value: nothing changes, no history.
	c, u, _ := store.pages.Apply("u", "a", "s2", []PageUpdate{{Page: id, Name: "HyAtlas", Set: []PageSet{set("extraction model", "poolside")}}}, map[string]bool{id: true})
	if c+u != 0 {
		t.Errorf("identical set counted as a change (%d,%d)", c, u)
	}
	pg, _ := store.pages.Get(id)
	if len(pg.History) != 0 {
		t.Errorf("identical set wrote history: %+v", pg.History)
	}

	// Different value: history line, new value, same page.
	c, u, _ = store.pages.Apply("u", "a", "s3", []PageUpdate{{Page: id, Set: []PageSet{set("extraction model", "stepfun")}}}, map[string]bool{id: true})
	if c != 0 || u != 1 {
		t.Errorf("created=%d updated=%d", c, u)
	}
	pg, _ = store.pages.Get(id)
	if len(pg.State) != 1 || pg.State[0].Value != "stepfun" || pg.State[0].Source != "s3" {
		t.Errorf("state not replaced: %+v", pg.State)
	}
	if len(pg.History) != 1 || pg.History[0].Text != "Extraction model: poolside → stepfun" || pg.History[0].Source != "s3" {
		t.Errorf("history: %+v", pg.History)
	}
}

func TestPagesNameMatchNeverDuplicates(t *testing.T) {
	store, _ := newPagesStore(t)
	_, _, _ = store.pages.Apply("u", "a", "s1", []PageUpdate{{Name: "hyatlas", Kind: "project", Set: []PageSet{set("version", "4.5.0")}}}, nil)
	c, u, _ := store.pages.Apply("u", "a", "s2", []PageUpdate{{Page: "new", Name: "HyAtlas v4", Set: []PageSet{set("version", "4.6.0")}}}, nil)
	if c != 0 || u != 1 {
		t.Fatalf("created=%d updated=%d", c, u)
	}
	pgs := store.pages.List("u", "a")
	if len(pgs) != 1 {
		t.Fatalf("duplicate page created: %d pages", len(pgs))
	}
	if pgs[0].State[0].Value != "4.6.0" {
		t.Errorf("state: %+v", pgs[0].State)
	}
	if len(pgs[0].Aliases) != 1 || pgs[0].Aliases[0] != "HyAtlas v4" {
		t.Errorf("alias not recorded: %v", pgs[0].Aliases)
	}
	// The alias now also finds the page.
	if c, _, _ := store.pages.Apply("u", "a", "s3", []PageUpdate{{Name: "HyAtlas v4", Event: "x"}}, nil); c != 0 {
		t.Error("alias match created a page")
	}
	// A different name is a different page, even if similar.
	if c, _, _ := store.pages.Apply("u", "a", "s4", []PageUpdate{{Name: "HyAtlas server"}}, nil); c != 1 {
		t.Error("a merely overlapping name must not match")
	}
}

func TestPagesUnshownIDTreatedAsNewByName(t *testing.T) {
	store, _ := newPagesStore(t)
	_, _, _ = store.pages.Apply("u", "a", "s1", []PageUpdate{{Name: "Hermes"}}, nil)
	id := store.pages.List("u", "a")[0].ID

	// The real id, but it was not shown: resolved by name instead.
	c, u, _ := store.pages.Apply("u", "a", "s2", []PageUpdate{{Page: id, Name: "Hermes", Set: []PageSet{set("a", "b")}}}, map[string]bool{})
	if c != 0 || u != 1 {
		t.Errorf("by-name fallback: created=%d updated=%d", c, u)
	}
	// An unshown id with an unknown name creates a new page, not an update of that id.
	c, _, _ = store.pages.Apply("u", "a", "s3", []PageUpdate{{Page: id, Name: "Something else"}}, map[string]bool{})
	if c != 1 || len(store.pages.List("u", "a")) != 2 {
		t.Errorf("unshown id with a new name should create: created=%d", c)
	}
	// A garbage id never matches anything.
	if c, _, _ := store.pages.Apply("u", "a", "s4", []PageUpdate{{Page: "pg-ghost", Name: "Ghost"}}, map[string]bool{"pg-ghost": true}); c != 1 {
		t.Error("shown-but-missing id should fall back to create")
	}
}

func TestPagesCaps(t *testing.T) {
	store, _ := newPagesStore(t)
	// At most three updates per call.
	var ups []PageUpdate
	for i := 0; i < 5; i++ {
		ups = append(ups, PageUpdate{Name: fmt.Sprintf("Thing%c", 'a'+i)})
	}
	if c, _, _ := store.pages.Apply("u", "a", "s", ups, nil); c != 3 {
		t.Errorf("created %d pages from 5 updates, want 3", c)
	}

	_, _, _ = store.pages.Apply("u", "a", "s", []PageUpdate{{Name: "Big"}}, nil)
	var id string
	for _, pg := range store.pages.List("u", "a") {
		if pg.Name == "Big" {
			id = pg.ID
		}
	}
	shown := map[string]bool{id: true}

	// State cap: 12 attributes, oldest Since dropped.
	var sets []PageSet
	for i := 0; i < 15; i++ {
		sets = append(sets, set(fmt.Sprintf("attr%02d", i), "v"))
	}
	_, _, _ = store.pages.Apply("u", "a", "s", []PageUpdate{{Page: id, Set: sets}}, shown)
	pg, _ := store.pages.Get(id)
	if len(pg.State) != pageStateMax {
		t.Fatalf("state len %d, want %d", len(pg.State), pageStateMax)
	}
	if pg.State[len(pg.State)-1].Attr != "attr14" {
		t.Errorf("newest attribute was dropped: %+v", pg.State[len(pg.State)-1])
	}

	// History cap: 200 events, oldest dropped.
	for i := 0; i < pageHistoryMax+10; i++ {
		_, _, _ = store.pages.Apply("u", "a", "s", []PageUpdate{{Page: id, Event: fmt.Sprintf("event %d", i)}}, shown)
	}
	pg, _ = store.pages.Get(id)
	if len(pg.History) != pageHistoryMax {
		t.Fatalf("history len %d, want %d", len(pg.History), pageHistoryMax)
	}
	if pg.History[0].Text != "event 10" || pg.History[len(pg.History)-1].Text != fmt.Sprintf("event %d", pageHistoryMax+9) {
		t.Errorf("wrong events kept: first %q last %q", pg.History[0].Text, pg.History[len(pg.History)-1].Text)
	}
}

func TestPagesOwnerIsolation(t *testing.T) {
	store, _ := newPagesStore(t)
	_, _, _ = store.pages.Apply("alice", "a", "s1", []PageUpdate{{Name: "HyAtlas", Set: []PageSet{set("version", "1")}}}, nil)
	aliceID := store.pages.List("alice", "a")[0].ID

	// Bob names the same thing: his own page, never alice's.
	c, _, _ := store.pages.Apply("bob", "a", "s2", []PageUpdate{{Name: "HyAtlas", Set: []PageSet{set("version", "2")}}}, nil)
	if c != 1 {
		t.Errorf("bob's same-named page not created (created=%d)", c)
	}
	// Bob even claims alice's id as shown.
	_, _, _ = store.pages.Apply("bob", "a", "s3", []PageUpdate{{Page: aliceID, Name: "Other", Set: []PageSet{set("version", "9")}}}, map[string]bool{aliceID: true})
	pg, _ := store.pages.Get(aliceID)
	if pg.State[0].Value != "1" || len(pg.History) != 0 {
		t.Errorf("alice's page was touched: %+v", pg)
	}
	if got := store.pages.List("alice", "a"); len(got) != 1 {
		t.Errorf("alice sees %d pages", len(got))
	}
	// Related is owner scoped too.
	if rel := store.pages.Related(pageText(pg), "bob", "a", 3); len(rel) > 0 {
		for _, r := range rel {
			if r.UserID != "bob" {
				t.Errorf("Related leaked %s's page", r.UserID)
			}
		}
	}
	if rel := store.pages.Related(pageText(pg), "carol", "a", 3); len(rel) != 0 {
		t.Errorf("a user with no pages got %d related", len(rel))
	}
}

func TestPagesPersistenceRoundTrip(t *testing.T) {
	store, dir := newPagesStore(t)
	_, _, _ = store.pages.Apply("u", "a", "s1", []PageUpdate{{Name: "HyAtlas", Kind: "project", Set: []PageSet{set("version", "4.6.0")}, Event: "shipped"}}, nil)
	want := store.pages.List("u", "a")[0]

	reloaded, err := NewPageStore(dir, store)
	if err != nil {
		t.Fatal(err)
	}
	got, ok := reloaded.Get(want.ID)
	if !ok {
		t.Fatal("page lost on reload")
	}
	if got.Name != want.Name || got.Kind != "project" || got.DocID != want.DocID || got.DocID == "" ||
		len(got.State) != 1 || len(got.History) != 1 || got.Updated != want.Updated {
		t.Errorf("reloaded page differs:\n got %+v\nwant %+v", got, want)
	}
	// A missing file starts empty rather than failing.
	empty, err := NewPageStore(t.TempDir(), store)
	if err != nil || len(empty.List("u", "a")) != 0 {
		t.Errorf("empty dir: err=%v", err)
	}
}

func TestPagesReindexKeepsOneLiveDoc(t *testing.T) {
	store, _ := newPagesStore(t)
	_, _, _ = store.pages.Apply("u", "a", "s1", []PageUpdate{{Name: "HyAtlas", Set: []PageSet{set("model", "poolside")}}}, nil)
	pg := store.pages.List("u", "a")[0]
	first := pg.DocID
	if first == "" {
		t.Fatal("no doc id after first apply")
	}
	shown := map[string]bool{pg.ID: true}
	_, _, _ = store.pages.Apply("u", "a", "s2", []PageUpdate{{Page: pg.ID, Set: []PageSet{set("model", "stepfun")}}}, shown)
	pg, _ = store.pages.Get(pg.ID)
	second := pg.DocID
	if second == first {
		t.Fatal("doc id did not change with the text")
	}
	// An event alone changes history, not the indexed text: no new document.
	_, _, _ = store.pages.Apply("u", "a", "s3", []PageUpdate{{Page: pg.ID, Event: "tested"}}, shown)
	if again, _ := store.pages.Get(pg.ID); again.DocID != second {
		t.Error("an event-only update re-indexed the page")
	}

	live, total := store.List(memory.L8Entity, "u", "a", 50, 0, false)
	if total != 1 || len(live) != 1 || live[0].ID != second {
		t.Fatalf("want exactly the current doc live, got %d: %+v", total, live)
	}
	if !strings.Contains(live[0].Content, "stepfun") || strings.Contains(live[0].Content, "poolside") {
		t.Errorf("live doc is not the current text: %q", live[0].Content)
	}
	if live[0].Meta["page_id"] != pg.ID || live[0].Meta["name"] != "HyAtlas" {
		t.Errorf("doc meta: %v", live[0].Meta)
	}
	old := store.GetMany([]string{first})
	if len(old) != 1 || !isSuperseded(old[0]) || old[0].Meta["superseded_by"] != second {
		t.Errorf("old version not superseded by the new one: %+v", old)
	}

	// Related finds the page through the live doc.
	rel := store.pages.Related(pageText(pg), "u", "a", 3)
	if len(rel) != 1 || rel[0].ID != pg.ID {
		t.Errorf("Related: %+v", rel)
	}
}

func TestPageUpdatesTolerantParse(t *testing.T) {
	cases := map[string]int{
		`{"facts":[]}`: 0,
		`{"facts":[],"pages":[{"page":"new","name":"A","set":[{"attr":"x","value":"y"}]},{"name":"B"}]}`:   2,
		`{"facts":[],"pages":{"name":"Solo","set":{"attr":"x","value":"y"}}}`:                              1,
		`{"facts":[],"pages":"nonsense"}`:                                                                  0,
		`{"facts":[],"pages":[1,"x",null,{"name":"Ok","set":"bad","kind":3}]}`:                             1,
		`{"facts":[],"pages":[{"name":"N","set":[{"attr":"v","value":4.6},{"attr":"b","value":true},7]}]}`: 1,
		`{"facts":[],"pages":null}`: 0,
	}
	for in, want := range cases {
		ex, err := parseExtraction(in)
		if err != nil {
			t.Errorf("%s: %v", in, err)
			continue
		}
		if len(ex.Pages) != want {
			t.Errorf("%s: %d pages, want %d", in, len(ex.Pages), want)
		}
	}
	ex, _ := parseExtraction(`{"facts":[],"pages":[{"name":"N","set":[{"attr":"v","value":4.6},{"attr":"b","value":true},7]}]}`)
	if got := ex.Pages[0].Set; len(got) != 2 || got[0].Value != "4.6" || got[1].Value != "true" {
		t.Errorf("scalar values: %+v", got)
	}
}
