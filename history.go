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
	// reasonNotRecorded is the drop_reason of a row dropped by a version that did
	// not record why (before 4.6.0) or retracted without one.
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
// while a pass is in flight. It takes rowMu per row the way Supersede does, so a
// same-id Add is never overwritten.
func (s *MemoryStore) relinkHistory(successors map[string]string) (int, error) {
	type fix struct {
		id    string
		by    string
		extra map[string]string
	}
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
	var fixes []fix
	for id := range s.superseded {
		d := s.index[id]
		by := d.Meta["superseded_by"]
		switch {
		case by != "":
			if _, ok := s.index[by]; ok {
				continue
			}
			if next := resolve(successors[by]); next != "" {
				fixes = append(fixes, fix{id, next, nil})
			} else {
				fixes = append(fixes, fix{id, "", map[string]string{"drop_reason": reasonReplacementDeleted}})
			}
		case d.Meta["drop_reason"] == "":
			fixes = append(fixes, fix{id, "", map[string]string{"drop_reason": reasonNotRecorded}})
		}
	}
	s.mu.RUnlock()
	sort.Slice(fixes, func(i, j int) bool { return fixes[i].id < fixes[j].id })

	edits := make([]historyEdit, len(fixes))
	for i, f := range fixes {
		f := f
		edits[i] = historyEdit{id: f.id, edit: func(meta map[string]string) {
			meta["superseded_by"] = f.by
			for k, v := range f.extra {
				meta[k] = v
			}
		}}
	}
	n, err := s.rewriteSuperseded(edits)
	if n > 0 {
		log.Printf("history: relinked %d superseded rows", n)
	}
	return n, err
}

// historyEdit changes the metadata of one superseded row.
type historyEdit struct {
	id   string
	edit func(meta map[string]string)
}

// rewriteSuperseded applies each edit to its row, if the row is still superseded,
// and writes the row back to chromem and the index. Content and vector are kept. It
// takes rowMu per row the way Supersede does, so a same-id Add is never
// overwritten, and writes the index file once. It returns how many rows changed.
func (s *MemoryStore) rewriteSuperseded(edits []historyEdit) (int, error) {
	n := 0
	var firstErr error
	for _, e := range edits {
		s.rowMu.Lock()
		s.mu.RLock()
		cur, ok := s.index[e.id]
		col := s.cols[memory.Layer(cur.Layer)]
		var old chromem.Document
		var err error
		if ok && isSuperseded(cur) && col != nil {
			old, err = col.GetByID(s.ctx, e.id)
		}
		s.mu.RUnlock()
		if !ok || !isSuperseded(cur) || col == nil {
			s.rowMu.Unlock()
			continue
		}
		if err != nil {
			s.rowMu.Unlock()
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		meta := make(map[string]string, len(cur.Meta)+1)
		for k, v := range cur.Meta {
			meta[k] = v
		}
		e.edit(meta)
		if err := col.AddDocument(s.ctx, chromem.Document{
			ID: e.id, Content: old.Content, Embedding: old.Embedding, Metadata: meta,
		}); err != nil {
			s.rowMu.Unlock()
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		s.mu.Lock()
		cur.Meta = meta
		s.putLocked(cur)
		s.mu.Unlock()
		s.rowMu.Unlock()
		n++
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

// Restore makes superseded rows live again: it removes invalid_at, superseded_by
// and drop_reason and records restored_at. It undoes a supersede the model got
// wrong, such as a fact merged into unrelated text. A row that is live or missing
// is skipped. It returns how many rows were restored.
func (s *MemoryStore) Restore(ids []string) (int, error) {
	now := time.Now().UTC().Format(time.RFC3339)
	edits := make([]historyEdit, 0, len(ids))
	for _, id := range ids {
		edits = append(edits, historyEdit{id: id, edit: func(meta map[string]string) {
			delete(meta, "invalid_at")
			delete(meta, "superseded_by")
			delete(meta, "drop_reason")
			meta["restored_at"] = now
		}})
	}
	return s.rewriteSuperseded(edits)
}
