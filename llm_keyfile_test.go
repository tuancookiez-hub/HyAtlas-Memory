package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
)

// The Nous Portal key is a 1-hour JWT that Hermes rotates in auth.json. The
// server must read the CURRENT key per call, not the one frozen at startup —
// otherwise extraction silently 401s an hour after boot.
func TestResolveKeyReadsFileLive(t *testing.T) {
	dir := t.TempDir()
	kf := filepath.Join(dir, "auth.json")
	writeKeyFile(t, kf, `{"providers":{"nous":{"agent_key":"key-A"}}}`)

	c := NewLLMClient("http://x", "frozen-bootstrap", "m")
	c.KeyFile = kf

	if got := c.resolveKey(); got != "key-A" {
		t.Fatalf("resolveKey = %q, want key-A (from file, not the frozen bootstrap)", got)
	}

	// Hermes rotates the JWT in place; the very next call must see the new key
	// with no restart and no refresh timer.
	writeKeyFile(t, kf, `{"providers":{"nous":{"agent_key":"key-B"}}}`)
	if got := c.resolveKey(); got != "key-B" {
		t.Fatalf("after rotation resolveKey = %q, want key-B", got)
	}
}

func TestResolveKeyFallsBackToStaticOnAnyFailure(t *testing.T) {
	dir := t.TempDir()

	// Missing file -> static bootstrap key still works (never bricks extraction).
	c := NewLLMClient("http://x", "static-key", "m")
	c.KeyFile = filepath.Join(dir, "does-not-exist.json")
	if got := c.resolveKey(); got != "static-key" {
		t.Fatalf("missing file: resolveKey = %q, want static-key fallback", got)
	}

	// Empty file -> fallback.
	empty := filepath.Join(dir, "empty.json")
	if err := os.WriteFile(empty, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	c.KeyFile = empty
	if got := c.resolveKey(); got != "static-key" {
		t.Fatalf("empty file: resolveKey = %q, want static-key", got)
	}

	// No KeyFile -> plain static behaviour (catalog users with a normal API key).
	c2 := NewLLMClient("http://x", "plain-api-key", "m")
	if got := c2.resolveKey(); got != "plain-api-key" {
		t.Fatalf("no keyfile: resolveKey = %q, want plain-api-key", got)
	}
}

func TestExtractKeyShapes(t *testing.T) {
	// access_token fallback when agent_key is absent
	if got := extractKey([]byte(`{"providers":{"nous":{"access_token":"tok-1"}}}`)); got != "tok-1" {
		t.Errorf("access_token fallback = %q, want tok-1", got)
	}
	// agent_key wins over access_token
	if got := extractKey([]byte(`{"providers":{"nous":{"agent_key":"ak","access_token":"tok"}}}`)); got != "ak" {
		t.Errorf("agent_key precedence = %q, want ak", got)
	}
	// plain-text key file (bare token on a line)
	if got := extractKey([]byte("  sk-live-abc123\n")); got != "sk-live-abc123" {
		t.Errorf("plain text = %q, want sk-live-abc123", got)
	}
	// unrelated JSON (no nous provider, looks like an object) -> empty, caller falls back
	if got := extractKey([]byte(`{"foo":"bar"}`)); got != "" {
		t.Errorf("unrelated json = %q, want empty", got)
	}
}

// End-to-end: the Authorization header the server actually sends must carry the
// live key from the file, proving the per-request resolution reaches the wire.
func TestCompleteSendsLiveKeyOnTheWire(t *testing.T) {
	dir := t.TempDir()
	kf := filepath.Join(dir, "auth.json")
	writeKeyFile(t, kf, `{"providers":{"nous":{"agent_key":"wire-key-1"}}}`)

	var sent atomic.Value // last Authorization header seen
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sent.Store(r.Header.Get("Authorization"))
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"facts\":[],\"summary\":{\"text\":\"ok\"}}"}}]}`))
	}))
	defer srv.Close()

	c := NewLLMClient(srv.URL, "frozen", "mock-model")
	c.KeyFile = kf
	if _, err := c.Complete(t.Context(), "some input"); err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if got := sent.Load().(string); got != "Bearer wire-key-1" {
		t.Fatalf("Authorization = %q, want 'Bearer wire-key-1' (live from file)", got)
	}

	// Rotate the file; the next call must send the NEW key on the wire.
	writeKeyFile(t, kf, `{"providers":{"nous":{"agent_key":"wire-key-2"}}}`)
	if _, err := c.Complete(t.Context(), "another input"); err != nil {
		t.Fatalf("Complete after rotation: %v", err)
	}
	if got := sent.Load().(string); got != "Bearer wire-key-2" {
		t.Fatalf("after rotation Authorization = %q, want 'Bearer wire-key-2'", got)
	}
}

func writeKeyFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}
