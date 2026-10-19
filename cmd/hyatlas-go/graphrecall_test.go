package main

import (
	"testing"
)

// TestGraphNeighbourNamesFollowsEdge checks that the neighbour of HyAtlas in the graph
// is returned for the owner who wrote the edge, and that another owner sees nothing.
func TestGraphNeighbourNamesFollowsEdge(t *testing.T) {
	st := guardStore(t, vecEmbedder{})
	if _, err := st.Graph().AddEdgeWithSources("u", "a", "HyAtlas", "uses", "stepfun", []string{"src1"}); err != nil {
		t.Fatal(err)
	}
	got := st.graphNeighbourNames([]string{"HyAtlas"}, []string{"u"}, "a")
	found := false
	for _, n := range got {
		if n == "stepfun" {
			found = true
		}
	}
	if !found {
		t.Errorf("neighbours of HyAtlas: got %v, want stepfun", got)
	}
	if other := st.graphNeighbourNames([]string{"HyAtlas"}, []string{"someone-else"}, "a"); len(other) != 0 {
		t.Errorf("another owner saw neighbours: %v", other)
	}
	if none := st.graphNeighbourNames([]string{"HyAtlas"}, nil, "a"); len(none) != 0 {
		t.Errorf("no owners should give no neighbours, got %v", none)
	}
}

// TestGraphAdmitAdmitsNamedNeighbour checks that a low-scoring candidate that names a
// neighbour is admitted after the existing hits, and that its score is left unchanged.
func TestGraphAdmitAdmitsNamedNeighbour(t *testing.T) {
	admitted := []SearchHit{{ID: "top", Content: "x", Score: 0.9}}
	candidates := []SearchHit{{ID: "low", Content: "stepfun shipped the model", Score: 0.30}}
	out := graphAdmit(candidates, admitted, []string{"stepfun"}, 0.25, graphRecallMax)
	if len(out) != 2 || out[1].ID != "low" {
		t.Fatalf("got %v, want low admitted after top", out)
	}
	if out[1].Score != 0.30 {
		t.Errorf("score changed to %v, want 0.30", out[1].Score)
	}
}

// TestGraphAdmitRefusesBelowFloor checks that a candidate under the floor is not admitted
// even when it names a neighbour.
func TestGraphAdmitRefusesBelowFloor(t *testing.T) {
	candidates := []SearchHit{{ID: "weak", Content: "stepfun", Score: 0.10}}
	out := graphAdmit(candidates, nil, []string{"stepfun"}, 0.25, graphRecallMax)
	if len(out) != 0 {
		t.Errorf("below-floor candidate admitted: %v", out)
	}
}

// TestGraphAdmitCapsAtMax checks that no more than max candidates are admitted, and that
// a candidate already admitted is not added a second time.
func TestGraphAdmitCapsAtMax(t *testing.T) {
	admitted := []SearchHit{{ID: "dup", Content: "stepfun", Score: 0.5}}
	candidates := []SearchHit{
		{ID: "dup", Content: "stepfun", Score: 0.5},
		{ID: "c1", Content: "stepfun one", Score: 0.4},
		{ID: "c2", Content: "stepfun two", Score: 0.4},
		{ID: "c3", Content: "stepfun three", Score: 0.4},
		{ID: "c4", Content: "stepfun four", Score: 0.4},
	}
	out := graphAdmit(candidates, admitted, []string{"stepfun"}, 0.25, 2)
	if len(out) != 3 {
		t.Fatalf("got %d hits, want the admitted one plus two extras", len(out))
	}
	if out[0].ID != "dup" || out[1].ID != "c1" || out[2].ID != "c2" {
		t.Errorf("unexpected order: %v", out)
	}
}

// TestGraphAdmitRequiresNeighbourMention checks that a candidate that does not name any
// neighbour is not admitted, however well it scores.
func TestGraphAdmitRequiresNeighbourMention(t *testing.T) {
	candidates := []SearchHit{{ID: "x", Content: "unrelated topic", Score: 0.9}}
	if out := graphAdmit(candidates, nil, []string{"stepfun"}, 0.25, graphRecallMax); len(out) != 0 {
		t.Errorf("candidate without a neighbour mention admitted: %v", out)
	}
}

// makeHits returns n hits with IDs a0, a1, ... and descending scores.
func makeHits(prefix string, n int) []SearchHit {
	out := make([]SearchHit, 0, n)
	for i := 0; i < n; i++ {
		out = append(out, SearchHit{ID: prefix + string(rune('0'+i%10)) + string(rune('a'+i/10)), Content: "plain", Score: 0.9})
	}
	return out
}

