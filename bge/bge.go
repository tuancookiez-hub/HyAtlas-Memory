// Package bgeemb implements a self-contained BGE-small-en-v1.5 embedder in Go.
//
// It loads the ONNX model via onnxruntime_go (cgo -> onnxruntime shared library),
// tokenizes text with the BERT WordPiece vocab, runs inference, mean-pools with
// the BGE attention mask, and normalizes. This is the in-Go embedder that
// removes the Python embed_server dependency from the v4 server.
//
// File layout (runtime, gitignored):
//
//	Windows: models/bge-small-en-v1.5.onnx(.data), vocab.txt, onnxruntime.dll
//	Linux:   models/bge-small-en-v1.5.onnx(.data), vocab.txt, libonnxruntime.so
//	macOS:   models/bge-small-en-v1.5.onnx(.data), vocab.txt, libonnxruntime.dylib
//
// Production single-EXE builds go:embed the model/vocab AND the right shared
// library for the target OS (one set per platform — Windows DLL on Windows,
// Linux .so on Linux, macOS .dylib on macOS). The build embeds whichever
// set the model/ directory contains at compile time. Cross-compile (e.g.
// from Linux to Windows) needs the target's library in models/ before
// `go build -tags embedded`.
package bgeemb

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"

	ort "github.com/yalue/onnxruntime_go"
)

const (
	VocabSize        = 30522
	HiddenSize       = 384
	MaxSeqLen        = 128
	CLSID      int64 = 101
	SEPID      int64 = 102
	PADID      int64 = 0
	UNKID      int64 = 100
)

// BGE is a thread-safe BGE-small-en-v1.5 embedder.
// Run() embeds a batch; Embed() embeds a single string with mean pooling.
type BGE struct {
	sess       *ort.DynamicSession[int64, float32]
	vocab      map[string]int64
	mu         sync.Mutex
	path       string // dir holding the model/vocab/dll
	embedFuncr func(ctx context.Context, text string) ([]float32, error)
}

// runtimeLibName returns the platform-appropriate onnxruntime shared library
// name. Production builds go:embed whichever one is present in models/ at
// compile time. Cross-compile sets up the right library per target OS.
func runtimeLibName() string {
	switch runtime.GOOS {
	case "windows":
		return "onnxruntime.dll"
	case "darwin":
		return "libonnxruntime.dylib"
	default:
		// linux, freebsd, etc.
		return "libonnxruntime.so"
	}
}

// New loads the model + vocab + sets the onnxruntime shared library path.
// baseDir should contain the model files and the platform's onnxruntime
// shared library (see package comment for the per-OS file layout).
func New(baseDir string) (*BGE, error) {
	libName := runtimeLibName()
	lib := filepath.Join(baseDir, libName)
	modelPath := filepath.Join(baseDir, "bge-small-en-v1.5.onnx")
	vocabPath := filepath.Join(baseDir, "vocab.txt")

	// If the platform-correct library isn't present, fall back to whichever
	// onnxruntime* file IS present in baseDir. This makes single-binary
	// releases (which embed one library) work regardless of where the
	// binary is run, as long as the runtime OS matches the build target.
	if _, err := os.Stat(lib); err != nil {
		if fallback, ferr := findLibFallback(baseDir); ferr == nil {
			lib = fallback
		} else {
			return nil, fmt.Errorf("onnxruntime library %s not found in %s (and no fallback present): %w", libName, baseDir, err)
		}
	}

	ort.SetSharedLibraryPath(lib)
	if err := ort.InitializeEnvironment(); err != nil {
		return nil, fmt.Errorf("ort init: %w", err)
	}

	// onnxruntime resolves the external .data weights relative to the process CWD,
	// not the model file's directory. chdir to the model dir for the session load;
	// restore afterwards (the server serves from its own cwd).
	priorWD, _ := os.Getwd()
	_ = os.Chdir(filepath.Dir(modelPath))
	sess, err := ort.NewDynamicSession[int64, float32](modelPath,
		[]string{"input_ids", "attention_mask"}, []string{"last_hidden_state"})
	_ = os.Chdir(priorWD)
	if err != nil {
		_ = ort.DestroyEnvironment()
		return nil, fmt.Errorf("new session: %w", err)
	}

	vocab, err := loadVocab(vocabPath)
	if err != nil {
		sess.Destroy()
		_ = ort.DestroyEnvironment()
		return nil, err
	}

	return &BGE{sess: sess, vocab: vocab, path: baseDir}, nil
}

// findLibFallback looks in baseDir for any onnxruntime* shared library and
// returns its full path. Used when the platform-default name isn't present
// (e.g. someone renamed onnxruntime.so to libonnxruntime.so.1.20.0).
func findLibFallback(baseDir string) (string, error) {
	entries, err := os.ReadDir(baseDir)
	if err != nil {
		return "", err
	}
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, "onnxruntime") || strings.HasPrefix(name, "libonnxruntime") {
			return filepath.Join(baseDir, name), nil
		}
	}
	return "", fmt.Errorf("no onnxruntime* file found in %s", baseDir)
}

// Destroy frees the onnxruntime environment. Call once at shutdown.
func (b *BGE) Destroy() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.sess != nil {
		b.sess.Destroy()
		b.sess = nil
	}
	_ = ort.DestroyEnvironment()
}

