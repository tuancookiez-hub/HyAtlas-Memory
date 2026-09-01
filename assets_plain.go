//go:build !embedded

package main

// useEmbeddedAssets is false in the non-embedded build: assets are read from
// ./models on disk instead of being embedded in the binary.
const useEmbeddedAssets = false

// materializeAssets is a no-op in the non-embedded build; assets stay on disk.
func materializeAssets() string { return "" }
