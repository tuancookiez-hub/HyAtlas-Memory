package main

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/tuancookiez-hub/hyatlas-v4/memory"
)

// A fact pile cannot say what is current: "extraction uses poolside" and
// "extraction uses stepfun" are two live rows until something decides. An entity
// page is the decision, kept: one page per thing that matters, holding its
// current state as attribute/value pairs and an append-only dated history of what
// it used to be. Extraction updates a page in the same call that writes facts, so
// the old and the new value meet at the one moment the model sees both.

const (
	// pageRelatedMin is the similarity a page needs to be shown to extraction or
	// recall. Pages are short and named, so a match below 0.60 is a stranger.
	pageRelatedMin = 0.60
	// pageUpdatesMax caps the page updates honoured from one extraction.
	pageUpdatesMax = 3
	// pageStateMax and pageHistoryMax bound a page so one busy project cannot grow
	// without limit; the oldest attribute or event is dropped first.
	pageStateMax   = 12
	pageHistoryMax = 200
	// pageAliasMax bounds the alternative names kept per page.
	pageAliasMax = 8
	// pageTextChars caps each stored name, attribute, value and event.
	pageTextChars = 300
)

// PageState is one current attribute of a page.
type PageState struct {
	Attr   string `json:"attr"`
	Value  string `json:"value"`
	Since  string `json:"since"`
	Source string `json:"source"`
}

// PageEvent is one dated entry in a page's append-only history.
type PageEvent struct {
	At     string `json:"at"`
	Text   string `json:"text"`
	Source string `json:"source"`
}

// Page is one thing that matters (a project, tool, person, place, organisation)
// with its current state and dated history.
type Page struct {
	ID      string      `json:"id"`
	UserID  string      `json:"user_id"`
	AgentID string      `json:"agent_id"`
	Name    string      `json:"name"`
	Kind    string      `json:"kind"`
	Aliases []string    `json:"aliases,omitempty"`
	State   []PageState `json:"state,omitempty"`
	History []PageEvent `json:"history,omitempty"` // append-only
	Updated string      `json:"updated"`           // RFC3339
	// DocID is the live l8_entity document for this page. Every re-index writes a
	// new document and supersedes the previous one, so older versions stay
	// recoverable but only the current one is searched.
	DocID string `json:"doc_id,omitempty"`
}

// PageSet is one attribute the model says is currently true of a page.
type PageSet struct {
	Attr  string `json:"attr"`
	Value string `json:"value"`
}

// PageUpdate is what extraction (and later the backlog pass) asks for.
type PageUpdate struct {
	Page  string    `json:"page"` // an existing page id that was shown, or "new"
	Name  string    `json:"name"`
	Kind  string    `json:"kind"` // project|tool|person|place|org|concept|other
	Set   []PageSet `json:"set"`
	Event string    `json:"event"`
}

var pageKinds = map[string]bool{"project": true, "tool": true, "person": true, "place": true,
	"org": true, "concept": true, "other": true}

// PageStore keeps the pages in memory, persists them to pages.json and mirrors
// each into the l8_entity layer so recall and extraction can find them by meaning.
type PageStore struct {
	store *MemoryStore
	path  string

	// applyMu serializes Apply end to end, because re-indexing embeds text and
	// must not interleave with another update to the same page. mu guards the map.
	applyMu sync.Mutex
	mu      sync.RWMutex
	pages   map[string]*Page
}

// NewPageStore opens <dataDir>/pages.json, loading it when present.
func NewPageStore(dataDir string, store *MemoryStore) (*PageStore, error) {
	p := &PageStore{store: store, path: filepath.Join(dataDir, "pages.json"), pages: map[string]*Page{}}
	b, err := os.ReadFile(p.path)
	if os.IsNotExist(err) || (err == nil && len(strings.TrimSpace(string(b))) == 0) {
		return p, nil
	}
	if err != nil {
		return nil, err
	}
	var list []*Page
	if err := json.Unmarshal(b, &list); err != nil {
		return nil, fmt.Errorf("pages.json: %w", err)
	}
	for _, pg := range list {
		if pg != nil && pg.ID != "" {
			p.pages[pg.ID] = pg
		}
	}
	return p, nil
}

