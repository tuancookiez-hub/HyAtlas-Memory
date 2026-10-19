package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

// postAdd calls handleAdd with a JSON body and decodes the reply.
func postAdd(srv *Server, body string) (int, map[string]any) {
	w := httptest.NewRecorder()
	srv.handleAdd(w, httptest.NewRequest("POST", "/api/v1/add", strings.NewReader(body)))
	var out map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return w.Code, out
}

// The doctor's round-trip row is stored but never extracted: no model call, no
// extract_wanted marker for the catch-up loop, and the status says skipped. An
// ordinary pro-mode write through the same stub does call the model.
func TestAddDoctorOriginSkipsExtraction(t *testing.T) {
	var calls atomic.Int32
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"facts\":[],\"summary\":{\"text\":\"s\"},\"knowledge\":[],\"schemas\":[],\"intention\":null}"}}]}`))
	}))
	defer stub.Close()
	srv := newTestServer(t, "test-model", stub.URL)
	srv.mode = ModePro
	srv.llm = NewLLMClient(stub.URL, "k", "test-model")

	code, out := postAdd(srv, `{"text":"hyatlas doctor check ab12","user_id":"hyatlas-doctor","agent_id":"doctor","metadata":{"write_origin":"doctor"}}`)
	if code != 200 || out["success"] != true || out["extraction_status"] != "skipped" {
		t.Fatalf("doctor add: %d %v", code, out)
	}
	if n := calls.Load(); n != 0 {
		t.Fatalf("doctor add made %d model call(s), want 0", n)
	}
	id, _ := out["memory_id"].(string)
	docs := srv.store.GetMany([]string{id})
	if len(docs) != 1 {
		t.Fatalf("doctor row was not stored: %v", docs)
	}
	if docs[0].Meta[metaExtractWanted] != "" {
		t.Errorf("doctor row carries extract_wanted, so catch-up would extract it")
	}

	code, out = postAdd(srv, `{"text":"an ordinary turn","user_id":"u","agent_id":"a"}`)
	if code != 200 || out["extraction_status"] != "done" || calls.Load() != 1 {
		t.Fatalf("ordinary add: %d %v, calls %d", code, out, calls.Load())
	}
}
