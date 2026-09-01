package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	bgeemb "github.com/tuancookiez-hub/hyatlas-v4/bge"
)

// Embedder produces normalized vectors (chromem requires normalized).
type Embedder interface {
	Embed(ctx context.Context, text string) ([]float32, error)
}

// OpenAIEmbedder calls an OpenAI-compatible /embeddings endpoint.
// Routes through the ai2api proxy on loopback (firewall-safe) by default.
type OpenAIEmbedder struct {
	BaseURL string
	APIKey  string
	Model   string
	Client  *http.Client
}

func NewOpenAIEmbedder(baseURL, key, model string) *OpenAIEmbedder {
	return &OpenAIEmbedder{BaseURL: baseURL, APIKey: key, Model: model,
		Client: &http.Client{Timeout: 60 * time.Second}}
}

func (e *OpenAIEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	body, _ := json.Marshal(map[string]any{"model": e.Model, "input": []string{text}})
	req, err := http.NewRequestWithContext(ctx, "POST", e.BaseURL+"/embeddings", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if e.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+e.APIKey)
	}
	resp, err := e.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("embed HTTP %d: %s", resp.StatusCode, truncStr(data, 200))
	}
	var out struct {
		Data []struct {
			Embedding []float32 `json:"embedding"`
		} `json:"data"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	if len(out.Data) == 0 {
		return nil, fmt.Errorf("embed: no data returned")
	}
	return normalize(out.Data[0].Embedding), nil
}

// LocalEmbedder is a deterministic fallback (BGE-ONNX wired later). Normalized.
type LocalEmbedder struct{ dim int }

func NewLocalEmbedder(dim int) *LocalEmbedder { return &LocalEmbedder{dim: dim} }

func (e *LocalEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	v := make([]float32, e.dim)
	for i, b := range []byte(text) {
		v[i%e.dim] += float32(b)
	}
	return normalize(v), nil
}

func normalize(v []float32) []float32 {
	var n float64
	for _, x := range v {
		n += float64(x) * float64(x)
	}
	if n == 0 {
		return v
	}
	r := float32(1 / sqrtf(float64(n)))
	for i := range v {
		v[i] *= r
	}
	return v
}

func sqrtf(x float64) float64 {
	if x <= 0 {
		return 0
	}
	g := x
	for i := 0; i < 24; i++ {
		g = (g + x/g) / 2
	}
	return g
}

// BGEGoEmbedder wraps the in-Go BGE model (bgeemb.BGE) as a chromem Embedder.
type BGEGoEmbedder struct {
	bge *bgeemb.BGE
}

func NewBGEGoEmbedder(modelDir string) (*BGEGoEmbedder, error) {
	m, err := bgeemb.New(modelDir)
	if err != nil {
		return nil, err
	}
	return &BGEGoEmbedder{bge: m}, nil
}

func (e *BGEGoEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	return e.bge.Embed(ctx, text)
}

func (e *BGEGoEmbedder) Destroy() {
	if e.bge != nil {
		e.bge.Destroy()
	}
}