// pageText is what gets embedded for a page: its name, aliases and current state.
// History is left out on purpose: it describes what is no longer true, and would
// pull a search for today's value toward yesterday's.
func pageText(pg Page) string {
	var b strings.Builder
	b.WriteString(pg.Name)
	if len(pg.Aliases) > 0 {
		b.WriteString(" (also: " + strings.Join(pg.Aliases, ", ") + ")")
	}
	for _, s := range pg.State {
		b.WriteString("\n" + s.Attr + ": " + s.Value)
	}
	return b.String()
}

// pageDocID names one version of a page's index document: the page id plus a
// short hash of its text, so an unchanged text keeps its id.
func pageDocID(pg Page) string {
	sum := sha1.Sum([]byte(pageText(pg)))
	return pg.ID + "-" + hex.EncodeToString(sum[:])[:8]
}

// Get returns a copy of the page with this id.
func (p *PageStore) Get(id string) (Page, bool) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	pg, ok := p.pages[id]
	if !ok {
		return Page{}, false
	}
	return clonePage(*pg), true
}

// List returns the owner's pages, newest Updated first.
func (p *PageStore) List(user, agent string) []Page {
	p.mu.RLock()
	defer p.mu.RUnlock()
	var out []Page
	for _, pg := range p.pages {
		if pg.UserID == user && pg.AgentID == agent {
			out = append(out, clonePage(*pg))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Updated != out[j].Updated {
			return out[i].Updated > out[j].Updated
		}
		return out[i].ID < out[j].ID
	})
	return out
}

// Related returns up to n of the owner's pages most similar to text, best first,
// keeping only those scoring at least pageRelatedMin. The owner filter is exact
// (an empty user matches only ownerless pages) so a page never crosses owners.
func (p *PageStore) Related(text, user, agent string, n int) []Page {
	if p == nil || n <= 0 || strings.TrimSpace(text) == "" {
		return nil
	}
	qv, err := p.store.embed.Embed(p.store.ctx, text)
	if err != nil {
		log.Printf("pages: related: embed: %v", err)
		return nil
	}
	hits, err := p.store.searchWhere(qv, n*3, memory.L8Entity, map[string]string{"user_id": user, "agent_id": agent})
	if err != nil {
		log.Printf("pages: related: %v", err)
		return nil
	}
	var out []Page
	seen := map[string]bool{}
	for _, h := range hits {
		if float64(h.Score) < pageRelatedMin {
			continue
		}
		id := h.Meta["page_id"]
		if id == "" || seen[id] {
			continue
		}
		pg, ok := p.Get(id)
		if !ok || pg.UserID != user || pg.AgentID != agent {
			continue
		}
		seen[id] = true
		out = append(out, pg)
		if len(out) == n {
			break
		}
	}
	return out
}

// Apply writes the page updates one extraction asked for and returns how many
// pages it created and changed. At most pageUpdatesMax updates are honoured. An
// update names a page that was shown to the model (shown) or "new"; any id the
// model was not shown is treated as new, and "new" first looks for an existing
// page of the same owner with that exact normalized name or alias, so a repeat
// mention never creates a duplicate. A failed update is logged and does not stop
// the next; the first error is returned.
func (p *PageStore) Apply(user, agent, sourceID string, ups []PageUpdate, shown map[string]bool) (created, updated int, err error) {
	if p == nil {
		return 0, 0, nil
	}
	p.applyMu.Lock()
	defer p.applyMu.Unlock()
	if len(ups) > pageUpdatesMax {
		ups = ups[:pageUpdatesMax]
	}
	for _, up := range ups {
		isNew, changed, e := p.applyOne(user, agent, sourceID, up, shown)
		if e != nil {
			log.Printf("pages: apply %q: %v", up.Name, e)
			if err == nil {
				err = e
			}
		}
		switch {
		case isNew:
			created++
		case changed:
			updated++
		}
	}
	return created, updated, err
}

