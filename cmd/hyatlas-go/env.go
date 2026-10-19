package main

import (
	"log"
	"os"
	"strconv"
	"strings"
	"time"
)

// envDuration reads the environment variable key as an on-or-off duration. An unset
// or blank value returns def. "0", "off", "false" and "no" in any case return 0,
// which turns the feature off. A Go duration such as "15m" is used as given, and a
// bare non-negative integer counts as seconds. Anything else, or a negative value,
// logs once and returns def, so a typo never silently changes the schedule.
func envDuration(key string, def time.Duration) time.Duration {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return def
	}
	switch strings.ToLower(raw) {
	case "0", "off", "false", "no":
		return 0
	}
	if n, err := strconv.Atoi(raw); err == nil && n >= 0 {
		return time.Duration(n) * time.Second
	}
	d, err := time.ParseDuration(raw)
	if err != nil || d < 0 {
		log.Printf("%s=%q is not a duration; using %s", key, raw, def)
		return def
	}
	return d
}
