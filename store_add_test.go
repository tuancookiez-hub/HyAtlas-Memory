package main

import (
	"context"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// blockingEmbedder embeds like LocalEmbedder, except that text containing block
// does not return until release is closed. entered closes when such a call starts.
type blockingEmbedder struct {
	inner   Embedder
	block   string
	entered chan struct{}
	release chan struct{}
	once    sync.Once
}

func (b *blockingEmbedder) Embed(ctx context.Context, text string) ([]float32, error) {
	if strings.Contains(text, b.block) {
		b.once.Do(func() { close(b.entered) })
		select {
		case <-b.release:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return b.inner.Embed(ctx, text)
}

// An Add whose embedding is slow must not hold the row lock. Otherwise every other
// Add and Supersede waits behind one HTTP call to the embedder.
func TestSlowEmbedDoesNotBlockOtherRows(t *testing.T) {
	dir := t.TempDir()
	em := &blockingEmbedder{inner: NewLocalEmbedder(384), block: "slow",
		entered: make(chan struct{}), release: make(chan struct{})}
	st, err := NewMemoryStore(context.Background(), dir, em, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(st.Close)
	t.Cleanup(func() {
		select {
		case <-em.release:
		default:
			close(em.release)
		}
	})
	addFact(t, st, memory.L3Fact, "Y", "content of Y", nil)

	slowDone := make(chan error, 1)
	go func() {
		slowDone <- st.Add(memory.L3Fact, "S", "slow content", map[string]string{"user_id": "u", "agent_id": "a"})
	}()
	select {
	case <-em.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("slow Add never reached the embedder")
	}

	supDone := make(chan int, 1)
	go func() {
		n, _ := st.Supersede([]string{"Y"}, "by")
		supDone <- n
	}()
	select {
	case n := <-supDone:
		if n != 1 {
			t.Errorf("Supersede marked %d rows, want 1", n)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Supersede of another row stalled behind a slow embed")
	}

	addDone := make(chan error, 1)
	go func() {
		addDone <- st.Add(memory.L3Fact, "F", "fast content", map[string]string{"user_id": "u", "agent_id": "a"})
	}()
	select {
	case err := <-addDone:
		if err != nil {
			t.Fatalf("Add of another row: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Add of another row stalled behind a slow embed")
	}

	close(em.release)
	select {
	case err := <-slowDone:
		if err != nil {
			t.Fatalf("slow Add: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("slow Add did not finish after its embed was released")
	}
	if got := st.GetMany([]string{"S", "F"}); len(got) != 2 {
		t.Errorf("rows written = %d, want 2", len(got))
	}
}

// An embedding failure is returned and nothing is written.
func TestAddEmbedFailureWritesNothing(t *testing.T) {
	dir := t.TempDir()
	em := &flakyEmbedder{inner: NewLocalEmbedder(384)}
	em.fail.Store(true)
	st, err := NewMemoryStore(context.Background(), dir, em, filepath.Join(dir, "graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(st.Close)

	if err := st.Add(memory.L3Fact, "E", "will not embed", nil); err == nil {
		t.Fatal("Add succeeded although the embedder failed")
	}
	if got := st.GetMany([]string{"E"}); len(got) != 0 {
		t.Errorf("row written despite embed failure: %+v", got)
	}
}
