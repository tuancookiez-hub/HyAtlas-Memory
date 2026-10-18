package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// reconcileLLM returns a client whose chat answers with reply as plain text, or with
// an HTTP error when status is not 200. calls counts the requests it receives.
func reconcileLLM(t *testing.T, calls *int, reply string, status int) *LLMClient {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*calls++
		if status != http.StatusOK {
			w.WriteHeader(status)
			_, _ = fmt.Fprint(w, "bad request")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"choices":[{"message":{"content":%s}}]}`, string(mustJSON([]byte(reply))))
	}))
	t.Cleanup(srv.Close)
	return NewLLMClient(srv.URL, "k", "m")
}

// The second reconcile call is off unless HYATLAS_RECONCILE is on.
func TestReconcileOffByDefault(t *testing.T) {
	t.Setenv("HYATLAS_RECONCILE", "")
	if reconcileEnabled() {
		t.Error("reconcile is on with the variable unset")
	}
	t.Setenv("HYATLAS_RECONCILE", "off")
	if reconcileEnabled() {
		t.Error("reconcile is on with HYATLAS_RECONCILE=off")
	}
	t.Setenv("HYATLAS_RECONCILE", "on")
	if !reconcileEnabled() {
		t.Error("reconcile is off with HYATLAS_RECONCILE=on")
	}
}

// A new fact with no similar stored fact makes no LLM call and changes nothing.
func TestReconcileNoCandidatesNoCall(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"new fact": {1, 0, 0},
		"far":      simTo(0.5),
	})
	newID := addRow(t, st, memory.L3Fact, "new fact", now)
	addRow(t, st, memory.L3Fact, "far", now)

	calls := 0
	llm := reconcileLLM(t, &calls, `{"ops":[]}`, http.StatusOK)
	n, err := st.reconcile(context.Background(), llm, []string{newID}, "u", "a")
	if err != nil || n != 0 {
		t.Fatalf("reconcile = %d, %v; want 0, nil", n, err)
	}
	if calls != 0 {
		t.Errorf("LLM calls = %d, want 0", calls)
	}
}

// An UPDATE writes one merged fact, and retires the new fact and its target as updated.
func TestReconcileUpdateMergesIntoOneFact(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"new fact":    {1, 0, 0},
		"old fact":    simTo(0.9),
		"merged fact": {0, 1, 0},
	})
	oldID := addRow(t, st, memory.L3Fact, "old fact", now)
	newID := addRow(t, st, memory.L3Fact, "new fact", now)

	calls := 0
	reply := fmt.Sprintf(`{"ops":[{"op":"UPDATE","fact":%q,"targets":[%q],"text":"merged fact"}]}`, newID, oldID)
	llm := reconcileLLM(t, &calls, reply, http.StatusOK)
	n, err := st.reconcile(context.Background(), llm, []string{newID}, "u", "a")
	if err != nil || n != 1 {
		t.Fatalf("reconcile = %d, %v; want 1, nil", n, err)
	}
	if calls != 1 {
		t.Errorf("LLM calls = %d, want 1", calls)
	}
	for _, d := range st.GetMany([]string{oldID, newID}) {
		if !isSuperseded(d) || d.Meta[metaSupersededHow] != howUpdated {
			t.Errorf("row %s: superseded=%v how=%q, want superseded as %q",
				d.ID, isSuperseded(d), d.Meta[metaSupersededHow], howUpdated)
		}
	}
	live, _ := st.List(memory.L3Fact, "u", "a", 10, 0, false)
	if len(live) != 1 || live[0].Content != "merged fact" {
		t.Fatalf("live facts = %+v, want one fact with the merged text", live)
	}
}

// A SUPERSEDE retires the target as replaced, with the reason, and leaves the new fact live.
func TestReconcileSupersedeReplacesTarget(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"new fact": {1, 0, 0},
		"old fact": simTo(0.8),
	})
	oldID := addRow(t, st, memory.L3Fact, "old fact", now)
	newID := addRow(t, st, memory.L3Fact, "new fact", now)

	calls := 0
	reply := fmt.Sprintf(`{"ops":[{"op":"SUPERSEDE","fact":%q,"targets":[%q],"reason":"moved to Paris"}]}`, newID, oldID)
	llm := reconcileLLM(t, &calls, reply, http.StatusOK)
	n, err := st.reconcile(context.Background(), llm, []string{newID}, "u", "a")
	if err != nil || n != 1 {
		t.Fatalf("reconcile = %d, %v; want 1, nil", n, err)
	}
	old := st.GetMany([]string{oldID})[0]
	if !isSuperseded(old) || old.Meta[metaSupersededHow] != howReplaced || old.Meta["drop_reason"] != "moved to Paris" {
		t.Errorf("old fact: superseded=%v how=%q reason=%q, want replaced with the reason",
			isSuperseded(old), old.Meta[metaSupersededHow], old.Meta["drop_reason"])
	}
	if isSuperseded(st.GetMany([]string{newID})[0]) {
		t.Error("new fact was retired by a SUPERSEDE")
	}
}

// Ids the model invented are ignored, so nothing changes and no op counts.
func TestReconcileInventedIdsChangeNothing(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"new fact": {1, 0, 0},
		"old fact": simTo(0.8),
	})
	oldID := addRow(t, st, memory.L3Fact, "old fact", now)
	newID := addRow(t, st, memory.L3Fact, "new fact", now)

	calls := 0
	reply := fmt.Sprintf(`{"ops":[`+
		`{"op":"SUPERSEDE","fact":"ghost","targets":[%q],"reason":"x"},`+
		`{"op":"SUPERSEDE","fact":%q,"targets":["ghost"],"reason":"x"}]}`, oldID, newID)
	llm := reconcileLLM(t, &calls, reply, http.StatusOK)
	n, err := st.reconcile(context.Background(), llm, []string{newID}, "u", "a")
	if err != nil || n != 0 {
		t.Fatalf("reconcile = %d, %v; want 0, nil", n, err)
	}
	for _, d := range st.GetMany([]string{oldID, newID}) {
		if isSuperseded(d) {
			t.Errorf("row %s was superseded by an op with an invented id", d.ID)
		}
	}
}

// An LLM error is returned and changes nothing, and it does not panic.
func TestReconcileLLMErrorChangesNothing(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"new fact": {1, 0, 0},
		"old fact": simTo(0.8),
	})
	oldID := addRow(t, st, memory.L3Fact, "old fact", now)
	newID := addRow(t, st, memory.L3Fact, "new fact", now)

	calls := 0
	llm := reconcileLLM(t, &calls, "", http.StatusBadRequest)
	n, err := st.reconcile(context.Background(), llm, []string{newID}, "u", "a")
	if err == nil || n != 0 {
		t.Fatalf("reconcile = %d, %v; want 0 and an error", n, err)
	}
	if calls != 1 {
		t.Errorf("LLM calls = %d, want 1", calls)
	}
	for _, d := range st.GetMany([]string{oldID, newID}) {
		if isSuperseded(d) {
			t.Errorf("row %s was superseded after an LLM error", d.ID)
		}
	}
}

// reconcileOwners compares a new fact with the facts of every ID the owner has, so a
// fact stored under an alias is a candidate. reconcile with only the write's own ID
// does not see it and makes no call.
func TestReconcileOwnersSeesAliasFacts(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"new fact": {1, 0, 0},
		"old fact": simTo(0.8),
	})
	addFact(t, st, memory.L3Fact, "old-alias", "old fact",
		map[string]string{"user_id": "u2", "agent_id": "a", "ts": now})
	newID := addRow(t, st, memory.L3Fact, "new fact", now)

	calls := 0
	reply := fmt.Sprintf(`{"ops":[{"op":"SUPERSEDE","fact":%q,"targets":["old-alias"],"reason":"moved"}]}`, newID)
	llm := reconcileLLM(t, &calls, reply, http.StatusOK)
	n, err := st.reconcile(context.Background(), llm, []string{newID}, "u", "a")
	if err != nil || n != 0 || calls != 0 {
		t.Fatalf("reconcile = %d, %v with %d calls; want 0, nil, 0 calls", n, err, calls)
	}

	calls = 0
	n, err = st.reconcileOwners(context.Background(), llm, []string{newID}, []string{"u", "u2"}, "u", "a")
	if err != nil || n != 1 || calls != 1 {
		t.Fatalf("reconcileOwners = %d, %v with %d calls; want 1, nil, 1 call", n, err, calls)
	}
	old := st.GetMany([]string{"old-alias"})[0]
	if !isSuperseded(old) || old.Meta[metaSupersededHow] != howReplaced {
		t.Errorf("alias fact: superseded=%v how=%q, want replaced", isSuperseded(old), old.Meta[metaSupersededHow])
	}
}

// A SUPERSEDE whose new fact an earlier UPDATE already retired is skipped, so the
// target stays live.
func TestReconcileSupersedeAfterUpdateSkipped(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"new fact":    {1, 0, 0},
		"old A":       simTo(0.9),
		"old B":       simTo(0.85),
		"merged fact": {0, 1, 0},
	})
	aID := addRow(t, st, memory.L3Fact, "old A", now)
	bID := addRow(t, st, memory.L3Fact, "old B", now)
	newID := addRow(t, st, memory.L3Fact, "new fact", now)

	calls := 0
	reply := fmt.Sprintf(`{"ops":[`+
		`{"op":"UPDATE","fact":%q,"targets":[%q],"text":"merged fact"},`+
		`{"op":"SUPERSEDE","fact":%q,"targets":[%q],"reason":"moved"}]}`, newID, aID, newID, bID)
	llm := reconcileLLM(t, &calls, reply, http.StatusOK)
	n, err := st.reconcile(context.Background(), llm, []string{newID}, "u", "a")
	if err != nil || n != 1 {
		t.Fatalf("reconcile = %d, %v; want 1, nil", n, err)
	}
	if isSuperseded(st.GetMany([]string{bID})[0]) {
		t.Error("old B was superseded through the fact an UPDATE had already retired")
	}
}

// An UPDATE whose new fact an earlier SUPERSEDE already used is skipped, so the fact
// that stands in for the superseded target is not retired.
func TestReconcileUpdateAfterSupersedeSkipped(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	st := guardStore(t, vecEmbedder{
		"new fact":    {1, 0, 0},
		"old A":       simTo(0.9),
		"old B":       simTo(0.85),
		"merged fact": {0, 1, 0},
	})
	aID := addRow(t, st, memory.L3Fact, "old A", now)
	bID := addRow(t, st, memory.L3Fact, "old B", now)
	newID := addRow(t, st, memory.L3Fact, "new fact", now)

	calls := 0
	reply := fmt.Sprintf(`{"ops":[`+
		`{"op":"SUPERSEDE","fact":%q,"targets":[%q],"reason":"moved"},`+
		`{"op":"UPDATE","fact":%q,"targets":[%q],"text":"merged fact"}]}`, newID, aID, newID, bID)
	llm := reconcileLLM(t, &calls, reply, http.StatusOK)
	n, err := st.reconcile(context.Background(), llm, []string{newID}, "u", "a")
	if err != nil || n != 1 {
		t.Fatalf("reconcile = %d, %v; want 1, nil", n, err)
	}
	a := st.GetMany([]string{aID})[0]
	if !isSuperseded(a) || a.Meta["superseded_by"] != newID {
		t.Errorf("old A: superseded=%v by=%q, want superseded by %s", isSuperseded(a), a.Meta["superseded_by"], newID)
	}
	if isSuperseded(st.GetMany([]string{newID})[0]) {
		t.Error("new fact was retired by an UPDATE after a SUPERSEDE used it")
	}
	if isSuperseded(st.GetMany([]string{bID})[0]) {
		t.Error("old B was retired by the skipped UPDATE")
	}
	live, _ := st.List(memory.L3Fact, "u", "a", 10, 0, false)
	for _, d := range live {
		if d.Content == "merged fact" {
			t.Errorf("a merged fact was written: %+v", d)
		}
	}
}
