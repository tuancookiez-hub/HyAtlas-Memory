package main

import (
	"fmt"
	"maps"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// timeWindow is a span of time a query asks about: [From, To). Label says what
// matched, so a caller can show it.
type timeWindow struct {
	From, To time.Time
	Label    string // what matched, e.g. "last week" or "2026-10-05"
}

// windowMiss is the share of its ranking weight a hit outside the window keeps. It
// is below one so a near-tie goes to the in-window hit, while a clearly better
// match still wins.
const windowMiss = 0.85

// windowScanMax caps how many dated facts windowCandidates scores. The newest are
// kept, so a long store cannot make a time-scoped search slow.
const windowScanMax = 500

// windowMonthPat matches a month name in full or abbreviated form. Each name is
// bounded by word boundaries where it is used, so "may" in "may I" is not special
// here; the caller decides whether a bare month counts.
const windowMonthPat = `(january|february|march|april|may|june|july|august|september|sept|sep|october|november|december|jan|feb|mar|apr|jun|jul|aug|oct|nov|dec)`

// windowMonths maps each month name the parser accepts to its month number.
var windowMonths = map[string]time.Month{
	"january": time.January, "jan": time.January,
	"february": time.February, "feb": time.February,
	"march": time.March, "mar": time.March,
	"april": time.April, "apr": time.April,
	"may":  time.May,
	"june": time.June, "jun": time.June,
	"july": time.July, "jul": time.July,
	"august": time.August, "aug": time.August,
	"september": time.September, "sept": time.September, "sep": time.September,
	"october": time.October, "oct": time.October,
	"november": time.November, "nov": time.November,
	"december": time.December, "dec": time.December,
}

// windowWeekdays maps each weekday name the parser accepts to its weekday.
var windowWeekdays = map[string]time.Weekday{
	"monday": time.Monday, "mon": time.Monday,
	"tuesday": time.Tuesday, "tues": time.Tuesday, "tue": time.Tuesday,
	"wednesday": time.Wednesday, "wed": time.Wednesday,
	"thursday": time.Thursday, "thurs": time.Thursday, "thur": time.Thursday, "thu": time.Thursday,
	"friday": time.Friday, "fri": time.Friday,
	"saturday": time.Saturday, "sat": time.Saturday,
	"sunday": time.Sunday, "sun": time.Sunday,
}

// The patterns below run on lower-cased query text, in the order parseTimeWindow
// tries them.
var (
	reWinISODate   = regexp.MustCompile(`\b\d{4}-\d{2}-\d{2}\b`)
	reWinISOMonth  = regexp.MustCompile(`\b\d{4}-\d{2}\b`)
	reWinMonthDay  = regexp.MustCompile(`\b` + windowMonthPat + `\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?`)
	reWinDayMonth  = regexp.MustCompile(`\b(\d{1,2})(?:st|nd|rd|th)?\s+` + windowMonthPat + `\b(?:,?\s+(\d{4})\b)?`)
	reWinToday     = regexp.MustCompile(`\btoday\b`)
	reWinYesterday = regexp.MustCompile(`\byesterday\b`)
	reWinDaysAgo   = regexp.MustCompile(`\b(\d{1,3}) days ago\b`)
	reWinLastPast  = regexp.MustCompile(`\b(?:last|past) (\d{1,2}) (days?|weeks?|months?)\b`)
	reWinThisWeek  = regexp.MustCompile(`\bthis week\b`)
	reWinLastWeek  = regexp.MustCompile(`\blast week\b`)
	reWinThisMonth = regexp.MustCompile(`\bthis month\b`)
	reWinLastMonth = regexp.MustCompile(`\blast month\b`)
	reWinWeekday   = regexp.MustCompile(`\b(last|on) (monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|sunday|sun)\b`)
	reWinInMonth   = regexp.MustCompile(`\bin\s+` + windowMonthPat + `(?:\s+(\d{4}))?\b`)
	reWinMonthYear = regexp.MustCompile(`\b` + windowMonthPat + `\s+(\d{4})\b`)
)

// parseTimeWindow returns the time span a query asks about, relative to now, in
// now's location. Rules are tried in a fixed order and the first valid match wins.
// ok is false when the query names no time, so ordinary sentences such as "may I
// ask" are left alone.
func parseTimeWindow(query string, now time.Time) (timeWindow, bool) {
	q := strings.ToLower(query)
	loc := now.Location()
	today0 := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, loc)

	// A full ISO date names one day.
	for _, m := range reWinISODate.FindAllString(q, -1) {
		if t, err := time.ParseInLocation("2006-01-02", m, loc); err == nil {
			return dayWindow(t, m), true
		}
	}
	// A year and month name a whole month, unless it is the start of a longer, invalid date.
	for _, idx := range reWinISOMonth.FindAllStringIndex(q, -1) {
		if windowDashDigit(q[idx[1]:]) {
			continue
		}
		if t, err := time.ParseInLocation("2006-01", q[idx[0]:idx[1]], loc); err == nil {
			return monthWindow(t.Year(), t.Month(), loc), true
		}
	}
	// A month and day, in either order, name one day.
	for _, m := range reWinMonthDay.FindAllStringSubmatch(q, -1) {
		if t, ok := windowMonthDay(m[1], m[2], m[3], today0); ok {
			return dayWindow(t, t.Format("2006-01-02")), true
		}
	}
	for _, m := range reWinDayMonth.FindAllStringSubmatch(q, -1) {
		if t, ok := windowMonthDay(m[2], m[1], m[3], today0); ok {
			return dayWindow(t, t.Format("2006-01-02")), true
		}
	}
	if reWinToday.MatchString(q) {
		return dayWindow(today0, "today"), true
	}
	if reWinYesterday.MatchString(q) {
		return dayWindow(today0.AddDate(0, 0, -1), "yesterday"), true
	}
	if m := reWinDaysAgo.FindStringSubmatch(q); m != nil {
		if n, _ := strconv.Atoi(m[1]); n >= 1 && n <= 365 {
			return dayWindow(today0.AddDate(0, 0, -n), m[0]), true
		}
	}
	// "last N units" and "past N units" cover today and the N units before it.
	if m := reWinLastPast.FindStringSubmatch(q); m != nil {
		if n, _ := strconv.Atoi(m[1]); n >= 1 && n <= 99 {
			var from time.Time
			switch {
			case strings.HasPrefix(m[2], "day"):
				from = today0.AddDate(0, 0, -n)
			case strings.HasPrefix(m[2], "week"):
				from = today0.AddDate(0, 0, -7*n)
			default:
				from = today0.AddDate(0, -n, 0)
			}
			return timeWindow{From: from, To: today0.AddDate(0, 0, 1), Label: m[0]}, true
		}
	}
	thisMon := mondayOf(today0)
	if reWinThisWeek.MatchString(q) {
		return timeWindow{From: thisMon, To: today0.AddDate(0, 0, 1), Label: "this week"}, true
	}
	if reWinLastWeek.MatchString(q) {
		return timeWindow{From: thisMon.AddDate(0, 0, -7), To: thisMon, Label: "last week"}, true
	}
	thisFirst := time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, loc)
	if reWinThisMonth.MatchString(q) {
		return timeWindow{From: thisFirst, To: today0.AddDate(0, 0, 1), Label: "this month"}, true
	}
	if reWinLastMonth.MatchString(q) {
		return timeWindow{From: thisFirst.AddDate(0, -1, 0), To: thisFirst, Label: "last month"}, true
	}
	// "last" or "on" with a weekday names the most recent such day before today.
	if m := reWinWeekday.FindStringSubmatch(q); m != nil {
		wd := windowWeekdays[m[2]]
		for i := 1; i <= 7; i++ {
			if d := today0.AddDate(0, 0, -i); d.Weekday() == wd {
				return dayWindow(d, m[0]), true
			}
		}
	}
	// "in <month>" or "<month> <year>" name a month. A bare month name does not count.
	if m := reWinInMonth.FindStringSubmatch(q); m != nil {
		return windowMonthOf(m[1], m[2], now, loc), true
	}
	if m := reWinMonthYear.FindStringSubmatch(q); m != nil {
		return windowMonthOf(m[1], m[2], now, loc), true
	}
	return timeWindow{}, false
}

