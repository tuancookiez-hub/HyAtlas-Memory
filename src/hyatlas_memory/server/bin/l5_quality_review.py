"""
L5 Phase 2.5 (review) — Quality assessment of the resolved L5 graph.

Input:  l5_resolved_entities.json + l5_resolved_relations.json (323-fact run)
Output: a structured review showing what's clean, what's noisy, and what
        should be filtered before writing to Kuzu.
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ENTITY_PATH = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs\l5_resolved_entities.json")
RELATION_PATH = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs\l5_resolved_relations.json")
OUT_DIR = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs")

ents = json.loads(ENTITY_PATH.read_text(encoding="utf-8"))
rels = json.loads(RELATION_PATH.read_text(encoding="utf-8"))

print(f"Input: {len(ents)} entities, {len(rels)} relations\n")

# ------------------------------------------------------------------
# Entity classification
# ------------------------------------------------------------------
# Heuristics: classify entities as REAL / BORDERLINE / NOISE

NOISE_KEYWORDS_RE = re.compile(
    r'\b('
    r'section|panel|tab|page|view|component|widget|branch|'
    r'patch[0-9]+|hotfix|feature/[a-z]|fix/[a-z]|'
    r'js$|ts$|tsx$|py$|json$|md$|yaml$|yml$|'
    r'^http://|^https://'
    r')\b',
    re.IGNORECASE
)

# "X feature", "X module", "X widget" — feature names, not entities
FEATURE_SUFFIX_RE = re.compile(
    r'\b(feature|widget|module|class|method|function|api|interface|'
    r'config|setting|option|param|argument|flag|key|value|token|'
    r'log|message|event|state|status|response|request|response)\b',
    re.IGNORECASE
)

# Code-like identifiers (snake_case function/variable names)
CODE_NAME_RE = re.compile(r'^[a-z]+_[a-z_]+$')  # snake_case
CAMEL_CLASS_RE = re.compile(r'^[A-Z][a-zA-Z]*$')  # PascalCase single word

# Common function-name patterns to filter (1-2 mentions and looks like code)
def is_codey_name(name: str) -> bool:
    if CODE_NAME_RE.match(name) and len(name.split('_')) <= 3:
        return True
    return False

def classify_entity(e: dict) -> str:
    """Returns 'real', 'borderline', or 'noise'.

    Heuristics tuned by entity type:
    - PERSON, PROJECT, TOOL, MODEL: high-value, 2+ mentions = real
    - CONCEPT: noisier, 3+ mentions = real
    - Anything with explicit noise patterns: noise regardless of count
    """
    name = e["name"]
    mentions = e["mention_count"]
    etype = e["type"]

    # Hard noise (regardless of count or type)
    if NOISE_KEYWORDS_RE.search(name):
        return "noise"
    if is_codey_name(name):
        return "noise"
    if name.lower() in {"json", "html", "css", "sql", "xml", "yaml", "toml"}:
        return "noise"
    if re.search(r'\d{4}-\d{2}-\d{2}', name):
        return "noise"

    # High-value entity types: 2+ mentions = real
    if etype in ("PERSON", "PROJECT", "TOOL", "MODEL"):
        if mentions >= 2:
            return "real"
        if mentions == 1 and e["confidence"] >= 0.95:
            return "borderline"
        return "noise"

    # CONCEPT type: noisier, need 3+ mentions for real
    if etype == "CONCEPT":
        if mentions >= 3:
            return "real"
        if mentions == 2 and e["confidence"] >= 0.95:
            return "borderline"
        return "noise"

    # Unknown type: borderline by default
    if mentions >= 2:
        return "borderline"
    return "noise"


entity_class = {e["name"]: classify_entity(e) for e in ents}
classification_counts = Counter(entity_class.values())
print("=== Entity classification ===")
for cls in ["real", "borderline", "noise"]:
    print(f"  {cls:11} {classification_counts.get(cls, 0):3}")

# Show the borderline + noise entities so we can decide
print()
print("=== BORDERLINE entities (mention_count=2, or single high-confidence) ===")
borderline = sorted(
    [e for e in ents if entity_class[e["name"]] == "borderline"],
    key=lambda x: (-x["mention_count"], -x["confidence"]),
)
for e in borderline[:30]:
    print(f"  {e['mention_count']}× {e['name']!r:30} [{e['type']:8}]  conf={e['confidence']:.2f}")
print(f"  ... {len(borderline)-30 if len(borderline) > 30 else 0} more")

# ------------------------------------------------------------------
# Relation classification
# ------------------------------------------------------------------
def classify_relation(r: dict, entity_class: dict) -> str:
    """Returns 'real', 'borderline', or 'noise'."""
    a, b, rtype = r["a"], r["b"], r["type"]
    a_cls = entity_class.get(a, "noise")
    b_cls = entity_class.get(b, "noise")
    # Both endpoints real → real
    if a_cls == "real" and b_cls == "real":
        if r["confidence"] >= 0.85:
            return "real"
        return "borderline"
    # Either endpoint borderline → borderline
    if a_cls == "borderline" or b_cls == "borderline":
        return "borderline"
    # Either endpoint noise → noise
    return "noise"


rel_class = {i: classify_relation(r, entity_class) for i, r in enumerate(rels)}
rel_class_counts = Counter(rel_class.values())
print()
print("=== Relation classification ===")
for cls in ["real", "borderline", "noise"]:
    print(f"  {cls:11} {rel_class_counts.get(cls, 0):3}")

# Show the noise relations
print()
print("=== NOISE relations (one or both endpoints classified as noise) ===")
noise_rels = [(i, r) for i, r in enumerate(rels) if rel_class[i] == "noise"]
for i, r in noise_rels[:25]:
    print(f"  {r['type']:14} {r['a']!r:30} → {r['b']!r:30}  conf={r['confidence']:.2f}")
print(f"  ... {len(noise_rels)-25 if len(noise_rels) > 25 else 0} more")

# Show borderline relations
print()
print("=== BORDERLINE relations (both endpoints real, but confidence < 0.85) ===")
bord_rels = [(i, r) for i, r in enumerate(rels) if rel_class[i] == "borderline"]
for i, r in bord_rels[:25]:
    print(f"  {r['type']:14} {r['a']!r:30} → {r['b']!r:30}  conf={r['confidence']:.2f}")
print(f"  ... {len(bord_rels)-25 if len(bord_rels) > 25 else 0} more")

# ------------------------------------------------------------------
# Quality summary
# ------------------------------------------------------------------
print()
print("=" * 60)
print("L5 Quality Review — Summary")
print("=" * 60)
n_real_e = classification_counts.get("real", 0)
n_real_r = rel_class_counts.get("real", 0)
total_e = len(ents)
total_r = len(rels)
print(f"  Entities:  {n_real_e}/{total_e} classified REAL ({100*n_real_e/max(1,total_e):.0f}%)")
print(f"  Relations: {n_real_r}/{total_r} classified REAL ({100*n_real_r/max(1,total_r):.0f}%)")
print()
# What would the Kuzu graph look like if we store only "real" entities + relations between them?
real_ents = [e for e in ents if entity_class[e["name"]] == "real"]
real_rels = [r for i, r in enumerate(rels) if rel_class[i] == "real"]
print(f"  Storing to Kuzu (REAL only):")
print(f"    {len(real_ents)} entities, {len(real_rels)} relations")
# Type distribution of real entities
type_counts = Counter(e["type"] for e in real_ents)
print()
print(f"  Type distribution of REAL entities:")
for t, c in type_counts.most_common():
    print(f"    {c:3}  {t}")
# Type distribution of real relations
rel_type_counts = Counter(r["type"] for r in real_rels)
print()
print(f"  Type distribution of REAL relations:")
for t, c in rel_type_counts.most_common():
    print(f"    {c:3}  {t}")

# Sample of real relations (sorted by mention count of a)
print()
print("=== Sample of REAL relations (top 30 by source-fact diversity) ===")
real_rels_sorted = sorted(real_rels, key=lambda r: (-r["confidence"],))[:30]
for r in real_rels_sorted:
    print(f"  {r['type']:14} {r['a']!r:24} → {r['b']!r:24}  conf={r['confidence']:.2f}")

# Save the recommended Kuzu store subset
recommended = {
    "store": True,
    "entities": real_ents,
    "relations": real_rels,
    "stats": {
        "n_entities": len(real_ents),
        "n_relations": len(real_rels),
        "entity_type_distribution": dict(type_counts),
        "relation_type_distribution": dict(rel_type_counts),
        "entity_classification_counts": dict(classification_counts),
        "relation_classification_counts": dict(rel_class_counts),
    },
}
(OUT_DIR / "l5_quality_review.json").write_text(
    json.dumps(recommended, indent=2, ensure_ascii=False), encoding="utf-8"
)
print()
print(f"Saved recommended Kuzu-store subset to: {OUT_DIR / 'l5_quality_review.json'}")
