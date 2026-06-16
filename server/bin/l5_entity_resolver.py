"""
L5 Phase 2.4 — Entity resolution.

Input:  l5_digest_entities.json + l5_digest_relations.json (200-fact output)
Output: resolved entities + relations, with audit log of merges

Pipeline (4 passes):
  1. Case folding (Git ≡ git → Git)
  2. Alias map (TunaCookie ≡ TuanCookiez, MCP ≡ Model Context Protocol, etc.)
  3. Fuzzy clustering + LLM tiebreaker for ambiguous cases
  4. Noise filtering (UI sections, internal function names that are borderline)

Output:
  - logs/l5_resolved_entities.json:  {canonical_name, type, aliases, count, source_fact_ids, confidence}
  - logs/l5_resolved_relations.json:  {a, b, type, confidence, source_fact_id}  (with canonical names)
  - logs/l5_resolution_audit.json:    every merge decision (before → after, why)
  - logs/l5_resolution_stats.json:    before/after counts
"""
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
ENTITY_PATH = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs\l5_full_entities.json")
RELATION_PATH = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs\l5_full_relations.json")
CFG_PATH = Path(r"C:\Users\tuanc\AppData\Local\hermes\hy_memory.json")
OUT_DIR = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs")

cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
LLM_MODEL = cfg["llm"]["model"]
LLM_API_KEY = cfg["llm"]["api_key"]
LLM_BASE_URL = cfg["llm"]["base_url"].rstrip("/")
LLM_EXTRA = cfg["llm"].get("extra_body", {})

# ------------------------------------------------------------------
# Alias map (manual, extended from observed data)
# Format: lowercase form → canonical form
# ------------------------------------------------------------------
ALIAS_MAP = {
    # People
    "tunacookie":       "TunaCookie",
    "tuancookiez":       "TunaCookie",
    "tuan abdullah":     "TunaCookie",   # user's full name from IBKR memory
    "the user":          "TunaCookie",   # sometimes the LLM extracts "the user" — fold to real name

    # Tools (case)
    "git":               "Git",
    "bun":               "Bun",
    "uv":                "uv",            # already lowercase
    "pip":               "pip",
    "pnpm":              "pnpm",
    "powershell":        "PowerShell",
    "python":            "Python",
    "bash":              "Bash",
    "javascript":        "JavaScript",

    # Projects / platforms
    "hermes":            "Hermes",
    "hy-memory":         "Hy-Memory",
    "hindsight":         "Hindsight",
    "minimax-m3":        "MiniMax-M3",
    "minimax m3":        "MiniMax-M3",
    "dola-seed-2.0-lite": "dola-seed-2.0-lite",
    "mimo-v2.5":         "MiMo-V2.5",
    "openai codex":      "OpenAI Codex",
    "codex cli":         "Codex CLI",
    "codex":             "Codex",
    "deepseek v4 flash": "DeepSeek V4 Flash",
    "deepseek v4 pro":   "DeepSeek V4 Pro",
    "eikon":             "Eikon",
    "gradio":            "Gradio",
    "discord":           "Discord",
    "mnemosyne":         "Mnemosyne",
    "ibkr":              "IBKR",
    "tui":               "TUI",
    "llm":               "LLM",
    "hugging face":      "Hugging Face",
    "vs code":           "VS Code",
    "ptd":               "PTD",
    "idm system":        "IDM system",
    "cron":              "Cron",

    # Concepts
    "circuit breaker":   "circuit breaker",
    "react-reconciler":  "react-reconciler",
    "overheadgauge":     "OverheadGauge",
    "overhead gauge":    "OverheadGauge",
    "contextgauge":      "ContextGauge",
    "vision_analyze":    "vision_analyze",  # internal function (kept — real)
    "herm-update":       "herm-update",
    "hermes-agent-fork-update": "hermes-agent-fork-update",

    # MCP variants — all the same protocol
    "mcp":               "MCP",
    "model context protocol": "MCP",
    "mcp server":        "MCP server",  # the server that runs MCP, not the protocol
    "mcp server status section": None,    # noise — UI section name, filter out

    # DIFFERENT ENTITIES — must NOT merge
    # "tuancookiez-hub" is the user's GitHub org, NOT the user
    # "Hermes" is the project, "Hermes TUI" is a component
    # "MiniMax" is the company, "MiniMax-M3" is a specific model
    # "patch6" / "patch7" / "patch8" are distinct patches
    # These are intentionally NOT in the alias map.
}

