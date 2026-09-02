// Package main — single-file embedded assets (macOS).
//
// This file is compiled with go:build embedded && darwin to bundle the BGE
// model, vocab, and libonnxruntime.dylib INSIDE the binary. At startup the
// embedded assets are written to a cache dir, then the BGE embedder loads
// them from there. Result: one binary that runs anywhere on macOS with no
// external files.
//
// Build the embedded variant:
//   go build -tags embedded -o hyatlas-go .
// when models/ (with libonnxruntime.dylib alongside the onnx file) sits next
// to the source. Without the tag, the server reads ./models at runtime.
//go:build embedded && darwin

package main

import (
	_ "embed"
	"log"
	"os"
	"path/filepath"
	"sync"
)

//go:embed models/bge-small-en-v1.5.onnx
var embeddedModel []byte

//go:embed models/bge-small-en-v1.5.onnx.data
var embeddedModelData []byte

//go:embed models/vocab.txt
var embeddedVocab []byte

//go:embed models/libonnxruntime.dylib
var embeddedMacDylib []byte

var (
	extractOnce sync.Once
	extractDir  string
)

// useEmbeddedAssets is true only in the embedded build (this file).
const useEmbeddedAssets = true

// materializeAssets writes the embedded model assets to a cache dir and returns that dir.
func materializeAssets() string {
	extractOnce.Do(func() {
		dir, err := os.MkdirTemp("", "hyatlas-embedded-*")
		if err != nil {
			log.Fatal("mkdtemp: ", err)
		}
		files := map[string][]byte{
			"bge-small-en-v1.5.onnx":      embeddedModel,
			"bge-small-en-v1.5.onnx.data": embeddedModelData,
			"vocab.txt":                   embeddedVocab,
			"libonnxruntime.dylib":        embeddedMacDylib,
		}
		for name, data := range files {
			p := filepath.Join(dir, name)
			if err := os.WriteFile(p, data, 0o755); err != nil {
				log.Fatalf("extract %s: %v", name, err)
			}
		}
		extractDir = dir
	})
	return extractDir
}
