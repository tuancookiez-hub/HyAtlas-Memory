package main

import (
	"testing"
	"time"
)

// TestEnvDuration checks the duration-or-off table for envDuration: unset and blank
// give the default, the off words give zero, durations and bare seconds are used as
// given, and anything negative or unparsable gives the default.
func TestEnvDuration(t *testing.T) {
	const key = "HYATLAS_TEST_ENV_DURATION"
	const def = 15 * time.Minute
	cases := []struct {
		name string
		val  string
		want time.Duration
	}{
		{"unset", "", def},
		{"blank", "   ", def},
		{"zero", "0", 0},
		{"off", "off", 0},
		{"OFF", "OFF", 0},
		{"false", "False", 0},
		{"no", "NO", 0},
		{"go duration", "2m", 2 * time.Minute},
		{"go duration with spaces", " 90s ", 90 * time.Second},
		{"bare seconds", "45", 45 * time.Second},
		{"bare seconds large", "3600", time.Hour},
		{"negative bare seconds", "-5", def},
		{"negative duration", "-5m", def},
		{"garbage", "soon", def},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Setenv(key, c.val)
			if got := envDuration(key, def); got != c.want {
				t.Fatalf("%s=%q: got %v, want %v", key, c.val, got, c.want)
			}
		})
	}
}