# Noise patterns (filter out, don't merge)
NOISE_PATTERNS = [
    re.compile(r'\bsection\b', re.IGNORECASE),     # "MCP server status section"
    re.compile(r'\bpanel\b', re.IGNORECASE),
    re.compile(r'\btab\b', re.IGNORECASE),
    re.compile(r'\bview\b', re.IGNORECASE),
    re.compile(r'\bpage\b', re.IGNORECASE),
    re.compile(r'\bcomponent\b', re.IGNORECASE),
    re.compile(r'\bwidget\b', re.IGNORECASE),
    re.compile(r'\bbranch\b', re.IGNORECASE),       # git branches
    re.compile(r'^feature/', re.IGNORECASE),         # git branch names
    re.compile(r'^fix/', re.IGNORECASE),
    re.compile(r'^hotfix/', re.IGNORECASE),
    re.compile(r'patch[0-9]+$', re.IGNORECASE),    # patch6, patch7, patch8 (different things)
    re.compile(r'\bjs$', re.IGNORECASE),            # file extensions
    re.compile(r'\bpy$', re.IGNORECASE),
    re.compile(r'\bjson$', re.IGNORECASE),
    re.compile(r'\bmd$', re.IGNORECASE),
    re.compile(r'\.tsx?$', re.IGNORECASE),
    re.compile(r'^(http|https)://', re.IGNORECASE),  # URLs
]


def is_noise(name: str) -> bool:
    """True if this entity is just noise (UI section, git branch, etc.)."""
    for pat in NOISE_PATTERNS:
        if pat.search(name):
            return True
    return False


# ------------------------------------------------------------------
# Pass 1: Case folding + alias map → canonical_name
# ------------------------------------------------------------------
def pass1_canonicalize(name: str) -> str | None:
    """Returns canonical form, or None if entity should be filtered as noise."""
    n = name.strip()
    if not n:
        return None
    if is_noise(n):
        return None
    # Apply alias map (case-insensitive lookup)
    key = n.lower()
    if key in ALIAS_MAP:
        canonical = ALIAS_MAP[key]
        if canonical is None:
            return None  # explicit "filter out" entry
        return canonical
    # No alias — but if it's all-lowercase and well-known, leave it
    return n


# ------------------------------------------------------------------
# Pass 2: Fuzzy clustering (for entities that survive Pass 1)
# ------------------------------------------------------------------
def pass2_fuzzy_clusters(canonical_names: list[str]) -> dict[str, str]:
    """Returns a map: variant_name → canonical_name for fuzzy matches.

    Uses SequenceMatcher to find high-similarity pairs, then verifies
    with an LLM tiebreaker for borderline cases.
    """
    # Step 1: find fuzzy pairs (similarity > 0.85, length difference ≤ 4)
    fuzzy_pairs = []
    unique = sorted(set(canonical_names))
    for i, n1 in enumerate(unique):
        for n2 in unique[i+1:]:
            if abs(len(n1) - len(n2)) > 4:
                continue
            sim = SequenceMatcher(None, n1.lower(), n2.lower()).ratio()
            if 0.85 < sim < 1.0:  # not exact (case-insensitive already handled)
                fuzzy_pairs.append((n1, n2, sim))
    fuzzy_pairs.sort(key=lambda x: -x[2])

    # Step 2: ask LLM to confirm merges (only for borderline 0.85-0.95)
    merges = {}  # variant → canonical
    for n1, n2, sim in fuzzy_pairs:
        if sim >= 0.96:  # very high confidence, auto-merge
            # pick the more-frequent one as canonical
            c1 = name_freq.get(n1, 0)
            c2 = name_freq.get(n2, 0)
            canonical = n1 if c1 >= c2 else n2
            variant = n2 if canonical == n1 else n1
            merges[variant] = canonical
        else:
            # 0.85-0.95 — ask the LLM
            try:
                verdict = call_llm_dedupe(n1, n2)
                if verdict == "yes":
                    c1 = name_freq.get(n1, 0)
                    c2 = name_freq.get(n2, 0)
                    canonical = n1 if c1 >= c2 else n2
                    variant = n2 if canonical == n1 else n1
                    merges[variant] = canonical
            except Exception as e:
                print(f"  LLM dedup failed for ({n1}, {n2}): {e}")
    return merges