// windowDashDigit reports whether rest starts with "-" and a digit. A match that
// is followed that way is the front of a longer, invalid date, so it is skipped.
func windowDashDigit(rest string) bool {
	return len(rest) >= 2 && rest[0] == '-' && rest[1] >= '0' && rest[1] <= '9'
}

// dayWindow returns the window covering the single local day d, labelled label.
func dayWindow(d time.Time, label string) timeWindow {
	return timeWindow{From: d, To: d.AddDate(0, 0, 1), Label: label}
}

// monthWindow returns the window covering calendar month mo of year y in loc, with
// the label YYYY-MM.
func monthWindow(y int, mo time.Month, loc *time.Location) timeWindow {
	from := time.Date(y, mo, 1, 0, 0, 0, 0, loc)
	return timeWindow{From: from, To: from.AddDate(0, 1, 0), Label: fmt.Sprintf("%04d-%02d", y, int(mo))}
}

// mondayOf returns the local Monday of the week containing day, at midnight.
// Weeks start on Monday, so Sunday (weekday 0) belongs to the week that began six
// days earlier.
func mondayOf(day time.Time) time.Time {
	return day.AddDate(0, 0, -((int(day.Weekday()) + 6) % 7))
}

// windowMonthDay returns the local date for a month name and day. With a year it is
// used as given; without one it is the most recent such date that is not after
// today0, looking back a few years so a Feb 29 can still resolve. ok is false when
// the month or day is not valid.
func windowMonthDay(monthName, dayStr, yearStr string, today0 time.Time) (time.Time, bool) {
	mo, known := windowMonths[monthName]
	day, err := strconv.Atoi(dayStr)
	if !known || err != nil {
		return time.Time{}, false
	}
	loc := today0.Location()
	if yearStr != "" {
		year, err := strconv.Atoi(yearStr)
		if err != nil {
			return time.Time{}, false
		}
		t := time.Date(year, mo, day, 0, 0, 0, 0, loc)
		return t, t.Day() == day && t.Month() == mo
	}
	for y := today0.Year(); y >= today0.Year()-8; y-- {
		t := time.Date(y, mo, day, 0, 0, 0, 0, loc)
		if t.Day() == day && t.Month() == mo && !t.After(today0) {
			return t, true
		}
	}
	return time.Time{}, false
}