// applyOne applies a single update; isNew means a page was created.
func (p *PageStore) applyOne(user, agent, sourceID string, up PageUpdate, shown map[string]bool) (isNew, changed bool, err error) {
	name := utf8Trunc(strings.TrimSpace(up.Name), pageTextChars)
	now := time.Now().UTC().Format(time.RFC3339)

	p.mu.Lock()
	pg := p.findLocked(user, agent, up, shown, name)
	if pg == nil {
		if normalizeName(name) == "" {
			p.mu.Unlock()
			return false, false, nil
		}
		pg = &Page{ID: "pg-" + newID(), UserID: user, AgentID: agent, Name: name, Updated: now}
		p.pages[pg.ID] = pg
		isNew, changed = true, true
	} else if name != "" && !strings.EqualFold(name, pg.Name) {
		// Found by id or by a normalized match under a different spelling:
		// remember the spelling as an alias.
		changed = p.addAliasLocked(pg, name) || changed
	}
	if pg.Kind == "" {
		if k := strings.ToLower(strings.TrimSpace(up.Kind)); pageKinds[k] {
			pg.Kind = k
			changed = true
		} else if isNew {
			pg.Kind = "other"
		}
	}
	for _, set := range up.Set {
		if p.setLocked(pg, set, sourceID, now) {
			changed = true
		}
	}
	if ev := utf8Trunc(strings.TrimSpace(up.Event), pageTextChars); ev != "" {
		p.appendLocked(pg, PageEvent{At: now, Text: ev, Source: sourceID})
		changed = true
	}
	if !changed {
		p.mu.Unlock()
		return false, false, nil
	}
	pg.Updated = now
	snap := clonePage(*pg)
	perr := p.persistLocked()
	p.mu.Unlock()

	if perr != nil {
		err = perr
	}
	if e := p.reindex(snap); e != nil && err == nil {
		err = e
	}
	return isNew, true, err
}

// appendLocked adds an event to the history, dropping the oldest past the cap.
func (p *PageStore) appendLocked(pg *Page, ev PageEvent) {
	pg.History = append(pg.History, ev)
	if over := len(pg.History) - pageHistoryMax; over > 0 {
		pg.History = append([]PageEvent(nil), pg.History[over:]...)
	}
}

// findLocked resolves an update to an existing page of this owner: a shown id
// first, otherwise an exact normalized match on the name or an alias.
func (p *PageStore) findLocked(user, agent string, up PageUpdate, shown map[string]bool, name string) *Page {
	if id := strings.TrimSpace(up.Page); id != "" && id != "new" && shown[id] {
		if pg, ok := p.pages[id]; ok && pg.UserID == user && pg.AgentID == agent {
			return pg
		}
	}
	if normalizeName(name) == "" {
		return nil
	}
	var best *Page
	for _, pg := range p.pages {
		if pg.UserID != user || pg.AgentID != agent || !namesPage(pg, name) {
			continue
		}
		// Map order is random; pick the same page every time.
		if best == nil || pg.ID < best.ID {
			best = pg
		}
	}
	return best
}

// namesPage reports whether name normalizes to the page's name or an alias.
func namesPage(pg *Page, name string) bool {
	want := normalizeName(name)
	if want == "" {
		return false
	}
	if normalizeName(pg.Name) == want {
		return true
	}
	for _, a := range pg.Aliases {
		if normalizeName(a) == want {
			return true
		}
	}
	return false
}

// addAliasLocked records name as an alternative name unless it is already the
// name or an alias (case-insensitively) or the alias list is full.
func (p *PageStore) addAliasLocked(pg *Page, name string) bool {
	if strings.EqualFold(name, pg.Name) || len(pg.Aliases) >= pageAliasMax {
		return false
	}
	for _, a := range pg.Aliases {
		if strings.EqualFold(a, name) {
			return false
		}
	}
	pg.Aliases = append(pg.Aliases, name)
	return true
}

// setLocked applies one attribute. A different value for a known attribute moves
// the old value into history as "attr: old → new"; an identical value is a no-op.
func (p *PageStore) setLocked(pg *Page, set PageSet, sourceID, now string) bool {
	attr := utf8Trunc(strings.TrimSpace(set.Attr), pageTextChars)
	val := utf8Trunc(strings.TrimSpace(set.Value), pageTextChars)
	if attr == "" || val == "" {
		return false
	}
	for i := range pg.State {
		if !strings.EqualFold(pg.State[i].Attr, attr) {
			continue
		}
		if pg.State[i].Value == val {
			return false
		}
		p.appendLocked(pg, PageEvent{At: now, Source: sourceID,
			Text: fmt.Sprintf("%s: %s → %s", pg.State[i].Attr, pg.State[i].Value, val)})
		pg.State[i].Value, pg.State[i].Since, pg.State[i].Source = val, now, sourceID
		return true
	}
	pg.State = append(pg.State, PageState{Attr: attr, Value: val, Since: now, Source: sourceID})
	if len(pg.State) > pageStateMax {
		// Drop the attribute that has been true the longest; the earliest index
		// wins a tie, which is never the one just added.
		oldest := 0
		for i := range pg.State {
			if pg.State[i].Since < pg.State[oldest].Since {
				oldest = i
			}
		}
		pg.State = append(pg.State[:oldest], pg.State[oldest+1:]...)
	}
	return true
}

