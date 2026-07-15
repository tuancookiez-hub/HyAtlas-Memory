"""
L5 Phase 2.3a — Batch LLM extraction (Design A from prompt design doc).

Reads L2_facts from disk (sampled 200), runs the LLM in batches of 5,
extracts entities and relations per fact.

Read-only on Qdrant/Kuzu (writes would be in 2.3b after quality validation).

Outputs:
  - Raw LLM responses per batch (saved to JSON)
  - Aggregated entity and relation counts
  - Sample of high-confidence and low-confidence extractions for human review
"""
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

# ------------------------------------------------------------------
import threading  # Required for HyMemoryProvider._prefetch_lock
import os
import sys

HERMES_HOME = Path(os.environ.get(
    "HERMES_HOME",
    str(Path.home() / "AppData" / "Local" / "hermes") if sys.platform == "win32"
    else str(Path.home() / ".hermes")
))

CFG_PATH = HERMES_HOME / "hy_memory.json"
SAMPLE_PATH = HERMES_HOME / "logs" / "l2_sample_200.json"
OUT_PATH = HERMES_HOME / "logs" / "l5_extraction_design_a.json"

cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
LLM_MODEL = cfg["llm"]["model"]
LLM_API_KEY = cfg["llm"]["api_key"]
LLM_BASE_URL = cfg["llm"]["base_url"].rstrip("/")
LLM_EXTRA = cfg["llm"].get("extra_body", {})

BATCH_SIZE = 5
RELATION_TYPES = {"works_on", "uses", "depends_on", "replaces", "related_to"}
ENTITY_TYPES = {"PERSON", "PROJECT", "TOOL", "MODEL", "CONCEPT"}

# ------------------------------------------------------------------
# System prompt (Design A from the prompt design doc)
# ------------------------------------------------------------------
SYSTEM_PROMPT_A = """You extract a knowledge graph from memory facts.

You will be given 1-10 memory facts. For each fact, identify:

1. **Entities** — concrete things mentioned (people, projects, tools, models, concepts)
2. **Relations** — typed connections between entities in the same fact

Entity types: PERSON, PROJECT, TOOL, MODEL, CONCEPT
Relation types: works_on, uses, depends_on, replaces, related_to

## Relation semantics
- works_on:   PERSON → PROJECT (e.g., <user> works_on Hy-Memory)
- uses:       PROJECT → TOOL/MODEL/LIBRARY (e.g., Hy-Memory uses Qdrant)
- depends_on: anything → anything (one is required for the other to function)
- replaces:   X replaces Y (e.g., Hy-Memory replaces Hindsight)
- related_to: generic association (use sparingly — only if no other fits)

## Rules
- Only extract entities EXPLICITLY named in the fact text. No inference.
- Only extract relations if BOTH entities appear in the SAME fact.
- Skip relations where you have low confidence (< 0.6). Mark as "none".
- Resolve obvious aliases: "Hermes Agent" = "Hermes" = "hermes-agent" (use the most common form)
- Date/number mentions are NOT entities (skip them)
- Generic words ("the user", "the system", "the project") are NOT entities

## Output (strict JSON, no prose outside)
{
  "facts": [
    {
      "fact_id": "<id from input>",
      "entities": [
        {"name": "Hermes", "type": "PROJECT", "confidence": 0.95}
      ],
      "relations": [
        {"a": "<user>", "b": "Hermes", "type": "works_on", "confidence": 0.85}
      ]
    }
  ]
}"""

# ------------------------------------------------------------------
# Load sample
# ------------------------------------------------------------------
facts = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
print(f"Loaded {len(facts)} L2_facts")

# ------------------------------------------------------------------
# LLM call
# ------------------------------------------------------------------
def call_llm_batch(facts_batch: list[dict]) -> dict:
    """Send a batch of facts to the LLM, return parsed JSON or error dict."""
    user_lines = [f"Extract entities and relations from these {len(facts_batch)} facts:\n"]
    for f in facts_batch:
        user_lines.append(f'Fact id={f["id"]}:')
        user_lines.append(f'  "{f["content"][:600]}"')
        user_lines.append("")
    user_msg = "\n".join(user_lines)

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_A},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "max_tokens": 2000,
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
    with urllib.request.urlopen(req, timeout=60) as resp:
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

# ------------------------------------------------------------------
# Run on batches
# ------------------------------------------------------------------
results = []
total_in = 0
total_out = 0
total_calls = 0
errors = 0

# Limit to first 50 facts (10 batches) for this prototype
facts_to_process = facts[:50]
n_batches = (len(facts_to_process) + BATCH_SIZE - 1) // BATCH_SIZE
print(f"Processing {len(facts_to_process)} facts in {n_batches} batches of {BATCH_SIZE}")

