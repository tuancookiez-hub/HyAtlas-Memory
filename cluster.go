package main

import (
	"sort"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

const (
	// clusterMinScore is the similarity at which two facts count as the same
	// topic. Below it a neighbour is just the next-nearest fact, and pulling it in
	// would fill a window with noise instead of the facts that bear on the seed.
	clusterMinScore = 0.55

	// clusterMinSize is the smallest topic worth its own window. A "topic" of two
	// or three facts is mostly coincidence, and a model call on it costs as much as
	// one on a full window, so such facts fall back to the time-ordered windows.
	clusterMinSize = 5
)

// clusterWindows cuts one owner's facts, newest first, into the windows the slow
// path sends to the model, grouping facts about the same topic together.
//
// Fixed time-ordered windows put a contradiction at the mercy of the calendar: an
// older "the extraction model is poolside" and a newer "now uses stepfun" sit
// hundreds of facts apart and are almost never in the same call, so the pass that
// exists to reconcile them cannot see both. Two facts that contradict each other
// are about the same thing, so grouping by similarity puts them in one prompt.
//
// The grouping is greedy. Facts are walked newest first; an unassigned fact seeds
// a window and takes its nearest unassigned neighbours of the same owner that
// score at least clusterMinScore, up to c.batch facts. A window smaller than
// clusterMinSize is dissolved into a leftover list, which is then cut into plain
// time-ordered windows, so every fact lands in exactly one window and none
// exceeds c.batch. A search that fails counts as no neighbours; the seed then
// falls through to the leftovers, so an embedding outage degrades to the old
// behaviour rather than to no consolidation.
//
// Windows come back ordered by the position of their seed, so the topic of the
// newest fact is first, with the time-ordered leftovers last. An owner that fits in
// one window is returned whole without a search.
func (c *Consolidator) clusterWindows(facts []DocIndex) [][]DocIndex {
	windows, _ := c.clusterPlan(facts)
	return windows
}

// clusterPlan is clusterWindows plus, for each window, whether it is a topic
// cluster (true) or a time-ordered leftover window (false), for the pass log.
func (c *Consolidator) clusterPlan(facts []DocIndex) ([][]DocIndex, []bool) {
	if len(facts) == 0 {
		return nil, nil
	}
	if len(facts) <= c.batch {
		return [][]DocIndex{facts}, []bool{false}
	}

	// Only this owner's facts may join a window, whatever the search returns: an
	// ownerless fact's search is unfiltered, and a hit outside this set is skipped.
	pos := make(map[string]int, len(facts))
	for i, f := range facts {
		pos[f.ID] = i
	}
	assigned := make([]bool, len(facts))
	leftover := make([]bool, len(facts))

	var windows [][]DocIndex
	var topic []bool
	for i, seed := range facts {
		if assigned[i] {
			continue
		}
		assigned[i] = true
		members := []int{i}
		hits, err := c.store.search(seed.Content, c.batch*2, memory.L3Fact, seed.UserID, seed.AgentID)
		if err == nil {
			for _, h := range hits {
				if len(members) >= c.batch {
					break
				}
				if h.Score < clusterMinScore {
					// Hits are ranked, so nothing after this one qualifies either.
					break
				}
				j, ok := pos[h.ID]
				if !ok || assigned[j] {
					continue
				}
				assigned[j] = true
				members = append(members, j)
			}
		}
		if len(members) < clusterMinSize {
			for _, j := range members {
				leftover[j] = true
			}
			continue
		}
		// Back in newest-first order, the way the model is shown any window.
		sort.Ints(members)
		w := make([]DocIndex, len(members))
		for k, j := range members {
			w[k] = facts[j]
		}
		windows = append(windows, w)
		topic = append(topic, true)
	}

	var rest []DocIndex
	for i, f := range facts {
		if leftover[i] {
			rest = append(rest, f)
		}
	}
	for _, w := range chunkTimeOrdered(rest, c.batch) {
		windows = append(windows, w)
		topic = append(topic, false)
	}
	return windows, topic
}

// chunkTimeOrdered cuts facts, already newest first, into consecutive windows of
// batch, with the last one pulled back to end at the final fact exactly as windowAt
// does: every window is full when there are at least batch facts, and the last never
// holds a single fact that has nothing to be reconciled against. The cost is that
// the last two leftover windows may share facts, which is harmless since each
// window is reconciled on its own.
func chunkTimeOrdered(facts []DocIndex, batch int) [][]DocIndex {
	var out [][]DocIndex
	for lo := 0; lo < len(facts); lo += batch {
		hi := lo + batch
		if hi > len(facts) {
			if lo > 0 {
				lo = len(facts) - batch
			}
			hi = len(facts)
		}
		out = append(out, facts[lo:hi])
	}
	return out
}
