package main

import (
	"crypto/rand"
	"encoding/hex"
	"time"
)

// newID returns a unique, sortable-ish id (hex) for a memory document.
func newID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	hexpart := hex.EncodeToString(b)
	return "m" + time.Now().UTC().Format("20060102150405") + hexpart[:8]
}
