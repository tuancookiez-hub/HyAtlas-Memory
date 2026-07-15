"""
L5 Phase 2.1a — NER prototype.

Read-only: pulls 200 sampled L2_facts from disk, runs spaCy NER, shows the
entity distribution. No writes to Qdrant or Kuzu. Pure analysis.

Outputs:
  - Top entities by frequency
  - Entity type distribution (PERSON, ORG, PRODUCT, etc.)
  - Sample of "noisy" extractions (low-info entities)
  - 5-10 example entities with their source sentence
"""
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import spacy

# Load the small English model
nlp = spacy.load("en_core_web_sm")

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / "AppData" / "Local" / "hermes")))
SAMPLE_PATH = HERMES_HOME / "logs" / "l2_sample_200.json"
facts = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
print(f"Loaded {len(facts)} L2_facts")

# spaCy NER (the small model labels)
ENTITY_LABELS = [
    "PERSON", "ORG", "GPE", "LOC", "PRODUCT", "EVENT",
    "WORK_OF_ART", "LAW", "LANGUAGE", "DATE", "TIME",
    "MONEY", "QUANTITY", "ORDINAL", "CARDINAL", "PERCENT",
    "FAC", "NORP",  # facilities, nationalities
]

# Custom labels the small model can't recognize but we care about
# (spaCy small doesn't have a way to add labels without retraining,
# so we'll just track any entity the model finds and manually bucket
# domain-specific terms afterwards.)
DOMAIN_KEYWORDS = {
    "Tool": [
        "Qdrant", "Chroma", "FAISS", "Docker", "Python", "FastAPI",
        "sentence-transformers", "spaCy", "Kuzu", "OpenClaw",
        "Hermes", "Hindsight", "Mem0", "Zep", "Graphiti", "Mysql",
        "XAMPP", "php", "React", "Bun", "TypeScript", "LangChain",
    ],
    "Project": [
        "Hy-Memory", "TNB Labs", "TNB", "PTD", "IDM", "Facade of Jade",
        "SmallHackathon", "Hugging Face", "Gradio", "LoRA", "Hackathon",
    ],
    "Person": [
        "<user>", "<user2>", "<user3>", "<user4>", "Tencent",
    ],
    "Model": [
        "Qwen3-4B", "Qwen", "dola-seed", "DeepSeek", "BGE", "BAAI",
    ],
    "Concept": [
        "LongMemEval", "L1_RAW", "L2_FACT", "L4_IDENTITY", "L5",
        "Kuzu", "VDB", "FAISS", "System2",
    ],
}

all_entities = []  # list of (text, label, source_fact_id)
entity_counter = Counter()
type_counter = Counter()
source_per_entity = defaultdict(list)

for fact in facts:
    text = fact["content"]
    if not text or len(text) < 5:
        continue
    doc = nlp(text[:2000])  # truncate very long facts to keep it fast

    # Standard spaCy entities
    for ent in doc.ents:
        if ent.label_ in ENTITY_LABELS and len(ent.text.strip()) > 1:
            ent_text = ent.text.strip()
            all_entities.append((ent_text, ent.label_, fact["id"]))
            entity_counter[ent_text] += 1
            type_counter[ent.label_] += 1
            if len(source_per_entity[ent_text]) < 3:
                source_per_entity[ent_text].append(fact["id"][:8])

    # Domain-specific keyword matching (simple exact-string match, case-insensitive)
    text_lower = text.lower()
    for cat, kws in DOMAIN_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in text_lower:
                # Count as entity
                all_entities.append((kw, f"DOMAIN-{cat}", fact["id"]))
                entity_counter[kw] += 1
                type_counter[f"DOMAIN-{cat}"] += 1
                if len(source_per_entity[kw]) < 3:
                    source_per_entity[kw].append(fact["id"][:8])

print()
print(f"=== Total entities extracted: {len(all_entities)} ===")
print()
print(f"=== Top 30 entities by frequency (likely real, useful) ===")
for ent, count in entity_counter.most_common(30):
    label = Counter(l for t, l, _ in all_entities if t == ent).most_common(1)[0][0]
    print(f"  {count:3}× {ent!r}  ({label})")

print()
print(f"=== Entity TYPE distribution ===")
for typ, count in type_counter.most_common():
    print(f"  {count:5}  {typ}")

print()
print(f"=== Sample of low-frequency entities (could be noise or one-off mentions) ===")
rare = [(e, c) for e, c in entity_counter.items() if c == 1]
print(f"  Total unique single-mention entities: {len(rare)}")
print(f"  Sample (10 random):")
import random
random.seed(42)
for e, _ in random.sample(rare, min(10, len(rare))):
    print(f"    {e!r}")

print()
print(f"=== Quality check: entities with 3+ mentions (high signal) ===")
high_signal = [(e, c) for e, c in entity_counter.items() if c >= 3]
high_signal.sort(key=lambda x: -x[1])
print(f"  Total high-signal entities (≥3 mentions): {len(high_signal)}")
print(f"  Top 15:")
for e, c in high_signal[:15]:
    print(f"    {c}× {e!r}")

print()
print(f"=== Verdict ===")
total = len(entity_counter)
high_signal_count = len(high_signal)
ratio = high_signal_count / total if total else 0
print(f"  Unique entities: {total}")
print(f"  High-signal (≥3 mentions): {high_signal_count} ({ratio*100:.0f}%)")
if ratio > 0.3:
    print(f"  ✅ NER quality looks GOOD. Lots of repeated real entities. L5 is worth building.")
elif ratio > 0.1:
    print(f"  ⚠️  NER quality is MEDIUM. Some real entities, some noise. L5 needs careful tuning.")
else:
    print(f"  ❌ NER quality is POOR. Most entities appear once. L5 may not be worth the build cost.")
