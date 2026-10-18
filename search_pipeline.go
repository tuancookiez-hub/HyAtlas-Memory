package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// searchRequest is the decoded body of POST /api/v1/search.
type searchRequest struct {
	Query    string   `json:"query"`
	Limit    int      `json:"limit"`
	Layer    string   `json:"layer"`     // optional: filter to one memory layer
	UserIDs  []string `json:"user_ids"`  // optional: restrict to these users
	AgentIDs []string `json:"agent_ids"` // optional: restrict to these agents
	UserID   string   `json:"user_id"`   // what the Hermes plugin sends
	AgentID  string   `json:"agent_id"`  // what the Hermes plugin sends
	MinScore *float64 `json:"min_score"` // optional: overrides HYATLAS_MIN_SCORE
	Reader   string   `json:"reader"`    // optional: see parseReader; empty is hybrid
	// SessionID is the caller's current session; its intentions rank above others.
	SessionID string `json:"session_id"`
}

// searchError is a failed search step with the HTTP status to answer with.
type searchError struct {
	code int
	msg  string
}

// Error returns the message shown to the caller.
func (e *searchError) Error() string { return e.msg }

// searchRun is the state of one search as it moves through the pipeline steps: the
// request and what was derived from it, the query's embedding (made once and shared
// by every step), and the hits so far.
type searchRun struct {
	srv      *Server
	req      searchRequest
	users    []string
	agentID  string
	limit    int
	minScore float64
	reader   searchReader
	// factsWanted is true when the search covers L3 facts, the only layer that has
	// clauses and event dates.
	factsWanted bool
	qv          []float32
	now         time.Time
	// vecAll is every vector candidate before the floor, for graph-assisted recall.
	vecAll []SearchHit
	hits   []SearchHit
}

// searchHitJSON is one hit as the search response shows it.
type searchHitJSON struct {
	MemoryID   string  `json:"memory_id"`
	Content    string  `json:"content"`
	Score      float64 `json:"score"`
	Layer      string  `json:"layer"`
	GmtCreated int64   `json:"gmt_created"`
	UserID     string  `json:"user_id,omitempty"`
	AgentID    string  `json:"agent_id,omitempty"`
	// EventDate is when the fact happened or is due, when extraction found one.
	EventDate string `json:"event_date,omitempty"`
	// History holds the older versions this hit replaced, newest first.
	History []HistoryEntry `json:"history,omitempty"`
}

// searchChannels lists the response channels in plugin order: now -> pages ->
// profile -> proactive -> experiences -> normal ("now" is added by handleSearch).
var searchChannels = []string{"pages", "profile", "proactive", "experiences", "normal"}

// handleSearch is POST /api/v1/search: decode the request, run the pipeline, write
// the channels. The steps are the methods of searchRun.
func (s *Server) handleSearch(w http.ResponseWriter, r *http.Request) {
	var body searchRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		jsonResponse(w, 400, map[string]any{"error": "bad body"})
		return
	}
	run, err := s.parseSearchRequest(body)
	if err == nil {
		err = run.retrieve()
	}
	if err != nil {
		code := 500
		if se, ok := err.(*searchError); ok {
			code = se.code
		}
		jsonResponse(w, code, map[string]any{"error": err.Error()})
		return
	}
	run.boostByContext()
	run.rankSignals()
	run.admitLinkedFacts()
	run.applyLayerQuotas()
	jsonResponse(w, 200, run.render())
}

// parseSearchRequest validates the request and derives the owners, agent, limit,
// score floor and reader from it.
func (s *Server) parseSearchRequest(body searchRequest) (*searchRun, error) {
	if body.Query == "" {
		return nil, &searchError{400, "query required"}
	}
	var users []string
	seenUser := map[string]bool{}
	for _, id := range append([]string{body.UserID}, body.UserIDs...) {
		if id == "" || seenUser[id] {
			continue
		}
		seenUser[id] = true
		users = append(users, id)
	}
	agentID := body.AgentID
	if agentID == "" && len(body.AgentIDs) > 0 {
		agentID = body.AgentIDs[0]
	}
	limit := body.Limit
	if limit <= 0 {
		limit = 5
	}
	minScore := s.minScore
	if body.MinScore != nil {
		minScore = *body.MinScore
	}
	return &searchRun{
		srv:         s,
		req:         body,
		users:       s.expandOwners(users),
		agentID:     agentID,
		limit:       limit,
		minScore:    minScore,
		reader:      parseReader(body.Reader),
		factsWanted: body.Layer == "" || memory.Layer(body.Layer) == memory.L3Fact,
	}, nil
}

