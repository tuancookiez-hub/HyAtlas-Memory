# v3.3.2 release infographic — copy & layout spec

Use for GitHub Release asset, X post, or `image_generate` prompt.  
**Canvas:** 1200×675 (16:9) or 1080×1350 (4:5 for X). **Style:** match HyAtlas dashboard — bg `#050505`, panels `#151b2b`, text `#c8d6e5`, accent `#4ade80` (scores), `#4a6fa5` (data), muted `#6b7280`.

---

## Headline (top, Playfair or similar)

**HyAtlas v3.3.2**  
*Measure memory, not vibes*

Sub: **Quality Metrics** — new dashboard tab · 7-day LLM token rollup · weekly baseline

---

## Left column — “What shipped”

1. **New sidebar tab: Quality Metrics**  
   Same nav as Overview / Settings — dedicated page, not buried in Settings.

2. **Your numbers (7-day window)**  
   - Composite / Evolution / Activity / Latency scores  
   - **LLM tokens** on memory writes (extract + reconcile)  
   - System1 writes · System2 digests · fresh L2 · graph L5/L6/L7  

3. **Save baseline**  
   One click → `~/.hyatlas/metrics/quality_baseline.json`  
   Next week: **Δ** on VDB, L6, relations, tokens.

4. **API**  
   `GET /api/quality-metrics` · `POST /api/quality-baseline`  
   `GET /api/v1/metrics` → `llm_tokens`

---

## Center — mock UI strip (optional visual)

Mini wireframe: sidebar item **Quality Metrics** highlighted → 4 stat cards:

| COMPOSITE | EVOLUTION | ACTIVITY | LATENCY |
|-----------|-----------|----------|---------|
| *live*    | *live*    | *live*   | *live*  |

Below: **LLM tokens (7d)** — large monospace number (pull from dashboard after restart).

---

## Right column — “Reference (published)”

Label clearly: **Industry reference — not measured on your instance**

| Metric | Ref % |
|--------|-------|
| Context tokens ↓ | **35%** |
| Memory count ↓ | **25%** |
| Long-term utility ↑ | **88%** |

Source line (small): Tencent Hy-Memory · OpenClaw integration (published).

---

## Bottom bar — stack reminder

**Zvec** · **Kuzu L5–L7** · **Hermes** `hermes-user` / `default` · **Weekly digest cron**

`hyatlas restart` → open **http://127.0.0.1:8765** → Quality Metrics → **Save baseline**

---

## Live numbers to paste (optional — refresh before export)

Run after `hyatlas restart` and one day of use:

```bash
curl -s http://127.0.0.1:8765/api/quality-metrics
```

Use from JSON:

- `snapshot.scores.composite`
- `snapshot.llm_tokens_7d.total`
- `snapshot.graph.l6` / `relations`
- `snapshot.fresh_l2_for_digest`
- `snapshot.digest_log_status`

Example footer line for graphic:  
*Your stack · L6 568 · relations 7922 · digest ok* (replace with curl output).

---

## GitHub Release

- **Tag:** `v3.3.2`  
- **Title:** `v3.3.2 — Quality Metrics dashboard`  
- **Asset filename:** `hyatlas-v3.3.2-quality-metrics.png`  
- **Body bullets:** copy from `CHANGELOG.md` [3.3.2] + link to this spec.

---

## Short X / social (≤280 chars template)

HyAtlas v3.3.2: new **Quality Metrics** tab on the dashboard — 7d LLM token rollup on memory writes, weekly baseline compare, plus published Hy-Memory ref benchmarks (labeled, not your numbers). Zvec + Kuzu + Hermes. `github.com/tuancookiez-hub/HyAtlas-Memory`

---

## image_generate prompt (optional)

Dark premium tech infographic, 16:9, title "HyAtlas v3.3.2 Quality Metrics", subtitle "Measure memory not vibes", three columns: left bullet features new dashboard tab token rollup baseline, center four score cards and large token counter, right three percentage badges 35% 25% 88% with disclaimer "industry reference", bottom strip Zvec Kuzu Hermes digest, colors black navy mint green accents, no fake logos, clean typography.