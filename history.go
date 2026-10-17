package main

import (
	"log"
	"sort"
	"time"

	chromem "github.com/philippgille/chromem-go"
	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// A superseded row's superseded_by names the row that replaced it, so a fact's
// history can be followed forward. These helpers keep that link honest when the
// replacement is deleted, and give every dropped row a reason, including rows
// dropped before reasons were recorded.

const (
	// reasonReplacementDeleted is the drop_reason of a row whose replacement was
	// deleted and had no live successor to point at instead.
	reasonReplacementDeleted = "the fact that replaced it was later deleted"
	// reasonNotRecorded is the drop_reason of a row dropped without a reason: by a
	// caller that gave none, or by a version before 4.6.0 that did not record one.
	reasonNotRecorded = "dropped without a recorded reason"
	// reasonMergeRetracted is the drop_reason of a merged fact taken back because
	// none of the facts it replaced could be marked.
	reasonMergeRetracted = "merge retracted: none of the facts it replaced could be marked"
)

// relinkHistory repairs the history links of superseded rows. A row whose
// superseded_by names a row that no longer exists is pointed at that row's own
// successor, taken from successors (deleted id -> what it was superseded by), and
// followed until it reaches a row that exists. When the chain ends without one, the
// row becomes a drop with reasonReplacementDeleted. A dropped row without a reason
// gets reasonNotRecorded. It returns how many rows were rewritten.
//
// It rewrites only rows that are already superseded, which a Supersede pass never
// touches, so it does not take supMu: Delete runs it and may itself be called
// while a pass is in flight.
func (s *MemoryStore) relinkHistory(successors map[string]string) (int, error) {
	s.mu.RLock()
	resolve := func(by string) string {
		seen := map[string]bool{}
		for by != "" && !seen[by] {
			if _, ok := s.index[by]; ok {
				return by
			}
			seen[by] = true
			by = successors[by]
		}
		return ""
	}
	var edits []historyEdit
	for id := range s.superseded {
		d := s.index[id]
		by := d.Meta["superseded_by"]
		switch {
		case by != "":
			if _, ok := s.index[by]; ok {
				continue
			}
			if next := resolve(successors[by]); next != "" {
				edits = append(edits, historyEdit{id: id, set: map[string]string{"superseded_by": next}})
			} else {
				edits = append(edits, historyEdit{id: id, set: map[string]string{
					"superseded_by": "", "drop_reason": reasonReplacementDeleted}})
			}
		case d.Meta["drop_reason"] == "":
			edits = append(edits, historyEdit{id: id, set: map[string]string{"drop_reason": reasonNotRecorded}})
		}
	}
	s.mu.RUnlock()
	sort.Slice(edits, func(i, j int) bool { return edits[i].id < edits[j].id })

	n, err := s.rewriteSuperseded(edits)
	if n > 0 {
		log.Printf("history: relinked %d superseded rows", n)
	}
	return n, err
}

// historyEdit changes the metadata of one superseded row: the keys in del are
// removed, then the keys in set are written.
type historyEdit struct {
	id  string
	set map[string]string
	del []string
}

// rewriteSuperseded applies each edit to its row while the row is still
// superseded, keeping content and vector, and writes the index file once. It
// returns how many rows changed and the first error.
func (s *MemoryStore) rewriteSuperseded(edits []historyEdit) (int, error) {
	n := 0
	var firstErr error
	for _, e := range edits {
		changed, err := s.rewriteOneSuperseded(e)
		if changed {
			n++
		}
		if err != nil && firstErr == nil {
			firstErr = err
		}
	}
	if n > 0 {
		s.mu.Lock()
		if err := s.persistIndexLocked(); err != nil && firstErr == nil {
			firstErr = err
		}
		s.mu.Unlock()
	}
	return n, firstErr
}

// rewriteOneSuperseded applies one edit. It holds rowMu the way Supersede does,
// so a same-id Add is never overwritten. A row that is missing or live is skipped.
func (s *MemoryStore) rewriteOneSuperseded(e historyEdit) (bool, error) {
	live, changed, err := s.rewriteRow(e)
	// The clause hook runs after rowMu is released, because it embeds and the
	// embedder must not run under rowMu. It fires only when the edit made the row live.
	if changed && live != nil {
		s.indexClauses(*live)
	}
	return changed, err
}

// rewriteRow applies one edit under rowMu and returns the row's new index record when
// the edit left it live (for a restore), or nil when it is still superseded. The
// bool reports whether the row was changed.
func (s *MemoryStore) rewriteRow(e historyEdit) (*DocIndex, bool, error) {
	s.rowMu.Lock()
	defer s.rowMu.Unlock()
	s.mu.RLock()
	cur, ok := s.index[e.id]
	col := s.cols[memory.Layer(cur.Layer)]
	if !ok || !isSuperseded(cur) || col == nil {
		s.mu.RUnlock()
		return nil, false, nil
	}
	old, err := col.GetByID(s.ctx, e.id)
	s.mu.RUnlock()
	if err != nil {
		return nil, false, err
	}
	meta := make(map[string]string, len(cur.Meta)+len(e.set))
	for k, v := range cur.Meta {
		meta[k] = v
	}
	for _, k := range e.del {
		delete(meta, k)
	}
	for k, v := range e.set {
		meta[k] = v
	}
	meta["layer"] = cur.Layer
	if err := col.AddDocument(s.ctx, chromem.Document{
		ID: e.id, Content: old.Content, Embedding: old.Embedding, Metadata: meta,
	}); err != nil {
		return nil, false, err
	}
	s.mu.Lock()
	cur.Meta = meta
	s.putLocked(cur)
	s.mu.Unlock()
	if isSuperseded(cur) {
		return nil, true, nil
	}
	return &cur, true, nil
}

// Restore makes superseded rows live again: it removes invalid_at, superseded_by
// and drop_reason and records restored_at. It undoes a supersede the model got
// wrong, such as a fact merged into unrelated text. A row that is live or missing
// is skipped. It returns how many rows were restored.
func (s *MemoryStore) Restore(ids []string) (int, error) {
	set := map[string]string{"restored_at": time.Now().UTC().Format(time.RFC3339)}
	del := []string{"invalid_at", "superseded_by", "drop_reason"}
	edits := make([]historyEdit, len(ids))
	for i, id := range ids {
		edits[i] = historyEdit{id: id, set: set, del: del}
	}
	return s.rewriteSuperseded(edits)
}
