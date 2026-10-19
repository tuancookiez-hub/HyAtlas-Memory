package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// crossRig is a consolidator over a store that holds schemas in known topic
// clusters, with a stub LLM that returns reply to every call and counts them.
type crossRig struct {
	c       *Consolidator
	owner   scopeKey
	schemas []string // schema ids, in creation order
	calls   *int32
}

// newCrossRig writes n schemas for owner u/a, schema i evidenced by two fresh facts
// held in cluster "t<i mod topics>". reply is a function of the schema ids, so a
// test can cite them.
func newCrossRig(t *testing.T, n, topics int, reply func(ids []string) string) *crossRig {
	t.Helper()
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	rig := &crossRig{owner: scopeKey{user: "u", agent: "a"}, calls: new(int32)}
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(rig.calls, 1)
		body, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{
			"message": map[string]string{"content": reply(rig.schemas)}}}})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	t.Cleanup(mock.Close)
	c := NewConsolidator(srv.store, NewLLMClient(mock.URL, "k", "m"), time.Hour, 0, 50)
	c.crossEvery = 24 * time.Hour
	rig.c = c

	oc := &ownerClusters{}
	byTopic := map[string]*clusterState{}
	for i := 0; i < n; i++ {
		topic := fmt.Sprintf("t%d", i%topics)
		cs := byTopic[topic]
		if cs == nil {
			cs = &clusterState{ID: topic}
			byTopic[topic] = cs
			oc.Clusters = append(oc.Clusters, cs)
		}
		var facts []string
		for j := 0; j < 2; j++ {
			id := newID()
			if err := srv.store.Add("l3_fact", id, fmt.Sprintf("%s fact %d %d", topic, i, j), map[string]string{
				"user_id": "u", "agent_id": "a", "ts": time.Now().UTC().Format(time.RFC3339)}); err != nil {
				t.Fatal(err)
			}
			facts = append(facts, id)
			cs.FactIDs = append(cs.FactIDs, id)
		}
		sid := newID()
		if err := srv.store.Add("l6_schema", sid, fmt.Sprintf("schema %d about %s", i, topic), map[string]string{
			"user_id": "u", "agent_id": "a", "evidence": strings.Join(facts, ","),
			"ts": time.Now().UTC().Add(time.Duration(i) * time.Second).Format(time.RFC3339)}); err != nil {
			t.Fatal(err)
		}
		rig.schemas = append(rig.schemas, sid)
	}
	c.clusters[rig.owner.String()] = oc
	return rig
}

// run executes the pass once and returns the report.
func (r *crossRig) run(t *testing.T) *Report {
	t.Helper()
	rep := &Report{}
	r.c.crossSchemaPass(context.Background(), []scopeKey{r.owner}, rep)
	return rep
}

// crossSchemas lists the cross-topic schemas now live for the owner.
func (r *crossRig) crossSchemas() []DocIndex {
	var out []DocIndex
	for _, d := range r.c.liveOf("l6_schema", r.owner) {
		if d.Meta["cross_topic"] == "true" {
			out = append(out, d)
		}
	}
	return out
}

func crossReplyOf(pattern string, ids ...string) string {
	b, _ := json.Marshal(map[string]any{"patterns": []any{map[string]any{"pattern": pattern, "evidence": ids}}})
	return string(b)
}

// A pattern that spans two topics is written as a cross-topic L6 schema whose
// evidence is the facts behind the schemas it generalises, and the existing schemas
// are untouched.
func TestCrossTopicWritesSchemaWithEvidence(t *testing.T) {
	rig := newCrossRig(t, 4, 2, func(ids []string) string {
		return crossReplyOf("Prefers small reversible steps in every area of life.", ids[0], ids[1])
	})
	rep := rig.run(t)
	if rep.CrossSchemas != 1 || atomic.LoadInt32(rig.calls) != 1 {
		t.Fatalf("CrossSchemas = %d, calls = %d, want 1 and 1 (errors: %v)", rep.CrossSchemas, *rig.calls, rep.Errors)
	}
	got := rig.crossSchemas()
	if len(got) != 1 {
		t.Fatalf("cross-topic schemas = %d, want 1", len(got))
	}
	m := got[0].Meta
	if m["consolidated"] != "true" || m["generalises"] != rig.schemas[0]+","+rig.schemas[1] {
		t.Errorf("meta = %v, want consolidated and generalises both schemas", m)
	}
	if n := len(strings.Split(m["evidence"], ",")); n != 4 {
		t.Errorf("evidence has %d fact ids, want the 4 behind the two schemas: %q", n, m["evidence"])
	}
	// Search explains the schema through the facts it cites.
	first := strings.Split(m["evidence"], ",")[0]
	if hits := rig.c.store.schemasSupporting([]string{first}, []string{"u"}, "a", 5); len(hits) < 2 {
		t.Errorf("schemasSupporting found %d schema(s) for an evidence fact, want the cross-topic one as well", len(hits))
	}
	for _, id := range rig.schemas {
		if len(rig.c.supersededOf([]string{id})) != 0 {
			t.Errorf("existing schema %s was retired by the cross-topic pass", id)
		}
	}
}

