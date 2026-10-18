package main

import (
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

func TestExperienceText(t *testing.T) {
	withEnv := experienceText(Experience{Task: "fix build", Solution: "set CGO flag", Environment: "Windows host"})
	if want := "Task: fix build. Solution: set CGO flag. Environment: Windows host."; withEnv != want {
		t.Errorf("with environment: got %q, want %q", withEnv, want)
	}
	noEnv := experienceText(Experience{Task: "fix build", Solution: "set CGO flag", Environment: "  "})
	if want := "Task: fix build. Solution: set CGO flag."; noEnv != want {
		t.Errorf("without environment: got %q, want %q", noEnv, want)
	}
}

func TestNormalizeConfidence(t *testing.T) {
	cases := map[string]string{
		"HIGH":   "high",
		"":       "medium",
		"maybe":  "medium",
		" low ":  "low",
		"medium": "medium",
	}
	for in, want := range cases {
		if got := normalizeConfidence(in); got != want {
			t.Errorf("normalizeConfidence(%q) = %q, want %q", in, got, want)
		}
	}
}

// expTextA and expTextB are two wordings of one lesson; expTextB embeds close
// enough to expTextA (0.95) that the write treats it as a repeat.
const (
	expTextA = "Task: fix build. Solution: set CGO flag. Environment: Windows host."
	expTextB = "Task: fix the build. Solution: set the CGO flag. Environment: Windows host."
)

func TestWriteExperiencesStoresLabelAndFields(t *testing.T) {
	st := guardStore(t, vecEmbedder{expTextA: {1, 0, 0}})
	ids := st.writeExperiences([]Experience{{
		Task: " fix build ", Solution: "set CGO flag", Environment: "Windows host", Confidence: "HIGH",
	}}, "u", "a", "src1", "2026-10-18T00:00:00Z")
	if len(ids) != 1 {
		t.Fatalf("wrote %d experiences, want 1", len(ids))
	}
	d := st.GetMany(ids)
	if len(d) != 1 {
		t.Fatalf("row missing")
	}
	m := d[0].Meta
	want := map[string]string{
		"source_layer_label": "experience", "task": "fix build", "solution": "set CGO flag",
		"environment": "Windows host", "confidence": "high", "source_id": "src1", "user_id": "u",
	}
	for k, v := range want {
		if m[k] != v {
			t.Errorf("meta[%s] = %q, want %q", k, m[k], v)
		}
	}
	if d[0].Content != expTextA {
		t.Errorf("content = %q, want %q", d[0].Content, expTextA)
	}
}

func TestWriteExperiencesSkipsEmptyTaskOrSolution(t *testing.T) {
	st := guardStore(t, vecEmbedder{})
	ids := st.writeExperiences([]Experience{
		{Task: "fix build", Solution: "  "},
		{Task: "", Solution: "set CGO flag"},
	}, "u", "a", "src1", "2026-10-18T00:00:00Z")
	if len(ids) != 0 {
		t.Fatalf("wrote %d experiences, want 0", len(ids))
	}
	if _, n := st.List(memory.L3Fact, "u", "a", 10, 0, false); n != 0 {
		t.Errorf("store holds %d rows, want none", n)
	}
}

func TestWriteExperiencesRepeatSupersedesAsUpdate(t *testing.T) {
	st := guardStore(t, vecEmbedder{
		expTextA: {1, 0, 0},
		expTextB: simTo(0.95),
	})
	first := st.writeExperiences([]Experience{{Task: "fix build", Solution: "set CGO flag",
		Environment: "Windows host", Confidence: "medium"}}, "u", "a", "src1", "2026-10-18T00:00:00Z")
	if len(first) != 1 {
		t.Fatalf("first write: %d ids, want 1", len(first))
	}
	second := st.writeExperiences([]Experience{{Task: "fix the build", Solution: "set the CGO flag",
		Environment: "Windows host", Confidence: "high"}}, "u", "a", "src2", "2026-10-18T01:00:00Z")
	if len(second) != 1 {
		t.Fatalf("second write: %d ids, want 1", len(second))
	}
	old := st.GetMany(first)
	if len(old) != 1 || !isSuperseded(old[0]) {
		t.Fatalf("earlier experience not superseded")
	}
	if how := old[0].Meta[metaSupersededHow]; how != howUpdated {
		t.Errorf("superseded_how = %q, want %q", how, howUpdated)
	}
	if by := old[0].Meta["superseded_by"]; by != second[0] {
		t.Errorf("superseded_by = %q, want %q", by, second[0])
	}
	if d := st.GetMany(second); len(d) != 1 || isSuperseded(d[0]) {
		t.Error("the repeat should stay live")
	}
}

func TestWriteExperiencesLeavesOtherOwnerUntouched(t *testing.T) {
	st := guardStore(t, vecEmbedder{
		expTextA: {1, 0, 0},
		expTextB: simTo(0.95),
	})
	otherID := newID()
	if err := st.Add(memory.L3Fact, otherID, expTextA, map[string]string{
		"user_id": "other", "agent_id": "a", "source_layer_label": "experience",
		"ts": "2026-10-17T00:00:00Z",
	}); err != nil {
		t.Fatal(err)
	}
	st.writeExperiences([]Experience{{Task: "fix build", Solution: "set CGO flag",
		Environment: "Windows host"}}, "u", "a", "src1", "2026-10-18T00:00:00Z")
	st.writeExperiences([]Experience{{Task: "fix the build", Solution: "set the CGO flag",
		Environment: "Windows host"}}, "u", "a", "src2", "2026-10-18T01:00:00Z")
	if d := st.GetMany([]string{otherID}); len(d) != 1 || isSuperseded(d[0]) {
		t.Error("another owner's experience was superseded")
	}
}

func TestIsExperience(t *testing.T) {
	cases := []struct {
		name string
		h    SearchHit
		want bool
	}{
		{"labelled L3", SearchHit{Layer: memory.L3Fact, Meta: map[string]string{"source_layer_label": "experience"}}, true},
		{"L3 fact", SearchHit{Layer: memory.L3Fact, Meta: map[string]string{"source_layer_label": "decision"}}, false},
		{"labelled but not L3", SearchHit{Layer: memory.L2Raw, Meta: map[string]string{"source_layer_label": "experience"}}, false},
		{"no label", SearchHit{Layer: memory.L3Fact}, false},
	}
	for _, c := range cases {
		if got := isExperience(c.h); got != c.want {
			t.Errorf("%s: isExperience = %v, want %v", c.name, got, c.want)
		}
	}
}

func TestExtractPromptsRequestExperiences(t *testing.T) {
	for _, withCtx := range []bool{false, true} {
		p := extractSystemPrompt(withCtx)
		if !strings.Contains(p, `"experiences"`) {
			t.Errorf("extractSystemPrompt(%v) lacks the experiences key", withCtx)
		}
		if !strings.Contains(p, "- experiences: 0-2 reusable lessons") {
			t.Errorf("extractSystemPrompt(%v) lacks the experiences rule", withCtx)
		}
	}
}

// TestWriteExperiencesDedupeIgnoresNearerFacts checks that an old experience is still
// found as the repeat's dedupe target when ten ordinary facts sit nearer to the repeat.
func TestWriteExperiencesDedupeIgnoresNearerFacts(t *testing.T) {
	st := guardStore(t, vecEmbedder{
		expTextA: {1, 0, 0},
		expTextB: simTo(0.95),
	})
	first := st.writeExperiences([]Experience{{Task: "fix build", Solution: "set CGO flag",
		Environment: "Windows host", Confidence: "medium"}}, "u", "a", "src1", "2026-10-18T00:00:00Z")
	if len(first) != 1 {
		t.Fatalf("first write: %d ids, want 1", len(first))
	}
	// Twelve ordinary facts with the repeat's exact text score 1.0 against it.
	for i := 0; i < 12; i++ {
		addRow(t, st, memory.L3Fact, expTextB, "2026-10-18T00:30:00Z")
	}
	second := st.writeExperiences([]Experience{{Task: "fix the build", Solution: "set the CGO flag",
		Environment: "Windows host", Confidence: "high"}}, "u", "a", "src2", "2026-10-18T01:00:00Z")
	if len(second) != 1 {
		t.Fatalf("second write: %d ids, want 1", len(second))
	}
	old := st.GetMany(first)
	if len(old) != 1 || !isSuperseded(old[0]) {
		t.Fatalf("earlier experience not superseded despite nearer facts")
	}
	if how := old[0].Meta[metaSupersededHow]; how != howUpdated {
		t.Errorf("superseded_how = %q, want %q", how, howUpdated)
	}
	if by := old[0].Meta["superseded_by"]; by != second[0] {
		t.Errorf("superseded_by = %q, want %q", by, second[0])
	}
}
