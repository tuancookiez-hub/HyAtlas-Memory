package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"time"
)

func ctxForTest() context.Context {
	return context.Background()
}

func timeForTest() time.Time {
	return time.Now()
}

// localRequest builds a request the way a loopback client sends it. guardLocal
// refuses httptest's default Host (example.com), so a test that goes through
// routes() must name a loopback Host.
func localRequest(method, target string, body io.Reader) *http.Request {
	r := httptest.NewRequest(method, target, body)
	r.Host = "127.0.0.1:19528"
	return r
}
