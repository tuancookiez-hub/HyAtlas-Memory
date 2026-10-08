// Package bgeemb — tests for the in-process BGE embedder.
//
// The tests that run the ONNX model need a model directory: either
// HYATLAS_TEST_MODEL_DIR, or models/ next to the repo root. When neither is
// present — which is the case in the unit-test CI job, where the ~133 MB model
// is not fetched — they skip rather than fail.
//
// That skip is why this package used to read as uncovered, and why the
// token_type_ids defect (a session built for two inputs against a graph that
// declares three) shipped in three release binaries: nothing in this job ever
// ran an embedding. The release workflow is the guard that always runs, and it
// now writes and searches against the shipped model.
package bgeemb

import (
	"context"
	"math"
	"os"
	"path/filepath"
	"testing"
)

// modelDir locates a usable BGE model directory, or returns "" if there is none.
func modelDir() string {
	if dir := os.Getenv("HYATLAS_TEST_MODEL_DIR"); dir != "" {
		return dir
	}
	// bge/ sits at the repo root, so the conventional runtime dir is ../models.
	candidate := filepath.Join("..", "models")
	if _, err := os.Stat(filepath.Join(candidate, "bge-small-en-v1.5.onnx")); err == nil {
		return candidate
	}
	return ""
}

func loadOrSkip(t *testing.T) *BGE {
	t.Helper()
	dir := modelDir()
	if dir == "" {
		t.Skip("no BGE model directory (set HYATLAS_TEST_MODEL_DIR or place models/ at the repo root)")
	}
	// The model directory must also carry the platform's onnxruntime library.
	m, err := New(dir)
	if err != nil {
		t.Fatalf("New(%q): %v", dir, err)
	}
	t.Cleanup(m.Destroy)
	return m
}

func embed(t *testing.T, m *BGE, text string) []float32 {
	t.Helper()
	v, err := m.Embed(context.Background(), text)
	if err != nil {
		// This is the assertion that would have caught the shipped defect:
		// loading the model succeeded and the session was created, but the
		// first inference failed with "Missing Input: token_type_ids".
		t.Fatalf("Embed(%q): %v", text, err)
	}
	return v
}

func norm(v []float32) float64 {
	var sum float64
	for _, x := range v {
		sum += float64(x) * float64(x)
	}
	return math.Sqrt(sum)
}

func cosine(a, b []float32) float64 {
	if len(a) != len(b) {
		return math.NaN()
	}
	var dot float64
	for i := range a {
		dot += float64(a[i]) * float64(b[i])
	}
	d := norm(a) * norm(b)
	if d == 0 {
		return math.NaN()
	}
	return dot / d
}

func TestVocabLoads(t *testing.T) {
	dir := modelDir()
	if dir == "" {
		t.Skip("no BGE model directory")
	}
	vocab, err := loadVocab(filepath.Join(dir, "vocab.txt"))
	if err != nil {
		t.Fatalf("loadVocab: %v", err)
	}
	if len(vocab) != VocabSize {
		t.Errorf("vocab has %d entries, want %d", len(vocab), VocabSize)
	}
	for _, tok := range []string{"[CLS]", "[SEP]", "[UNK]", "the"} {
		if _, ok := vocab[tok]; !ok {
			t.Errorf("vocab is missing %q", tok)
		}
	}
}

// TestEmbedShapeAndUnitNorm pins the wire contract the store depends on: 384
// float32s, L2-normalized, all finite. A degenerate vector (all zeros or NaN)
// would still "work" as a write while making retrieval meaningless.
func TestEmbedShapeAndUnitNorm(t *testing.T) {
	m := loadOrSkip(t)
	v := embed(t, m, "the quick brown fox jumps over the lazy dog")
	if len(v) != HiddenSize {
		t.Fatalf("embedding has %d dims, want %d", len(v), HiddenSize)
	}
	for i, x := range v {
		if math.IsNaN(float64(x)) || math.IsInf(float64(x), 0) {
			t.Fatalf("component %d is not finite: %v", i, x)
		}
	}
	if n := norm(v); math.Abs(n-1) > 1e-3 {
		t.Errorf("embedding norm = %.6f, want 1", n)
	}
}

// TestEmbedDeterministic guards the session-reuse path: the same text twice must
// give identical vectors, so a cached or reused session cannot drift.
func TestEmbedDeterministic(t *testing.T) {
	m := loadOrSkip(t)
	a := embed(t, m, "memory systems should be boring and predictable")
	b := embed(t, m, "memory systems should be boring and predictable")
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("component %d differs across identical calls: %v vs %v", i, a[i], b[i])
		}
	}
}

// TestEmbedDistinguishesText checks the vectors carry meaning rather than being
// a constant: different text must land in different places.
func TestEmbedDistinguishesText(t *testing.T) {
	m := loadOrSkip(t)
	a := embed(t, m, "the deployment failed because the database was unreachable")
	b := embed(t, m, "sunlight filtered through the kitchen window")
	if sim := cosine(a, b); sim > 0.99 {
		t.Errorf("unrelated sentences are nearly identical (cosine %.4f)", sim)
	}
}

// TestEmbedSemanticOrdering is a loose sanity check that related text ranks
// above unrelated text. The margin is deliberately wide so the test reports a
// broken embedder rather than ranking taste.
func TestEmbedSemanticOrdering(t *testing.T) {
	m := loadOrSkip(t)
	q := embed(t, m, "server deployment problem")
	near := embed(t, m, "the production server would not start after deployment")
	far := embed(t, m, "a recipe for sourdough bread")
	if cosine(q, near) <= cosine(q, far) {
		t.Errorf("related text did not score above unrelated: near=%.4f far=%.4f",
			cosine(q, near), cosine(q, far))
	}
}

// TestEmbedEmptyAndLongText covers the tokenizer's edges: empty input and input
// far longer than MaxSeqLen must both produce a usable vector rather than an
// error or an out-of-range token id.
func TestEmbedEmptyAndLongText(t *testing.T) {
	m := loadOrSkip(t)
	for _, text := range []string{"", "   ", "[unk]", "x", longText(400)} {
		v := embed(t, m, text)
		if len(v) != HiddenSize {
			t.Errorf("text %q: got %d dims, want %d", truncate(text), len(v), HiddenSize)
			continue
		}
		if n := norm(v); math.Abs(n-1) > 1e-3 {
			t.Errorf("text %q: norm = %.6f, want 1", truncate(text), n)
		}
	}
}

func longText(words int) string {
	out := make([]byte, 0, words*6)
	for i := 0; i < words; i++ {
		if i > 0 {
			out = append(out, ' ')
		}
		out = append(out, "token"...)
	}
	return string(out)
}

func truncate(s string) string {
	if len(s) > 24 {
		return s[:24] + "..."
	}
	return s
}
