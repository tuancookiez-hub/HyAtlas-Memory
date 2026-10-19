package main

import (
	"fmt"
	"reflect"
	"sort"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// TestKeywordSearchRawOnlyForLite checks that raw rows are keyword-ranked in an
// all-layer search only when the caller asks for them, and that the lite pipeline
// asks while the pro one does not.
func TestKeywordSearchRawOnlyForLite(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	a, b, c := newID(), newID(), newID()
	for id, text := range map[string]string{
		a: "the build failed with exit code 19528 on CI",
		b: "lunch was good today",
		c: "nothing about numbers here",
	} {
		if err := srv.store.Add(memory.L2Raw, id, text, map[string]string{"user_id": "u", "agent_id": "a"}); err != nil {
			t.Fatal(err)
		}
	}
	qv, _ := srv.store.embed.Embed(srv.store.ctx, "19528")

	off, err := srv.store.keywordSearch("19528", qv, 5, "", []string{"u"}, "a", false)
	if err != nil || len(off) != 0 {
		t.Fatalf("includeRaw=false: %d hits, %v; want none", len(off), err)
	}
	on, err := srv.store.keywordSearch("19528", qv, 5, "", []string{"u"}, "a", true)
	if err != nil || len(on) != 1 || on[0].ID != a {
		t.Fatalf("includeRaw=true: %+v, %v; want only %s", on, err, a)
	}

	// The pipeline passes the flag from the server's mode.
	for mode, want := range map[Mode]int{ModeLite: 1, ModePro: 0} {
		srv.mode = mode
		run, err := srv.parseSearchRequest(searchRequest{Query: "19528", UserID: "u", AgentID: "a", Reader: "keyword"})
		if err != nil {
			t.Fatal(err)
		}
		if err := run.retrieve(); err != nil {
			t.Fatal(err)
		}
		if len(run.hits) != want {
			t.Errorf("mode %s: keyword reader found %d hits, want %d", mode, len(run.hits), want)
		}
		if want == 1 && run.hits[0].ID != a {
			t.Errorf("mode %s: found %s, want %s", mode, run.hits[0].ID, a)
		}
	}
}

// TestSearchWhereExcludesLabelInside checks that twenty experiences nearer to the query
// than one ordinary fact do not hide the fact from extractContext or
// reconcileCandidates.
func TestSearchWhereExcludesLabelInside(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	vecs := vecEmbedder{"query fact": {1, 0, 0}, "ordinary fact": simTo(0.8)}
	for i := 0; i < 20; i++ {
		vecs[fmt.Sprintf("lesson %d", i)] = simTo(0.99)
	}
	st := guardStore(t, vecs)
	for i := 0; i < 20; i++ {
		meta := map[string]string{"user_id": "u", "agent_id": "a", "ts": now, "source_layer_label": experienceLabel}
		if err := st.Add(memory.L3Fact, newID(), fmt.Sprintf("lesson %d", i), meta); err != nil {
			t.Fatal(err)
		}
	}
	factID := addRow(t, st, memory.L3Fact, "ordinary fact", now)
	queryID := addRow(t, st, memory.L3Fact, "query fact", now)

	ec := st.extractContext("query fact", "u", "a", time.Now())
	found := false
	for _, r := range ec.Related {
		found = found || r.ID == factID
	}
	if !found {
		t.Errorf("extractContext related = %+v, want it to hold %s", ec.Related, factID)
	}

	q := st.GetMany([]string{queryID})
	cands := st.reconcileCandidates(q[0], []string{"u"}, "a")
	if len(cands) != 1 || cands[0].ID != factID {
		t.Errorf("reconcileCandidates = %+v, want only %s", cands, factID)
	}

	// Once the experiences are deleted the over-fetch count falls back to zero.
	if n, err := st.Delete(nil, memory.L3Fact, "u", "a"); err != nil || n != 22 {
		t.Fatalf("delete = %d, %v", n, err)
	}
	if got := st.labelRows[memory.L3Fact][experienceLabel]; got != 0 {
		t.Errorf("labelRows after delete = %d, want 0", got)
	}
}

// TestUpdateMeta checks that updateMeta sets and removes keys on a live row, keeps its
// content and vector, and refuses a superseded or missing row.
func TestUpdateMeta(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{"keep me": {1, 0, 0}, "gone": {0, 1, 0}})
	id := addRow(t, st, memory.L3Fact, "keep me", now)
	before := st.vectors([]string{id})[id]

	if err := st.updateMeta(id, map[string]string{"note": "hello", "ts": ""}); err != nil {
		t.Fatal(err)
	}
	d := st.GetMany([]string{id})[0]
	if d.Meta["note"] != "hello" || d.Meta["ts"] != "" || d.Meta["user_id"] != "u" || d.Content != "keep me" {
		t.Errorf("index row after patch: %+v", d)
	}
	if after := st.vectors([]string{id})[id]; !reflect.DeepEqual(before, after) {
		t.Errorf("vector changed: %v -> %v", before, after)
	}
	doc, err := st.cols[memory.L3Fact].GetByID(st.ctx, id)
	if err != nil || doc.Content != "keep me" || doc.Metadata["note"] != "hello" || doc.Metadata["ts"] != "" {
		t.Errorf("chromem row after patch: %+v, %v", doc, err)
	}
	if err := st.updateMeta(id, map[string]string{"note": ""}); err != nil {
		t.Fatal(err)
	}
	if v, ok := st.GetMany([]string{id})[0].Meta["note"]; ok {
		t.Errorf("note still present: %q", v)
	}

	if _, err := st.Supersede([]string{id}, ""); err != nil {
		t.Fatal(err)
	}
	if err := st.updateMeta(id, map[string]string{"x": "y"}); err == nil {
		t.Error("updateMeta on a superseded row succeeded, want an error")
	}
	if err := st.updateMeta("missing", map[string]string{"x": "y"}); err == nil {
		t.Error("updateMeta on a missing row succeeded, want an error")
	}
}

