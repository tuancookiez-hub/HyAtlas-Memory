package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// seedOwnerFacts stores three facts for each user, all tagged with agent a1.
func seedOwnerFacts(t *testing.T, srv *Server, users ...string) {
	t.Helper()
	for _, u := range users {
		for i := 1; i <= 3; i++ {
			text := fmt.Sprintf("%s likes tea %d", u, i)
			if err := srv.store.Add(memory.L3Fact, newID(), text,
				map[string]string{"user_id": u, "agent_id": "a1"}); err != nil {
				t.Fatal(err)
			}
		}
	}
}

// searchOwnerUserIDs runs handleSearch with body and returns the status code and
// the user_id of every hit across all three channels.
func searchOwnerUserIDs(t *testing.T, srv *Server, body string) (int, []string) {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/v1/search", strings.NewReader(body))
	w := httptest.NewRecorder()
	srv.handleSearch(w, req)
	if w.Code != http.StatusOK {
		return w.Code, nil
	}
	var resp struct {
		Memories struct {
			Profile   []map[string]any `json:"profile"`
			Proactive []map[string]any `json:"proactive"`
			Normal    []map[string]any `json:"normal"`
		} `json:"memories"`
	}
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	var ids []string
	for _, group := range [][]map[string]any{resp.Memories.Profile, resp.Memories.Proactive, resp.Memories.Normal} {
		for _, h := range group {
			uid, _ := h["user_id"].(string)
			ids = append(ids, uid)
		}
	}
	return w.Code, ids
}

func TestSearchHonoursSingularUserID(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnerFacts(t, srv, "alice", "bob")
	code, ids := searchOwnerUserIDs(t, srv,
		`{"query":"likes tea","user_id":"alice","agent_id":"a1","limit":10}`)
	if code != http.StatusOK {
		t.Fatalf("status: want 200, got %d", code)
	}
	if len(ids) == 0 {
		t.Fatal("want at least one hit")
	}
	for _, uid := range ids {
		if uid != "alice" {
			t.Errorf("hit for user %q, want only alice", uid)
		}
	}
}

func TestSearchStillHonoursUserIDsArray(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnerFacts(t, srv, "alice", "bob")
	code, ids := searchOwnerUserIDs(t, srv,
		`{"query":"likes tea","user_ids":["bob"],"agent_ids":["a1"],"limit":10}`)
	if code != http.StatusOK {
		t.Fatalf("status: want 200, got %d", code)
	}
	if len(ids) == 0 {
		t.Fatal("want at least one hit")
	}
	for _, uid := range ids {
		if uid != "bob" {
			t.Errorf("hit for user %q, want only bob", uid)
		}
	}
}

func TestSearchExpandsUserAliases(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnerFacts(t, srv, "alice", "alice-discord", "bob")
	srv.ownerAliases = aliasMap(parseUserAliases("alice,alice-discord"))
	code, ids := searchOwnerUserIDs(t, srv,
		`{"query":"likes tea","user_id":"alice","agent_id":"a1","limit":10}`)
	if code != http.StatusOK {
		t.Fatalf("status: want 200, got %d", code)
	}
	seen := map[string]bool{}
	for _, uid := range ids {
		seen[uid] = true
		if uid == "bob" {
			t.Errorf("hit for bob, want none")
		}
	}
	if !seen["alice"] || !seen["alice-discord"] {
		t.Errorf("want hits for alice and alice-discord, got %v", ids)
	}
}

func TestSearchOwnersCountsOneSearch(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	seedOwnerFacts(t, srv, "alice", "alice-discord", "bob")
	srv.ownerAliases = aliasMap(parseUserAliases("alice,alice-discord"))
	before := srv.store.searches.Load()
	code, _ := searchOwnerUserIDs(t, srv,
		`{"query":"likes tea","user_id":"alice","agent_id":"a1","limit":10}`)
	if code != http.StatusOK {
		t.Fatalf("status: want 200, got %d", code)
	}
	after := srv.store.searches.Load()
	if after-before != 1 {
		t.Errorf("search counter moved by %d, want 1", after-before)
	}
}

func TestParseUserAliases(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want [][]string
	}{
		{"empty", "", nil},
		{"trims and drops single-ID group", "a,b; c , d ;e", [][]string{{"a", "b"}, {"c", "d"}}},
		{"dedupes within group", "a,a,b", [][]string{{"a", "b"}}},
		{"all duplicates dropped", "a,a", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := parseUserAliases(tc.raw)
			if len(got) != len(tc.want) || (len(got) > 0 && !reflect.DeepEqual(got, tc.want)) {
				t.Errorf("parseUserAliases(%q) = %v, want %v", tc.raw, got, tc.want)
			}
		})
	}

	m := aliasMap(parseUserAliases("a,b;b,c"))["b"]
	if len(m) != 3 {
		t.Fatalf("aliasMap[b] = %v, want exactly a, b, c", m)
	}
	set := map[string]bool{}
	for _, id := range m {
		set[id] = true
	}
	if len(set) != 3 || !set["a"] || !set["b"] || !set["c"] {
		t.Errorf("aliasMap[b] = %v, want exactly a, b, c with no duplicates", m)
	}
	sorted := append([]string(nil), m...)
	sort.Strings(sorted)
	if !reflect.DeepEqual(sorted, []string{"a", "b", "c"}) {
		t.Errorf("sorted aliasMap[b] = %v", sorted)
	}
}
