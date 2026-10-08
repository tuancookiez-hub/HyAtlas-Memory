package main

import (
	"testing"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// The dashboard sends "all" for every owner. It must mean no filter on the
// memory and schema lists, as it already does on the graph endpoints.
func TestDashboardOwnerFilterAllMeansAll(t *testing.T) {
	srv := newTestServer(t, "m", "http://127.0.0.1:1/v1")
	for _, who := range []string{"alice", "bob"} {
		if err := srv.store.Add(memory.L2Raw, who+"-raw", who+" raw", map[string]string{
			"user_id": who, "agent_id": "a1", "ts": "2026-01-02T03:04:05Z",
		}); err != nil {
			t.Fatal(err)
		}
		if err := srv.store.Add(memory.L6Schema, who+"-schema", who+" schema", map[string]string{
			"user_id": who, "agent_id": "a1", "ts": "2026-01-02T03:04:05Z",
		}); err != nil {
			t.Fatal(err)
		}
	}

	count := func(out map[string]any, bucket string) int {
		buckets := out["memories"].(map[string]any)
		return len(buckets[bucket].([]any))
	}
	all := dashGet(t, srv.handleDashMemories, "GET", "/api/memories?layer=l2_raw&user_id=all&agent_id=all", "")
	if n := count(all, "normal"); n != 2 {
		t.Errorf("user_id=all agent_id=all: %d memories, want 2", n)
	}
	alice := dashGet(t, srv.handleDashMemories, "GET", "/api/memories?layer=l2_raw&user_id=alice&agent_id=all", "")
	if n := count(alice, "normal"); n != 1 {
		t.Errorf("user_id=alice: %d memories, want 1", n)
	}

	l6all := dashGet(t, srv.handleDashL6Schemas, "GET", "/api/l6-schemas?n=10&user_id=all&agent_id=all", "")
	if n := len(l6all["schemas"].([]any)); n != 2 {
		t.Errorf("l6 all: %d schemas, want 2", n)
	}
	l6alice := dashGet(t, srv.handleDashL6Schemas, "GET", "/api/l6-schemas?n=10&user_id=alice", "")
	if n := len(l6alice["schemas"].([]any)); n != 1 {
		t.Errorf("l6 user_id=alice: %d schemas, want 1", n)
	}
}
