package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// A drop is applied only with a reason, and the reason is kept on the dropped row.
// A bare id (the old format) or an empty reason leaves the fact live.
func TestDropNeedsAReason(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ids := seed(t, srv.store, 3, "drop")

	calls := 0
	mock := mockConsolidationServer(t, &calls, map[string]any{
		"merges": []any{},
		"drops": []any{
			ids[0],
			map[string]string{"id": ids[1], "reason": "  "},
			map[string]string{"id": ids[2], "reason": "a temporary state that has passed"},
		},
		"schemas": []any{},
	})
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rep.Dropped != 1 {
		t.Fatalf("Dropped = %d, want 1 (only the drop with a reason)", rep.Dropped)
	}
	all, _ := srv.store.ListAll("l3_fact", "", "", 10, 0, false)
	for _, d := range all {
		dead := d.Meta["invalid_at"] != ""
		switch d.ID {
		case ids[0], ids[1]:
			if dead {
				t.Errorf("fact %s was dropped without a reason", d.ID)
			}
		case ids[2]:
			if !dead || d.Meta["drop_reason"] != "a temporary state that has passed" {
				t.Errorf("dropped fact: invalid_at=%q drop_reason=%q", d.Meta["invalid_at"], d.Meta["drop_reason"])
			}
		}
	}

	// The reason is visible where history is read: /api/v1/list with superseded rows.
	w := httptest.NewRecorder()
	srv.handleList(w, httptest.NewRequest("GET", "/api/v1/list?layer=l3_fact&include_superseded=true", nil))
	var resp struct {
		Memories []map[string]any `json:"memories"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, m := range resp.Memories {
		if m["memory_id"] == ids[2] {
			found = m["drop_reason"] == "a temporary state that has passed"
		}
	}
	if !found {
		t.Error("/api/v1/list did not show the dropped fact's drop_reason")
	}
}

// graphPass runs one pass over two facts from two turns whose reply asks for an
// L5 edge and an arc, and returns the report and the prompt the model was sent.
func graphPass(t *testing.T, graph bool) (*Report, string) {
	t.Helper()
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	ids := seed(t, srv.store, 2, fmt.Sprintf("graph-%v", graph))
	arc := "the user has been building a memory server"
	reply, _ := json.Marshal(Consolidation{
		Knowledge: []CitedRelation{{From: "HyAtlas", Relation: "runs_on", To: "port 19528", Evidence: ids}},
		Arc:       &arc,
	})
	var prompt string
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		prompt = string(b)
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"choices":[{"message":{"content":%s}}]}`, string(mustJSON(reply)))
	}))
	defer mock.Close()

	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 200)
	c.graph = graph
	rep, err := c.Once(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return rep, prompt
}

// With the graph off, the prompt does not ask for edges or an arc, and any the
// model sends are not written. With it on (the constructor's default), they are.
func TestGraphSettingControlsEdgesAndArc(t *testing.T) {
	rep, prompt := graphPass(t, false)
	if rep.Edges != 0 || rep.Arc {
		t.Errorf("graph off: edges=%d arc=%v, want none", rep.Edges, rep.Arc)
	}
	if strings.Contains(prompt, `\"knowledge\"`) || strings.Contains(prompt, `\"arc\"`) {
		t.Error("graph off: the prompt still asks for knowledge or arc")
	}

	rep, prompt = graphPass(t, true)
	if rep.Edges != 1 || !rep.Arc {
		t.Errorf("graph on: edges=%d arc=%v, want 1 and true", rep.Edges, rep.Arc)
	}
	if !strings.Contains(prompt, `\"knowledge\"`) {
		t.Error("graph on: the prompt does not ask for knowledge")
	}
}

func TestEnvOn(t *testing.T) {
	for v, want := range map[string]bool{"": false, "off": false, "0": false, "on": true, "TRUE": true, " yes ": true, "1": true} {
		t.Setenv("HYATLAS_TEST_ON", v)
		if got := envOn("HYATLAS_TEST_ON"); got != want {
			t.Errorf("envOn(%q) = %v, want %v", v, got, want)
		}
	}
}
