package main

import (
	"log"
	"sort"
	"strconv"
	"time"

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
		hits, err := c.neighbours(seed, c.batch*2)
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
// batch, with the last one pulled back to end at the final fact:
// every window is full when there are at least batch facts, and the last never
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

// neighbours returns the facts nearest to f among its owner's, where the owner is
// every user ID of the same person (HYATLAS_USER_ALIASES): consolidation groups
// those IDs as one owner, so a cluster may span them.
func (c *Consolidator) neighbours(f DocIndex, k int) ([]SearchHit, error) {
	if group := c.groupOf(f.UserID); len(group) > 1 {
		return c.store.SearchOwners(f.Content, k, memory.L3Fact, group, f.AgentID)
	}
	return c.store.search(f.Content, k, memory.L3Fact, f.UserID, f.AgentID)
}

// StableClusters returns each owner's stored clusters as their live facts (newest
// first, clusters of fewer than two facts left out), synced with the store the way
// a pass syncs them, so other passes group facts exactly as consolidation does. It
// waits for a running pass, and saves the synced layout.
func (c *Consolidator) StableClusters() ([]scopeKey, map[scopeKey][][]DocIndex) {
	c.gate.Lock()
	defer c.gate.Unlock()
	byOwner, owners := c.groupOwners(c.listAllLive(memory.L3Fact))
	out := map[scopeKey][][]DocIndex{}
	var keys []scopeKey
	for _, k := range owners {
		fs := byOwner[k]
		if len(fs) < 2 {
			continue
		}
		sortNewestFirst(fs)
		oc := c.syncClusters(k, fs)
		for _, cs := range oc.Clusters {
			if members := c.liveMembers(cs.FactIDs); len(members) >= 2 {
				out[k] = append(out[k], members)
			}
		}
		if len(out[k]) > 0 {
			keys = append(keys, k)
		}
	}
	c.stateMu.Lock()
	if err := c.saveState(); err != nil {
		log.Printf("consolidate: save state: %v", err)
	}
	c.stateMu.Unlock()
	return keys, out
}

// clusterSearchK is how many neighbours a new fact looks at when it is assigned to
// a cluster. Only the first one in a cluster decides, so this just has to reach past
// other facts that are themselves still unassigned.
const clusterSearchK = 10

// syncClusters brings an owner's stored clusters in line with its live facts
// (newest first) and returns them.
//
// Clusters are stable on purpose. Cutting them again each pass lets one new fact
// reshuffle many windows, which dirties them all; here a cluster only changes when
// one of its facts dies or a new fact is assigned to it, and only a changed cluster
// is due (see clusterState.due).
//
//   - An owner with no stored clusters, or whose clusters are older than
//     HYATLAS_CONSOLIDATE_RECLUSTER, is cut once with clusterPlan. A re-cut cluster
//     whose members are unchanged keeps its progress.
//   - Otherwise facts that are no longer live leave their clusters, and empty
//     clusters go. Each live fact in no cluster joins the cluster of its nearest
//     neighbour when that scores at least clusterMinScore and the cluster has room.
//     If not, it joins the newest time cluster with room, or starts a new one. A new
//     time cluster is seeded with the fact's nearest neighbour as well, so it is
//     reconciled against something rather than sitting alone.
func (c *Consolidator) syncClusters(k scopeKey, facts []DocIndex) *ownerClusters {
	key := k.String()
	oc := c.clusters[key]
	if oc != nil && len(oc.Clusters) > 0 && c.recluster > 0 {
		if built, err := time.Parse(time.RFC3339, oc.Built); err != nil || time.Since(built) >= c.recluster {
			oc = c.buildClusters(facts, oc)
			c.clusters[key] = oc
			return oc
		}
	}
	if oc == nil || len(oc.Clusters) == 0 {
		oc = c.buildClusters(facts, nil)
		c.clusters[key] = oc
		return oc
	}

	live := idSetOf(facts)
	where := map[string]*clusterState{}
	kept := make([]*clusterState, 0, len(oc.Clusters))
	for _, cs := range oc.Clusters {
		ids := cs.FactIDs[:0:0]
		for _, id := range cs.FactIDs {
			if live[id] {
				ids = append(ids, id)
			}
		}
		if len(ids) == 0 {
			continue
		}
		cs.FactIDs = ids
		for _, id := range ids {
			where[id] = cs
		}
		kept = append(kept, cs)
	}
	oc.Clusters = kept

	for _, f := range facts {
		if where[f.ID] == nil {
			c.assignFact(oc, f, where)
		}
	}
	// A failure count belongs to one membership; once the members change it starts over.
	for _, cs := range oc.Clusters {
		if cs.FailFP != "" && cs.FailFP != cs.fp() {
			cs.Fails, cs.FailFP = 0, ""
		}
	}
	return oc
}

// buildClusters cuts facts into clusters with clusterPlan. A cluster whose members
// equal one of old's keeps that cluster's progress, so a re-cut that changes
// nothing costs nothing.
func (c *Consolidator) buildClusters(facts []DocIndex, old *ownerClusters) *ownerClusters {
	prior := map[string]*clusterState{}
	if old != nil {
		for _, cs := range old.Clusters {
			prior[cs.fp()] = cs
		}
	}
	windows, kinds := c.clusterPlan(facts)
	oc := &ownerClusters{Built: time.Now().UTC().Format(time.RFC3339)}
	for i, w := range windows {
		cs := &clusterState{ID: oc.newClusterID(), Topic: kinds[i], FactIDs: idsOf(dedupDocs(w))}
		if p := prior[cs.fp()]; p != nil {
			cs.DoneFP, cs.Fails, cs.FailFP = p.DoneFP, p.Fails, p.FailFP
		}
		oc.Clusters = append(oc.Clusters, cs)
	}
	return oc
}

// newClusterID numbers a cluster within its owner.
func (oc *ownerClusters) newClusterID() string {
	oc.NextID++
	return "c" + strconv.Itoa(oc.NextID)
}

// dedupDocs drops repeated facts, keeping the first.
func dedupDocs(fs []DocIndex) []DocIndex {
	seen := make(map[string]bool, len(fs))
	out := make([]DocIndex, 0, len(fs))
	for _, f := range fs {
		if !seen[f.ID] {
			seen[f.ID] = true
			out = append(out, f)
		}
	}
	return out
}

// assignFact places one fact that is in no cluster; where maps live fact IDs to
// their clusters and is updated. See syncClusters for the rules.
func (c *Consolidator) assignFact(oc *ownerClusters, f DocIndex, where map[string]*clusterState) {
	room := func(cs *clusterState) bool { return len(cs.FactIDs) < c.batch }
	var target *clusterState
	nearest := ""
	if len(oc.Clusters) == 1 && room(oc.Clusters[0]) {
		// The only cluster: nothing to choose between, so no search.
		target = oc.Clusters[0]
	} else if hits, err := c.neighbours(f, clusterSearchK); err == nil {
		for _, h := range hits {
			cs := where[h.ID]
			if h.ID == f.ID || cs == nil {
				continue
			}
			// The nearest assigned fact decides; if its cluster is full or too far
			// away, the fact goes to the time clusters instead of its second choice.
			nearest = h.ID
			if h.Score >= clusterMinScore && room(cs) {
				target = cs
			}
			break
		}
	}
	if target == nil {
		for i := len(oc.Clusters) - 1; i >= 0; i-- {
			if cs := oc.Clusters[i]; !cs.Topic && room(cs) {
				target = cs
				break
			}
		}
	}
	if target == nil {
		target = &clusterState{ID: oc.newClusterID()}
		oc.Clusters = append(oc.Clusters, target)
		if nearest != "" {
			target.FactIDs = append(target.FactIDs, nearest)
		}
	}
	target.FactIDs = append(target.FactIDs, f.ID)
	where[f.ID] = target
}
