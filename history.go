package main

import (
	"log"
	"sort"

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

	n := 0
	var firstErr error
	for _, f := range fixes {
		s.rowMu.Lock()
		s.mu.RLock()
		cur, ok := s.index[f.id]
		col := s.cols[memory.Layer(cur.Layer)]
		var old chromem.Document
		var err error
		if ok && isSuperseded(cur) && col != nil {
			old, err = col.GetByID(s.ctx, f.id)
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
		meta["superseded_by"] = f.by
		for k, v := range f.extra {
			meta[k] = v
		}
		if err := col.AddDocument(s.ctx, chromem.Document{
			ID: f.id, Content: old.Content, Embedding: old.Embedding, Metadata: meta,
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
		log.Printf("history: relinked %d superseded rows", n)
	}
	return n, firstErr
}
