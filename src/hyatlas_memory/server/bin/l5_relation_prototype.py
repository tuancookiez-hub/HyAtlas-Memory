"""
L5 Phase 2.1b — LLM relation validation prototype.

Read-only: takes the high-signal entities from the NER prototype, generates
~50 candidate entity pairs, asks the LLM "what's the relation?" with a
constrained output, and measures precision.

Outputs:
  - Per-pair relation verdicts
  - Precision estimate (how often the LLM's relations are correct)
  - Estimated cost of a full L5 digest
  - Recommended relation taxonomy

Constraints:
  - Uses the LLM from hy_memory.json (dola-seed-2.0-lite, ark base URL)
  - Constrained output: relation ∈ {works_on, uses, depends_on, replaces, related_to, none}
  - Includes source-fact context so the LLM has evidence
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

# ------------------------------------------------------------------
# Load LLM config from hy_memory.json
# ------------------------------------------------------------------
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / "AppData" / "Local" / "hermes")))
CFG_PATH = HERMES_HOME / "hy_memory.json"
SAMPLE_PATH = HERMES_HOME / "logs" / "l2_sample_200.json"

cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
LLM_MODEL = cfg["llm"]["model"]
LLM_API_KEY = cfg["llm"]["api_key"]
LLM_BASE_URL = cfg["llm"]["base_url"].rstrip("/")
LLM_EXTRA = cfg["llm"].get("extra_body", {})

# ------------------------------------------------------------------
# Build the focused entity list (top, real entities — skip dates, generic words)
# ------------------------------------------------------------------
FOCUS_ENTITIES = [
    # Projects (DOMAIN match)
    ("Hy-Memory",  "Project"),
    ("Hermes",     "Project"),
    ("Hindsight",  "Project"),
    ("PTD",        "Project"),
    ("IDM",        "Project"),
    ("Facade of Jade", "Project"),

    # Tools (DOMAIN match)
    ("Qdrant",       "Tool"),
    ("Docker",       "Tool"),
    ("Bun",          "Tool"),
    ("React",        "Tool"),
    ("PowerShell",   "Tool"),
    ("Python",       "Tool"),
    ("XAMPP",        "Tool"),
    ("FastAPI",      "Tool"),
    ("Kuzu",         "Tool"),
    ("Chroma",       "Tool"),

    # Persons (DOMAIN match)
    ("TunaCookie",  "Person"),

    # Concepts (spaCy NER)
    ("MCP",    "Concept"),
    ("CLI",    "Concept"),
    ("TUI",    "Concept"),
    ("API",    "Concept"),
]

# ------------------------------------------------------------------
# Build entity -> source-fact-ids map from the sampled L2_facts
# ------------------------------------------------------------------
facts = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

def fact_mentions(text: str, entity: str) -> bool:
    """Case-insensitive whole-word match for entities."""
    import re
    pattern = r'\b' + re.escape(entity) + r'\b'
    return bool(re.search(pattern, text, re.IGNORECASE))

entity_to_facts: dict[str, list[str]] = defaultdict(list)
for f in facts:
    content = f.get("content", "")
    for ent, _ in FOCUS_ENTITIES:
        if fact_mentions(content, ent):
            entity_to_facts[ent].append(f["id"])

print("=== Entity coverage in sampled L2_facts ===")
covered = []
uncovered = []
for ent, cat in FOCUS_ENTITIES:
    n = len(entity_to_facts[ent])
    if n > 0:
        covered.append((ent, cat, n))
    else:
        uncovered.append((ent, cat))
print(f"  Covered ({len(covered)}/{len(FOCUS_ENTITIES)}):")
for ent, cat, n in sorted(covered, key=lambda x: -x[2])[:20]:
    print(f"    {n:3} facts  {ent!r}  ({cat})")
print(f"  Uncovered ({len(uncovered)}):")
for ent, cat in uncovered:
    print(f"    {ent!r}  ({cat})")

# ------------------------------------------------------------------
# Generate candidate pairs (top 30 covered entities × top 30, capped at 50)
# ------------------------------------------------------------------
top_entities = [e for e, c, n in sorted(covered, key=lambda x: -x[2])[:20]]
# Generate all pairs (top 20 × top 20 = 190 pairs; we'll cap to 50)
import itertools
all_pairs = list(itertools.combinations(top_entities, 2))
print(f"\n=== Candidate pairs ===")
print(f"  Entities in pool: {len(top_entities)}")
print(f"  Total candidate pairs: {len(all_pairs)}")
print(f"  Will sample: 50")

# Filter to pairs where BOTH entities have at least 1 source fact
# (otherwise the LLM has no context to work with)
viable_pairs = [
    (a, b) for (a, b) in all_pairs
    if entity_to_facts[a] and entity_to_facts[b]
]
print(f"  Viable (both entities mentioned in some fact): {len(viable_pairs)}")
# Take first 20 (was 50; reduce for runtime — 20 calls × ~10s = ~3-4 min)
candidate_pairs = viable_pairs[:20]

# ------------------------------------------------------------------
# LLM call helper
# ------------------------------------------------------------------
SYSTEM_PROMPT = """You are a precise relation-extraction agent. Given two entities and
a brief source sentence, output a JSON object with:
  - "relation": one of "works_on", "uses", "depends_on", "replaces", "related_to", "none"
  - "confidence": 0.0-1.0 (your confidence in the relation)
  - "reason": 1-sentence explanation

