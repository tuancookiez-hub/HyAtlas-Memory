This is a research pack on memory system design for HyAtlas, prepared for a decision on whether and how to improve the current graph memory structure. It covers how leading systems (MemGPT/Letta, Zep/Graphiti, Cognee, Reflection) model memory, what human cognitive science suggests, and how HyAtlas's current L0-L7 stack maps to those ideas. The recommendation is to keep the 7-layer architecture but fix three specific gaps: entity typing, episodic-to-semantic consolidation, and relation quality. Philosophical speculation about brain replication is not included; the focus is on production memory systems that improve recall and reasoning.

# Memory System Design Research for HyAtlas

## 1. How Leading Systems Organize Memory

### 1.1 MemGPT / Letta: The OS Analogy
MemGPT (Packer et al., 2023, UC Berkeley) treats the LLM context window as RAM and manages memory tiers through a virtual memory system. The core idea is not what is stored, but how it is moved into and out of the agent's immediate attention. Letta, the successor framework, extends this to explicit memory blocks: `persona`, `human`, `core`, and custom blocks that are in-context, plus an `archival` store for long-term retrieval. Agents can edit their own memory blocks using tools like `memory_insert`, `memory_replace`, and `memory_rethink`. The content of those memories is mostly free-form text; the architecture does not enforce semantic typing of entities or episodic structure. It is context management first, knowledge representation second.

Key takeaway: **Tiered context management is solved well. Typed memory is not.**

### 1.2 Zep / Graphiti: Temporal Knowledge Graph
Zep (Daniel et al., 2025, arXiv:2501.13956) is explicitly built around a temporal knowledge graph. It uses three subgraphs:
- **Episode subgraph**: raw interactions, messages, and observations.
- **Semantic entity subgraph**: entities extracted from episodes, resolved against existing entities, and connected by semantic edges.
- **Community subgraph**: derived clusters for higher-level retrieval.

Zep's design separates *what happened* (episodic) from *what is known* (semantic), and it tracks how facts change over time. This is a direct response to the failure mode of plain RAG: treating facts as static. The Graphiti engine adds custom entity types (e.g., `PERSON`, `ORGANIZATION`, `PRODUCT`, `PROJECT`) so the graph does not collapse into a single generic concept type. This is the critical feature your current graph is missing.

Key takeaway: **Episodic + semantic separation with typed entities and temporal edges is the current best-practice for agent memory graphs.**

### 1.3 Cognee: Knowledge Extraction Pipeline
Cognee focuses on extracting structured knowledge from documents, Slack, Notion, and other corpora. It is less about live episodic memory and more about building a queryable graph from existing data. It is relevant to HyAtlas as a comparison point for batch ingestion, but less relevant for real-time session memory.

Key takeaway: **Batch knowledge extraction and live episodic memory are different pipelines. HyAtlas is live-first.**

### 1.4 Reflection and Generative Agents (Park et al., 2023)
The Stanford Generative Agents paper introduced a memory stream scored by recency, importance, and relevance. Memories are observed, reflected upon, and plans are derived. Reflection turns raw observations into higher-level abstractions. The architecture is more about how agents behave socially than about how to store engineering knowledge, but the scoring function (recency × importance × relevance) is directly applicable to memory retention and retrieval.

Key takeaway: **Memory retrieval should rank by a composite score, not just vector similarity.**

## 2. What Human Memory Suggests (and Where It Helps)

Human memory is not a single store. The parts useful for AI memory design are:
- **Episodic memory**: specific events with context (time, place, sequence). This maps to L1 raw and L2 fact in your stack.
- **Semantic memory**: general knowledge, facts, and concepts. This maps to L5 knowledge graph entities.
- **Procedural memory**: skills, habits, how to do things. This maps to skills and L6 schema in your stack.
- **Working memory**: active context window. This is the LLM prompt itself.
- **Emotional tagging**: memory strength is modulated by emotional salience. AI analog: importance scoring.
- **Consolidation**: sleep/replay moves information from episodic to semantic. AI analog: periodic digest/reflection jobs.
- **Forgetting curve**: unrehearsed memories decay. AI analog: retention policies and pruning.

The human analogy is useful as a **taxonomy**, not as an implementation blueprint. Brains do not use UUIDs, vector embeddings, or Kuzu. But the distinction between *what happened*, *what is known*, and *how to act* is a durable organizing principle that improves retrieval and reasoning.

## 3. Current HyAtlas Stack: What Is Working and What Is Missing

Your stack has a clear 7-layer model:
- L0: basic info
- L1: raw
- L2: fact
- L3: summary
- L4: identity
- L5: knowledge graph entities
- L6: schema
- L7: intention

This is more sophisticated than MemGPT/Letta's free-form blocks and roughly aligned with Zep's layered approach. The current numbers (5,809 VDB points, 1,934 graph nodes, 5,236 relations) suggest the pipeline is active and functional.

### What is working
- L4 identity captures durable facts about the user.
- L5 graph entities are extracted from digests and build a knowledge web.
- L6 schema captures learned rules.
- L7 intention captures goals and plans.
- The pipeline is automatic and produces real output.