// TestClauseHitsOwnersManyOwners checks that several owners are served by one query
// and only their rows come back.
func TestClauseHitsOwnersManyOwners(t *testing.T) {
	long := func(tag string) string {
		return tag + " first sentence is quite long and detailed about things, going on for a good while longer than strictly needed. " +
			tag + " second sentence is also long and detailed about other things, also going on for a good while longer than needed. " +
			tag + " third sentence goes on and on about yet more things here, with still more words added to be safe."
	}
	vecs := vecEmbedder{}
	st := guardStore(t, vecs)
	now := time.Now().UTC().Format(time.RFC3339)
	texts := map[string]string{}
	for _, u := range []string{"u1", "u2", "u3"} {
		texts[u] = long(u)
	}
	// Every text and clause embeds to the same vector, so ranking ties and only the
	// owner filter decides.
	for _, tx := range texts {
		vecs[tx] = []float32{1, 0, 0}
		for _, cl := range splitClauses(tx) {
			vecs[cl] = []float32{1, 0, 0}
		}
	}
	ids := map[string]string{}
	for u, tx := range texts {
		id := newID()
		if err := st.Add(memory.L3Fact, id, tx, map[string]string{"user_id": u, "agent_id": "a", "ts": now}); err != nil {
			t.Fatal(err)
		}
		ids[u] = id
	}
	got := st.clauseHitsOwners([]float32{1, 0, 0}, []string{"u1", "u2"}, "a", 5)
	var gotIDs []string
	for _, h := range got {
		gotIDs = append(gotIDs, h.ID)
	}
	sort.Strings(gotIDs)
	want := []string{ids["u1"], ids["u2"]}
	sort.Strings(want)
	if !reflect.DeepEqual(gotIDs, want) {
		t.Errorf("clauseHitsOwners = %v, want %v", gotIDs, want)
	}
}

