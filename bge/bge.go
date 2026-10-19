// Package bgeemb implements a self-contained BGE-small-en-v1.5 embedder in Go.
//
// It loads the ONNX model via onnxruntime_go (cgo -> onnxruntime shared library),
// tokenizes text with the BERT WordPiece vocab, runs inference, mean-pools with
// the BGE attention mask, and normalizes. This is the in-Go embedder that
// removes the Python embed_server dependency from the server.
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
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"unicode/utf8"

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
	maxTok     int // rune length of the longest vocab entry
	mu         sync.Mutex
	path       string // dir holding the model/vocab/dll
	tokenTypes bool   // graph declares token_type_ids as an input
}

// inputNames returns the session's input tensors in the order Run expects them.
func (b *BGE) inputNames() []string {
	if b.tokenTypes {
		return []string{"input_ids", "attention_mask", "token_type_ids"}
	}
	return []string{"input_ids", "attention_mask"}
}

// graphWantsTokenTypes reports whether the model declares token_type_ids as an
// input. BERT exports disagree on this point and the disagreement is not
// cosmetic: feeding a two-input session against a three-input graph makes
// onnxruntime substitute an empty tensor for the missing input, which surfaces
// at run time as `Gather node /embeddings/token_type_embeddings` failing with a
// non-zero status. Every Embed call then errors, so every write and every
// search on such a build 500s. Best-effort: a probe failure returns false and
// the session keeps the two-input shape it had before.
func graphWantsTokenTypes(modelPath string) bool {
	ins, _, err := ort.GetInputOutputInfo(modelPath)
	if err != nil {
		return false
	}
	for _, in := range ins {
		if in.Name == "token_type_ids" {
			return true
		}
	}
	return false
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
	tokenTypes := graphWantsTokenTypes(modelPath)
	b := &BGE{vocab: nil, path: baseDir, tokenTypes: tokenTypes}
	sess, err := ort.NewDynamicSession[int64, float32](modelPath,
		b.inputNames(), []string{"last_hidden_state"})
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

	b.sess = sess
	b.vocab = vocab
	b.maxTok = longestToken(vocab)
	return b, nil
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
	ids, mask := tokenize(b.vocab, b.maxTok, text, MaxSeqLen)
	return b.runMeanPool(ids, mask)
}

// runMeanPool token-embeds and mean-pools over the attention mask (BGE semantics).
func (b *BGE) runMeanPool(ids, mask []int64) ([]float32, error) {
	seq := len(ids)
	shape := ort.NewShape(1, int64(seq))
	// Every tensor holds native memory that Go's garbage collector never frees, so
	// each one is destroyed when this call returns.
	inT, err := ort.NewTensor(shape, ids)
	if err != nil {
		return nil, err
	}
	defer inT.Destroy()
	msk := make([]int64, seq)
	copy(msk, mask)
	maskT, err := ort.NewTensor(shape, msk)
	if err != nil {
		return nil, err
	}
	defer maskT.Destroy()
	// output: [1, seq, 384]
	flat := make([]float32, 1*seq*HiddenSize)
	outT, err := ort.NewTensor(ort.NewShape(1, int64(seq), HiddenSize), flat)
	if err != nil {
		return nil, err
	}
	defer outT.Destroy()

	b.mu.Lock()
	inputs := []*ort.Tensor[int64]{inT, maskT}
	// Single-segment BERT inference: segment ids are all zero. Only fed when the
	// graph declares the input (see graphWantsTokenTypes); the order must match
	// inputNames().
	if b.tokenTypes {
		tt := make([]int64, seq)
		ttT, ttErr := ort.NewTensor(shape, tt)
		if ttErr != nil {
			b.mu.Unlock()
			return nil, ttErr
		}
		defer ttT.Destroy()
		inputs = append(inputs, ttT)
	}
	runErr := b.sess.Run(inputs, []*ort.Tensor[float32]{outT})
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

// longestToken returns the rune length of the longest vocab entry. No longer
// substring can match, so wordpiece never needs to try one.
func longestToken(vocab map[string]int64) int {
	n := 0
	for w := range vocab {
		if l := utf8.RuneCountInString(w); l > n {
			n = l
		}
	}
	return n
}

// tokenize does BERT-style lowercase WordPiece to input_ids + attention_mask.
// maxTok is the vocab's longest entry (see longestToken). Words stop being split
// once the sequence is full, since the rest would be truncated anyway, so a long
// pasted text costs no more than a short one.
func tokenize(vocab map[string]int64, maxTok int, text string, maxLen int) ([]int64, []int64) {
	text = strings.ToLower(strings.TrimSpace(text))
	words := strings.Fields(text)
	if len(words) == 0 {
		words = []string{"[unk]"}
	}
	ids := []int64{CLSID}
	for _, w := range words {
		room := maxLen - 1 - len(ids)
		if room <= 0 {
			break
		}
		ids = append(ids, wordpiece(vocab, maxTok, w, room)...)
	}
	ids = append(ids, SEPID)
	seq := len(ids)
	mask := make([]int64, seq)
	for i := range mask {
		mask[i] = 1
	}
	// No padding: onnxruntime's dynamic axes take the sequence at its own length.
	return ids, mask
}

// wordpiece greedily splits an already lowercased word into subword pieces using
// the vocab, returning at most maxPieces of them. It only tries substrings up to
// maxTok runes long (none longer is in the vocab), so a long token such as a hash
// costs time in proportion to its length rather than its length squared.
func wordpiece(vocab map[string]int64, maxTok int, word string, maxPieces int) []int64 {
	if id, ok := vocab[word]; ok {
		return []int64{id}
	}
	candidates := []rune(word)
	idx := 0
	allPieces := make([]int64, 0, min(len(candidates), maxPieces))
	// basic greedy: try the longest substring that is in the vocab
	for idx < len(candidates) && len(allPieces) < maxPieces {
		best := -1
		for end := min(len(candidates), idx+maxTok); end > idx; end-- {
			if _, ok := vocab[string(candidates[idx:end])]; ok {
				best = end
				break
			}
		}
		if best == -1 {
			allPieces = append(allPieces, UNKID)
			idx++
			continue
		}
		sub := string(candidates[idx:best])
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
	return allPieces
}
