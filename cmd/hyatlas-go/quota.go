package main

import "github.com/tuancookiez-hub/hyatlas-v4/memory"

// hasHitID reports whether a hit with the given ID is already in hits.
func hasHitID(hits []SearchHit, id string) bool {
	for _, h := range hits {
		if h.ID == id {
			return true
		}
	}
	return false
}

// ensureQuota gives a layer one place in the first limit results when the results
// have none of it. The best extra hit of that layer then replaces a hit near the end
// of the first limit results, so a quiet layer such as the schema is not crowded out
// by a stronger one. The replaced hit is never an L8 entity page, and never the only
// L3 fact in the window, because those are the hits the window is most likely to need.
// When the results are shorter than limit there is a free slot, so the extra is
// appended instead. The returned slice is new, and the inputs are not modified.
func ensureQuota(res []SearchHit, extra []SearchHit, layer memory.Layer, limit int) []SearchHit {
	out := make([]SearchHit, len(res))
	copy(out, res)
	if limit <= 0 {
		return out
	}
	head := limit
	if head > len(out) {
		head = len(out)
	}
	for _, h := range out[:head] {
		if h.Layer == layer {
			return out
		}
	}
	var pick *SearchHit
	for i := range extra {
		if extra[i].Layer == layer && !hasHitID(out, extra[i].ID) {
			pick = &extra[i]
			break
		}
	}
	if pick == nil {
		return out
	}
	if head < limit {
		return append(out, *pick)
	}
	l3 := 0
	for _, h := range out[:head] {
		if h.Layer == memory.L3Fact {
			l3++
		}
	}
	for i := head - 1; i >= 0; i-- {
		h := out[i]
		if h.Layer == memory.L8Entity {
			continue
		}
		if h.Layer == memory.L3Fact && l3 == 1 {
			continue
		}
		out[i] = *pick
		return out
	}
	return out
}
