"""
L5 Phase 2.3a — Reliable batch LLM extraction.

Improvements over the prototype (l5_batch_extract.py):
  1. Retry with exponential backoff (handles 60% error rate)
  2. Parallelism via ThreadPoolExecutor (5-10x speedup)
  3. Lower batch size (3 facts instead of 5) — reduces per-call token count
  4. Entity normalization (lowercase, alias map)
  5. Confidence threshold (>= 0.85 for relations, >= 0.7 for entities)
  6. Idempotent output: saves to JSON files (not yet to Kuzu)

Output:
  - logs/l5_digest_entities.json:  {name, type, aliases, source_fact_ids, confidence}
  - logs/l5_digest_relations.json: {a, b, type, confidence, source_fact_ids}
  - logs/l5_digest_stats.json:     run stats (timing, errors, cost)
"""
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ------------------------------------------------------------------
import threading  # Required for HyMemoryProvider._prefetch_lock
from pathlib import Path
import os
import sys

HERMES_HOME = Path(os.environ.get(
    "HERMES_HOME",
    str(Path.home() / "AppData" / "Local" / "hermes") if sys.platform == "win32"
    else str(Path.home() / ".hermes")
))

CFG_PATH = HERMES_HOME / "hy_memory.json"
SAMPLE_PATH = HERMES_HOME / "logs" / "l2_all_1011.json"
OUT_DIR = HERMES_HOME / "logs"
ENTITIES_PATH = OUT_DIR / "l5_full_entities.json"
RELATIONS_PATH = OUT_DIR / "l5_full_relations.json"
STATS_PATH = OUT_DIR / "l5_full_stats.json"

cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
LLM_MODEL = cfg["llm"]["model"]
LLM_API_KEY = cfg["llm"]["api_key"]
LLM_BASE_URL = cfg["llm"]["base_url"].rstrip("/")
LLM_EXTRA = cfg["llm"].get("extra_body", {})

# Tunables
BATCH_SIZE = 3            # facts per LLM call (was 5)
CONCURRENCY = 4           # parallel workers
MAX_RETRIES = 4           # retries per batch
RETRY_BASE_DELAY = 2.0    # seconds; doubles each retry
RELATION_CONFIDENCE_FLOOR = 0.85
ENTITY_CONFIDENCE_FLOOR = 0.70
DEFAULT_TIMEOUT = 90      # per-call timeout (was 60; some calls took longer)

RELATION_TYPES = {"owns", "visited", "lives_in", "works_on", "uses", "depends_on", "replaces", "friend_of", "family_of", "related_to", "happened_at", "attended"}
ENTITY_TYPES = {"PERSON", "PLACE", "PRODUCT", "ORGANIZATION", "EVENT", "PROJECT", "TOOL", "MODEL", "CONCEPT"}

# Known alias map (extend as we learn more)
ALIAS_MAP = {
    "herm": "Hermes",
    "hermes agent": "Hermes",
    "hermes-agent": "Hermes",
    "hermes ai agent": "Hermes",
    "hermes ai": "Hermes",
    "herm-tui": "Hermes",
    "hermes cli": "Hermes",
    "hy_memory": "Hy-Memory",
    "hy-memory": "Hy-Memory",
    "tuanc": "TunaCookie",
    "tuna cookie": "TunaCookie",
    "windows powershell": "PowerShell",
}

