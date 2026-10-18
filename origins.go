package main

// metaWriteOrigin is the metadata key a writer sets to say where a raw row came from.
const metaWriteOrigin = "write_origin"

// The write origins the server treats specially. Any other value, or none, is an
// ordinary write.
const (
	// originMemoryTool marks a row Hermes's built-in memory tool wrote, mirrored by
	// the plugin. Only these rows can be forgotten by /api/v1/forget.
	originMemoryTool = "memory_tool"
	// originDoctor marks the row `hermes hyatlas doctor` writes for its round-trip
	// check. It is stored but never extracted, so the check makes no model call.
	originDoctor = "doctor"
)

// originPolicy is what the server does with a raw row of one write origin.
type originPolicy struct {
	// extracts is whether the row goes through extraction (in a mode that extracts).
	extracts bool
	// forgettable is whether /api/v1/forget may retire the row.
	forgettable bool
}

// originPolicies holds the origins that differ from an ordinary write. It is the one
// place to look up how a write origin is handled.
var originPolicies = map[string]originPolicy{
	originMemoryTool: {extracts: true, forgettable: true},
	originDoctor:     {extracts: false, forgettable: false},
}

// policyFor returns the policy of a write origin. An unknown or empty origin is an
// ordinary write: extracted, and not forgettable.
func policyFor(origin string) originPolicy {
	if p, ok := originPolicies[origin]; ok {
		return p
	}
	return originPolicy{extracts: true}
}