t_start = time.time()
for i in range(0, len(facts_to_process), BATCH_SIZE):
    batch = facts_to_process[i:i+BATCH_SIZE]
    try:
        result = call_llm_batch(batch)
        results.append({"batch": i // BATCH_SIZE, "result": result})
        total_in += result.get("_input_tokens", 0)
        total_out += result.get("_output_tokens", 0)
        total_calls += 1
        n_facts_in_response = len(result.get("facts", []))
        n_entities = sum(len(f.get("entities", [])) for f in result.get("facts", []))
        n_relations = sum(len(f.get("relations", [])) for f in result.get("facts", []))
        print(f"  Batch {i//BATCH_SIZE + 1}/{n_batches}  facts={n_facts_in_response}  entities={n_entities}  relations={n_relations}  [{total_in} in / {total_out} out]")
    except Exception as e:
        errors += 1
        print(f"  Batch {i//BATCH_SIZE + 1}/{n_batches}  ERROR: {e}")
        results.append({"batch": i // BATCH_SIZE, "error": str(e)})

elapsed = time.time() - t_start
print()
print(f"=== Prototype summary ===")
print(f"  Batches: {total_calls} OK, {errors} errors")
print(f"  Total time: {elapsed:.1f}s ({elapsed/max(1,total_calls):.1f}s per batch)")
print(f"  Total input tokens: {total_in}")
print(f"  Total output tokens: {total_out}")
print(f"  Cost (rough, dola-seed): ${total_in * 0.00000015 + total_out * 0.0000003:.4f}")
print(f"  Per-fact cost: ${(total_in * 0.00000015 + total_out * 0.0000003) / max(1, len(facts_to_process)):.6f}")
print(f"  Extrapolated to 1,011 L2_facts: ${(total_in * 0.00000015 + total_out * 0.0000003) / max(1, len(facts_to_process)) * 1011:.4f}")
print(f"  Extrapolated time: {elapsed / max(1, len(facts_to_process)) * 1011:.0f}s = {elapsed / max(1, len(facts_to_process)) * 1011 / 60:.1f} min")

# ------------------------------------------------------------------
# Aggregate
# ------------------------------------------------------------------
all_entities = []
all_relations = []
for r in results:
    if "error" in r:
        continue
    for f in r["result"].get("facts", []):
        for e in f.get("entities", []):
            all_entities.append({"name": e.get("name", ""), "type": e.get("type", ""),
                                  "confidence": e.get("confidence", 0), "fact_id": f.get("fact_id", "")})
        for rel in f.get("relations", []):
            all_relations.append({"a": rel.get("a", ""), "b": rel.get("b", ""),
                                   "type": rel.get("type", ""), "confidence": rel.get("confidence", 0),
                                   "fact_id": f.get("fact_id", "")})

# Sanity-check against taxonomy
bad_types_e = Counter(e["type"] for e in all_entities if e["type"] not in ENTITY_TYPES)
bad_types_r = Counter(r["type"] for r in all_relations if r["type"] not in RELATION_TYPES)
print()
print(f"=== Extraction quality ===")
print(f"  Entities: {len(all_entities)} total, {len(set((e['name'].lower(), e['type']) for e in all_entities))} unique (case-insensitive)")
print(f"  Relations: {len(all_relations)} total")
print(f"  Out-of-vocabulary entity types: {sum(bad_types_e.values())} (types: {dict(bad_types_e)})")
print(f"  Out-of-vocabulary relation types: {sum(bad_types_r.values())} (types: {dict(bad_types_r)})")
print()
print(f"  Top entities (by name):")
name_counts = Counter(e["name"] for e in all_entities)
for name, count in name_counts.most_common(20):
    print(f"    {count:3}× {name!r}")
print()
print(f"  Relation type distribution:")
type_counts = Counter(r["type"] for r in all_relations)
for typ, count in type_counts.most_common():
    print(f"    {count:3}  {typ}")
print()
print(f"  High-confidence relations (≥0.7):")
high_conf = [r for r in all_relations if r["confidence"] >= 0.7]
print(f"    Total: {len(high_conf)} / {len(all_relations)} ({len(high_conf)/max(1,len(all_relations))*100:.0f}%)")
print()
print(f"  Sample high-confidence relations:")
for r in sorted(high_conf, key=lambda x: -x["confidence"])[:15]:
    print(f"    {r['type']:14} ({r['confidence']:.2f})  {r['a']!r:24} → {r['b']!r}")

# Save raw results
OUT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n=== Raw results saved to {OUT_PATH} ===")