// Embed returns a normalized 384-dim vector for a single string (mean pooling + L2).
func (b *BGE) Embed(ctx context.Context, text string) ([]float32, error) {
	ids, mask := tokenize(b.vocab, text, MaxSeqLen)
	return b.runMeanPool(ids, mask)
}

// runMeanPool token-embeds and mean-pools over the attention mask (BGE semantics).
func (b *BGE) runMeanPool(ids, mask []int64) ([]float32, error) {
	seq := len(ids)
	shape := ort.NewShape(1, int64(seq))
	inT, err := ort.NewTensor(shape, ids)
	if err != nil {
		return nil, err
	}
	msk := make([]int64, seq)
	copy(msk, mask)
	maskT, err := ort.NewTensor(shape, msk)
	if err != nil {
		return nil, err
	}
	// output: [1, seq, 384]
	flat := make([]float32, 1*seq*HiddenSize)
	outT, err := ort.NewTensor(ort.NewShape(1, int64(seq), HiddenSize), flat)
	if err != nil {
		return nil, err
	}

	b.mu.Lock()
	runErr := b.sess.Run([]*ort.Tensor[int64]{inT, maskT}, []*ort.Tensor[float32]{outT})
	b.mu.Unlock()
	if runErr != nil {
		return nil, runErr
	}

	out := outT.GetData()
	// mean pooling over "valid" tokens (mask==1), BGE-style
	vec := make([]float64, HiddenSize)
	var count float64
	for t := 0; t < seq; t++ {
		if mask[t] == 0 {
			continue
		}
		row := out[t*HiddenSize : (t+1)*HiddenSize]
		for i := 0; i < HiddenSize; i++ {
			vec[i] += float64(row[i])
		}
		count++
	}
	if count == 0 {
		count = 1
	}
	res := make([]float32, HiddenSize)
	var norm float64
	for i := 0; i < HiddenSize; i++ {
		res[i] = float32(vec[i] / count)
		norm += float64(res[i]) * float64(res[i])
	}
	// L2 normalize
	if norm > 0 {
		r := float32(1 / math.Sqrt(norm))
		for i := range res {
			res[i] *= r
		}
	}
	return res, nil
}

// loadVocab reads the BERT vocab.txt into a token->id map.
func loadVocab(path string) (map[string]int64, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("vocab: %w", err)
	}
	lines := strings.Split(strings.ReplaceAll(string(b), "\r\n", "\n"), "\n")
	vocab := make(map[string]int64, len(lines))
	for i, w := range lines {
		if w == "" {
			continue
		}
		vocab[w] = int64(i)
	}
	return vocab, nil
}

// tokenize does BERT-style lowercase WordPiece to input_ids + attention_mask.
func tokenize(vocab map[string]int64, text string, maxLen int) ([]int64, []int64) {
	text = strings.ToLower(strings.TrimSpace(text))
	words := strings.Fields(text)
	if len(words) == 0 {
		words = []string{"[unk]"}
	}
	ids := []int64{CLSID}
	for _, w := range words {
		for _, piece := range wordpiece(vocab, w, 200) {
			ids = append(ids, piece)
		}
	}
	// truncate to leave room for [SEP]
	if len(ids) >= maxLen-1 {
		ids = ids[:maxLen-1]
	}
	ids = append(ids, SEPID)
	seq := len(ids)
	mask := make([]int64, seq)
	for i := range mask {
		mask[i] = 1
	}
	// pad to a fixed block (BGE uses 128 or the seq itself; we use the sequence length
	// directly, no padding beyond, since onnxruntime dynamic axes handle variable length)
	return ids, mask
}

// wordpiece greedily splits a word into subword pieces using the vocab.
func wordpiece(vocab map[string]int64, word string, maxPieces int) []int64 {
	if id, ok := vocab[word]; ok {
		return []int64{id}
	}
	candidates := []rune(word)
	idx := 0
	allPieces := make([]int64, 0, len(candidates))
	// basic greedy: try longest substring that is in vocab
	for idx < len(candidates) {
		// find the longest token starting at idx
		best := -1
		for end := len(candidates); end > idx; end-- {
			sub := strings.ToLower(string(candidates[idx:end]))
			if _, ok := vocab[sub]; ok {
				best = end
				break
			}
		}
		if best == -1 {
			// try ## continuation
			allPieces = append(allPieces, UNKID)
			idx++
			continue
		}
		sub := strings.ToLower(string(candidates[idx:best]))
		if idx != 0 {
			sub = "##" + sub
		}
		if id, ok := vocab[sub]; ok {
			allPieces = append(allPieces, id)
		} else {
			allPieces = append(allPieces, UNKID)
		}
		idx = best
	}
	if len(allPieces) > maxPieces {
		allPieces = allPieces[:maxPieces]
	}
	return allPieces
}

// EmbedBatch embeds multiple strings (convenience wrapper).
func (b *BGE) EmbedBatch(ctx context.Context, texts []string) ([][]float32, error) {
	out := make([][]float32, len(texts))
	for i, t := range texts {
		v, err := b.Embed(ctx, t)
		if err != nil {
			return nil, err
		}
		out[i] = v
	}
	return out, nil
}

var _ = sort.Strings
var _ = json.Marshal