// windowMonthOf returns the window of a named month. With no year it is the most
// recent such month that has started by now, so "november" asked in October means
// the previous November.
func windowMonthOf(monthName, yearStr string, now time.Time, loc *time.Location) timeWindow {
	mo := windowMonths[monthName]
	y := now.Year()
	if yearStr != "" {
		if parsed, err := strconv.Atoi(yearStr); err == nil {
			y = parsed
		}
	} else if mo > now.Month() {
		y--
	}
	return monthWindow(y, mo, loc)
}

// hitTimeRange returns the span of time a row's metadata places it in. An event_at
// date wins, since it says when the fact happened. Without one, the row's write
// time ts is used as a one-second instant. ok is false when neither is usable.
func hitTimeRange(meta map[string]string, loc *time.Location) (from, to time.Time, ok bool) {
	if loc == nil {
		loc = time.UTC
	}
	if s := normalizeEventDate(meta[metaEventAt]); s != "" {
		return eventDateRange(s, loc)
	}
	if t, err := time.Parse(time.RFC3339, meta["ts"]); err == nil {
		return t, t.Add(time.Second), true
	}
	return time.Time{}, time.Time{}, false
}

// overlaps reports whether the span [from, to) shares any time with the window.
func (w timeWindow) overlaps(from, to time.Time) bool {
	return from.Before(w.To) && to.After(w.From)
}

