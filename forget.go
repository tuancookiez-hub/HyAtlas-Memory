package main

import (
	"encoding/json"
	"net/http"
	"strings"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// howForgotten is the superseded_how of a row that Hermes's memory tool removed or
// replaced, mirrored to HyAtlas by /api/v1/forget. Such a row is retired, not
// deleted, so the admin restore brings it back.
const howForgotten = "forgotten"

// memoryToolOrigin is the write_origin metadata value that marks a raw row as one
// the Hermes memory tool wrote. Only rows with this origin are forgotten.
const memoryToolOrigin = "memory_tool"

// defaultForgetReason is the drop_reason recorded when a forget request gives none.
const defaultForgetReason = "removed with Hermes's memory tool"

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
	jsonResponse(w, 200, map[string]any{"forgotten": nRaw, "derived": nDerived})
}

// forgetTargets finds the live raw rows that forget should retire and the live
// rows derived from them. A raw row qualifies when it belongs to exactly userID and
// agentID, carries the memory-tool write origin, and its content equals text after
// both are trimmed of surrounding space. A derived row is any live non-raw row whose
// sources include a qualifying raw row's id. Both lists are read under one lock, so
// a row written in between is never half-matched.
func (s *MemoryStore) forgetTargets(text, userID, agentID string) (raw []string, derived []string) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rawSet := make(map[string]bool)
	for _, d := range s.index {
		if d.Layer != string(memory.L2Raw) || isSuperseded(d) {
			continue
		}
		if d.UserID != userID || d.AgentID != agentID {
			continue
		}
		if d.Meta["write_origin"] != memoryToolOrigin {
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
