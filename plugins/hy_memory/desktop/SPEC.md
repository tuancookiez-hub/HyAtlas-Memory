# HyAtlas Mind Palace — Temporal Memory Map (v2.0)

**Spec version:** 2.0
**Date:** 2026-09-04
**Status:** Draft — awaiting Tuna's review and approval before implementation
**Target:** Hermes Desktop pane (`hy_memory` plugin)
**References:**
- [Hermes Desktop starmap](https://github.com/tuancookiez-hub/hermes-agent/tree/main/apps/desktop/src/app/starmap) — the proven implementation we are adapting
- [Utopia](https://github.com/deeplethe/utopia) — bitemporal world model (data layer only)

---

## 1. Concept & Vision

HyAtlas has a memory system — 7 layers, a semantic graph, usage counters. What it lacks is a way to *feel* that memory as a space. This spec adds a temporal visualization that takes the proven approach from Hermes Desktop's built-in **starmap** and adapts it for HyAtlas.

The starmap is the reference — same architecture, same techniques, different data source. Looking at the actual implementation (`apps/desktop/src/app/starmap/`), five things make it work:

1. **Time is radial** — oldest at the core, newest on the outer rings. This is the visual metaphor.
2. **D3-force simulation** with `forceRadial` as the dominant force — nodes float around their date ring instead of pinning to it like beads.
3. **Static scene cached, animated core** — the "scramble" effect in the empty core keeps the loop alive cheaply. Static ring/band/node drawing only re-renders when something changed.
4. **Constellation-style scrubber** — a starfield along the timeline bottom that brightens as the scrubber passes, not a boring `<input type="range">`.
5. **Cinematic 15s sweep** — when the user hits play, the whole map reveals itself with smooth ease in/out. The reveal ratio drives fade buckets for nodes/links/rings.

We use the same five techniques over HyAtlas data. The visual language is identical (so it feels native to the desktop app), but the data model is HyAtlas's 7-layer + L5 graph instead of memory+skill.

---

## 2. What Exists vs What Is New

### Current HyAtlas pane
| Tab | What it shows |
|---|---|
| Overview | 4 health dots + 7 layer bars + 4 stat cards (writes, searches, VDB points, embed dims) |
| Memories | Paginated memory list, filterable by layer |
| Search | Semantic recall |
| Add | Manual memory write |

### New: Mind Palace tab
A fifth tab — toggle **List / Spatial** in the header. Spatial view is the temporal map.

---

## 3. Data Model

### 3.1 What a HyAtlas memory becomes on the map

Every memory in HyAtlas v4 has:
- `id` (string)
- `content` (text)
- `layer` (L0..L7)
- `gmt_created` (timestamp, seconds)
- `session_id` (string, optional)

We map this to a starmap node:
```ts
{
  id: memory.id,
  kind: layer,                    // L0..L7 → visual kind
  label: content.slice(0, 60),
  timestamp: memory.gmt_created,
  useCount: 1,                    // could be enriched with access count
  state: 'active' | 'archived',   // could be enriched
  pinned: false,                  // could be enriched
}
```

### 3.2 L5 graph → links

L5 graph edges become links between nodes. Both endpoints must be present in the node list. Edge weight = occurrence count (count of identical relations).

### 3.3 Time axis

**Bucketing:** Choose a calendar unit (day, week, month, etc.) so the time axis has 5–12 buckets. The starmap's `chooseUnit` function does this — same algorithm, just operate on HyAtlas memory timestamps.

**Recency ratio:** `(gmt_created - minTs) / (maxTs - minTs)` clamped to `[0, 1]`. `null` for undated memories → falls back to ordinal position.

### 3.4 Bitemporal (optional, lifted from Utopia)

If we add `valid_from` and `valid_to` to the L5 graph edges (Utopia pattern), the scrubber gains a bitemporal mode:
- World axis (validity): filter edges where `valid_from <= scrubber <= valid_to`
- Recording axis: filter edges where `recorded_at <= scrubber AND (invalidated_at IS NULL OR invalidated_at > scrubber)`

For v1, the simpler "single timestamp" mode is fine — bitemporal can be added without UI changes when the Go server supports it.

---

## 4. UI Specification

### 4.1 View toggle

In the HyAtlas pane header:
```
[Overview] [Memories] [Search] [Add]    [○ List  ● Spatial]
```

`SegmentedControl` with two options. When spatial is active, the tab bar collapses and the canvas fills the pane.

### 4.2 Canvas (adapted from starmap)

**Container:** `<div>` with `ResizeObserver` (re-sizes canvas on pane drag). DPR-aware backing store. Pause the render loop when the window is hidden/unfocused (saves CPU).

**Renderer:** HTML5 `<canvas>` 2D, not SVG. The starmap uses canvas because there are up to 1000+ nodes with fade animations — SVG would re-render the DOM on every frame. Canvas lets us re-blit the cached static layer (1 drawImage) per frame instead of redrawing.

**Static scene (cached offscreen, re-rendered only on dirty):**
- Ring outlines + band washes (5–12 rings, dated labels)
- Node glyphs (one shape per layer kind)
- Link curves between connected nodes
- Label pills above selected/hovered node

**Animated core (per-frame, cheap):**
- Empty-core "scramble" — JetBrains Mono character soup that animates in the inner disk when there are no memories yet, or in the unswept region during playback

**Composite order flips on focus:**
- Idle: static scene first, scramble on top (so the core wash dims the busy center)
- Focused/hovered: scramble first, scene on top (so the active node's tooltip + lit lines lift ABOVE the wash)

**Render loop throttling:** Cap to 30fps when idle. Force immediate redraw on interaction (force simulation tick, pan, zoom, scrub).

### 4.3 Layer kind → node glyph

Adapted from starmap's `memory: diamond, skill: circle`:

```
L0 raw         hexagon  (sensory, raw, irregular)
L1 profile     square   (stable, persistent)
L2 episodic    triangle (events, moments)
L3 factual     circle   (truth, atomic)
L4 summary     diamond  (compressed, refined)
L5 knowledge   hexagon  (graph nodes)
L6 schema      square   (structure)
L7 intention   triangle (goals, direction)
```

### 4.4 Color palette

Use theme-driven colors (resolve via `getComputedStyle()` for `--ui-accent`, `--ui-text-secondary`, etc.), not hardcoded. The starmap computes the palette once per theme change.

Per-layer accent (subtle, not loud):
```
L0 raw         indigo    #6366F1
L1 profile     violet    #8B5CF6
L2 episodic    purple    #A855F7
L3 factual     pink      #EC4899
L4 summary     orange    #F97316
L5 knowledge   green     #22C55E  ← graph edges: #16A34A
L6 schema      teal      #14B8A6
L7 intention   amber     #F59E0B
```

Ages (recency) follow the starmap's smoothstep gradient: old = dim, new = bright.

### 4.5 Detail card (on fact selection)

```
┌─────────────────────────────────────────────────┐
│ L3 Fact                                2d ago  │
│ ────────────────────────────────────────────  │
│ Bitcoin ETF approved by SEC on Jan 10, 2024   │
│                                                 │
│ Source: L2 Episodic · Session #14              │
│         "The SEC announced today…"             │
│         [Go to source]                         │
│                                                 │
│ Connected: 3 nodes → 5 edges                   │
└─────────────────────────────────────────────────┘
```

### 4.6 Constellation scrubber (adapted from starmap)

Bottom of the canvas, identical to starmap's `Timeline.tsx`:

- A track with dim "stars" — each star is a memory, size and opacity scaled by bucket density
- Stars are positioned along the track by their `gmt_created`
- Two star colors: memory-colored vs skill-colored (in our case, layer-colored: L0–L3 cold, L4–L7 warm)
- Drag the playhead → stars to the LEFT of the playhead ignite (bright + twinkling)
- A "play" button sweeps the playhead left-to-right over 15s
- Spacebar toggles play/pause (when no input is focused)
- Ring-spawn anchor ticks at each dated ring boundary — light up as the playhead passes

The constellation is a much better visual than a `<input type="range">` because:
1. The stars telegraph the data distribution (you see clusters before you scrub)
2. Ignite-bright is a more honest metaphor than thumb-position
3. It feels native to the "tilted disk of memories" metaphor

### 4.7 L5 graph overlay (D3 force)

When zoom ≥ 2×, the L5 ring expands to show graph nodes in a spring layout. Edges are curves with arrow markers. Reuses the starmap's `forceSimulation` with `forceLink`, `forceManyBody`, `forceCollide`, `forceRadial` (radial fixed to the L5 ring's radius).

This is optional for v1 — the radial placement of L5 nodes by timestamp already gives a meaningful map. Add the force layout when we have enough L5 edges to make it worth visualizing.

---

## 5. Interaction Contract

| Action | Result |
|---|---|
| Toggle List/Spatial | Switch view |
| Hover node | Tooltip with content preview + layer + relative time |
| Click node | Select, highlight, show detail card |
| Drag empty canvas | Pan |
| Scroll wheel | Zoom (0.3× – 5×) |
| Double-click node | Open source memory in Memories tab |
| Click "Go to source" | Same |
| Drag scrubber | Rewind, fade unbuilt nodes/links |
| Click play / Spacebar | 15s cinematic sweep, full build-up |
| Click ring | Filter by layer, show that layer only |
| Right-click node | Context menu: "Copy", "Open source", "Find related" |

---

## 6. API Surface

### Already exists
- `GET /api/v1/status` — layers, writes, searches
- `GET /api/v1/memories?limit=500` — all memories
- `GET /api/graph-nodes?limit=500` — L5 graph nodes
- `GET /api/graph-edges?limit=1000` — L5 edges

### New endpoint needed
- `GET /api/v1/sessions` — returns `[{session_id, gmt_created, memory_count}]` for time bucketing

### Bitemporal additions (optional, future)
- Add `valid_from`, `valid_to`, `recorded_at`, `invalidated_at` to L5 graph edges
- Add `/api/v1/graph-edges?as_of=<timestamp>` for bitemporal queries

---

## 7. Component Inventory (adapted from starmap)

### Files to create in hy_memory plugin

| Component | File | Source in starmap | Notes |
|---|---|---|---|
| `MindPalace` | `src/mind-palace/mind-palace.tsx` | `star-map.tsx` | Root canvas component |
| `useTimeAxis` | `src/mind-palace/time-axis.ts` | `time-axis.ts` | Bucket memories by time |
| `useSimulation` | `src/mind-palace/simulation.ts` | `simulation.ts` | Build D3 force sim |
| `Scrubber` | `src/mind-palace/scrubber.tsx` | `timeline.tsx` | Constellation timeline |
| `constants.ts` | `src/mind-palace/constants.ts` | `constants.ts` | Disk geometry, palette defaults |
| `geometry.ts` | `src/mind-palace/geometry.ts` | `geometry.ts` | Hash, clamp, fit, radius |
| `render.ts` | `src/mind-palace/render.ts` | `render.ts` | Canvas drawScene + drawScramble |
| `types.ts` | `src/mind-palace/types.ts` | `types.ts` | SimNode, Viewport, Palette |

### Plugin changes
- `plugin.js` — add List/Spatial toggle, render MindPalace inside a new `<MindPalacePane>` component

### Adapted, not copied
- Hash function: same FNV-1a
- Radial force: same `forceRadial((n) => n.tr, 0, 0).strength(0.92)`
- Constellation timeline: same star scatter algorithm
- Render loop: same rAF cap at 30fps, same pause-on-blur

### Different from starmap
- Data source: HyAtlas v4 (L0..L7) instead of memory+skill
- 8 layer kinds instead of 2 — more glyphs
- 5–12 rings instead of fixed 4
- Bitemporal scrubber (when server supports it) instead of just timestamp

---

## 8. Technical Approach

### Stack
- **Renderer:** HTML5 `<canvas>` 2D (starmap's choice — SVG would re-render the DOM per frame for 1000+ nodes)
- **Layout:** `d3-force` via the SDK's React Query + d3 available as a dependency
- **Pan/zoom:** manual transform on a viewport state (starmap pattern)
- **Data:** React Query (`useQuery` from SDK) — 30s polling, since the map is a "loose" view
- **Render loop:** rAF with dirty flag, static layer cached offscreen

### Performance
- Fetch all memories (≤500) once on spatial tab mount
- L5 graph: fetch only when zoom ≥ 2× (lazy load)
- Scubber/zoom updates: filter in-memory, no re-fetch
- ResizeObserver on container
- Pause rAF on blur, resume on focus

### Storage
- `ctx.storage` for: view mode (list/spatial), last scrubber position
- `ctx.onDispose` for: ResizeObserver disconnect, rAF cancel, d3 sim.stop()

### Plugin file layout
```
desktop-plugins/hy_memory/
  plugin.js                          ← add spatial toggle, new tab
  src/mind-palace/
    mind-palace.tsx                   ← root
    time-axis.ts                      ← bucketing
    simulation.ts                     ← d3-force
    scrubber.tsx                      ← constellation timeline
    render.ts                         ← canvas drawing
    constants.ts                      ← geometry
    geometry.ts                       ← helpers
    types.ts                          ← SimNode etc
```

---

## 9. Implementation Phases

### Phase 1 — Bring up the canvas (1 day)
- Port `star-map.tsx` skeleton over to plugin
- Empty canvas with rings + scramble effect
- Fetches memories, builds time axis, populates ring positions
- No scrubbing yet — just static view at full reveal

### Phase 2 — Constellation scrubber (1 day)
- Port `Timeline.tsx` over
- Wire `revealStore` (atom) into the canvas
- Wire play/pause, spacebar, drag
- Verify cinematic 15s sweep

### Phase 3 — Interaction polish (1 day)
- Hover tooltip
- Click selection + detail card
- Pan/zoom
- Right-click context menu

### Phase 4 — L5 graph overlay (1 day)
- Lazy fetch graph edges on zoom ≥ 2×
- D3 force sim on the L5 ring
- Curve edges with arrows
- Color by relation type

### Phase 5 — Bitemporal mode (when server supports it)
- Add `valid_from/valid_to/recorded_at/invalidated_at` to Go server
- Add `/api/v1/graph-edges?as_of=<ts>` endpoint
- Switch scrubber to bitemporal mode (toggle in UI)

---

## 10. Open Questions for Tuna

1. **Bitemporal in v1 or v2?** — If we add `valid_from/valid_to` to the L5 graph store first, the scrubber gains "what was true at T" capability. If we ship the map first without it, the scrubber is "what existed at T". Utopia-quality requires the former. Cost is 2 days of Go work.
2. **L5 graph overlay — v1 or v4?** — The D3 force overlay on the L5 ring is a polish feature. The map works without it (radial placement of L5 nodes by timestamp is meaningful on its own).
3. **Constellation vs slider** — Confirm you want the starfield timeline (starmap style) instead of a plain `<input type="range">` slider. The constellation is more thematic but less "control-like".
4. **Layer accent colors** — Confirmed, or different palette?
5. **Desktop only** — Same as v1: lives in the desktop pane only, not the TUI or web dashboard.
6. **Persistence** — Pan/zoom/scrubber state survives pane reload via `ctx.storage`?

---

## 11. Out of Scope (v1)

- Write to memory from spatial view
- Multi-user / shared palaces
- L5 graph > 50 nodes on initial load
- Export spatial view as PNG
- Agent-authored nodes on the canvas (Mind Palace canvas owns that)
- TUI / web dashboard surface
