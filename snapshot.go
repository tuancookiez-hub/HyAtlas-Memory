package main

import (
	"archive/zip"
	"context"
	"fmt"
	"io"
	"io/fs"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// snapshotFirstDelay is how long the server waits after start before its first
// snapshot. A minute keeps startup quiet while still giving a fresh install a
// backup early. It is a var so tests can shorten it.
var snapshotFirstDelay = time.Minute

// snapshotDefaultKeep is how many snapshot zips are kept when the setting is
// missing or not a positive number.
const snapshotDefaultKeep = 7

// snapshotConfig controls the built-in rolling snapshots of the data directory.
type snapshotConfig struct {
	Every time.Duration // 0 = off
	Keep  int           // newest zips kept; <1 means 7
	Dir   string        // where zips go
}

// resolveSnapshot reads the snapshot settings from the environment. The default
// directory is a sibling of the data directory rather than a child of it,
// because chromem treats every subdirectory of the data dir as a collection, so
// a snapshot stored inside it would be read back as one.
func resolveSnapshot(dataDir string) snapshotConfig {
	cfg := snapshotConfig{Keep: envInt("HYATLAS_SNAPSHOT_KEEP", snapshotDefaultKeep)}

	// parseDuration reads a bare "0" as zero seconds, so "0" and "off" are
	// handled here, before it, to make the intent explicit.
	raw := strings.TrimSpace(os.Getenv("HYATLAS_SNAPSHOT_EVERY"))
	if strings.EqualFold(raw, "off") || raw == "0" {
		cfg.Every = 0
	} else {
		cfg.Every = parseDuration("HYATLAS_SNAPSHOT_EVERY", 24*time.Hour)
	}

	if dir := strings.TrimSpace(os.Getenv("HYATLAS_SNAPSHOT_DIR")); dir != "" {
		cfg.Dir = dir
	} else {
		clean := filepath.Clean(dataDir)
		cfg.Dir = filepath.Join(filepath.Dir(clean), filepath.Base(clean)+"-snapshots")
	}
	// A snapshot folder inside the data directory would be loaded by chromem as a
	// collection on the next start, so such a setting turns snapshots off instead.
	if rel, err := filepath.Rel(filepath.Clean(dataDir), filepath.Clean(cfg.Dir)); err == nil &&
		rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		log.Printf("snapshot: HYATLAS_SNAPSHOT_DIR %s is inside the data directory %s; snapshots are off", cfg.Dir, dataDir)
		cfg.Every = 0
	}
	return cfg
}

// snapshotLine describes the snapshot setting for the startup log, so an operator
// can see at a glance whether backups are running and where they go.
func snapshotLine(cfg snapshotConfig) string {
	if cfg.Every <= 0 {
		return "snapshots: off"
	}
	keep := cfg.Keep
	if keep < 1 {
		keep = snapshotDefaultKeep
	}
	return fmt.Sprintf("snapshots: every %s, keeping %d, in %s", cfg.Every, keep, cfg.Dir)
}

// Snapshot writes a zip of the store's data directory to dst. Writes to the store
// are held off while the files are copied, so the zip is a consistent view of
// the index and the chromem rows rather than a mix of two moments. The zip is
// built under a temporary name and renamed into place, so an interrupted
// snapshot never looks complete.
func (s *MemoryStore) Snapshot(dataDir, dst string) error {
	// Same lock order as Supersede and Add, so the snapshot cannot deadlock with them.
	s.supMu.Lock()
	defer s.supMu.Unlock()
	s.rowMu.Lock()
	defer s.rowMu.Unlock()
	s.mu.RLock()
	defer s.mu.RUnlock()

	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	tmp := dst + ".tmp"
	if err := writeSnapshotZip(dataDir, dst, tmp); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, dst)
}

