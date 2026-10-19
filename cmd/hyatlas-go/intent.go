package main

import (
	"strings"
	"unicode"
)

// queryIntent is the kind of question a search query asks. The intent sets how much
// the vector ranking and the keyword ranking each count in the fusion.
type queryIntent int

const (
	// intentFactual is the default: a question about a specific fact.
	intentFactual queryIntent = iota
	// intentNavigational is a short query made of an identifier, such as a code or a number.
	intentNavigational
	// intentConceptual is a question about reasons, habits or patterns.
	intentConceptual
)

// conceptualWords are the whole words that mark a query as conceptual. Matching whole
// words stops "preferred" or "patterns" from counting as "prefer" or "pattern".
var conceptualWords = map[string]bool{
	"how":     true,
	"why":     true,
	"usually": true,
	"pattern": true,
	"prefer":  true,
	"explain": true,
}

// classifyQuery returns the intent of a query. An identifier-only query is navigational,
// because the exact term is what matters. A query that asks how, why, or about a habit
// or pattern is conceptual. Every other query is factual. The checks run in that order,
// so a short identifier query wins over a conceptual word.
func classifyQuery(q string) queryIntent {
	if len(identifierTerms(q)) > 0 {
		return intentNavigational
	}
	words := strings.FieldsFunc(strings.ToLower(q), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	})
	for i, w := range words {
		if conceptualWords[w] {
			return intentConceptual
		}
		if w == "tend" && i+1 < len(words) && words[i+1] == "to" {
			return intentConceptual
		}
	}
	return intentFactual
}

// intentWeights returns the weights of the vector ranking and the keyword ranking for an
// intent. A navigational query leans on keywords, a conceptual one leans on meaning, and
// a factual query weighs both equally. The orchestrator passes these into the RRF fusion.
func intentWeights(i queryIntent) (vector, keyword float64) {
	switch i {
	case intentNavigational:
		return 0.6, 1.4
	case intentConceptual:
		// Mild: at 1.3/0.7 a "why" question lost a directly relevant fact that
		// matched by keyword to a vaguer relation (4.9.0 check on live data).
		return 1.15, 0.85
	default:
		return 1, 1
	}
}
