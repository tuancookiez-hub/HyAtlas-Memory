package main

import (
	"encoding/json"
	"log"
	"net/http"
	"slices"
	"strings"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// handleForget is POST /api/v1/forget: it retires the memory-tool raw rows of one
// owner whose content equals text, and every live row derived from them, as
// forgotten. It is reversible through the admin restore, so it needs no admin
// switch. The response counts the raw rows and the derived rows it retired.
func (s *Server) handleForget(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		jsonResponse(w, 405, map[string]any{"error": "method not allowed: use POST"})
		return
	}
	var body struct {
		Text    string `json:"text"`
		UserID  string `json:"user_id"`
		AgentID string `json:"agent_id"`
		Reason  string `json:"reason"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		jsonResponse(w, 400, map[string]any{"error": "bad body", "success": false})
		return
	}
	text := strings.TrimSpace(body.Text)
	if text == "" {
		jsonResponse(w, 400, map[string]any{"error": "text required", "success": false})
		return
	}
	reason := body.Reason
	if strings.TrimSpace(reason) == "" {
		reason = defaultForgetReason
	}

	raw, derived := s.store.forgetTargets(text, body.UserID, body.AgentID)
	nRaw, nDerived := 0, 0
	if len(raw) > 0 {
		n, err := s.store.retire(raw, Retire{How: howForgotten, Reason: reason})
		if err != nil {
			jsonResponse(w, 500, map[string]any{"success": false, "error": err.Error()})
			return
		}
		nRaw = n
	}
	if len(derived) > 0 {
		n, err := s.store.retire(derived, Retire{How: howForgotten, Reason: reason})
		if err != nil {
			jsonResponse(w, 500, map[string]any{"success": false, "error": err.Error(), "forgotten": nRaw})
			return
		}
		nDerived = n
	}
	nPages, nEdges := s.forgetBeyondRows(raw, body.UserID, body.AgentID, nRaw+nDerived > 0)
	jsonResponse(w, 200, map[string]any{"forgotten": nRaw, "derived": nDerived, "pages": nPages, "edges": nEdges})
}

// forgetBeyondRows clears what a forgotten raw row left outside the row store: the
// entity-page entries and the graph edges that cite it, and then it asks for the Now
// page to be rebuilt. It returns how many pages and edges it changed. Failures are
// logged and never fail the request, because the rows are already retired. These
// are not undone by a restore: a restored row comes back, its page entries and
// edges do not. The refresh runs only when retired is true.
func (s *Server) forgetBeyondRows(raw []string, userID, agentID string, retired bool) (pages, edges int) {
	if len(raw) > 0 {
		ids := make(map[string]bool, len(raw))
		for _, id := range raw {
			ids[id] = true
		}
		if s.store.pages != nil {
			n, err := s.store.pages.ForgetSources(ids)
			if err != nil {
				log.Printf("forget: pages: %v", err)
			}
			pages = n
		}
		if g := s.store.Graph(); g != nil {
			removed, trimmed, err := g.RemoveSources(ids)
			if err != nil {
				log.Printf("forget: graph: %v", err)
			}
			edges = removed + trimmed
		}
	}
	if retired && s.now != nil {
		s.now.Touch(userID, agentID)
	}
	return pages, edges
}

// forgetTargets finds the live raw rows that forget should retire and the live
// rows derived from them. A raw row qualifies when it belongs to userID (or one of its
// aliases) and exactly agentID, carries the memory-tool write origin, and its content equals text after
// both are trimmed of surrounding space. A derived row is any live non-raw row whose
// sources include a qualifying raw row's id. Both lists are read under one lock, so
// a row written in between is never half-matched.
func (s *MemoryStore) forgetTargets(text, userID, agentID string) (raw []string, derived []string) {
	// A row written under any alias of the owner qualifies.
	users := s.owners(userID)
	s.mu.RLock()
	defer s.mu.RUnlock()
	rawSet := make(map[string]bool)
	for _, d := range s.index {
		if d.Layer != string(memory.L2Raw) || isSuperseded(d) {
			continue
		}
		if !slices.Contains(users, d.UserID) || d.AgentID != agentID {
			continue
		}
		if !policyFor(d.Meta[metaWriteOrigin]).forgettable {
			continue
		}
		if strings.TrimSpace(d.Content) != text {
			continue
		}
		rawSet[d.ID] = true
		raw = append(raw, d.ID)
	}
	if len(rawSet) == 0 {
		return nil, nil
	}
	for _, d := range s.index {
		if d.Layer == string(memory.L2Raw) || isSuperseded(d) {
			continue
		}
		for _, src := range sourcesOf(d) {
			if rawSet[src] {
				derived = append(derived, d.ID)
				break
			}
		}
	}
	return raw, derived
}
