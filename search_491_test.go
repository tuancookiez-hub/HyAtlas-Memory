package main

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// TestExtractReconcilePassesAliases checks the server wiring end to end: with
// HYATLAS_RECONCILE=on and aliases u and u2, a write under u whose extracted fact is
// similar to a fact stored under u2 reaches the reconcile call, and the model's
// SUPERSEDE retires the u2 fact.
func TestExtractReconcilePassesAliases(t *testing.T) {
	t.Setenv("HYATLAS_RECONCILE", "on")
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"new fact": {1, 0, 0},
		"old fact": simTo(0.8),
	})
	addFact(t, st, memory.L3Fact, "old-alias", "old fact",
		map[string]string{"user_id": "u2", "agent_id": "a", "ts": now})
	rawID := newID()
	if err := st.Add(memory.L2Raw, rawID, "new fact",
		map[string]string{"user_id": "u", "agent_id": "a", "ts": now}); err != nil {
		t.Fatal(err)
	}

	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		reply := `{"facts":[{"data":"new fact","layer":"decision"}]}`
		if strings.Contains(string(b), "SUPERSEDE") {
			newID := ""
			if rows, _ := st.List(memory.L3Fact, "u", "a", 10, 0, false); len(rows) > 0 {
				newID = rows[0].ID
			}
			reply = fmt.Sprintf(`{"ops":[{"op":"SUPERSEDE","fact":%q,"targets":["old-alias"],"reason":"moved"}]}`, newID)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"choices":[{"message":{"content":%s}}]}`, string(mustJSON([]byte(reply))))
	}))
	t.Cleanup(mock.Close)

	srv := &Server{
		store:        st,
		llm:          NewLLMClient(mock.URL, "k", "m"),
		mode:         ModePro,
		dedupeScore:  defaultDedupeScore,
		ownerAliases: aliasMap(parseUserAliases("u,u2")),
	}
	if err := srv.extract("new fact", "u", "a", rawID); err != nil {
		t.Fatal(err)
	}
	old := st.GetMany([]string{"old-alias"})
	if len(old) != 1 || !isSuperseded(old[0]) {
		t.Fatalf("alias fact not superseded: %+v", old)
	}
}
