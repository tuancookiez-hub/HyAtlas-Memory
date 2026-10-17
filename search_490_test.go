package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// searchAll runs one search through the handler and returns every channel.
func searchAll(t *testing.T, srv *Server, query string, extra ...string) map[string][]map[string]any {
	t.Helper()
	body := `{"query":"` + query + `","user_id":"u","agent_id":"a","min_score":0.5,"limit":5` + strings.Join(extra, "") + `}`
	w := httptest.NewRecorder()
	srv.handleSearch(w, httptest.NewRequest("POST", "/api/v1/search", strings.NewReader(body)))
	if w.Code != 200 {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	var resp struct {
		Memories map[string][]map[string]any `json:"memories"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	return resp.Memories
}

func hasID(hits []map[string]any, id string) bool {
	for _, h := range hits {
		if h["memory_id"] == id {
			return true
		}
	}
	return false
}

// A long fact whose whole-text vector misses the query is still found through
// one of its clauses.
func TestSearchFindsLongFactByClause(t *testing.T) {
	long := "TradingMaker is a paper-only trading lab that never moves real money. " +
		"Its SQLite ledger records simulated fills only, and every report says so. " +
		"TWS on port 7497 is the source of truth for positions and fills at all times. " +
		"The desk writes briefs that the agent rail consumes before any paper order. " +
		"Every brief names its ticker, its size and the catalyst it is waiting for."
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	srv.store = guardStore(t, vecEmbedder{
		long:                   {0, 0, 1},
		"which port is TWS on": {1, 0, 0},
		"TradingMaker is a paper-only trading lab that never moves real money.":         {0, 0, 1},
		"Its SQLite ledger records simulated fills only, and every report says so.":     {0, 0, 1},
		"TWS on port 7497 is the source of truth for positions and fills at all times.": simTo(0.9),
		"The desk writes briefs that the agent rail consumes before any paper order.":   {0, 0, 1},
		"Every brief names its ticker, its size and the catalyst it is waiting for.":    {0, 0, 1},
	})
	id := addRow(t, srv.store, memory.L3Fact, long, nowTS())
	// Vector-only, so keyword search on "TWS" and "port" cannot find it instead.
	if !hasID(searchAll(t, srv, "which port is TWS on", `,"reader":"legacy"`)["normal"], id) {
		t.Fatal("long fact not found through its clause")
	}
}

// A schema citing a returned fact as evidence gets a slot in the profile channel.
func TestSearchAddsSchemaBehindFacts(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	fact := addRow(t, srv.store, memory.L3Fact, "deploys go behind a feature flag first", nowTS())
	other := addRow(t, srv.store, memory.L3Fact, "the flag is removed after a week", nowTS())
	if err := srv.store.Add(memory.L6Schema, "sch1", "The user ships risky changes behind feature flags",
		map[string]string{"user_id": "u", "agent_id": "a", "ts": nowTS(), "evidence": fact + "," + other}); err != nil {
		t.Fatal(err)
	}
	got := searchAll(t, srv, "deploys go behind a feature flag first")
	if !hasID(got["normal"], fact) {
		t.Fatal("fact itself not found")
	}
	if !hasID(got["profile"], "sch1") {
		t.Fatalf("schema behind the fact not added: %v", got["profile"])
	}
}

// mergeBest keeps one hit per id at its best score, best first.
func TestMergeBest(t *testing.T) {
	a := []SearchHit{{ID: "x", Score: 0.5}, {ID: "y", Score: 0.4}}
	b := []SearchHit{{ID: "y", Score: 0.9}, {ID: "z", Score: 0.1}}
	got := mergeBest(a, b)
	if len(got) != 3 || got[0].ID != "y" || got[0].Score != 0.9 || got[2].ID != "z" {
		t.Fatalf("mergeBest = %+v", got)
	}
}

// Fusion weights shift which ranking wins a tie.
func TestFuseHitsWeighted(t *testing.T) {
	vec := []SearchHit{{ID: "v", Score: 0.9}}
	kw := []SearchHit{{ID: "k", Score: 0.9}}
	if got := fuseHitsWeighted(vec, kw, 0, 0, 0.6, 1.4); got[0].ID != "k" {
		t.Errorf("keyword-weighted fusion put %s first", got[0].ID)
	}
	if got := fuseHitsWeighted(vec, kw, 0, 0, 1.3, 0.7); got[0].ID != "v" {
		t.Errorf("vector-weighted fusion put %s first", got[0].ID)
	}
}