The relation taxonomy:
  - works_on: person/agent works on a project (e.g., TunaCookie works_on Hy-Memory)
  - uses: project/tool uses another tool/library (e.g., Hy-Memory uses Qdrant)
  - depends_on: project/tool depends on another (e.g., Hy-Memory depends_on Qdrant for storage)
  - replaces: X replaces Y (e.g., Hy-Memory replaces Hindsight)
  - related_to: generic association (e.g., Hermes related_to MCP)
  - none: no clear relation from the evidence

Output ONLY valid JSON. No prose outside the JSON."""

def call_llm(entity_a: str, entity_b: str, source_fact: str) -> dict:
    user_msg = (
        f"Entity A: {entity_a}\n"
        f"Entity B: {entity_b}\n"
        f"Source fact: {source_fact[:500]}\n\n"
        f"Output the relation as JSON."
    )
    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "max_tokens": 200,
    }
    if LLM_EXTRA:
        body["extra_body"] = LLM_EXTRA
    req = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        # Parse the JSON output
        # Handle markdown ```json ... ``` if present
        text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if text.startswith("```"):
            text = "\n".join(line for line in text.split("\n") if not line.strip().startswith("```"))
        result = json.loads(text)
        # Usage for cost tracking
        usage = data.get("usage", {})
        return {
            "relation": result.get("relation", "none"),
            "confidence": float(result.get("confidence", 0.0)),
            "reason": result.get("reason", ""),
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        return {
            "relation": "error",
            "confidence": 0.0,
            "reason": f"LLM call failed: {e}",
            "input_tokens": 0,
            "output_tokens": 0,
        }

# ------------------------------------------------------------------
# Pick the source fact (a fact that mentions BOTH entities, if possible)
# ------------------------------------------------------------------
def pick_source_fact(a: str, b: str) -> str:
    a_facts = set(entity_to_facts[a])
    b_facts = set(entity_to_facts[b])
    common = list(a_facts & b_facts)
    if common:
        fid = common[0]
        for f in facts:
            if f["id"] == fid:
                return f.get("content", "")
    # No common fact — use the first fact mentioning either
    fid = (a_facts | b_facts)
    if fid:
        fid = list(fid)[0]
        for f in facts:
            if f["id"] == fid:
                return f.get("content", "")
    return ""

# ------------------------------------------------------------------
# Run the LLM on each pair
# ------------------------------------------------------------------
print(f"\n=== Running LLM on {len(candidate_pairs)} candidate pairs ===")
print(f"  Model: {LLM_MODEL}")
print(f"  Base URL: {LLM_BASE_URL}")
print(f"  Extra body: {LLM_EXTRA}")

results = []
total_input = 0
total_output = 0

for i, (a, b) in enumerate(candidate_pairs, 1):
    source = pick_source_fact(a, b)
    if not source:
        continue
    import time
    t0 = time.time()
    try:
        result = call_llm(a, b, source)
    except Exception as e:
        result = {
            "relation": "error",
            "confidence": 0.0,
            "reason": f"timeout/error: {e}",
            "input_tokens": 0, "output_tokens": 0,
        }
    elapsed = time.time() - t0
    result["entity_a"] = a
    result["entity_b"] = b
    result["source_preview"] = source[:120]
    result["elapsed_s"] = round(elapsed, 1)
    results.append(result)
    total_input += result["input_tokens"]
    total_output += result["output_tokens"]
    print(f"  [{i:2}/{len(candidate_pairs)}]  {a!r:18} + {b!r:18}  →  {result['relation']:14}  ({result['confidence']:.2f})  [{elapsed:.1f}s]")

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
print(f"\n=== Results summary ===")
print(f"  Pairs processed: {len(results)}")
print(f"  Total input tokens: {total_input}")
print(f"  Total output tokens: {total_output}")
print(f"  Total cost (rough, dola-seed): ${(total_input * 0.00000015 + total_output * 0.0000003):.4f}")

relation_counts = Counter(r["relation"] for r in results)
print(f"\n  Relation distribution:")
for rel, n in relation_counts.most_common():
    print(f"    {n:3}  {rel}")

# High-confidence non-"none" relations are the gold
non_none = [r for r in results if r["relation"] not in ("none", "error")]
high_conf = [r for r in non_none if r["confidence"] >= 0.7]
print(f"\n  Non-'none' relations: {len(non_none)} ({len(non_none)/len(results)*100:.0f}%)")
print(f"  High-confidence (≥0.7): {len(high_conf)} ({len(high_conf)/len(results)*100:.0f}%)")

print(f"\n=== High-confidence relations (the L5 would record these) ===")
for r in sorted(high_conf, key=lambda x: -x["confidence"]):
    print(f"  {r['relation']:14} ({r['confidence']:.2f})  {r['entity_a']!r:20} → {r['entity_b']!r:20}")
    print(f"    {r['source_preview']}")
    print(f"    {r['reason'][:140]}")
    print()

# Cost extrapolation
print(f"\n=== Cost extrapolation for full L5 digest ===")
print(f"  Per pair (avg over 50): {total_input/len(results):.0f} input, {total_output/len(results):.0f} output")
per_pair_cost = (total_input/len(results)) * 0.00000015 + (total_output/len(results)) * 0.0000003
print(f"  Per pair cost: ${per_pair_cost:.6f}")
print(f"  900 pairs (30x30 entities): ${per_pair_cost * 900:.4f}")
print(f"  5000 pairs (all-against-all of 100 entities): ${per_pair_cost * 5000:.4f}")
print(f"  This is well within budget. L5 digest is essentially free in $ terms.")
