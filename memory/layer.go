// Package memory defines the HyAtlas v4 seven-layer memory model.
//
// This is the truthful layer set for the new system — no dead "identity" slot.
// The former L4 IDENTITY (retired) is folded into Profile/Fact; L4 SUMMARY is a
// real, enabled layer (session narrative arc), not dormant baggage.
package memory

// Layer identifies one of the seven memory layers.
type Layer string

const (
	// L1 Profile — stable user attributes / self-model (who the user is).
	L1Profile Layer = "l1_profile"
	// L2 Raw — what was said; the unprocessed trace (source material).
	L2Raw Layer = "l2_raw"
	// L3 Fact — atomic durable facts extracted from conversation (what was learned).
	L3Fact Layer = "l3_fact"
	// L4 Summary — session narrative arc; what happened and why (enabled layer).
	L4Summary Layer = "l4_summary"
	// L5 Knowledge — graph nodes + relations (how things connect).
	L5Knowledge Layer = "l5_knowledge"
	// L6 Schema — recurring patterns / structural templates.
	L6Schema Layer = "l6_schema"
	// L7 Intention — what the user is trying to do (goals, current intent).
	L7Intention Layer = "l7_intention"
)

// All returns the seven layers in pipeline order.
func All() []Layer {
	return []Layer{L1Profile, L2Raw, L3Fact, L4Summary, L5Knowledge, L6Schema, L7Intention}
}

// String implements fmt.Stringer.
func (l Layer) String() string { return string(l) }