# ------------------------------------------------------------------
# System prompt (Design A — same as prototype, proven to work)
# ------------------------------------------------------------------
SYSTEM_PROMPT = """You extract a knowledge graph from memory facts.

You will be given 1-3 memory facts. For each fact, identify:

1. **Entities** — concrete things mentioned (people, places, products, events, projects, tools, etc.)
2. **Relations** — typed connections between entities in the same fact

Entity types: PERSON, PLACE, PRODUCT, ORGANIZATION, EVENT, PROJECT, TOOL, MODEL, CONCEPT
Relation types: owns, visited, lives_in, works_on, uses, depends_on, replaces, friend_of, family_of, related_to, happened_at, attended

## Relation semantics
- owns:        PERSON → PRODUCT (e.g., Tom owns Samsung Galaxy S22)
- visited:     PERSON → PLACE (e.g., Tom visited Tokyo)
- lives_in:    PERSON → PLACE (e.g., Tom lives_in Berlin)
- works_on:    PERSON → PROJECT
- uses:        PROJECT → TOOL/MODEL/LIBRARY
- depends_on:  anything → anything
- replaces:    X replaces Y
- friend_of:   PERSON → PERSON
- family_of:   PERSON → PERSON
- related_to:  generic association (use sparingly)
- happened_at: EVENT → PLACE
- attended:    PERSON → EVENT

## Rules
- Only extract entities EXPLICITLY named in the fact text. No inference.
- Only extract relations if BOTH entities appear in the SAME fact.
- Be LENIENT with entity types — a phone is a PRODUCT, a city is a PLACE, a concert is an EVENT.
- Skip relations where you have low confidence (< 0.85). Mark as "none".
- Resolve obvious aliases: "Hermes Agent" = "Hermes" = "hermes-agent" (use the most common form)
- Date/number mentions are NOT entities (skip them)
- Generic words ("the user", "the system", "the project") are NOT entities
- Internal class/file/branch names (e.g., "OverheadGauge.tsx", "feature/overhead-gauge") are NOT useful entities — skip them

## Output (strict JSON, no prose outside)
{
  "facts": [
    {
      "fact_id": "<id from input>",
      "entities": [
        {"name": "Hermes", "type": "PROJECT", "confidence": 0.95}
      ],
      "relations": [
        {"a": "TunaCookie", "b": "Hermes", "type": "works_on", "confidence": 0.90}
      ]
    }
  ]
}"""


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def normalize_entity_name(name: str) -> str:
    """Apply alias map + basic cleanup."""
    n = name.strip()
    n = re.sub(r'\s+', ' ', n)
    key = n.lower()
    return ALIAS_MAP.get(key, n)


def is_internal_noise(name: str) -> bool:
    """Filter class names, file names, branch names, etc."""
    if not name:
        return True
    # CamelCase class names with .ts/.tsx/.py/.tsx
    if re.search(r'\.(ts|tsx|js|jsx|py|md|json|yaml|yml)$', name):
        return True
    # Git branch / file path
    if '/' in name and not name.startswith('/'):
        return True
    # URLs
    if name.startswith(('http://', 'https://')):
        return True
    # Very short or all-numeric
    if len(name) < 2:
        return True
    return False


def call_llm_batch(facts_batch: list[dict]) -> dict:
    """Call LLM on a batch of facts. Returns parsed dict with usage stats."""
    user_lines = [f"Extract entities and relations from these {len(facts_batch)} facts:\n"]
    for f in facts_batch:
        user_lines.append(f'Fact id={f["id"]}:')
        # Truncate very long facts
        content = f["content"][:800] if len(f["content"]) > 800 else f["content"]
        user_lines.append(f'  "{content}"')
        user_lines.append("")
    user_msg = "\n".join(user_lines)

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
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
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.split("\n") if not line.strip().startswith("```"))
    parsed = json.loads(text)
    usage = data.get("usage", {})
    parsed["_input_tokens"] = usage.get("prompt_tokens", 0)
    parsed["_output_tokens"] = usage.get("completion_tokens", 0)
    return parsed


def call_with_retry(batch: list[dict]) -> tuple[dict | None, str | None, dict]:
    """Returns (parsed_response, error, stats). Retries on failure."""
    stats = {"retries": 0, "in_tokens": 0, "out_tokens": 0, "elapsed_s": 0.0}
    for attempt in range(MAX_RETRIES):
        try:
            t0 = time.time()
            result = call_llm_batch(batch)
            stats["elapsed_s"] = time.time() - t0
            stats["in_tokens"] = result.get("_input_tokens", 0)
            stats["out_tokens"] = result.get("_output_tokens", 0)
            return result, None, stats
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError) as e:
            stats["retries"] += 1
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                time.sleep(delay)
            last_err = e
    return None, str(last_err), stats


