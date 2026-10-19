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

// schemaRows returns the live L6 schemas of owner u and agent a.
func schemaRows(t *testing.T, st *MemoryStore) []DocIndex {
	t.Helper()
	rows, _ := st.List(memory.L6Schema, "u", "a", 100, 0, false)
	return rows
}

// A schema keeps the batch facts it cites, each once, and drops invented ids.
func TestSchemaEvidenceIsStored(t *testing.T) {
	st := guardStore(t, vecEmbedder{
		"fact a":  {1, 0, 0},
		"fact b":  {0, 1, 0},
		"pattern": {0, 0, 1},
	})
	a := addRow(t, st, memory.L3Fact, "fact a", time.Now().UTC().Format(time.RFC3339))
	b := addRow(t, st, memory.L3Fact, "fact b", time.Now().UTC().Format(time.RFC3339))

	runScope(t, st, map[string]any{
		"schemas": []map[string]any{{
			"pattern":  "pattern",
			"evidence": []string{a, b, "invented-id"},
		}},
	})

	rows := schemaRows(t, st)
	if len(rows) != 1 {
		t.Fatalf("got %d schemas, want 1", len(rows))
	}
	if got := rows[0].Meta["evidence"]; got != a+","+b {
		t.Fatalf("evidence = %q, want %q", got, a+","+b)
	}
}

// A schema with only one valid evidence id is still written, without the key.
func TestSchemaWithTooLittleEvidenceHasNoKey(t *testing.T) {
	st := guardStore(t, vecEmbedder{
		"fact a":  {1, 0, 0},
		"fact b":  {0, 1, 0},
		"pattern": {0, 0, 1},
	})
	a := addRow(t, st, memory.L3Fact, "fact a", time.Now().UTC().Format(time.RFC3339))
	addRow(t, st, memory.L3Fact, "fact b", time.Now().UTC().Format(time.RFC3339))

	rep := runScope(t, st, map[string]any{
		"schemas": []map[string]any{{
			"pattern":  "pattern",
			"evidence": []string{a, "invented-id"},
		}},
	})

	rows := schemaRows(t, st)
	if len(rows) != 1 {
		t.Fatalf("got %d schemas, want 1 (rep errors: %v)", len(rows), rep.Errors)
	}
	if _, ok := rows[0].Meta["evidence"]; ok {
		t.Fatalf("evidence key present with one valid id: %q", rows[0].Meta["evidence"])
	}
}

// schemasSupporting puts the schema citing more of the given facts first, leaves out
// a superseded schema and another owner's schema, and caps the result at limit.
func TestSchemasSupportingOrderingAndFilters(t *testing.T) {
	st := guardStore(t, vecEmbedder{
		"one cite older":        {1, 0, 0},
		"two cites":             {1, 0, 0},
		"superseded two cites":  {1, 0, 0},
		"other owner two cites": {1, 0, 0},
		"one cite newer":        {1, 0, 0},
	})
	now := time.Now().UTC()
	add := func(user, content string, ts time.Time, evidence string) string {
		t.Helper()
		id := newID()
		meta := map[string]string{"user_id": user, "agent_id": "a", "ts": ts.Format(time.RFC3339)}
		if evidence != "" {
			meta["evidence"] = evidence
		}
		if err := st.Add(memory.L6Schema, id, content, meta); err != nil {
			t.Fatal(err)
		}
		return id
	}

	oneCite := add("u", "one cite older", now.Add(-2*time.Hour), "f1")
	twoCites := add("u", "two cites", now.Add(-3*time.Hour), "f1,f2")
	superseded := add("u", "superseded two cites", now.Add(-time.Hour), "f1,f2")
	otherOwner := add("v", "other owner two cites", now.Add(-time.Hour), "f1,f2")
	newerOneCite := add("u", "one cite newer", now.Add(-time.Minute), "f1")
	if _, err := st.Supersede([]string{superseded}, ""); err != nil {
		t.Fatal(err)
	}

	got := st.schemasSupporting([]string{"f1", "f2", "f3"}, []string{"u"}, "a", 2)
	if len(got) != 2 {
		t.Fatalf("got %d hits, want 2", len(got))
	}
	if got[0].ID != twoCites {
		t.Fatalf("first hit %q, want the two-cite schema %q", got[0].Content, twoCites)
	}
	if got[1].ID != newerOneCite {
		t.Fatalf("second hit %q, want the newer one-cite schema (older one-cite %q is capped out)", got[1].Content, oneCite)
	}
	for _, h := range got {
		if h.ID == superseded || h.ID == otherOwner {
			t.Fatalf("hit %q must not be returned", h.Content)
		}
		if h.Layer != memory.L6Schema || h.Score != 0 {
			t.Fatalf("hit %q has layer %q score %v", h.Content, h.Layer, h.Score)
		}
	}
	if got[0].Meta["evidence"] != "f1,f2" {
		t.Fatalf("meta evidence = %q, want a copy of the row's metadata", got[0].Meta["evidence"])
	}
	// The returned Meta is a copy, so changing it leaves the index untouched.
	got[0].Meta["evidence"] = "changed"
	again := st.schemasSupporting([]string{"f1"}, nil, "", 10)
	for _, h := range again {
		if h.ID == twoCites && h.Meta["evidence"] != "f1,f2" {
			t.Fatalf("index row changed through a returned hit: %q", h.Meta["evidence"])
		}
	}
}

// The consolidation prompt asks the model for evidence on each schema.
func TestConsolidationPromptAsksForEvidence(t *testing.T) {
	var prompt string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Messages []struct {
				Content string `json:"content"`
			} `json:"messages"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err == nil && len(req.Messages) > 0 {
			prompt = req.Messages[0].Content
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{}"}}]}`))
	}))
	t.Cleanup(srv.Close)

	st := guardStore(t, vecEmbedder{})
	c := NewConsolidator(st, NewLLMClient(srv.URL, "k", "m"), time.Hour, 0, 50)
	facts := []DocIndex{{ID: "f1", Content: "x", Ts: time.Now().UTC().Format(time.RFC3339)}}
	if _, err := c.ask(context.Background(), facts, nil); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(prompt, `"evidence"`) {
		t.Fatalf("prompt does not mention evidence:\n%s", prompt)
	}
}
