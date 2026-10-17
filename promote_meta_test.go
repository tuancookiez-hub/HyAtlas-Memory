package main

import (
	"testing"
	"time"
)

func TestNormalizeEventDate(t *testing.T) {
	cases := map[string]string{
		"2026-10-05":           "2026-10-05",
		" 2026-10-05 ":         "2026-10-05",
		"2026-10-05T09:00:00Z": "2026-10-05",
		"2026-10":              "2026-10",
		"2026-13-01":           "",
		"2026-02-30":           "",
		"next Tuesday":         "",
		"":                     "",
		"2026":                 "",
	}
	for in, want := range cases {
		if got := normalizeEventDate(in); got != want {
			t.Errorf("normalizeEventDate(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestEventDateRange(t *testing.T) {
	from, to, ok := eventDateRange("2026-10-05", time.UTC)
	if !ok || to.Sub(from) != 24*time.Hour || from.Day() != 5 {
		t.Fatalf("day range: %v %v %v", from, to, ok)
	}
	from, to, ok = eventDateRange("2026-02", time.UTC)
	if !ok || from.Month() != time.February || to.Month() != time.March {
		t.Fatalf("month range: %v %v %v", from, to, ok)
	}
	if _, _, ok := eventDateRange("soon", time.UTC); ok {
		t.Fatal("non-date accepted")
	}
}