// rerankWindow returns hits reordered so that rows inside the window move up. The
// incoming order is the relevance ranking, so each hit at index i gets the rank
// weight 1/(rrfK+i+1), multiplied by windowMiss when it falls outside w. The sort
// is stable, and the returned hits keep their Scores, so only the order changes.
func rerankWindow(hits []SearchHit, w timeWindow) []SearchHit {
	order := make([]int, len(hits))
	weight := make([]float64, len(hits))
	loc := w.From.Location()
	for i, h := range hits {
		order[i] = i
		wt := 1 / float64(rrfK+i+1)
		from, to, ok := hitTimeRange(h.Meta, loc)
		if !ok || !w.overlaps(from, to) {
			wt *= windowMiss
		}
		weight[i] = wt
	}
	sort.SliceStable(order, func(a, b int) bool {
		return weight[order[a]] > weight[order[b]]
	})
	out := make([]SearchHit, len(hits))
	for i, j := range order {
		out[i] = hits[j]
	}
	return out
}

// windowCandidates returns the live L3 facts whose time span overlaps w, scored
// against qv by cosine similarity. users limits the owners (empty means any owner),
// and agentID limits the agent (empty means any agent). It keeps scores of at least
// minScore, the best k of them. Only the newest windowScanMax matching facts are
// scored, so a large store stays cheap.
func (s *MemoryStore) windowCandidates(qv []float32, users []string, agentID string, w timeWindow, k int, minScore float64) []SearchHit {
	if len(qv) == 0 || k <= 0 {
		return nil
	}
	loc := w.From.Location()

	// Collect candidates under the read lock. The lock is released before the
	// vectors are read, because vectors takes the lock itself.
	s.mu.RLock()
	var live []DocIndex
	for _, d := range s.index {
		if d.Layer != string(memory.L3Fact) || isSuperseded(d) {
			continue
		}
		if _, dead := s.superseded[d.ID]; dead {
			continue
		}
		if len(users) > 0 && !slices.Contains(users, d.UserID) {
			continue
		}
		if agentID != "" && d.AgentID != agentID {
			continue
		}
		from, to, ok := hitTimeRange(d.Meta, loc)
		if !ok || !w.overlaps(from, to) {
			continue
		}
		live = append(live, d)
	}
	s.mu.RUnlock()

	// Keep the newest windowScanMax facts. An unreadable Ts sorts as the oldest.
	tsOf := func(d DocIndex) time.Time {
		t, _ := time.Parse(time.RFC3339, d.Ts)
		return t
	}
	sort.SliceStable(live, func(a, b int) bool {
		return tsOf(live[a]).After(tsOf(live[b]))
	})
	if len(live) > windowScanMax {
		live = live[:windowScanMax]
	}
	ids := make([]string, len(live))
	for i, d := range live {
		ids[i] = d.ID
	}
	vecs := s.vectors(ids)

	var hits []SearchHit
	for _, d := range live {
		v, ok := vecs[d.ID]
		if !ok {
			continue
		}
		score := cosineSim(qv, v)
		if score < minScore {
			continue
		}
		hits = append(hits, SearchHit{
			ID:      d.ID,
			Content: d.Content,
			Score:   float32(score),
			Layer:   memory.L3Fact,
			Meta:    maps.Clone(d.Meta),
		})
	}
	sort.SliceStable(hits, func(a, b int) bool {
		return hits[a].Score > hits[b].Score
	})
	if len(hits) > k {
		hits = hits[:k]
	}
	return hits
}