// retrieve embeds the query once, then gathers and fuses the candidates: the vector
// search, the long-fact clauses, the keyword search, and the identifier filter.
func (r *searchRun) retrieve() error {
	s := r.srv
	layer := memory.Layer(r.req.Layer)
	qv, err := s.store.embed.Embed(s.store.ctx, r.req.Query)
	if err != nil {
		return fmt.Errorf("couldn't create embedding of query: %w", err)
	}
	r.qv = qv
	// Fetch three times the limit so the floor, the duplicate drop and the L5 cap
	// still leave limit hits. The vector search runs in every mode, because it
	// counts the request as a search.
	res, err := s.store.SearchOwnersVec(qv, r.limit*3, layer, r.users, r.agentID)
	if err != nil {
		return err
	}
	// A long fact is also found by any one of its sentences (clauses.go), so it no
	// longer loses to short facts on a query about one part of it.
	if r.factsWanted && len(qv) > 0 {
		res = mergeBest(res, s.store.clauseHitsOwners(qv, r.users, r.agentID, r.limit*3))
	}
	// Every vector candidate, before the floor, for graph-assisted recall below.
	r.vecAll = res
	// Keyword search catches exact identifiers (HYATLAS_SYNC_EXTRACT, a port, a
	// ticker) that the embedding scores low; the two rankings are fused.
	switch r.reader {
	case readVector:
		res = refineHits(res, r.minScore, 0)
	default:
		// Keyword candidates are fetched deep: BM25 favours short documents, so an
		// identifier's top ranks can all be terse L5 relations, which the L5 cap below
		// then thins out. A shallow fetch left nothing else to fill the slots (2 hits
		// for "19528" though 20 facts mention it). Ranking already scores every row.
		// Lite stores only raw rows, so a lite server ranks them; the modes that extract
		// leave them out of an all-layer search, as before.
		kw, err := s.store.keywordSearch(r.req.Query, qv, r.limit*10, layer, r.users, r.agentID, !s.mode.UsesLLM())
		if err != nil {
			return err
		}
		if r.reader == readKeyword {
			res = nil
		}
		// The query's kind sets how much each ranking counts: identifiers lean on
		// keyword search, "how" and "why" questions on meaning.
		wVec, wKw := intentWeights(classifyQuery(r.req.Query))
		res = fuseHitsWeighted(res, kw, r.minScore, 0, wVec, wKw)
		// A query that is an identifier (RSI(2), HYATLAS_SYNC_EXTRACT, a port) is
		// answered only by memories that mention it; embedding look-alikes are noise.
		if terms := identifierTerms(r.req.Query); len(terms) > 0 {
			res = requireTerms(res, terms)
		}
	}
	r.hits = res
	return nil
}

// boostByContext moves the hits that fit what the query names: a time window, then
// the owner's entities and the facts the knowledge graph links to them.
func (r *searchRun) boostByContext() {
	s := r.srv
	res := r.hits
	// A query that names a time ("last week", "on 2026-10-05") favours memories
	// dated in that window: dated facts the vector search missed join the pool,
	// then out-of-window hits move down a few places. Only L3 facts carry dates.
	r.now = time.Now()
	if win, ok := parseTimeWindow(r.req.Query, r.now); ok {
		if r.factsWanted && len(r.qv) > 0 {
			res = appendNewHits(res, s.store.windowCandidates(r.qv, r.users, r.agentID, win, r.limit, windowFloor(r.minScore)))
		}
		res = rerankWindow(res, win)
	}
	// A query naming one of the owner's entities (an entity page's name or alias)
	// favours hits about it, and facts linked to it in the knowledge graph may pass
	// a slightly lower floor: they are related even when their wording is not.
	if names := s.store.queryEntities(r.req.Query, r.users, r.agentID); len(names) > 0 {
		res = rerankEntities(res, names)
		if nb := s.store.graphNeighbourNames(names, r.users, r.agentID); len(nb) > 0 {
			// Placed inside the limit, or the final cut would drop them. A keyword-only
			// search admits no vector candidates.
			cands := r.vecAll
			if r.reader == readKeyword {
				cands = nil
			}
			res = graphAdmitWithin(cands, res, nb, r.minScore-0.10, graphRecallMax, r.limit)
		}
	}
	r.hits = res
}

