package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// Close must drain in-flight async persistence. Before the WaitGroup fix,
// persistUsageAsync spawned fire-and-forget goroutines that wrote the usage
// file into the data dir *after* Close returned, so t.TempDir() cleanup failed
// with "directory not empty" and took down the whole `go test` run on CI.
//
// A driver goroutine keeps async persists in flight across the Close call so
// the race is reproduced deterministically rather than depending on luck: with
// the fix, Close rejects new async work and waits for what is already running,
// so nothing can touch the dir afterwards.
func TestCloseDrainsAsyncPersistence(t *testing.T) {
	dir := t.TempDir()
	store, err := NewMemoryStore(ctxForTest(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	if store.countsPath == "" {
		t.Fatal("countsPath is empty; async persistence is disabled and this test proves nothing")
	}
	if err := store.Add(memory.L3Fact, "doc-drain", "a fact", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": "2026-10-07T00:00:00Z",
	}); err != nil {
		t.Fatalf("add: %v", err)
	}

	// Keep async persists continuously in flight until told to stop.
	stop := make(chan struct{})
	driver := make(chan struct{})
	go func() {
		defer close(driver)
		for {
			select {
			case <-stop:
				return
			default:
				store.persistUsageAsync()
			}
		}
	}()

	store.Close()

	// Close returned. Nothing may touch the data dir from here on.
	close(stop)
	<-driver

	if err := os.Remove(store.countsPath); err != nil && !os.IsNotExist(err) {
		t.Fatalf("remove usage file after Close: %v", err)
	}
	if _, err := os.Stat(store.countsPath); err == nil {
		t.Fatal("a straggler goroutine re-created the usage file AFTER Close returned — Close did not drain")
	}
	if _, err := os.Stat(store.countsPath + ".tmp"); err == nil {
		t.Fatalf("%s left behind after Close — async write was still in flight", store.countsPath+".tmp")
	}
	// The dir must be removable: exactly what t.TempDir() cleanup does.
	if err := os.RemoveAll(dir); err != nil {
		t.Fatalf("RemoveAll after Close: %v", err)
	}
}

// Close is documented as safe to call multiple times; concurrent calls must not
// double-persist or panic.
func TestCloseIsIdempotentAndConcurrentSafe(t *testing.T) {
	dir := t.TempDir()
	store, err := NewMemoryStore(ctxForTest(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	if err := store.Add(memory.L3Fact, "doc-close-idem", "a fact", map[string]string{
		"user_id": "u", "agent_id": "a", "ts": "2026-10-07T00:00:00Z",
	}); err != nil {
		t.Fatalf("add: %v", err)
	}

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); store.Close() }()
	}
	wg.Wait()
	store.Close() // and once more serially

	// Final counters survived the single real persist.
	b, err := os.ReadFile(store.countsPath)
	if err != nil {
		t.Fatalf("usage file %s missing after Close: %v", store.countsPath, err)
	}
	if len(b) == 0 {
		t.Fatal("usage file is empty")
	}
}

// Concurrent persists share one fixed .tmp path; without serialization two
// goroutines can interleave and rename a half-written file. Under -race this
// catches the unsynchronized write.
func TestConcurrentPersistUsageDoesNotCorrupt(t *testing.T) {
	dir := t.TempDir()
	store, err := NewMemoryStore(ctxForTest(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Skipf("MemoryStore unavailable in test env: %v", err)
	}
	defer store.Close()

	// Generate real counters first: the assertion below checks the persisted
	// values are coherent, which is meaningless on an empty store.
	for i := 0; i < 5; i++ {
		if err := store.Add(memory.L3Fact, "doc-corrupt-"+strconv.Itoa(i), "a fact", map[string]string{
			"user_id": "u", "agent_id": "a", "ts": "2026-10-07T00:00:00Z",
		}); err != nil {
			t.Fatalf("add: %v", err)
		}
	}

	var wg sync.WaitGroup
	for i := 0; i < 40; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); store.persistUsage() }()
	}
	wg.Wait()

	b, err := os.ReadFile(store.countsPath)
	if err != nil {
		t.Fatalf("usage file %s: %v", store.countsPath, err)
	}
	var c UsageCounters
	if err := json.Unmarshal(b, &c); err != nil {
		t.Fatalf("usage file is corrupt (interleaved writes): %v — content %q", err, string(b))
	}
	if c.Writes != 5 {
		t.Fatalf("writes = %d, want exactly 5 (interleaved writes lost or garbled counters): %+v", c.Writes, c)
	}
}