// persistLocked writes pages.json through a temp file and a rename, so a crash
// never leaves a half-written file.
func (p *PageStore) persistLocked() error {
	list := make([]*Page, 0, len(p.pages))
	for _, pg := range p.pages {
		list = append(list, pg)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].ID < list[j].ID })
	b, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return err
	}
	tmp := p.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, p.path)
}

// reindex makes the page's current text the one live l8_entity document. The new
// version is added under a new id first and the previous one superseded after,
// so a failure between the two leaves the page findable rather than missing.
func (p *PageStore) reindex(pg Page) error {
	docID := pageDocID(pg)
	if docID == pg.DocID {
		return nil
	}
	err := p.store.Add(memory.L8Entity, docID, pageText(pg), map[string]string{
		"user_id": pg.UserID, "agent_id": pg.AgentID, "ts": pg.Updated,
		"page_id": pg.ID, "name": pg.Name,
	})
	if err != nil {
		return fmt.Errorf("index page %s: %w", pg.ID, err)
	}
	if pg.DocID != "" {
		if _, err := p.store.Supersede([]string{pg.DocID}, docID); err != nil {
			log.Printf("pages: supersede old version of %s: %v", pg.ID, err)
		}
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if cur, ok := p.pages[pg.ID]; ok {
		cur.DocID = docID
		return p.persistLocked()
	}
	return nil
}

func clonePage(pg Page) Page {
	pg.Aliases = append([]string(nil), pg.Aliases...)
	pg.State = append([]PageState(nil), pg.State...)
	pg.History = append([]PageEvent(nil), pg.History...)
	return pg
}

// pageUpdates is the tolerant list form of Extraction.Pages. Small models send
// one object instead of a list, or put junk in it; a decode error would throw
// away the whole extraction over a field that only adds to it, so this accepts a
// list or a single object, skips anything that is not an update, and never errors.
type pageUpdates []PageUpdate

// UnmarshalJSON implements the tolerant decode described on pageUpdates.
func (l *pageUpdates) UnmarshalJSON(b []byte) error {
	*l = nil
	var many []json.RawMessage
	if err := json.Unmarshal(b, &many); err != nil {
		many = []json.RawMessage{b}
	}
	for _, raw := range many {
		var up PageUpdate
		if json.Unmarshal(raw, &up) == nil && (up.Name != "" || up.Page != "") {
			*l = append(*l, up)
		}
	}
	return nil
}

// UnmarshalJSON reads each field loosely: a field of the wrong type is ignored
// rather than failing the update, and "set" may be a list of {attr,value}.
func (u *PageUpdate) UnmarshalJSON(b []byte) error {
	*u = PageUpdate{}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	str := func(key string) string {
		var s string
		if json.Unmarshal(raw[key], &s) != nil {
			return ""
		}
		return s
	}
	u.Page, u.Name, u.Kind, u.Event = str("page"), str("name"), str("kind"), str("event")
	var sets []json.RawMessage
	if json.Unmarshal(raw["set"], &sets) != nil {
		if one, ok := raw["set"]; ok {
			sets = []json.RawMessage{one}
		}
	}
	for _, s := range sets {
		var ps PageSet
		if json.Unmarshal(s, &ps) == nil && ps.Attr != "" {
			u.Set = append(u.Set, ps)
		}
	}
	return nil
}

// UnmarshalJSON accepts a value of any scalar type, since a model may send
// 4.6 or true rather than a string.
func (s *PageSet) UnmarshalJSON(b []byte) error {
	var obj struct {
		Attr  string `json:"attr"`
		Value any    `json:"value"`
	}
	if err := json.Unmarshal(b, &obj); err != nil {
		return err
	}
	s.Attr = obj.Attr
	switch v := obj.Value.(type) {
	case string:
		s.Value = v
	case float64, bool:
		s.Value = fmt.Sprint(v)
	}
	return nil
}