// writeSnapshotZip walks dataDir and writes every file into the zip at tmp. It
// skips the snapshot directory when that sits inside dataDir, so a snapshot never
// archives earlier snapshots or itself.
func writeSnapshotZip(dataDir, dst, tmp string) error {
	out, err := os.Create(tmp)
	if err != nil {
		return err
	}
	zw := zip.NewWriter(out)

	dataAbs, err := filepath.Abs(dataDir)
	if err != nil {
		out.Close()
		return err
	}
	snapAbs, err := filepath.Abs(filepath.Dir(dst))
	if err != nil {
		out.Close()
		return err
	}

	walkErr := filepath.WalkDir(dataDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		pathAbs, err := filepath.Abs(path)
		if err != nil {
			return err
		}
		if d.IsDir() {
			if pathAbs == snapAbs && pathAbs != dataAbs {
				return filepath.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(d.Name(), ".tmp") {
			return nil
		}
		rel, err := filepath.Rel(dataDir, path)
		if err != nil {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		hdr, err := zip.FileInfoHeader(info)
		if err != nil {
			return err
		}
		hdr.Name = filepath.ToSlash(rel)
		hdr.Method = zip.Deflate
		w, err := zw.CreateHeader(hdr)
		if err != nil {
			return err
		}
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		defer f.Close()
		_, err = io.Copy(w, f)
		return err
	})

	closeErr := zw.Close()
	if fileErr := out.Close(); closeErr == nil {
		closeErr = fileErr
	}
	if walkErr != nil {
		return walkErr
	}
	return closeErr
}

// runSnapshots takes a snapshot of the data directory every cfg.Every, starting
// after snapshotFirstDelay, and keeps only the newest cfg.Keep. It blocks until
// ctx ends, so the caller runs it in a goroutine. Failures are logged and the
// loop carries on, because a missed backup must never stop the server.
func runSnapshots(ctx context.Context, store *MemoryStore, dataDir string, cfg snapshotConfig) {
	if cfg.Every <= 0 {
		return
	}
	if err := os.MkdirAll(cfg.Dir, 0o755); err != nil {
		log.Printf("snapshot: create %s: %v", cfg.Dir, err)
	}
	keep := cfg.Keep
	if keep < 1 {
		keep = snapshotDefaultKeep
	}

	timer := time.NewTimer(snapshotFirstDelay)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
		takeSnapshot(store, dataDir, cfg.Dir, keep)
		timer.Reset(cfg.Every)
	}
}

// takeSnapshot writes one timestamped zip and prunes the directory to keep. It
// only logs, since a failed snapshot must not stop the server.
func takeSnapshot(store *MemoryStore, dataDir, dir string, keep int) {
	name := "hyatlas-" + time.Now().UTC().Format("20060102-150405") + ".zip"
	dst := filepath.Join(dir, name)
	if err := store.Snapshot(dataDir, dst); err != nil {
		log.Printf("snapshot: %v", err)
		return
	}
	info, err := os.Stat(dst)
	if err != nil {
		log.Printf("snapshot: stat %s: %v", dst, err)
		return
	}
	names, err := listSnapshots(dir)
	if err != nil {
		log.Printf("snapshot: list %s: %v", dir, err)
		return
	}
	if err := pruneSnapshots(dir, keep); err != nil {
		log.Printf("snapshot: prune %s: %v", dir, err)
	}
	kept := len(names)
	if kept > keep {
		kept = keep
	}
	log.Printf("snapshot: wrote %s (%d bytes, kept %d)", dst, info.Size(), kept)
}

// listSnapshots returns the names of the snapshot zips in dir, oldest first. The
// timestamp in the name makes lexical order the same as time order.
func listSnapshots(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if ok, _ := filepath.Match("hyatlas-*.zip", e.Name()); ok {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	return names, nil
}

// pruneSnapshots deletes all but the newest keep snapshot zips in dir. Only files
// matching hyatlas-*.zip are ever removed, so anything else a user keeps in the
// same folder is left alone.
func pruneSnapshots(dir string, keep int) error {
	if keep < 1 {
		keep = snapshotDefaultKeep
	}
	names, err := listSnapshots(dir)
	if err != nil {
		return err
	}
	if len(names) <= keep {
		return nil
	}
	var firstErr error
	for _, name := range names[:len(names)-keep] {
		if err := os.Remove(filepath.Join(dir, name)); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}
