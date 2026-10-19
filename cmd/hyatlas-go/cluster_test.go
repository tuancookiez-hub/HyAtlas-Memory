package main

import (
	"context"
	"fmt"
	"net/http"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// topicEmbedder is a stand-in for a semantic embedder. Texts that start with
// "topic:<name>" all map to one unit vector per name, so facts of one topic are
// identical to each other and orthogonal to every other topic; any other text gets
// its own unused axis. It counts Embed calls so a test can tell whether a search ran.
type topicEmbedder struct {
	mu    sync.Mutex
	axes  map[string]int
	calls int
}

const topicDims = 256

func (e *topicEmbedder) Embed(_ context.Context, text string) ([]float32, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.calls++
	key := text
	if strings.HasPrefix(text, "topic:") {
		key = strings.SplitN(text, " ", 2)[0]
	}
	if e.axes == nil {
		e.axes = map[string]int{}
	}
	axis, ok := e.axes[key]
	if !ok {
		axis = len(e.axes) % topicDims
		e.axes[key] = axis
	}
	v := make([]float32, topicDims)
	v[axis] = 1
	return v, nil
}

func (e *topicEmbedder) count() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.calls
}

func newTopicStore(t *testing.T) (*MemoryStore, *topicEmbedder) {
	t.Helper()
	dir := t.TempDir()
	em := &topicEmbedder{}
	store, err := NewMemoryStore(context.Background(), dir, em, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	t.Cleanup(store.Close)
	return store, em
}

// seedTexts writes the texts as alice/a1 facts, texts[0] the newest, and returns
// the owner's live facts newest first, the way Once holds them.
func seedTexts(t *testing.T, store *MemoryStore, texts []string) []DocIndex {
	t.Helper()
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	n := len(texts)
	for i, text := range texts {
		ts := base.Add(time.Duration(n-i) * time.Minute).Format(time.RFC3339)
		seedAt(t, store, ts, text)
	}
	_, total := store.List(memory.L3Fact, "", "", 1, 0, false)
	docs, _ := store.List(memory.L3Fact, "", "", total, 0, false)
	sortNewestFirst(docs)
	return docs
}

// checkPartition fails unless every fact is in exactly one window and no window is
// larger than batch.
func checkPartition(t *testing.T, facts []DocIndex, windows [][]DocIndex, batch int) {
	t.Helper()
	seen := map[string]int{}
	for wi, w := range windows {
		if len(w) > batch {
			t.Errorf("window %d holds %d facts, over batch %d", wi, len(w), batch)
		}
		for _, f := range w {
			seen[f.ID]++
		}
	}
	for _, f := range facts {
		if seen[f.ID] != 1 {
			t.Errorf("fact %q appears in %d windows, want 1", f.Content, seen[f.ID])
		}
	}
	if len(seen) != len(facts) {
		t.Errorf("windows hold %d distinct facts, want %d", len(seen), len(facts))
	}
}

// Facts about two topics interleaved in time must each land in one window; with
// time-ordered windows every window would hold half of each.
func TestClusterWindowsGroupsInterleavedTopics(t *testing.T) {
	store, _ := newTopicStore(t)
	var texts []string
	for i := 0; i < 30; i++ {
		topic := "topic:a"
		if i%2 == 1 {
			topic = "topic:b"
		}
		texts = append(texts, fmt.Sprintf("%s fact %d", topic, i))
	}
	facts := seedTexts(t, store, texts)
	c := &Consolidator{store: store, batch: 20}

	windows, kinds := c.clusterPlan(facts)
	if len(windows) != 2 {
		t.Fatalf("got %d windows, want 2 (one per topic)", len(windows))
	}
	checkPartition(t, facts, windows, 20)
	for wi, w := range windows {
		if !kinds[wi] {
			t.Errorf("window %d is not marked as a topic cluster", wi)
		}
		prefix := strings.SplitN(w[0].Content, " ", 2)[0]
		for _, f := range w {
			if !strings.HasPrefix(f.Content, prefix) {
				t.Errorf("window %d mixes %q with %q", wi, prefix, f.Content)
			}
		}
	}
	// The newest fact (index 0) is topic a, so its window comes first.
	if !strings.HasPrefix(windows[0][0].Content, "topic:a") {
		t.Errorf("first window starts with %q, want the newest fact's topic", windows[0][0].Content)
	}
}

// Topics under clusterMinSize dissolve into time-ordered leftover windows. Every
// fact is still in exactly one window and none is over batch.
func TestClusterWindowsDissolvesSmallTopics(t *testing.T) {
	store, _ := newTopicStore(t)
	var texts []string
	for i := 0; i < 12; i++ { // one big topic: a 10-fact window plus a 2-fact remainder
		texts = append(texts, fmt.Sprintf("topic:big fact %d", i))
	}
	for i := 0; i < 3; i++ { // unrelated one-offs
		texts = append(texts, fmt.Sprintf("lone fact %d", i))
	}
	facts := seedTexts(t, store, texts)
	c := &Consolidator{store: store, batch: 10}

	windows, kinds := c.clusterPlan(facts)
	checkPartition(t, facts, windows, 10)
	if len(windows) != 2 || !kinds[0] || kinds[1] {
		t.Fatalf("windows=%d kinds=%v, want one topic window then one time window", len(windows), kinds)
	}
	if len(windows[0]) != 10 {
		t.Errorf("topic window holds %d facts, want 10", len(windows[0]))
	}
	// The leftovers (2 big-topic remainder + 3 lone) stay newest first.
	rest := windows[1]
	for i := 1; i < len(rest); i++ {
		if rest[i-1].Ts < rest[i].Ts {
			t.Errorf("leftover window is not newest first at %d", i)
		}
	}
}

// Leftover windows are consecutive chunks like windowAt's: the last is pulled back
// to end at the final fact, so it is full and never a lone fact.
func TestChunkTimeOrderedAvoidsLoneLastWindow(t *testing.T) {
	var facts []DocIndex
	for i := 0; i < 11; i++ {
		facts = append(facts, DocIndex{ID: fmt.Sprintf("f%d", i)})
	}
	got := chunkTimeOrdered(facts, 5)
	sizes := []int{}
	var flat []string
	for _, w := range got {
		sizes = append(sizes, len(w))
		for _, f := range w {
			flat = append(flat, f.ID)
		}
	}
	if fmt.Sprint(sizes) != "[5 5 5]" {
		t.Errorf("sizes = %v, want [5 5 5]", sizes)
	}
	if flat[0] != "f0" || flat[5] != "f5" || flat[10] != "f6" || flat[14] != "f10" {
		t.Errorf("windows = %v, want f0..f4, f5..f9, f6..f10", flat)
	}
}

// An owner that fits in one window needs no grouping, so no search runs.
func TestClusterWindowsSmallOwnerIsOneWindowWithoutSearch(t *testing.T) {
	store, em := newTopicStore(t)
	var texts []string
	for i := 0; i < 8; i++ {
		texts = append(texts, fmt.Sprintf("topic:a fact %d", i))
	}
	facts := seedTexts(t, store, texts)
	c := &Consolidator{store: store, batch: 8}

	before := em.count()
	windows := c.clusterWindows(facts)
	if len(windows) != 1 || len(windows[0]) != 8 {
		t.Fatalf("windows = %d, want one window of 8", len(windows))
	}
	if em.count() != before {
		t.Errorf("a search ran for an owner that fits in one window")
	}
}

// The reason for clustering: an old fact and the newer one that contradicts it,
// 60 facts apart in time, must reach the model in the same call.
func TestOnceSendsContradictingFactsTogether(t *testing.T) {
	store, _ := newTopicStore(t)
	topicAt := map[int]string{
		0:  "topic:model the extraction model now uses stepfun",
		10: "topic:model extraction model notes 10",
		30: "topic:model extraction model notes 30",
		60: "topic:model the extraction model is poolside, 403",
		65: "topic:model extraction model notes 65",
	}
	var texts []string
	for i := 0; i < 70; i++ {
		if s, ok := topicAt[i]; ok {
			texts = append(texts, s)
		} else {
			texts = append(texts, fmt.Sprintf("unrelated fact %d", i))
		}
	}
	seedTexts(t, store, texts)

	var log promptLog
	mock := scopeMock(t, &log, func(string) (int, string) {
		return http.StatusOK, chatBody(t, Consolidation{})
	})
	defer mock.Close()
	c := NewConsolidator(store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 20)

	if _, err := c.Once(context.Background()); err != nil {
		t.Fatal(err)
	}
	// One topic cluster plus the 65 unrelated facts in time clusters of 20: five
	// calls, all in this one pass.
	prompts := log.all()
	if len(prompts) != 5 {
		t.Fatalf("pass made %d calls, want 5", len(prompts))
	}
	var topic string
	for _, p := range prompts {
		if strings.Contains(p, "stepfun") {
			topic = p
		}
	}
	if topic == "" || !strings.Contains(topic, "poolside") {
		t.Fatalf("the contradicting pair was not sent in one call")
	}
	if got := len(factIDs(topic)); got != 5 {
		t.Errorf("topic cluster holds %d facts, want the 5 about the topic", got)
	}
	// The clusters are stored, so later passes decide what is due without cutting them again.
	key := scopeKey{user: "alice", agent: "a1"}.String()
	if got := len(c.clusters[key].Clusters); got != 1+4 {
		t.Errorf("stored cluster count = %d, want 5", got)
	}
}

// clustersOf returns an owner's stored clusters as sorted-member strings, so two
// clusterings can be compared regardless of cluster order.
func clustersOf(c *Consolidator, k scopeKey) []string {
	var out []string
	for _, cs := range c.clusters[k.String()].Clusters {
		ids := append([]string(nil), cs.FactIDs...)
		sort.Strings(ids)
		out = append(out, strings.Join(ids, ","))
	}
	sort.Strings(out)
	return out
}

// dueIDs returns the IDs of an owner's due clusters.
func dueIDs(c *Consolidator, k scopeKey) []string {
	var out []string
	for _, cs := range c.clusters[k.String()].Clusters {
		if cs.due() {
			out = append(out, cs.ID)
		}
	}
	return out
}

// seedTopics writes n facts for each of two topics (interleaved) and returns the
// consolidator and the owner's facts.
func seedTopics(t *testing.T, n int) (*Consolidator, []DocIndex, scopeKey) {
	t.Helper()
	store, _ := newTopicStore(t)
	var texts []string
	for i := 0; i < 2*n; i++ {
		topic := "topic:a"
		if i%2 == 1 {
			topic = "topic:b"
		}
		texts = append(texts, fmt.Sprintf("%s fact %d", topic, i))
	}
	facts := seedTexts(t, store, texts)
	return &Consolidator{store: store, batch: 20, clusters: map[string]*ownerClusters{}}, facts, scopeKey{"alice", "a1"}
}

// Cutting the same facts twice gives the same clusters, and a second sync with
// nothing changed leaves them and their membership alone.
func TestSyncClustersIsStable(t *testing.T) {
	c, facts, k := seedTopics(t, 15)
	c.syncClusters(k, facts)
	first := clustersOf(c, k)
	if len(first) != 2 {
		t.Fatalf("got %d clusters, want 2 topics: %v", len(first), first)
	}
	c.syncClusters(k, facts)
	if got := clustersOf(c, k); fmt.Sprint(got) != fmt.Sprint(first) {
		t.Errorf("resync changed the clusters:\n%v\n%v", first, got)
	}
	other := &Consolidator{store: c.store, batch: 20, clusters: map[string]*ownerClusters{}}
	other.syncClusters(k, facts)
	if got := clustersOf(other, k); fmt.Sprint(got) != fmt.Sprint(first) {
		t.Errorf("building twice differs:\n%v\n%v", first, got)
	}
}

// One new fact about topic A dirties only A's cluster; one superseded fact dirties
// only its own.
func TestNewAndRemovedFactDirtyOnlyTheirCluster(t *testing.T) {
	c, facts, k := seedTopics(t, 15)
	oc := c.syncClusters(k, facts)
	for _, cs := range oc.Clusters {
		cs.DoneFP = cs.fp() // as if both had just run
	}
	if due := dueIDs(c, k); len(due) != 0 {
		t.Fatalf("clusters due right after running: %v", due)
	}

	seedAt(t, c.store, "2026-06-01T00:00:00Z", "topic:a a brand new fact")
	_, total := c.store.List(memory.L3Fact, "", "", 1, 0, false)
	docs, _ := c.store.List(memory.L3Fact, "", "", total, 0, false)
	sortNewestFirst(docs)
	oc = c.syncClusters(k, docs)
	due := dueIDs(c, k)
	if len(due) != 1 {
		t.Fatalf("due after one new fact = %v, want exactly one cluster", due)
	}
	var hit *clusterState
	for _, cs := range oc.Clusters {
		if cs.ID == due[0] {
			hit = cs
		}
	}
	for _, d := range c.store.GetMany(hit.FactIDs) {
		if !strings.HasPrefix(d.Content, "topic:a") {
			t.Errorf("the dirty cluster holds %q, want only topic a", d.Content)
		}
	}
	for _, cs := range oc.Clusters {
		cs.DoneFP = cs.fp()
	}

	// Supersede one topic b fact.
	var gone string
	for _, d := range docs {
		if strings.HasPrefix(d.Content, "topic:b") {
			gone = d.ID
			break
		}
	}
	if _, err := c.store.Supersede([]string{gone}, ""); err != nil {
		t.Fatal(err)
	}
	live := docs[:0:0]
	for _, d := range docs {
		if d.ID != gone {
			live = append(live, d)
		}
	}
	c.syncClusters(k, live)
	due = dueIDs(c, k)
	if len(due) != 1 {
		t.Fatalf("due after a removal = %v, want exactly one cluster", due)
	}
}
