package main

import (
	"archive/zip"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// clearSnapshotEnv blanks the snapshot settings so a developer's shell cannot
// change what the defaults tests see.
func clearSnapshotEnv(t *testing.T) {
	t.Helper()
	t.Setenv("HYATLAS_SNAPSHOT_EVERY", "")
	t.Setenv("HYATLAS_SNAPSHOT_KEEP", "")
	t.Setenv("HYATLAS_SNAPSHOT_DIR", "")
}

func TestResolveSnapshotDefaults(t *testing.T) {
	clearSnapshotEnv(t)
	parent := t.TempDir()
	dataDir := filepath.Join(parent, "data")

	cfg := resolveSnapshot(dataDir)
	if cfg.Every != 24*time.Hour {
		t.Fatalf("Every = %s, want 24h", cfg.Every)
	}
	if cfg.Keep != 7 {
		t.Fatalf("Keep = %d, want 7", cfg.Keep)
	}
	if want := filepath.Join(parent, "data-snapshots"); cfg.Dir != want {
		t.Fatalf("Dir = %q, want %q", cfg.Dir, want)
	}
}

func TestResolveSnapshotOffDisables(t *testing.T) {
	for _, v := range []string{"off", "OFF", "0"} {
		clearSnapshotEnv(t)
		t.Setenv("HYATLAS_SNAPSHOT_EVERY", v)
		if cfg := resolveSnapshot(t.TempDir()); cfg.Every != 0 {
			t.Fatalf("HYATLAS_SNAPSHOT_EVERY=%q: Every = %s, want 0", v, cfg.Every)
		}
	}
}

func TestResolveSnapshotOverrides(t *testing.T) {
	clearSnapshotEnv(t)
	t.Setenv("HYATLAS_SNAPSHOT_EVERY", "6h")
	t.Setenv("HYATLAS_SNAPSHOT_KEEP", "3")
	custom := t.TempDir()
	t.Setenv("HYATLAS_SNAPSHOT_DIR", custom)

	cfg := resolveSnapshot(filepath.Join(t.TempDir(), "data"))
	if cfg.Every != 6*time.Hour {
		t.Fatalf("Every = %s, want 6h", cfg.Every)
	}
	if cfg.Keep != 3 {
		t.Fatalf("Keep = %d, want 3", cfg.Keep)
	}
	if cfg.Dir != custom {
		t.Fatalf("Dir = %q, want %q", cfg.Dir, custom)
	}
}

// A snapshot folder inside the data directory would be loaded as a chromem
// collection, so that setting turns snapshots off; a sibling folder whose name
// merely starts with the data dir's name is fine.
func TestResolveSnapshotRejectsDirInsideData(t *testing.T) {
	clearSnapshotEnv(t)
	data := filepath.Join(t.TempDir(), "data")
	t.Setenv("HYATLAS_SNAPSHOT_DIR", filepath.Join(data, "snaps"))
	if cfg := resolveSnapshot(data); cfg.Every != 0 {
		t.Fatalf("dir inside data: Every = %s, want 0 (off)", cfg.Every)
	}
	t.Setenv("HYATLAS_SNAPSHOT_DIR", data)
	if cfg := resolveSnapshot(data); cfg.Every != 0 {
		t.Fatalf("dir equal to data: Every = %s, want 0 (off)", cfg.Every)
	}
	t.Setenv("HYATLAS_SNAPSHOT_DIR", data+"-snapshots")
	if cfg := resolveSnapshot(data); cfg.Every == 0 {
		t.Fatal("sibling dir: snapshots turned off, want on")
	}
}

func TestSnapshotWritesZipWithoutTmp(t *testing.T) {
	dir := t.TempDir()
	s, err := NewMemoryStore(context.Background(), dir, NewLocalEmbedder(384), filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	defer s.Close()
	meta := map[string]string{"user_id": "u", "agent_id": "a", "ts": "2026-10-06T00:00:00Z"}
	if err := s.Add(memory.L2Raw, "snap-1", "first remembered thing", meta); err != nil {
		t.Fatalf("add 1: %v", err)
	}
	if err := s.Add(memory.L2Raw, "snap-2", "second remembered thing", meta); err != nil {
		t.Fatalf("add 2: %v", err)
	}

	dstDir := t.TempDir()
	dst := filepath.Join(dstDir, "hyatlas-20261012-000000.zip")
	if err := s.Snapshot(dir, dst); err != nil {
		t.Fatalf("snapshot: %v", err)
	}

	zr, err := zip.OpenReader(dst)
	if err != nil {
		t.Fatalf("open zip: %v", err)
	}
	defer zr.Close()
	var hasIndex, hasGob bool
	for _, f := range zr.File {
		if strings.HasSuffix(f.Name, ".tmp") {
			t.Fatalf("zip contains temp entry %q", f.Name)
		}
		if f.Name == "doc_index.json" {
			hasIndex = true
		}
		if strings.HasSuffix(f.Name, ".gob") {
			hasGob = true
		}
	}
	if !hasIndex {
		t.Fatal("zip is missing doc_index.json")
	}
	if !hasGob {
		t.Fatal("zip has no .gob file")
	}

	entries, err := os.ReadDir(dstDir)
	if err != nil {
		t.Fatalf("read dst dir: %v", err)
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".tmp") {
			t.Fatalf("temp file left behind: %s", e.Name())
		}
	}
}

func TestPruneSnapshotsKeepsNewest(t *testing.T) {
	dir := t.TempDir()
	var names []string
	for i := 1; i <= 9; i++ {
		name := "hyatlas-2026010" + string(rune('0'+i)) + "-000000.zip"
		names = append(names, name)
		if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o644); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, "other.txt"), []byte("keep me"), 0o644); err != nil {
		t.Fatalf("write other: %v", err)
	}

	if err := pruneSnapshots(dir, 7); err != nil {
		t.Fatalf("prune: %v", err)
	}

	for i, name := range names {
		_, err := os.Stat(filepath.Join(dir, name))
		if i < 2 && !os.IsNotExist(err) {
			t.Fatalf("%s should have been pruned (err=%v)", name, err)
		}
		if i >= 2 && err != nil {
			t.Fatalf("%s should have been kept: %v", name, err)
		}
	}
	b, err := os.ReadFile(filepath.Join(dir, "other.txt"))
	if err != nil || string(b) != "keep me" {
		t.Fatalf("other.txt changed or removed: %v", err)
	}
}

func TestRunSnapshotsOffReturnsAtOnce(t *testing.T) {
	done := make(chan struct{})
	go func() {
		runSnapshots(context.Background(), nil, t.TempDir(), snapshotConfig{Every: 0})
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("runSnapshots with Every=0 did not return")
	}
}