### What is missing or weak
1. **Entity typing is collapsed into `CONCEPT`**. Your graph has 1,262 nodes and all of them are `CONCEPT`. This means `Hermes`, `Malaysia`, `Chest press`, and `Vendor 3` are treated the same way. Typed entities (PERSON, ORG, PRODUCT, PROJECT, TECHNOLOGY, CONCEPT, LOCATION, etc.) improve retrieval precision and multi-hop reasoning because they constrain what kinds of relations make sense.
2. **Mention counts are not consolidating**. Every top node has `mention_count=1`. This means the entity resolver is not merging repeated references to the same thing. A well-consolidated graph would have `Hermes` with many mentions and `Chest press` with few; that signal alone tells you what is central and what is noise.
3. **Episodic/semantic separation is weak in the live graph**. L5 is supposed to be semantic knowledge, but it contains raw technical artifacts (`Sidebar.tsx`, `scripts/build.ts`, `package.json`) as concepts rather than as properties of a project or as episode references. Some of these should be in L1/L2 or linked to a project node, not flattened into the concept graph.
4. **No temporal or source edges visible**. Relations do not carry provenance (which session, which message, which time). This makes it hard to answer "when did I learn this?" or "what did I actually say?"
5. **Retrieval ranking is likely vector-only**. The S-class criteria mention hybrid_v2 reader, BM25, and cross-encoder reranking. If those are not yet implemented, the graph is being searched by embedding similarity only, which misses exact matches and phrase-level relevance.

## 4. Recommendation: Do Not Replicate the Brain; Fix the Three Gaps

You do not need a more philosophical memory model. You need to make the existing 7-layer model more precise. The three highest-impact changes are:

### Gap 1: Typed Entities (Weeks 1-2)
Introduce a small, fixed ontology for L5 nodes. Start with 8-12 types:
- `PERSON`
- `ORGANIZATION`
- `PRODUCT` (software, libraries, tools)
- `PROJECT` (user's own projects, hackathons)
- `TECHNOLOGY` (languages, frameworks, platforms)
- `CONCEPT` (abstract ideas)
- `LOCATION` (when relevant)
- `EVENT` (hackathons, releases, sessions)
- `SKILL` (learned capabilities)
- `FILE` (code files, config files)
- `API` (endpoints, services)
- `ISSUE` (bugs, blockers, tasks)

Update the L5 extraction pipeline (`l5_entity_resolver.py` or equivalent) to predict type during extraction, and store it in the `entity_type` field. This alone makes the graph meaningful and the dashboard useful.

### Gap 2: Entity Consolidation and Mention Counting (Weeks 2-3)
Make the entity resolver actually resolve. When `Hermes` appears in a new digest, it should merge with the existing `l5_hermes` node and increment `mention_count`. If two names are similar but ambiguous (e.g., `Hermes` vs `Hermes AI assistant agent`), use a disambiguation step: if they share aliases or context, merge; if not, keep separate. This requires:
- Canonical name selection
- Alias list maintenance
- Confidence adjustment based on repeated observation
- A merge log so you can audit what got combined

### Gap 3: Episodic/Semantic Linkage (Weeks 3-4)
Separate *what happened* from *what is known* in the graph edges:
- L1/L2 store episodic observations with source message IDs and timestamps.
- L5 entities are semantic summaries extracted from those episodes.
- Edges from L5 to L5 represent semantic relationships (e.g., `Hermes` `is_a` `AI agent`; `Hermes` `includes` `Herm TUI`).
- Edges from L5 to L1/L2 represent provenance (e.g., `Hermes` `mentioned_in` `session_2026_07_05`).

This gives you both the answer and the ability to trace back to where it came from.

## 5. S-Class Alignment

Your stated S-class criteria already include the right targets. This recommendation maps directly to them:
- `BM25 keyword search` → improves exact recall
- `graph quota merge` → fixes entity consolidation
- `cross-encoder reranking` → improves retrieval ranking beyond vectors
- `1024-dim+ embeddings` → already done
- `retention/forgetting policy` → use mention count + recency + importance
- `L7 proactive recall` → needs L5/L6 to be clean first, otherwise it recalls noise
- `LongMemEval benchmarking` → measures whether the above actually works

## 6. What to Avoid

- **Rebuilding around a brain metaphor without concrete retrieval gains.** The 7-layer design is already good. Do not add layers for philosophical completeness.
- **Custom ontology explosion.** Start with 8-12 types, not 50. You can expand once the resolver works.
- **Perfect entity resolution.** 80% correct consolidation with a visible merge log beats 95% correct consolidation that hides its mistakes.
- **Ignoring provenance.** Every L5 fact should be traceable to an L1/L2 source. Without this, the graph becomes un-auditable.

## 7. Suggested Next Step

Open a small design issue or patch in HyAtlas-Memory to add typed entity extraction to L5. The change is localized: update the extraction prompt / model call, store the type, and update the dashboard to color/filter by type. This is a concrete, measurable improvement that aligns with the S-class roadmap and with how the best current systems (Zep/Graphiti) actually work.