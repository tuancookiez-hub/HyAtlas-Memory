package main

import (
	"regexp"
	"strings"
)

// versionToken matches a word that is only a version label: "v4", "v4.6", "4.6" or
// "4.6.0". Four dotted parts ("127.0.0.1") and plain numbers ("19528") are not
// versions, they are addresses and ports, and are kept.
var versionToken = regexp.MustCompile(`^(v\d+(\.\d+){0,2}|\d+\.\d+(\.\d+)?)$`)

// nameTokens splits an entity name into lower-case words with surrounding
// punctuation trimmed, dropping version labels when other words remain, so
// "HyAtlas v4" and "hyatlas" name the same thing while "4.6.0" alone stays a name.
func nameTokens(s string) []string {
	var words []string
	for _, w := range strings.Fields(strings.ToLower(s)) {
		w = strings.Trim(w, ".,;:!?\"'()[]{}")
		if w != "" {
			words = append(words, w)
		}
	}
	var kept []string
	for _, w := range words {
		if !versionToken.MatchString(w) {
			kept = append(kept, w)
		}
	}
	if len(kept) == 0 {
		return words
	}
	return kept
}

// normalizeName is the canonical form of an entity name: its name tokens joined
// by single spaces. Two names with the same normal form are the same entity.
func normalizeName(s string) string {
	return strings.Join(nameTokens(s), " ")
}

// sameEntity reports whether two entity names plausibly refer to one thing: equal
// once normalized, or one's words a subset of the other's ("hyatlas server" and
// "HyAtlas v4"). The subset rule is loose on purpose and only safe alongside other
// evidence, such as an identical other endpoint and a high text similarity; page
// lookup uses normalizeName equality instead.
func sameEntity(a, b string) bool {
	ta, tb := nameTokens(a), nameTokens(b)
	if len(ta) == 0 || len(tb) == 0 {
		return false
	}
	if len(ta) > len(tb) {
		ta, tb = tb, ta
	}
	have := map[string]bool{}
	for _, t := range tb {
		have[t] = true
	}
	for _, t := range ta {
		if !have[t] {
			return false
		}
	}
	return true
}
