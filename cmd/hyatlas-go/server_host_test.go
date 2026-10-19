package main

import (
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

// An empty Host (HTTP/1.0 without one) names no other host, so it is served.
func TestGuardAllowsEmptyHost(t *testing.T) {
	if !hostAllowed("", nil) {
		t.Error("empty Host refused, want allowed as local")
	}
	req := httptest.NewRequest("GET", "/healthz", nil)
	req.Host = ""
	w := httptest.NewRecorder()
	guardLocal(nil, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})).ServeHTTP(w, req)
	if w.Code != http.StatusNoContent {
		t.Errorf("guard status = %d for an empty Host, want 204", w.Code)
	}
}

// HYATLAS_ALLOWED_HOSTS entries may carry a port, a trailing dot or capitals, and
// still match the bare host a request sends.
func TestParseHostListStripsPortAndNormalises(t *testing.T) {
	got := parseHostList(" myhost:8080 , Other.Example. ,, [fe80::1]:9000 ,")
	want := []string{"myhost", "other.example", "fe80::1"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("parseHostList = %v, want %v", got, want)
	}
	if !hostAllowed("myhost:19528", got) {
		t.Error("allowlisted host with a port in its entry is refused")
	}
}

// An IPv6 bind address must produce a valid listen address, and the banner must
// show the same one.
func TestListenAddressJoinsIPv6Host(t *testing.T) {
	clearRuntimeEnv(t)
	t.Setenv("HYATLAS_GO_HOST", "::1")
	rt := resolveRuntime()
	if rt.Host != "::1" {
		t.Fatalf("Host = %q, want ::1", rt.Host)
	}
	addr := net.JoinHostPort(rt.Host, rt.Port)
	if addr != "[::1]:"+rt.Port {
		t.Errorf("listen address = %q, want [::1]:%s", addr, rt.Port)
	}
	if _, _, err := net.SplitHostPort(addr); err != nil {
		t.Errorf("listen address %q does not split: %v", addr, err)
	}
	if line := listeningLine(rt); !strings.Contains(line, "[::1]:"+rt.Port) {
		t.Errorf("banner does not show the bind address: %q", line)
	}

	t.Setenv("HYATLAS_GO_HOST", "[::1]")
	if h := resolveRuntime().Host; h != "::1" {
		t.Errorf("bracketed host resolved to %q, want ::1", h)
	}
}
