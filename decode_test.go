package main

import (
	"encoding/json"
	"testing"
	"time"
)

// TestIntentionWrongTypeDecodesWithoutError checks that an intention sent as a number
// becomes the goal text, and that the facts beside it in the same reply survive.
func TestIntentionWrongTypeDecodesWithoutError(t *testing.T) {
	ex, err := parseExtraction(`{"facts":[{"data":"Cats are great","layer":"user_preferences"}],"intention":42}`)
	if err != nil {
		t.Fatalf("a numeric intention failed the whole reply: %v", err)
	}
	if ex.Intention == nil || ex.Intention.Goal != "42" {
		t.Fatalf("intention = %+v, want goal \"42\"", ex.Intention)
	}
	if len(ex.Facts) != 1 || ex.Facts[0].Data != "Cats are great" {
		t.Fatalf("facts = %+v, want the one fact to survive", ex.Facts)
	}
}

// TestIntentionObjectFieldOfWrongTypeKeepsOthers checks that a wrong-typed due field
// empties itself while the goal and replaces list in the same object survive.
func TestIntentionObjectFieldOfWrongTypeKeepsOthers(t *testing.T) {
	var in Intention
	if err := json.Unmarshal([]byte(`{"goal":"ship 5.0.0","due":["2026-11-01"],"replaces":"int-a"}`), &in); err != nil {
		t.Fatalf("a list-valued due failed the intention: %v", err)
	}
	if in.Goal != "ship 5.0.0" || in.Due != "" {
		t.Fatalf("goal=%q due=%q, want goal kept and due empty", in.Goal, in.Due)
	}
	if len(in.Replaces) != 1 || in.Replaces[0] != "int-a" {
		t.Fatalf("replaces = %v, want [int-a]", in.Replaces)
	}
}

// TestDropWrongTypeDecodesWithoutError checks that a drop sent as an array decodes to
// an empty drop without error, and that the drops beside it still decode.
func TestDropWrongTypeDecodesWithoutError(t *testing.T) {
	var reply struct {
		Drops []Drop `json:"drops"`
	}
	raw := `{"drops":[["m-1","m-2"],{"id":"m-3","reason":"a temporary state that has passed"}]}`
	if err := json.Unmarshal([]byte(raw), &reply); err != nil {
		t.Fatalf("an array drop failed the whole reply: %v", err)
	}
	if len(reply.Drops) != 2 {
		t.Fatalf("drops = %+v, want 2 entries", reply.Drops)
	}
	if reply.Drops[0].ID != "" || reply.Drops[0].Reason != "" {
		t.Fatalf("array drop = %+v, want empty", reply.Drops[0])
	}
	if reply.Drops[1].ID != "m-3" || reply.Drops[1].Reason == "" {
		t.Fatalf("object drop = %+v, want id m-3 with its reason", reply.Drops[1])
	}
}

// TestPageFieldOfWrongTypeKeepsOthers checks that a page sent as an object empties
// that field alone, and the name, kind and set of the same update survive.
func TestPageFieldOfWrongTypeKeepsOthers(t *testing.T) {
	var ups pageUpdates
	raw := `[{"page":{"id":1},"name":"Ada","kind":"person","set":[{"attr":"role","value":"lead"}]}]`
	if err := json.Unmarshal([]byte(raw), &ups); err != nil {
		t.Fatalf("an object page failed the update: %v", err)
	}
	if len(ups) != 1 {
		t.Fatalf("updates = %+v, want 1", ups)
	}
	u := ups[0]
	if u.Page != "" || u.Name != "Ada" || u.Kind != "person" {
		t.Fatalf("update = %+v, want page empty and name/kind kept", u)
	}
	if len(u.Set) != 1 || u.Set[0].Attr != "role" || u.Set[0].Value != "lead" {
		t.Fatalf("set = %+v, want role=lead", u.Set)
	}
}

// TestLooseStringReadings checks the one tolerance rule: text as it is, numbers and
// bools as their JSON spelling, and empty for missing, null, object and array values.
func TestLooseStringReadings(t *testing.T) {
	cases := map[string]string{
		`"text"`:  "text",
		`42`:      "42",
		`-1.5`:    "-1.5",
		`true`:    "true",
		`null`:    "",
		`{"a":1}`: "",
		`[1,2]`:   "",
		``:        "",
	}
	for raw, want := range cases {
		if got := looseString(json.RawMessage(raw)); got != want {
			t.Errorf("looseString(%s) = %q, want %q", raw, got, want)
		}
	}
}

// TestParseDurationUsesEnvDurationRules checks that the one duration parser gives the
// off words zero and a negative or garbage value the default.
func TestParseDurationUsesEnvDurationRules(t *testing.T) {
	const key = "HYATLAS_TEST_DECODE_DURATION"
	const def = 3 * time.Hour
	cases := []struct {
		val  string
		want time.Duration
	}{
		{"", def},
		{"90", 90 * time.Second},
		{"off", 0},
		{"0", 0},
		{"-5m", def},
		{"soon", def},
	}
	for _, c := range cases {
		t.Setenv(key, c.val)
		if got := envDuration(key, def); got != c.want {
			t.Errorf("envDuration(%q) = %v, want %v", c.val, got, c.want)
		}
	}
}
