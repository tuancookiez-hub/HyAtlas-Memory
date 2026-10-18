package main

import (
	"log"
	"os"
	"strconv"
	"strings"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// defaultMMRLambda is the relevance weight of the MMR pass when HYATLAS_MMR_LAMBDA is
// unset: mostly relevance, with a clear push away from repeats.
const defaultMMRLambda = 0.75

// mmrMinWindow is the smallest window MMR works on; with fewer hits there is nothing
// to diversify.
const mmrMinWindow = 3

// parseMMRLambda reads HYATLAS_MMR_LAMBDA, a float in (0, 1]. Unset or blank gives
// defaultMMRLambda; a value outside the range or not a number gives the default and
// logs one line. 1 means MMR is off.
func parseMMRLambda() float64 {
	const key = "HYATLAS_MMR_LAMBDA"
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return defaultMMRLambda
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil || f <= 0 || f > 1 {
		log.Printf("%s=%q is not a number in (0, 1]; using %v", key, v, defaultMMRLambda)
		return defaultMMRLambda
	}
	return f
}

// mmrEligible reports whether a hit of layer l takes part in MMR. Profile, schema,
// intention and entity-page hits are placed by their own rules, so they keep their slot.
func mmrEligible(l memory.Layer) bool {
	switch l {
	case memory.L2Raw, memory.L3Fact, memory.L4Summary, memory.L5Knowledge:
		return true
	}
	return false
}

// mmrRerank applies Maximal Marginal Relevance to the first 2*limit hits so the
// visible results cover different aspects. The relevance of the hit at window position
// i is its position weight 1/(rrfK+i+1), scaled to [0, 1] over the window, so the
// ranking the earlier passes made stays the relevance signal. Greedily, the next hit is
// the one with the largest lambda*relevance - (1-lambda)*redundancy, where redundancy
// is its highest cosine to a hit already chosen. Hits without a stored vector, and
// hits of layers that other rules place, keep their slot. Hits after the window keep
// their order. A window under three hits, or a lambda of 1 or more, returns hits
// unchanged. The input slice is not changed.
func mmrRerank(hits []SearchHit, vecs map[string][]float32, limit int, lambda float64) []SearchHit {
	window := 2 * limit
	if window > len(hits) {
		window = len(hits)
	}
	if window < mmrMinWindow || lambda >= 1 {
		return hits
	}
	wHi := 1.0 / float64(rrfK+1)
	wLo := 1.0 / float64(rrfK+window)
	rel := func(i int) float64 {
		if wHi == wLo {
			return 1
		}
		return (1.0/float64(rrfK+i+1) - wLo) / (wHi - wLo)
	}

	// slots are the window positions MMR may refill, in ascending order.
	var slots []int
	for i := 0; i < window; i++ {
		if mmrEligible(hits[i].Layer) && len(vecs[hits[i].ID]) > 0 {
			slots = append(slots, i)
		}
	}
	if len(slots) < mmrMinWindow {
		return hits
	}

	out := make([]SearchHit, len(hits))
	copy(out, hits)
	remaining := append([]int(nil), slots...)
	var chosen [][]float32
	for _, slot := range slots {
		best, bestScore := -1, 0.0
		for ri, pos := range remaining {
			red := 0.0
			v := vecs[hits[pos].ID]
			for _, c := range chosen {
				if s := cosineSim(c, v); s > red {
					red = s
				}
			}
			score := lambda*rel(pos) - (1-lambda)*red
			// Strictly greater keeps the earlier hit on a tie, so the result is stable.
			if best < 0 || score > bestScore {
				best, bestScore = ri, score
			}
		}
		pick := remaining[best]
		remaining = append(remaining[:best], remaining[best+1:]...)
		chosen = append(chosen, vecs[hits[pick].ID])
		out[slot] = hits[pick]
	}
	return out
}
