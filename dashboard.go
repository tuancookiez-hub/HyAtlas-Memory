package main

import (
	"embed"
	"io/fs"
	"net/http"
)

// HTTP frontend for HyAtlas v4. A single-page dashboard that renders from the
// real /api/v1 endpoints — no mock data. Tabs: Overview, Layers, Explore,
// Graph, Activity. Night-mode-first (dark) like the rest of the workspace.
//
//go:embed dashboard/dist
var dashboardFS embed.FS

// handleDashboard serves the SPA shell + static assets, rooted at dashboard/dist.
func (s *Server) handleDashboard() http.Handler {
	sub, err := fs.Sub(dashboardFS, "dashboard/dist")
	if err != nil {
		return http.NotFoundHandler()
	}
	files := http.FileServer(http.FS(sub))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Serve index.html bytes directly for the bare path. Using FileServer for
		// "/" causes a canonical 301 redirect loop (FileServer redirects
		// /index.html -> / and back). So we read the shell and write it ourselves.
		if r.URL.Path == "/" || r.URL.Path == "" {
			data, err := fs.ReadFile(sub, "index.html")
			if err != nil {
				http.Error(w, "dashboard missing", http.StatusInternalServerError)
				return
			}
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write(data)
			return
		}
		files.ServeHTTP(w, r)
	})
}