// TestGraphAdmitWithinSlotsInsideLimit checks that with 12 admitted hits, limit 5 and one
// qualifying candidate, the candidate lands at index 4 and the old admitted[4] moves to 5.
func TestGraphAdmitWithinSlotsInsideLimit(t *testing.T) {
	admitted := makeHits("h", 12)
	candidates := []SearchHit{{ID: "g1", Content: "stepfun shipped", Score: 0.4}}
	out := graphAdmitWithin(candidates, admitted, []string{"stepfun"}, 0.25, graphRecallMax, 5)
	if len(out) != 13 {
		t.Fatalf("got %d hits, want 13", len(out))
	}
	if out[4].ID != "g1" {
		t.Errorf("index 4 is %q, want g1", out[4].ID)
	}
	if out[5].ID != admitted[4].ID {
		t.Errorf("index 5 is %q, want old admitted[4] %q", out[5].ID, admitted[4].ID)
	}
}

// TestGraphAdmitWithinFewAdmitted checks that with 2 admitted hits, limit 5 and one
// candidate, the candidate is placed at the end, index 2, and the length is 3.
func TestGraphAdmitWithinFewAdmitted(t *testing.T) {
	admitted := makeHits("h", 2)
	candidates := []SearchHit{{ID: "g1", Content: "stepfun shipped", Score: 0.4}}
	out := graphAdmitWithin(candidates, admitted, []string{"stepfun"}, 0.25, graphRecallMax, 5)
	if len(out) != 3 {
		t.Fatalf("got %d hits, want 3", len(out))
	}
	if out[2].ID != "g1" {
		t.Errorf("index 2 is %q, want g1 at the end", out[2].ID)
	}
}

// TestGraphAdmitWithinRoomForTwo checks that with 12 admitted hits, limit 6 and three
// candidates with max 3, the room is 2: candidates 1 and 2 land at indexes 4 and 5, and
// candidate 3 is at the very end.
func TestGraphAdmitWithinRoomForTwo(t *testing.T) {
	admitted := makeHits("h", 12)
	candidates := []SearchHit{
		{ID: "g1", Content: "stepfun one", Score: 0.4},
		{ID: "g2", Content: "stepfun two", Score: 0.4},
		{ID: "g3", Content: "stepfun three", Score: 0.4},
	}
	out := graphAdmitWithin(candidates, admitted, []string{"stepfun"}, 0.25, 3, 6)
	if len(out) != 15 {
		t.Fatalf("got %d hits, want 15", len(out))
	}
	if out[4].ID != "g1" || out[5].ID != "g2" {
		t.Errorf("indexes 4 and 5 are %q and %q, want g1 and g2", out[4].ID, out[5].ID)
	}
	if out[len(out)-1].ID != "g3" {
		t.Errorf("last hit is %q, want g3", out[len(out)-1].ID)
	}
}

// TestGraphAdmitWithinZeroLimit checks that with limit 0 the result equals graphAdmit's.
func TestGraphAdmitWithinZeroLimit(t *testing.T) {
	admitted := makeHits("h", 4)
	candidates := []SearchHit{{ID: "g1", Content: "stepfun shipped", Score: 0.4}}
	want := graphAdmit(candidates, admitted, []string{"stepfun"}, 0.25, graphRecallMax)
	got := graphAdmitWithin(candidates, admitted, []string{"stepfun"}, 0.25, graphRecallMax, 0)
	if len(got) != len(want) {
		t.Fatalf("got %d hits, want %d", len(got), len(want))
	}
	for i := range want {
		if got[i].ID != want[i].ID || got[i].Content != want[i].Content || got[i].Score != want[i].Score {
			t.Errorf("index %d: got %s, want %s", i, got[i].ID, want[i].ID)
		}
	}
}

// TestGraphAdmitWithinInputsUnchanged checks that the inputs are not modified by the call.
func TestGraphAdmitWithinInputsUnchanged(t *testing.T) {
	admitted := makeHits("h", 12)
	candidates := []SearchHit{{ID: "g1", Content: "stepfun shipped", Score: 0.4}}
	admittedCopy := append([]SearchHit(nil), admitted...)
	candidatesCopy := append([]SearchHit(nil), candidates...)
	_ = graphAdmitWithin(candidates, admitted, []string{"stepfun"}, 0.25, graphRecallMax, 5)
	for i := range admitted {
		if admitted[i].ID != admittedCopy[i].ID || admitted[i].Score != admittedCopy[i].Score {
			t.Errorf("admitted[%d] changed to %s", i, admitted[i].ID)
		}
	}
	for i := range candidates {
		if candidates[i].ID != candidatesCopy[i].ID || candidates[i].Score != candidatesCopy[i].Score {
			t.Errorf("candidates[%d] changed to %s", i, candidates[i].ID)
		}
	}
}
