package main

import (
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// quotaIDs returns the IDs of hits in order, so a test can compare a result by eye.
func quotaIDs(hits []SearchHit) []string {
	ids := make([]string, len(hits))
	for i, h := range hits {
		ids[i] = h.ID
	}
	return ids
}

// TestEnsureQuotaMissingSchemaGetsSlot checks that a schema hit missing from the first
// three results takes the last slot in that window.
func TestEnsureQuotaMissingSchemaGetsSlot(t *testing.T) {
	res := []SearchHit{
		{ID: "r1", Layer: memory.L4Summary},
		{ID: "r2", Layer: memory.L4Summary},
		{ID: "r3", Layer: memory.L4Summary},
	}
	extra := []SearchHit{{ID: "s1", Layer: memory.L6Schema}}
	out := ensureQuota(res, extra, memory.L6Schema, 3)
	if got := quotaIDs(out); len(got) != 3 || got[2] != "s1" || got[0] != "r1" || got[1] != "r2" {
		t.Errorf("got %v, want [r1 r2 s1]", got)
	}
	if len(res) != 3 || res[2].ID != "r3" {
		t.Errorf("input slice was modified: %v", quotaIDs(res))
	}
}

// TestEnsureQuotaExistingSchemaLeftAlone checks that when the window already holds a
// schema hit, the results are returned as they were.
func TestEnsureQuotaExistingSchemaLeftAlone(t *testing.T) {
	res := []SearchHit{
		{ID: "r1", Layer: memory.L4Summary},
		{ID: "s0", Layer: memory.L6Schema},
		{ID: "r3", Layer: memory.L4Summary},
	}
	extra := []SearchHit{{ID: "s1", Layer: memory.L6Schema}}
	out := ensureQuota(res, extra, memory.L6Schema, 3)
	if got := quotaIDs(out); len(got) != 3 || got[0] != "r1" || got[1] != "s0" || got[2] != "r3" {
		t.Errorf("got %v, want the original order", got)
	}
}

// TestEnsureQuotaNeverReplacesEntityPage checks that an L8 entity page at the end of the
// window is kept, and that the hit before it is the one replaced.
func TestEnsureQuotaNeverReplacesEntityPage(t *testing.T) {
	res := []SearchHit{
		{ID: "a", Layer: memory.L4Summary},
		{ID: "b", Layer: memory.L4Summary},
		{ID: "page", Layer: memory.L8Entity},
	}
	extra := []SearchHit{{ID: "s1", Layer: memory.L6Schema}}
	out := ensureQuota(res, extra, memory.L6Schema, 3)
	if got := quotaIDs(out); len(got) != 3 || got[2] != "page" || got[1] != "s1" || got[0] != "a" {
		t.Errorf("got %v, want [a s1 page]", got)
	}
}

// TestEnsureQuotaKeepsOnlyL3Fact checks that the only L3 fact in the window is not the
// hit replaced, so the earlier L4 hit gives way instead.
func TestEnsureQuotaKeepsOnlyL3Fact(t *testing.T) {
	res := []SearchHit{
		{ID: "a", Layer: memory.L4Summary},
		{ID: "page", Layer: memory.L8Entity},
		{ID: "fact", Layer: memory.L3Fact},
	}
	extra := []SearchHit{{ID: "s1", Layer: memory.L6Schema}}
	out := ensureQuota(res, extra, memory.L6Schema, 3)
	if got := quotaIDs(out); len(got) != 3 || got[0] != "s1" || got[2] != "fact" {
		t.Errorf("got %v, want [s1 page fact]", got)
	}
}

// TestEnsureQuotaShortResultsAppend checks that a missing layer takes the free slot when
// the results are shorter than the limit.
func TestEnsureQuotaShortResultsAppend(t *testing.T) {
	res := []SearchHit{{ID: "a", Layer: memory.L4Summary}}
	extra := []SearchHit{{ID: "s1", Layer: memory.L6Schema}}
	out := ensureQuota(res, extra, memory.L6Schema, 3)
	if got := quotaIDs(out); len(got) != 2 || got[1] != "s1" {
		t.Errorf("got %v, want [a s1]", got)
	}
}