// TestMirrorsOfTracksPutsAndForgets checks the (layer, source_id) map follows adds,
// content changes, supersedes and deletes.
func TestMirrorsOfTracksPutsAndForgets(t *testing.T) {
	st := guardStore(t, vecEmbedder{"likes tea": {1, 0, 0}, "likes coffee": {0, 1, 0}})
	m1, m2 := newID(), newID()
	for _, id := range []string{m1, m2} {
		if err := st.Add(memory.L1Profile, id, "likes tea", map[string]string{"source_id": "raw-1"}); err != nil {
			t.Fatal(err)
		}
	}
	want := []string{m1, m2}
	sort.Strings(want)
	if got := st.MirrorsOf(memory.L1Profile, "raw-1", "likes tea"); !reflect.DeepEqual(got, want) {
		t.Fatalf("MirrorsOf = %v, want %v", got, want)
	}
	// Re-adding a row under the same id must not duplicate it in the map.
	if err := st.Add(memory.L1Profile, m1, "likes coffee", map[string]string{"source_id": "raw-1"}); err != nil {
		t.Fatal(err)
	}
	if got := st.MirrorsOf(memory.L1Profile, "raw-1", "likes tea"); !reflect.DeepEqual(got, []string{m2}) {
		t.Errorf("after rewrite MirrorsOf = %v, want [%s]", got, m2)
	}
	if got := st.MirrorsOf(memory.L1Profile, "raw-1", "likes coffee"); !reflect.DeepEqual(got, []string{m1}) {
		t.Errorf("coffee MirrorsOf = %v, want [%s]", got, m1)
	}
	if _, err := st.Delete([]string{m2}, "", "", ""); err != nil {
		t.Fatal(err)
	}
	if got := st.MirrorsOf(memory.L1Profile, "raw-1", "likes tea"); len(got) != 0 {
		t.Errorf("after delete MirrorsOf = %v, want none", got)
	}
	if len(st.mirrors) != 1 {
		t.Errorf("mirror buckets = %d, want 1", len(st.mirrors))
	}
}

// TestListTopKMatchesFullSort checks that a small page of many rows comes out in the
// same order, with the same total, as the full sort.
func TestListTopKMatchesFullSort(t *testing.T) {
	st := guardStore(t, vecEmbedder{})
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	for i := 0; i < 80; i++ {
		// Some timestamps repeat, so the id tie-break matters.
		ts := base.Add(time.Duration(i%50) * time.Hour).Format(time.RFC3339)
		id := fmt.Sprintf("row-%03d", i)
		st.mu.Lock()
		st.putLocked(docIndexFrom(id, string(memory.L3Fact), id, map[string]string{"user_id": "u", "ts": ts}))
		st.mu.Unlock()
	}
	all, total := st.List(memory.L3Fact, "u", "", 80, 0, false)
	if total != 80 || len(all) != 80 {
		t.Fatalf("full list = %d of %d", len(all), total)
	}
	for _, c := range []struct{ limit, offset int }{{5, 0}, {3, 4}, {1, 0}, {5, 5}} {
		page, total := st.List(memory.L3Fact, "u", "", c.limit, c.offset, false)
		if total != 80 || !reflect.DeepEqual(page, all[c.offset:c.offset+c.limit]) {
			t.Errorf("limit %d offset %d: page %v total %d", c.limit, c.offset, docIDs(page), total)
		}
	}
}

// docIDs lists the ids of docs, for test messages.
func docIDs(docs []DocIndex) []string {
	out := make([]string, len(docs))
	for i, d := range docs {
		out[i] = d.ID
	}
	return out
}

// TestSetExtractedReachesChromem checks the extracted flag is written to the chromem
// row, so an index rebuilt from chromem does not extract the row again.
func TestSetExtractedReachesChromem(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{"raw words": {1, 0, 0}})
	id := addRow(t, st, memory.L2Raw, "raw words", now)
	if err := st.SetExtracted(id, true); err != nil {
		t.Fatal(err)
	}
	if !st.GetMany([]string{id})[0].Extracted {
		t.Error("index row not marked extracted")
	}
	doc, err := st.cols[memory.L2Raw].GetByID(st.ctx, id)
	if err != nil || doc.Metadata["extracted"] != "true" {
		t.Errorf("chromem row not marked extracted: %+v, %v", doc.Metadata, err)
	}
}