def call_llm_dedupe(name1: str, name2: str) -> str:
    """Ask the LLM: 'are these the same entity?' Returns 'yes' or 'no'."""
    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": (
                "You decide if two names refer to the same real-world entity. "
                "Reply ONLY with 'yes' or 'no'. No explanation."
            )},
            {"role": "user", "content": f'Are "{name1}" and "{name2}" the same entity? Answer yes or no.'},
        ],
        "temperature": 0.0,
        "max_tokens": 5,
    }
    if LLM_EXTRA:
        body["extra_body"] = LLM_EXTRA
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"].strip().lower()
    return "yes" if text.startswith("y") else "no"


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    ents = json.loads(ENTITY_PATH.read_text(encoding="utf-8"))
    rels = json.loads(RELATION_PATH.read_text(encoding="utf-8"))
    print(f"Input: {len(ents)} entities, {len(rels)} relations")

    # Build frequency map (for canonicalization choices)
    global name_freq
    name_freq = Counter(e["name"] for e in ents)
    print(f"  Unique raw names: {len(name_freq)}")

    # ------------------------------------------------------------------
    # Pass 1: canonicalize (case + alias)
    # ------------------------------------------------------------------
    print("\n=== Pass 1: case + alias canonicalization ===")
    canonical_map: dict[str, str] = {}  # raw name → canonical
    for raw_name in name_freq:
        canon = pass1_canonicalize(raw_name)
        if canon is None:
            canonical_map[raw_name] = "__NOISE__"  # filter out
        else:
            canonical_map[raw_name] = canon
    n_collapsed = sum(1 for raw, c in canonical_map.items() if c != "__NOISE__" and c != raw)
    n_noised = sum(1 for c in canonical_map.values() if c == "__NOISE__")
    n_unique_after_pass1 = len(set(c for c in canonical_map.values() if c != "__NOISE__"))
    print(f"  After pass 1: {n_unique_after_pass1} unique canonicals ({len(name_freq) - n_unique_after_pass1 - n_noised} merged by case/alias, {n_noised} filtered as noise)")
    for raw, canon in sorted(canonical_map.items(), key=lambda x: name_freq[x[0]], reverse=True):
        if canon == "__NOISE__":
            print(f"    FILTER: {raw!r}  ({name_freq[raw]} mentions)")
        elif canon != raw:
            print(f"    MERGE: {raw!r:30}  →  {canon!r:30}  ({name_freq[raw]} mentions)")

    # ------------------------------------------------------------------
    # Pass 2: fuzzy clustering on the remaining unique canonicals
    # ------------------------------------------------------------------
    print("\n=== Pass 2: fuzzy clustering (high-similarity pairs) ===")
    surviving_canonicals = [c for c in set(canonical_map.values()) if c != "__NOISE__"]
    fuzzy_merges = pass2_fuzzy_clusters(surviving_canonicals)
    print(f"  Fuzzy merges found: {len(fuzzy_merges)}")
    for variant, canon in sorted(fuzzy_merges.items(), key=lambda x: name_freq.get(x[0], 0), reverse=True):
        print(f"    FUZZY: {variant!r:30}  →  {canon!r:30}")

    # Update canonical_map with fuzzy merges
    for raw_name, current_canon in list(canonical_map.items()):
        if current_canon == "__NOISE__":
            continue
        if current_canon in fuzzy_merges:
            canonical_map[raw_name] = fuzzy_merges[current_canon]

    n_unique_after_pass2 = len(set(c for c in canonical_map.values() if c != "__NOISE__"))
    print(f"  After pass 2: {n_unique_after_pass2} unique entities")

    # ------------------------------------------------------------------
    # Build resolved entity records
    # ------------------------------------------------------------------
    by_canon = defaultdict(list)
    for e in ents:
        canon = canonical_map.get(e["name"], "__NOISE__")
        if canon == "__NOISE__":
            continue
        by_canon[canon].append(e)

    resolved_ents = []
    for canon, group in by_canon.items():
        # Aggregate across the group
        types = Counter(e["type"] for e in group)
        # Use most common type, with ties broken by alphabetical
        type_counts = types.most_common()
        primary_type = type_counts[0][0] if type_counts else "CONCEPT"
        confs = [e["confidence"] for e in group]
        source_fact_ids = list(set(e["source_fact_id"] for e in group if e.get("source_fact_id")))
        aliases = sorted(set(e["name"] for e in group if e["name"] != canon))
        resolved_ents.append({
            "name": canon,
            "type": primary_type,
            "confidence": round(sum(confs) / len(confs), 3),
            "mention_count": len(group),
            "unique_source_fact_count": len(source_fact_ids),
            "source_fact_ids": source_fact_ids[:20],  # cap for readability
            "aliases": aliases,
            "type_distribution": dict(types),
        })
    resolved_ents.sort(key=lambda x: -x["mention_count"])

    # ------------------------------------------------------------------
    # Rewrite relations to use canonical names
    # ------------------------------------------------------------------
    print("\n=== Rewriting relations with canonical names ===")
    n_rels_rewritten = 0
    for r in rels:
        canon_a = canonical_map.get(r["a"], "__NOISE__")
        canon_b = canonical_map.get(r["b"], "__NOISE__")
        # If either side is noise, drop the relation
        if canon_a == "__NOISE__" or canon_b == "__NOISE__":
            r["_dropped"] = True
            continue
        if canon_a != r["a"]:
            n_rels_rewritten += 1
            r["_a_was"] = r["a"]
            r["a"] = canon_a
        if canon_b != r["b"]:
            n_rels_rewritten += 1
            r["_b_was"] = r["b"]
            r["b"] = canon_b
    print(f"  Relations rewritten: {n_rels_rewritten} endpoints")

    # Drop noise relations and dedup
    surviving_rels = [r for r in rels if not r.get("_dropped")]
    # Dedup by (a, b, type) — keep highest confidence
    rel_dedup = {}
    for r in surviving_rels:
        key = (r["a"], r["b"], r["type"])
        if key not in rel_dedup or r["confidence"] > rel_dedup[key]["confidence"]:
            # Merge: keep max confidence, union source fact ids
            existing = rel_dedup.get(key)
            if existing:
                # Don't actually merge source fact ids here, just keep the new one
                # for clarity; in production we'd aggregate
                pass
            rel_dedup[key] = {
                "a": r["a"],
                "b": r["b"],
                "type": r["type"],
                "confidence": r["confidence"],
                "source_fact_id": r.get("source_fact_id", ""),
                "a_was": r.get("_a_was"),
                "b_was": r.get("_b_was"),
            }
    resolved_rels = sorted(rel_dedup.values(), key=lambda x: -x["confidence"])
    print(f"  After drop + dedup: {len(resolved_rels)} unique relation triples")

    # ------------------------------------------------------------------
    # Save outputs
    # ------------------------------------------------------------------
    audit = {
        "pass1_alias_map_size": len([v for v in ALIAS_MAP.values() if v is not None]),
        "pass1_merges": [(raw, canon) for raw, canon in canonical_map.items() if canon != "__NOISE__" and canon != raw],
        "pass1_noise_filtered": [raw for raw, c in canonical_map.items() if c == "__NOISE__"],
        "pass2_fuzzy_merges": fuzzy_merges,
    }
    (OUT_DIR / "l5_resolution_audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "l5_resolved_entities.json").write_text(
        json.dumps(resolved_ents, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT_DIR / "l5_resolved_relations.json").write_text(
        json.dumps(resolved_rels, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    stats = {
        "input": {
            "raw_entities": len(ents),
            "unique_raw_names": len(name_freq),
            "raw_relations": len(rels),
        },
        "output": {
            "unique_resolved_entities": len(resolved_ents),
            "unique_resolved_relations": len(resolved_rels),
        },
        "pass1": {
            "alias_map_size": audit["pass1_alias_map_size"],
            "merges": len(audit["pass1_merges"]),
            "noise_filtered": len(audit["pass1_noise_filtered"]),
        },
        "pass2": {
            "fuzzy_merges": len(fuzzy_merges),
        },
        "reduction": {
            "entity_reduction_pct": round(100 * (1 - len(resolved_ents) / max(1, len(name_freq))), 1),
            "relation_reduction_pct": round(100 * (1 - len(resolved_rels) / max(1, len(rels))), 1),
        },
    }
    (OUT_DIR / "l5_resolution_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print()
    print("=" * 60)
    print("L5 Entity Resolution — Final Results")
    print("=" * 60)
    print(f"  Raw entities:            {len(ents):4}  ({len(name_freq)} unique raw)")
    print(f"  Resolved entities:       {len(resolved_ents):4}  ({stats['reduction']['entity_reduction_pct']}% reduction)")
    print(f"  Raw relations:           {len(rels):4}")
    print(f"  Resolved relations:      {len(resolved_rels):4}  ({stats['reduction']['relation_reduction_pct']}% reduction)")
    print()
    print(f"  Top 20 resolved entities:")
    for e in resolved_ents[:20]:
        alias_str = f" (aliases: {e['aliases']})" if e["aliases"] else ""
        print(f"    {e['mention_count']:3}× {e['name']!r:24} [{e['type']:8}]{alias_str}")
    print()
    print(f"  Top 15 resolved relations:")
    for r in resolved_rels[:15]:
        canon_str = ""
        if r.get("a_was"):
            canon_str += f" [a_was: {r['a_was']!r}]"
        if r.get("b_was"):
            canon_str += f" [b_was: {r['b_was']!r}]"
        print(f"    {r['type']:14} {r['a']!r:24} → {r['b']!r:24}  ({r['confidence']:.2f}){canon_str}")
    print()
    print(f"  Saved:")
    print(f"    {OUT_DIR / 'l5_resolved_entities.json'}")
    print(f"    {OUT_DIR / 'l5_resolved_relations.json'}")
    print(f"    {OUT_DIR / 'l5_resolution_audit.json'}")
    print(f"    {OUT_DIR / 'l5_resolution_stats.json'}")


if __name__ == "__main__":
    main()
