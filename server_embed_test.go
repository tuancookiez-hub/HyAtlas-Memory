package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The relative default "./models" used to be handed straight to the embedder.
// On Windows the onnxruntime loader and the directory check resolve a relative
// path against different bases, so the same value found the model file and then
// failed on onnxruntime.dll. resolveModelDir exists to make the documented
// default work from every layout, so each layout is pinned here.

func TestResolveModelDirKeepsAbsolutePath(t *testing.T) {
	dir := t.TempDir()
	got, _ := resolveModelDir(dir)
	if !filepath.IsAbs(got) {
		t.Fatalf("absolute input came back relative: %q", got)
	}
	if filepath.Clean(got) != filepath.Clean(dir) {
		t.Fatalf("absolute path rewritten: got %q want %q", got, dir)
	}
}

func TestResolveModelDirFindsModelsInCWD(t *testing.T) {
	dir := t.TempDir()
	models := filepath.Join(dir, "models")
	if err := os.MkdirAll(models, 0o755); err != nil {
		t.Fatal(err)
	}

	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(wd) })
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}

	got, _ := resolveModelDir("./models")
	if !filepath.IsAbs(got) {
		t.Fatalf("relative ./models stayed relative: %q", got)
	}
	if filepath.Clean(got) != filepath.Clean(models) {
		t.Fatalf("resolved to the wrong directory: got %q want %q", got, models)
	}
}

func TestResolveModelDirMissingStillAbsolute(t *testing.T) {
	// Nothing on disk matched. The value must still be absolute so the failure
	// message names one real location instead of an ambiguous "./models".
	dir := t.TempDir()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(wd) })
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}

	got, _ := resolveModelDir("./models-absent")
	if !filepath.IsAbs(got) {
		t.Fatalf("missing dir came back relative: %q", got)
	}
	if !strings.HasSuffix(filepath.Clean(got), "models-absent") {
		t.Fatalf("resolved path lost the leaf name: %q", got)
	}
}

// describeEmbed exists because the startup log printed embedModel
// unconditionally, so a local in-process BGE server announced itself as
// "text-embedding-3-small" — which reads like a remote OpenAI embedder and
// misdescribes where text goes.

func TestDescribeEmbedNamesTheRealEmbedder(t *testing.T) {
	cases := []struct{ base, model, want string }{
		{"bge", "text-embedding-3-small", "bge-small (in-process)"},
		{"BGE", "text-embedding-3-small", "bge-small (in-process)"},
		{"local", "text-embedding-3-small", "local-stub (deterministic, 384-d)"},
		{"https://api.example.com/v1", "text-embedding-3-small",
			"https://api.example.com/v1 (text-embedding-3-small)"},
	}
	for _, c := range cases {
		if got := describeEmbed(c.base, c.model); got != c.want {
			t.Errorf("describeEmbed(%q, %q) = %q, want %q", c.base, c.model, got, c.want)
		}
	}
}

// Everything below asserts on resolveRuntime() — the values main() actually runs
// with. The first version of TestEmbedDefaultIsLocal called envOr directly with
// its own default argument, so it passed whether or not main() agreed, and
// reverting the real default did not make it fail.

func clearRuntimeEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{
		"HYATLAS_GO_PORT", "HYATLAS_GO_DATA", "HYATLAS_GRAPH_PATH",
		"HYATLAS_LLM_BASE", "HYATLAS_LLM_MODEL",
		"HYATLAS_EMBED_BASE", "HYATLAS_EMBED_MODEL", "HYATLAS_MODEL_DIR",
	} {
		t.Setenv(k, "")
	}
}

// The embedder must default to the local in-process path. It previously
// defaulted to one developer's machine-local proxy, which exists on nobody
// else's, so a fresh install silently talked to nothing.
func TestEmbedDefaultIsLocal(t *testing.T) {
	clearRuntimeEnv(t)
	rt := resolveRuntime()

	if rt.EmbedBase != "bge" {
		t.Errorf("EmbedBase default = %q, want %q (local in-process embedder)", rt.EmbedBase, "bge")
	}
	if strings.Contains(rt.EmbedBase, "49200") {
		t.Errorf("EmbedBase still points at a developer-local proxy: %q", rt.EmbedBase)
	}
}

// The shipped server assumes no LLM endpoint. Extraction is the one thing that
// sends memory text off-machine, so the endpoint must be the user's choice: an
// unconfigured server stores the raw trace and reports "unconfigured" rather
// than quietly picking a provider. Pinned so any reintroduced default is a
// deliberate change and not a regression.
func TestLLMHasNoShippedDefault(t *testing.T) {
	clearRuntimeEnv(t)
	rt := resolveRuntime()

	if rt.LLMBase != "" {
		t.Errorf("LLMBase default = %q, want empty (no endpoint may be assumed)", rt.LLMBase)
	}
	if rt.LLMModel != "" {
		t.Errorf("LLMModel default = %q, want empty", rt.LLMModel)
	}
	// An empty config must not report itself as ready.
	if c := NewLLMClient(rt.LLMBase, "", rt.LLMModel); c.Configured() {
		t.Error("an unset endpoint reported Configured()")
	}
	// And the suggestion shown to users is not used as a default.
	if rt.LLMBase == suggestLLMBase || rt.LLMModel == suggestLLMModel {
		t.Error("the suggested endpoint leaked into the resolved defaults")
	}
}

