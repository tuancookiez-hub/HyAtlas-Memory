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
