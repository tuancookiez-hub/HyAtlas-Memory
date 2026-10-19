package main

import (
	"encoding/json"
	"net/http/httptest"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// retire with an empty How picks howReplaced when there is a replacement and
// howDropped, with the default reason, when there is not.
func TestRetireDefaultsHow(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	defer st.Close()
	a := addRow(t, st, memory.L3Fact, "fact a", nowTS())
	b := addRow(t, st, memory.L3Fact, "fact b", nowTS())
	c := addRow(t, st, memory.L3Fact, "fact c", nowTS())
	if _, err := st.retire([]string{a}, Retire{By: c}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.retire([]string{b}, Retire{}); err != nil {
		t.Fatal(err)
	}
	ra := retirementOf(st.GetMany([]string{a})[0])
	if ra.How != howReplaced || ra.By != c || ra.Reason != "" {
		t.Errorf("replaced row: %+v", ra)
	}
	rb := retirementOf(st.GetMany([]string{b})[0])
	if rb.How != howDropped || rb.By != "" || rb.Reason != reasonNotRecorded {
		t.Errorf("dropped row: %+v", rb)
	}
}

// Every how value, and a given reason, reach the row's metadata.
func TestRetireRecordsEveryHow(t *testing.T) {
	st := openHistoryStore(t, t.TempDir())
	defer st.Close()
	by := addRow(t, st, memory.L3Fact, "the newer fact", nowTS())
	for _, how := range []string{howReplaced, howUpdated, howRestated, howMerged, howDone, howDropped} {
		id := addRow(t, st, memory.L3Fact, "old "+how, nowTS())
		r := Retire{By: by, How: how, Reason: "because " + how}
		if how == howDropped {
			r.By = ""
		}
		if n, err := st.retire([]string{id}, r); err != nil || n != 1 {
			t.Fatalf("%s: retire = %d, %v", how, n, err)
		}
		m := metaOf(t, st, id)
		if m[metaSupersededHow] != how || m[metaDropReason] != "because "+how || m[metaSupersededBy] != r.By || m[metaInvalidAt] == "" {
			t.Errorf("%s: meta = %v", how, m)
		}
	}
}

// /api/v1/list shows how a superseded row was retired, and why it was dropped.
func TestListShowsHowAndReason(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	st := srv.store
	by := addRow(t, st, memory.L3Fact, "the newer fact", nowTS())
	merged := addRow(t, st, memory.L3Fact, "merged away", nowTS())
	dropped := addRow(t, st, memory.L3Fact, "dropped away", nowTS())
	live := addRow(t, st, memory.L3Fact, "still live", nowTS())
	if _, err := st.retire([]string{merged}, Retire{By: by, How: howMerged}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.retire([]string{dropped}, Retire{How: howDropped, Reason: "no longer true"}); err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	srv.handleList(w, httptest.NewRequest("GET", "/api/v1/list?layer=l3_fact&include_superseded=true", nil))
	var resp struct {
		Memories []map[string]any `json:"memories"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	for _, m := range resp.Memories {
		switch m["memory_id"] {
		case merged:
			if m["superseded_how"] != howMerged || m["superseded_by"] != by {
				t.Errorf("merged row: %v", m)
			}
		case dropped:
			if m["superseded_how"] != howDropped || m["drop_reason"] != "no longer true" {
				t.Errorf("dropped row: %v", m)
			}
		case live:
			if _, ok := m["superseded_how"]; ok {
				t.Errorf("live row shows superseded_how: %v", m)
			}
		}
	}
}