// rankSignals applies the signals that come from the memories themselves: stale
// intentions leave, newer and recently used memories move up, and near-duplicates
// move down.
func (r *searchRun) rankSignals() {
	s := r.srv
	// Time before the L5 cap, so the cap keeps the relations that rank best once
	// age is counted.
	res := dropStaleIntentions(r.hits, s.intentionTTL, r.now)
	if s.recency {
		// Age counts from the later of written and last used, so an old memory the
		// agent still relies on does not sink.
		var lastUsed func(string) time.Time
		if s.usage != nil {
			lastUsed = s.usage.LastUsed
		}
		res = rerankRecencyUsed(res, r.now, lastUsed)
	}
	if s.usage != nil {
		res = rerankUsage(res, s.usage)
	}
	// The current session's intentions come first: what the user is doing now outranks
	// a plan from another session. Without a session id nothing changes.
	if sid := r.req.SessionID; sid != "" {
		res = rerankByFactor(res, func(_ int, h SearchHit) float64 {
			return sessionFactor(h, sid)
		})
	}
	// Two near-identical lines waste a slot; the lower one moves to the end, where
	// the limit usually cuts it. Only the first 2*limit hits are compared, because a
	// duplicate further down cannot reach the result; the rest keep their order after.
	if head := 2 * r.limit; len(res) > head {
		res = append(s.store.demoteNearDuplicates(res[:head], nearDupScore), res[head:]...)
	} else {
		res = s.store.demoteNearDuplicates(res, nearDupScore)
	}
	r.hits = res
}

// Factors for an intention of the current session and of another session.
const (
	sameSessionFactor  = 1.08
	otherSessionFactor = 0.95
)

// sessionFactor is the rank factor of hit h for a search from session sid: an L7
// intention written in sid gets sameSessionFactor, one from another session gets
// otherSessionFactor, and every other hit (or an intention with no session) gets 1.
func sessionFactor(h SearchHit, sid string) float64 {
	if h.Layer != memory.L7Intention || h.Meta["session_id"] == "" {
		return 1
	}
	if h.Meta["session_id"] == sid {
		return sameSessionFactor
	}
	return otherSessionFactor
}

// admitLinkedFacts brings in facts that the top hits link to (see admitLinked). It
// only applies when the search covers L3 facts.
func (r *searchRun) admitLinkedFacts() {
	if r.factsWanted {
		r.hits = admitLinked(r.srv.store, r.hits, r.users, r.agentID, r.limit)
	}
}

// applyLayerQuotas caps the L5 and L8 layers, makes room for the schemas behind the
// facts found and for an open intention, and cuts the list to the limit.
func (r *searchRun) applyLayerQuotas() {
	s := r.srv
	res := r.hits
	limit := r.limit
	if r.req.Layer == "" {
		res = capLayer(res, memory.L5Knowledge, l5Share(limit))
		// An entity page is a whole summary of one thing, so two are plenty; the
		// rest of the slots go to the facts and rules around them.
		res = capLayer(res, memory.L8Entity, 2)
		// The patterns behind the facts found (schemas citing them as evidence) and
		// an open intention that ranked just below the cut each get a slot, so the
		// profile and proactive channels are not crowded out by facts.
		var factIDs []string
		for i, h := range res {
			if i >= limit {
				break
			}
			if h.Layer == memory.L3Fact {
				factIDs = append(factIDs, h.ID)
			}
		}
		res = ensureQuota(res, s.store.schemasSupporting(factIDs, r.users, r.agentID, schemaEvidenceMax), memory.L6Schema, limit)
		if len(res) > limit {
			var intents []SearchHit
			for _, h := range res[limit:] {
				if h.Layer == memory.L7Intention {
					intents = append(intents, h)
				}
			}
			res = ensureQuota(res, intents, memory.L7Intention, limit)
		}
	}
	if len(res) > limit {
		res = res[:limit]
	}
	r.hits = res
}

// channelOf names the response channel a hit belongs to.
func channelOf(h SearchHit) string {
	switch h.Layer {
	case memory.L8Entity:
		return "pages"
	case memory.L1Profile, memory.L6Schema:
		return "profile"
	case memory.L7Intention:
		return "proactive"
	}
	if isExperience(h) {
		return "experiences"
	}
	return "normal"
}

// render builds the response: the hits sorted into their channels (all five always
// present, empty when nothing fits), the owner's Now page when there is one, and
// "order", the channel order the plugin should show them in.
func (r *searchRun) render() map[string]any {
	s := r.srv
	ids := make([]string, len(r.hits))
	for i, h := range r.hits {
		ids[i] = h.ID
	}
	history := s.store.historyOf(ids, historyPerHit)
	channels := make(map[string][]searchHitJSON, len(searchChannels))
	for _, c := range searchChannels {
		channels[c] = []searchHitJSON{}
	}
	for _, h := range r.hits {
		c := channelOf(h)
		channels[c] = append(channels[c], searchHitJSON{MemoryID: h.ID, Content: h.Content,
			Score: float64(h.Score), Layer: string(h.Layer), GmtCreated: gmtCreated(h.Meta["ts"]),
			UserID: h.Meta["user_id"], AgentID: h.Meta["agent_id"],
			EventDate: normalizeEventDate(h.Meta[metaEventAt]), History: history[h.ID]})
	}
	order := append([]string{"now"}, searchChannels...)
	out := map[string]any{"memories": channels, "order": order}
	if np, ok := s.nowFor(r.users, r.agentID); ok {
		out["now"] = np
	}
	return out
}