// The pass runs at most once per interval per owner, survives a restart through the
// state file, and is off at zero.
func TestCrossTopicGateAndOff(t *testing.T) {
	rig := newCrossRig(t, 4, 2, func(ids []string) string {
		return crossReplyOf("Pattern one across topics.", ids[0], ids[1])
	})
	rig.run(t)
	rig.run(t)
	if n := atomic.LoadInt32(rig.calls); n != 1 {
		t.Errorf("calls after two runs inside the interval = %d, want 1", n)
	}
	// A restarted consolidator reads the last run time from the state file.
	rig.c.statePath = t.TempDir() + "/consolidate_state.json"
	rig.c.markCrossRan(rig.owner, &Report{})
	again := NewConsolidator(rig.c.store, rig.c.llm, time.Hour, 0, 50)
	again.statePath = rig.c.statePath
	again.loadState()
	if again.crossSchemaDue(rig.owner) {
		t.Error("after a restart the pass is due again inside the interval")
	}
	// Once the interval has passed it runs again.
	rig.c.crossRan[rig.owner.String()] = time.Now().Add(-25 * time.Hour).UTC().Format(time.RFC3339)
	rig.run(t)
	if n := atomic.LoadInt32(rig.calls); n != 2 {
		t.Errorf("calls after the interval passed = %d, want 2", n)
	}
	// Off.
	rig.c.crossEvery = 0
	rig.c.crossRan = map[string]string{}
	rig.run(t)
	if n := atomic.LoadInt32(rig.calls); n != 2 {
		t.Errorf("calls with the pass off = %d, want 2", n)
	}
}

// An owner with fewer than four schemas, or schemas from one topic only, costs no call.
func TestCrossTopicNeedsFourSchemasFromTwoTopics(t *testing.T) {
	few := newCrossRig(t, 3, 3, func(ids []string) string { return `{"patterns":[]}` })
	few.run(t)
	if n := atomic.LoadInt32(few.calls); n != 0 {
		t.Errorf("3 schemas made %d call(s), want 0", n)
	}
	one := newCrossRig(t, 5, 1, func(ids []string) string { return `{"patterns":[]}` })
	one.run(t)
	if n := atomic.LoadInt32(one.calls); n != 0 {
		t.Errorf("5 schemas of one topic made %d call(s), want 0", n)
	}
}

// Evidence ids the model was not shown are dropped, and a pattern left with fewer than
// two real schemas is not written.
func TestCrossTopicDropsInventedEvidence(t *testing.T) {
	rig := newCrossRig(t, 4, 2, func(ids []string) string {
		b, _ := json.Marshal(map[string]any{"patterns": []any{
			map[string]any{"pattern": "Only one real schema backs this.", "evidence": []string{ids[0], "made-up"}},
			map[string]any{"pattern": "Two real schemas back this.", "evidence": []string{ids[0], "made-up", ids[1]}},
		}})
		return string(b)
	})
	rep := rig.run(t)
	got := rig.crossSchemas()
	if rep.CrossSchemas != 1 || len(got) != 1 || got[0].Content != "Two real schemas back this." {
		t.Fatalf("wrote %d (%v), want only the pattern with two real schemas", rep.CrossSchemas, got)
	}
	if strings.Contains(got[0].Meta["generalises"], "made-up") {
		t.Errorf("generalises kept an invented id: %q", got[0].Meta["generalises"])
	}
}

// A pattern the owner already has is not written again.
func TestCrossTopicSkipsDuplicateOfExistingSchema(t *testing.T) {
	rig := newCrossRig(t, 4, 2, func(ids []string) string {
		return crossReplyOf("Schema 2 about t0", ids[0], ids[1])
	})
	rep := rig.run(t)
	if rep.CrossSchemas != 0 || len(rig.crossSchemas()) != 0 {
		t.Errorf("a duplicate of an existing schema was written (%d)", rep.CrossSchemas)
	}
}

// A reply that is not JSON changes nothing and is reported.
func TestCrossTopicGarbageReplyChangesNothing(t *testing.T) {
	rig := newCrossRig(t, 4, 2, func(ids []string) string { return "I could not find any patterns, sorry." })
	_, before := rig.c.store.List("l6_schema", "", "", 1, 0, false)
	rep := rig.run(t)
	_, after := rig.c.store.List("l6_schema", "", "", 1, 0, false)
	if rep.CrossSchemas != 0 || before != after {
		t.Errorf("garbage reply changed the store: wrote %d, rows %d -> %d", rep.CrossSchemas, before, after)
	}
	if len(rep.Errors) == 0 {
		t.Error("garbage reply was not reported in Errors")
	}
}
