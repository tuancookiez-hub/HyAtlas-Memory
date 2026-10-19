package main

import (
	"log"
	"strings"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// experienceLabel is the source_layer_label of an experience row. An experience is
// stored as an L3 fact with this label, so the layer model, the mode counts and the
// dashboard do not change; search reads the label to route it to its own channel.
const experienceLabel = "experience"

// experienceDedupeLimit is how many nearest live L3 rows are checked for an older
// experience of the same lesson. The nearest experience is found among these.
const experienceDedupeLimit = 10

// experienceText is the stored text of an experience, the text an agent reads. The
// environment is left out when the model gave none, so the line never ends in an
// empty clause.
func experienceText(e Experience) string {
	text := "Task: " + strings.TrimSpace(e.Task) + ". Solution: " + strings.TrimSpace(e.Solution) + "."
	if env := strings.TrimSpace(e.Environment); env != "" {
		text += " Environment: " + env + "."
	}
	return text
}

// normalizeConfidence maps a model's confidence word to high, medium or low. Anything
// else, including an empty value, is medium: the model did not say it was sure, and
// a lesson should not be stored as certain on that basis.
func normalizeConfidence(c string) string {
	switch strings.ToLower(strings.TrimSpace(c)) {
	case "high":
		return "high"
	case "low":
		return "low"
	default:
		return "medium"
	}
}

// writeExperiences stores the solved-task lessons an extraction reported, for one
// owner. An experience with no task or no solution is skipped, because a lesson
// that cannot say what worked cannot be repeated. When the owner already has a
// near-identical live experience, the new one supersedes it as an update, so a
// repeat confirms or refines the lesson instead of duplicating it. It returns the
// ids it wrote, which the episode grouping treats like fact ids.
func (s *MemoryStore) writeExperiences(exs []Experience, userID, agentID, sourceID, ts string) []string {
	// The dedupe filter is built once per call: only experiences of this owner are
	// searched, so ordinary facts nearer than an old experience cannot hide it.
	where := map[string]string{"source_layer_label": experienceLabel}
	if userID != "" {
		where["user_id"] = userID
	}
	if agentID != "" {
		where["agent_id"] = agentID
	}
	var ids []string
	for _, e := range exs {
		e.Task = strings.TrimSpace(e.Task)
		e.Solution = strings.TrimSpace(e.Solution)
		e.Environment = strings.TrimSpace(e.Environment)
		if e.Task == "" || e.Solution == "" {
			continue
		}
		text := experienceText(e)

		// The nearest live experience of this owner that is the same lesson. It is
		// found before the new row is written, so the new row cannot match itself.
		// A failed embed or search only skips the dedupe; the write still happens.
		var old []string
		qv, err := s.embed.Embed(s.ctx, text)
		if err != nil {
			log.Printf("experience: dedupe embed: %v", err)
		}
		var near []SearchHit
		if err == nil {
			near, err = s.searchWhere(qv, experienceDedupeLimit, memory.L3Fact, where)
			if err != nil {
				log.Printf("experience: dedupe search: %v", err)
			}
		}
		for _, h := range near {
			// Only an experience can be the same lesson; the where filter already
			// restricts the search, and isExperience keeps the check explicit.
			if isExperience(h) && float64(h.Score) >= defaultDedupeScore && sameNumbers(h.Content, text) {
				old = append(old, h.ID)
				break
			}
		}

		id := newID()
		meta := map[string]string{
			"user_id": userID, "agent_id": agentID,
			"source_layer_label": experienceLabel,
			"task":               e.Task,
			"solution":           e.Solution,
			"environment":        e.Environment,
			"confidence":         normalizeConfidence(e.Confidence),
			"source_id":          sourceID, "ts": ts,
		}
		// The vector from the dedupe search is reused, so the lesson is embedded once.
		if err := s.addWithVector(memory.L3Fact, id, text, meta, qv); err != nil {
			log.Printf("experience: write: %v", err)
			continue
		}
		ids = append(ids, id)
		if len(old) > 0 {
			if _, err := s.retire(old, Retire{By: id, How: howUpdated}); err != nil {
				log.Printf("experience: supersede %s: %v", old[0], err)
			}
		}
	}
	return ids
}

// isExperience reports whether a search hit is a task experience rather than an
// ordinary fact. Only an L3 row labelled experience qualifies.
func isExperience(h SearchHit) bool {
	return h.Layer == memory.L3Fact && h.Meta["source_layer_label"] == experienceLabel
}
