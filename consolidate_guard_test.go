package main

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// guardStore returns a store whose embedder knows exactly the given texts.
func guardStore(t *testing.T, vecs vecEmbedder) *MemoryStore {
	t.Helper()
	dir := t.TempDir()
	st, err := NewMemoryStore(context.Background(), dir, vecs, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(st.Close)
	return st
}

func addRow(t *testing.T, st *MemoryStore, layer memory.Layer, text, ts string) string {
	t.Helper()
	id := newID()
	if err := st.Add(layer, id, text, map[string]string{"user_id": "u", "agent_id": "a", "ts": ts}); err != nil {
		t.Fatal(err)
	}
	return id
}

// runScope sends one owner's live facts through consolidateScope with reply as the
// model's answer.
func runScope(t *testing.T, st *MemoryStore, reply map[string]any) *Report {
	t.Helper()
	calls := 0
	mock := mockConsolidationServer(t, &calls, reply)
	t.Cleanup(mock.Close)
	c := NewConsolidator(st, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 50)
	owner := scopeKey{user: "u", agent: "a"}
	rep := &Report{}
	c.consolidateScope(context.Background(), owner, c.liveOf(memory.L3Fact, owner), rep)
	return rep
}

// A merge absorbs only the facts its text is about. A fact the merged text barely
// resembles stays live, and a merge left with fewer than two facts is not made.
func TestMergeLeavesOffTopicFactLive(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"merged":   {1, 0, 0},
		"close":    simTo(0.9),
		"near":     simTo(0.8),
		"offtopic": simTo(0.5),
		"pair":     {0, 0, 1},
	})
	closeID := addRow(t, st, memory.L3Fact, "close", now)
	nearID := addRow(t, st, memory.L3Fact, "near", now)
	offID := addRow(t, st, memory.L3Fact, "offtopic", now)

	rep := runScope(t, st, map[string]any{
		"merges": []any{map[string]any{"text": "merged", "layer": "project_state",
			"supersedes": []string{closeID, nearID, offID}}},
		"drops": []any{}, "schemas": []any{},
	})
	if rep.Merged != 1 || rep.Absorbed != 2 || rep.KeptOffTopic != 1 {
		t.Fatalf("merged=%d absorbed=%d kept_off_topic=%d, want 1, 2, 1", rep.Merged, rep.Absorbed, rep.KeptOffTopic)
	}
	if isSuperseded(st.GetMany([]string{offID})[0]) {
		t.Error("off-topic fact was absorbed")
	}
	for _, id := range []string{closeID, nearID} {
		if !isSuperseded(st.GetMany([]string{id})[0]) {
			t.Errorf("on-topic fact %s was not absorbed", id)
		}
	}
}

func TestMergeOfOneOnTopicFactIsNotMade(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"merged":   {1, 0, 0},
		"close":    simTo(0.9),
		"offtopic": simTo(0.4),
	})
	closeID := addRow(t, st, memory.L3Fact, "close", now)
	offID := addRow(t, st, memory.L3Fact, "offtopic", now)
	rep := runScope(t, st, map[string]any{
		"merges": []any{map[string]any{"text": "merged", "supersedes": []string{closeID, offID}}},
		"drops":  []any{}, "schemas": []any{},
	})
	if rep.Merged != 0 || rep.Absorbed != 0 {
		t.Fatalf("merged=%d absorbed=%d, want no merge", rep.Merged, rep.Absorbed)
	}
	if live, _ := st.List(memory.L3Fact, "", "", 10, 0, false); len(live) != 2 {
		t.Fatalf("live facts = %d, want the 2 originals", len(live))
	}
}

// A new schema that says what a stored one already says is not written, unless it
// is meant to replace that one.
func TestSchemaNearDuplicateIsNotWritten(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"fact one":  {0, 0, 1},
		"fact two":  {0, 0, 1},
		"stored":    {1, 0, 0},
		"restated":  simTo(0.95),
		"refined":   simTo(0.94),
		"different": simTo(0.5),
	})
	addRow(t, st, memory.L3Fact, "fact one", now)
	addRow(t, st, memory.L3Fact, "fact two", now)
	stored := addRow(t, st, memory.L6Schema, "stored", now)

	rep := runScope(t, st, map[string]any{
		"merges": []any{}, "drops": []any{},
		"schemas": []any{
			map[string]any{"pattern": "restated", "context": "x"},
			map[string]any{"pattern": "refined", "context": "x", "supersedes": []string{stored}},
			map[string]any{"pattern": "different", "context": "x"},
		},
	})
	if rep.Schemas != 2 {
		t.Fatalf("schemas written = %d, want 2 (the refinement and the different one)", rep.Schemas)
	}
	got := map[string]bool{}
	live, _ := st.List(memory.L6Schema, "", "", 10, 0, false)
	for _, d := range live {
		got[d.Content] = true
	}
	if got["restated"] || got["stored"] || !got["refined"] || !got["different"] {
		t.Fatalf("live schemas = %v, want refined and different", got)
	}
}

// With more schemas than the prompt holds, the ones shown are those nearest the
// cluster's facts, not the newest.
func TestRelevantSchemasPicksNearest(t *testing.T) {
	vecs := vecEmbedder{"the fact": {1, 0, 0}, "related old schema": simTo(0.9)}
	for i := 0; i < maxPromptSchemas+5; i++ {
		vecs[fmt.Sprintf("unrelated schema %d", i)] = []float32{0, 0, 1}
	}
	st := guardStore(t, vecs)
	old := time.Now().Add(-48 * time.Hour).UTC().Format(time.RFC3339)
	now := time.Now().UTC().Format(time.RFC3339)
	fact := addRow(t, st, memory.L3Fact, "the fact", now)
	related := addRow(t, st, memory.L6Schema, "related old schema", old)
	for i := 0; i < maxPromptSchemas+5; i++ {
		addRow(t, st, memory.L6Schema, fmt.Sprintf("unrelated schema %d", i), now)
	}

	c := NewConsolidator(st, nil, time.Hour, 0, 50)
	owner := scopeKey{user: "u", agent: "a"}
	got := c.relevantSchemas(owner, st.GetMany([]string{fact}))
	if len(got) == 0 || got[0].ID != related {
		t.Fatalf("first schema shown = %v, want the related one", got)
	}
	if len(got) > maxPromptSchemas {
		t.Fatalf("%d schemas shown, cap is %d", len(got), maxPromptSchemas)
	}
}