def process_batch(batch: list[dict]) -> tuple[int, list[dict], list[dict], dict]:
    """Process one batch: returns (batch_idx, entities, relations, stats)."""
    result, error, stats = call_with_retry(batch)
    entities_out = []
    relations_out = []
    if error or result is None:
        return -1, entities_out, relations_out, {"error": error, **stats}

    for f in result.get("facts", []):
        fact_id = f.get("fact_id", "")
        for e in f.get("entities", []):
            name = normalize_entity_name(e.get("name", ""))
            if not name or is_internal_noise(name):
                continue
            etype = e.get("type", "CONCEPT")
            if etype not in ENTITY_TYPES:
                etype = "CONCEPT"
            econf = e.get("confidence", 0.0)
            if econf < ENTITY_CONFIDENCE_FLOOR:
                continue
            entities_out.append({
                "name": name,
                "type": etype,
                "confidence": econf,
                "source_fact_id": fact_id,
            })
        for r in f.get("relations", []):
            a = normalize_entity_name(r.get("a", ""))
            b = normalize_entity_name(r.get("b", ""))
            rtype = r.get("type", "")
            if rtype not in RELATION_TYPES:
                continue
            rconf = r.get("confidence", 0.0)
            if rconf < RELATION_CONFIDENCE_FLOOR:
                continue
            if not a or not b or a == b:
                continue
            if is_internal_noise(a) or is_internal_noise(b):
                continue
            relations_out.append({
                "a": a,
                "b": b,
                "type": rtype,
                "confidence": rconf,
                "source_fact_id": fact_id,
            })
    return 0, entities_out, relations_out, stats


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    facts = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    # Use ALL 200 facts (full sample, not the 50-fact subset)
    print(f"Loaded {len(facts)} L2_facts")

    # Build batches
    batches = []
    for i in range(0, len(facts), BATCH_SIZE):
        batch = facts[i:i + BATCH_SIZE]
        batches.append((i // BATCH_SIZE, batch))
    n_batches = len(batches)
    print(f"  → {n_batches} batches of {BATCH_SIZE} (concurrency={CONCURRENCY})")

    all_entities = []
    all_relations = []
    errors = []
    total_in = 0
    total_out = 0
    total_retries = 0
    completed = 0

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {
            ex.submit(process_batch, batch): (idx, batch)
            for idx, batch in batches
        }
        for fut in as_completed(futures):
            idx, batch = futures[fut]
            try:
                _, ents, rels, stats = fut.result()
                completed += 1
                if "error" in stats:
                    errors.append({"batch_idx": idx, "error": stats["error"]})
                else:
                    all_entities.extend(ents)
                    all_relations.extend(rels)
                    total_in += stats.get("in_tokens", 0)
                    total_out += stats.get("out_tokens", 0)
                    total_retries += stats.get("retries", 0)
                if completed % 5 == 0 or completed == n_batches:
                    elapsed = time.time() - t_start
                    print(f"  [{completed:3}/{n_batches}]  ents={len(all_entities)}  rels={len(all_relations)}  errors={len(errors)}  retries={total_retries}  [{elapsed:.0f}s]")
            except Exception as e:
                errors.append({"batch_idx": idx, "error": f"outer: {e}"})
    elapsed = time.time() - t_start

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    cost = total_in * 0.00000015 + total_out * 0.0000003
    per_fact = (total_in + total_out) / max(1, len(facts))
    ext_total = cost / max(1, len(facts)) * 1011
    ext_time = elapsed / max(1, len(facts)) * 1011

    print()
    print("=" * 60)
    print(f"L5 Digest Prototype A — Results")
    print("=" * 60)
    print(f"  Facts processed:      {len(facts)}")
    print(f"  Batches:              {n_batches} ({len(errors)} errors, {total_retries} retries)")
    print(f"  Time:                 {elapsed:.1f}s ({elapsed/max(1,n_batches):.1f}s per batch)")
    print(f"  Tokens:               {total_in} in / {total_out} out")
    print(f"  Cost:                 ${cost:.4f} (${per_fact * 1500 * 1000:.2f}/M tokens roughly)")
    print(f"  Per-fact cost:        ${cost/max(1,len(facts)):.6f}")
    print(f"  Extrapolated to 1,011: ${ext_total:.4f}, {ext_time:.0f}s = {ext_time/60:.1f} min")
    print()
    print(f"  Entities: {len(all_entities)} extracted, {len(set(e['name'] for e in all_entities))} unique")
    print(f"  Relations: {len(all_relations)} extracted, {len(set((r['a'], r['b'], r['type']) for r in all_relations))} unique triples")

    # Top entities
    name_counts = Counter(e["name"] for e in all_entities)
    print()
    print(f"  Top 20 entities:")
    for n, c in name_counts.most_common(20):
        print(f"    {c:3}× {n!r}")

    # Top relations
    rel_type_counts = Counter(r["type"] for r in all_relations)
    print()
    print(f"  Relation types:")
    for t, c in rel_type_counts.most_common():
        print(f"    {c:3}  {t}")

    # Sample relations
    rel_pairs = Counter((r["a"], r["b"]) for r in all_relations)
    print()
    print(f"  Top 15 relation pairs:")
    for (a, b), c in rel_pairs.most_common(15):
        # Get the type for this pair
        rt = next((r["type"] for r in all_relations if r["a"] == a and r["b"] == b), "?")
        print(f"    {c:2}× {a!r:24} {rt:14} → {b!r}")

    # Out-of-vocab check
    bad_e = Counter(e["type"] for e in all_entities if e["type"] not in ENTITY_TYPES)
    bad_r = Counter(r["type"] for r in all_relations if r["type"] not in RELATION_TYPES)
    print()
    print(f"  Out-of-vocab entity types: {sum(bad_e.values())} {dict(bad_e)}")
    print(f"  Out-of-vocab relation types: {sum(bad_r.values())} {dict(bad_r)}")

    # Save outputs
    ENTITIES_PATH.write_text(json.dumps(all_entities, indent=2, ensure_ascii=False), encoding="utf-8")
    RELATIONS_PATH.write_text(json.dumps(all_relations, indent=2, ensure_ascii=False), encoding="utf-8")
    stats = {
        "facts_processed": len(facts),
        "batches": n_batches,
        "errors": errors,
        "total_retries": total_retries,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cost_usd": cost,
        "elapsed_s": elapsed,
        "per_fact_cost_usd": cost / max(1, len(facts)),
        "extrapolated_1011_cost_usd": ext_total,
        "extrapolated_1011_time_s": ext_time,
        "entity_count": len(all_entities),
        "unique_entity_count": len(set(e["name"] for e in all_entities)),
        "relation_count": len(all_relations),
        "unique_relation_triples": len(set((r["a"], r["b"], r["type"]) for r in all_relations)),
        "config": {
            "batch_size": BATCH_SIZE,
            "concurrency": CONCURRENCY,
            "max_retries": MAX_RETRIES,
            "relation_confidence_floor": RELATION_CONFIDENCE_FLOOR,
            "entity_confidence_floor": ENTITY_CONFIDENCE_FLOOR,
        },
    }
    STATS_PATH.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print(f"  Saved:")
    print(f"    {ENTITIES_PATH}")
    print(f"    {RELATIONS_PATH}")
    print(f"    {STATS_PATH}")


if __name__ == "__main__":
    main()
