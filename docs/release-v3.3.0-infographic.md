# v3.3.0 release infographic — match v3.2.1 layout

**Reference asset (yours):** v3.2.1 infographic — `1672×941`, dark HyAtlas brand, evolution story.  
**New asset:** `assets/hyatlas-v3.3.0-quality-metrics.png` — **same canvas size and visual system**, swap story to Quality Metrics.

> I couldn’t render your PNG in-tool on this host; this spec maps **slot-for-slot** off the v3.2.1 README narrative (*second brain that evolves — capture, weekly digest, graph patterns*) and your 3.2.1 graphic. Reuse fonts, margins, icon style, and bottom pill bar from 3.2.1.

---

## Canvas & style (lock to 3.2.1)

| Property | Value |
|----------|--------|
| Size | **1672 × 941** px |
| Background | Near-black `#050505`–`#0a0e1a`, subtle grid or vignette if 3.2.1 had it |
| Title font | Serif display (Playfair-class) — same as 3.2.1 |
| Body | Inter / system sans |
| Data | JetBrains Mono for token counts & API paths |
| Accents | Green `#4ade80` (healthy/scores), blue `#4a6fa5` (data), purple for graph if 3.2.1 used it |

---

## Slot map: v3.2.1 → v3.3.0

| Region (3.2.1) | 3.2.1 copy theme | **v3.3.0 replacement** |
|----------------|------------------|-------------------------|
| **Top title** | HyAtlas **v3.2.1** (or v3.2) | HyAtlas **v3.3.0** |
| **Tagline** | Second brain that **evolves** | **Measure memory, not vibes** |
| **Sub-tag** | Capture · weekly digest · graph patterns | **Quality Metrics tab** · 7d LLM tokens · weekly baseline |
| **Center flow (3 nodes)** | ① Capture → ② Weekly digest → ③ Graph / L6 patterns | ① **Memory writes** (S1 extract) → ② **7d rollup** (scores + `llm_tokens`) → ③ **Save baseline** (week-over-week Δ) |
| **Side / layer strip** | 6 active layers + L7 experimental | **4 score cards:** Composite · Evolution · Activity · Latency (mini dashboard mock) |
| **Stack pills (bottom)** | Zvec · Kuzu · Hermes · digest cron | **Same pills** + add **:8765 Quality Metrics** |
| **Callout box** | L6 visible / graph counts (if on 3.2.1) | **Reference (published):** 35% ctx tokens ↓ · 25% memories ↓ · 88% utility ↑ — *industry ref, not your stack* |

---

## Exact text blocks (paste into Figma / generator)

### Header
```
HyAtlas v3.3.0
Measure memory, not vibes
Quality Metrics — new dashboard tab
```

### Center arrows (three nodes)
```
① CAPTURE
Every chat → facts in Zvec (System1)
LLM tokens counted per write

② ROLLUP (7 DAYS)
Composite · Evolution · Activity · Latency
GET /api/quality-metrics

③ BASELINE
Save snapshot → compare next week
Δ VDB · L6 · relations · tokens
```

### Right or lower panel — reference
```
Published benchmarks (Tencent Hy-Memory)
Not measured on your instance

35%  fewer context tokens
25%  fewer memories
88%  long-term utility

Your instance: live scores on dashboard
```

### Bottom strip
```
Zvec  ·  Kuzu L5–L7  ·  Hermes  ·  weekly digest  ·  localhost:8765
```

### Optional live footer (from curl before export)
```
L6 · relations · digest · composite score
```
Fill from: `curl -s http://127.0.0.1:8765/api/quality-metrics`

---

## README / Release wiring

After PNG is done:

```html
<img src="./assets/hyatlas-v3.3.0-quality-metrics.png"
     alt="HyAtlas v3.3.0: Quality Metrics — 7d token rollup, dashboard scores, weekly baseline"
     width="720" />
```

Place **above** or **replace** the v3.2 second-brain image in README for the current release highlight (keep 3.2 image in CHANGELOG or older section if you want history).

**GitHub Release:** tag `v3.3.0`, attach PNG, body from `CHANGELOG.md` [3.3.0].

---

## image_generate prompt (sibling of 3.2.1)

Use if you want Hermes/FAL to draft; then **tweak in Figma** to match 3.2.1 pixel-perfect.

```
Infographic 1672x941, dark navy-black background, matching prior HyAtlas v3.2 release poster style: large serif title "HyAtlas v3.3.0", subtitle "Measure memory not vibes", horizontal three-step flow with rounded cards and arrows: (1) Memory writes System1 token count (2) 7-day rollup dashboard scores (3) Save weekly baseline delta, right side four small metric cards COMPOSITE EVOLUTION ACTIVITY LATENCY, lower right box "Reference 35% 25% 88%" with small disclaimer industry benchmarks, bottom row pill badges Zvec Kuzu Hermes digest port 8765, mint green and steel blue accents, clean tech aesthetic, no watermark
```

---

## Social

**X:** HyAtlas v3.3.0 — same evolution story as 3.2.1, now with **numbers**: Quality Metrics tab, 7d LLM token rollup on writes, one-click weekly baseline. Ref benchmarks labeled honestly. [PNG] github.com/tuancookiez-hub/HyAtlas-Memory