// Setting the endpoint explicitly must still work, and be the only way.
func TestLLMConfiguredOnlyWhenAllThreeAreSet(t *testing.T) {
	cases := []struct {
		name        string
		base, model string
		key         string
		want        bool
	}{
		{"nothing set", "", "", "", false},
		{"key only", "", "", "k", false},
		{"base and model, no key", suggestLLMBase, suggestLLMModel, "", false},
		{"base and key, no model", suggestLLMBase, "", "k", false},
		{"model and key, no base", "", suggestLLMModel, "k", false},
		{"all three", suggestLLMBase, suggestLLMModel, "k", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cl := NewLLMClient(c.base, c.key, c.model)
			if got := cl.Configured(); got != c.want {
				t.Errorf("Configured() = %v, want %v", got, c.want)
			}
		})
	}
}

// A nil client must never report itself as ready.
func TestNilLLMClientIsNotConfigured(t *testing.T) {
	var cl *LLMClient
	if cl.Configured() {
		t.Error("nil client reported Configured()")
	}
}

func TestResolveRuntimeDefaults(t *testing.T) {
	clearRuntimeEnv(t)
	rt := resolveRuntime()

	if rt.Port != "19528" {
		t.Errorf("Port default = %q, want 19528", rt.Port)
	}
	if rt.DataDir != "./data" {
		t.Errorf("DataDir default = %q, want ./data", rt.DataDir)
	}
	if rt.EmbedModel != "text-embedding-3-small" {
		t.Errorf("EmbedModel default = %q", rt.EmbedModel)
	}
}

// ModelDir must come back absolute from the resolved config, not just from the
// helper: a relative "./models" is what broke on Windows.
func TestResolveRuntimeModelDirIsAbsolute(t *testing.T) {
	clearRuntimeEnv(t)
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "models"), 0o755); err != nil {
		t.Fatal(err)
	}
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(wd) })
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}

	rt := resolveRuntime()
	if !filepath.IsAbs(rt.ModelDir) {
		t.Errorf("ModelDir = %q, want an absolute path", rt.ModelDir)
	}
	if filepath.Clean(rt.ModelDir) != filepath.Clean(filepath.Join(dir, "models")) {
		t.Errorf("ModelDir = %q, want %q", rt.ModelDir, filepath.Join(dir, "models"))
	}
}

func TestResolveRuntimeHonorsOverrides(t *testing.T) {
	clearRuntimeEnv(t)
	dir := t.TempDir()
	models := filepath.Join(dir, "m")
	if err := os.MkdirAll(models, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HYATLAS_GO_PORT", "20111")
	t.Setenv("HYATLAS_GO_DATA", dir)
	t.Setenv("HYATLAS_EMBED_BASE", "https://embed.example.com/v1")
	t.Setenv("HYATLAS_EMBED_MODEL", "some-embedder")
	t.Setenv("HYATLAS_LLM_BASE", "http://127.0.0.1:11434/v1")
	t.Setenv("HYATLAS_MODEL_DIR", models)

	rt := resolveRuntime()
	for _, c := range []struct{ name, got, want string }{
		{"Port", rt.Port, "20111"},
		{"DataDir", rt.DataDir, dir},
		{"EmbedBase", rt.EmbedBase, "https://embed.example.com/v1"},
		{"EmbedModel", rt.EmbedModel, "some-embedder"},
		{"LLMBase", rt.LLMBase, "http://127.0.0.1:11434/v1"},
		{"ModelDir", rt.ModelDir, models},
	} {
		if filepath.Clean(c.got) != filepath.Clean(c.want) {
			t.Errorf("%s = %q, want %q", c.name, c.got, c.want)
		}
	}
}

// GraphPath derives from DataDir unless overridden, so a custom data directory
// keeps its graph beside it.
func TestResolveRuntimeGraphPathFollowsDataDir(t *testing.T) {
	clearRuntimeEnv(t)
	dir := t.TempDir()
	t.Setenv("HYATLAS_GO_DATA", dir)

	got, want := resolveRuntime().GraphPath, filepath.Join(dir, "graph.json")
	if filepath.Clean(got) != filepath.Clean(want) {
		t.Errorf("GraphPath = %q, want %q", got, want)
	}

	custom := filepath.Join(dir, "custom.json")
	t.Setenv("HYATLAS_GRAPH_PATH", custom)
	if got := resolveRuntime().GraphPath; got != custom {
		t.Errorf("GraphPath override ignored: got %q want %q", got, custom)
	}
}

// The startup banner must describe the embedder that was actually resolved.
// Asserting on the function main() calls, rather than grepping server.go, so a
// refactor of the log statement cannot silently reintroduce embedModel.
func TestListeningLineReportsTheRealEmbedder(t *testing.T) {
	clearRuntimeEnv(t)
	rt := resolveRuntime()
	line := listeningLine(rt)

	if !strings.Contains(line, "bge-small (in-process)") {
		t.Errorf("default banner does not name the local embedder: %q", line)
	}
	if strings.Contains(line, "text-embedding-3-small") {
		t.Errorf("banner still claims a remote OpenAI embedder: %q", line)
	}

	t.Setenv("HYATLAS_EMBED_BASE", "https://embed.example.com/v1")
	rt2 := resolveRuntime()
	line2 := listeningLine(rt2)
	if !strings.Contains(line2, "https://embed.example.com/v1") {
		t.Errorf("remote embedder not named in banner: %q", line2)
	}
}
