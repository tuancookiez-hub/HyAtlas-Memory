"""L5 in-process knowledge graph extraction.

Runs inside S2's digest cycle as a peer step after the cross-domain sweeper.
Extracts entities + relations from L2 facts, writes to Kuzu (same process,
same connection — no lock conflict), and embeds entities to Qdrant for
semantic search.

Feature flag: MEMORY_L5_VERSION=2 enables this. =1 keeps old subprocess.
Unset = L5 off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Config from env
_L5_VERSION = os.getenv("MEMORY_L5_VERSION", "").strip().lower()
# Enabled by default post-v3.1.0 (zvec-only). Only the legacy stop-server
# batch mode ("1") disables the in-process path.
_L5_ENABLED = _L5_VERSION != "1"
_L5_WATERMARK_PATH = Path(os.getenv(
    "MEMORY_L5_WATERMARK_PATH",
    str(Path.home() / "AppData" / "Local" / "hermes" / "logs" / "l5_pipeline_state.json"),
))
_L5_ENTITY_FLOOR = float(os.getenv("MEMORY_L5_ENTITY_FLOOR", "0.4"))
_L5_RELATION_FLOOR = float(os.getenv("MEMORY_L5_RELATION_FLOOR", "0.6"))
_L5_DEDUP_MERGE = float(os.getenv("MEMORY_L5_DEDUP_MERGE", "0.92"))
_L5_DEDUP_REVIEW = float(os.getenv("MEMORY_L5_DEDUP_REVIEW", "0.75"))
_L5_MAX_FACTS_PER_DIGEST = int(os.getenv("MEMORY_L5_MAX_FACTS", "50"))
# Legacy Qdrant endpoint — removed in v3.1.0 (zvec is the only VDB now).
# Kept for backward-compat reads of MEMORY_L5_QDRANT_URL but unused by default.
_L5_QDRANT_URL = os.getenv("MEMORY_L5_QDRANT_URL", "http://127.0.0.1:6333")
_L5_COLLECTION = os.getenv("MEMORY_L5_COLLECTION", "agent_memories_1024")

# Entity types (aligned with Hindsight — 6 types, not 9)
ENTITY_TYPES = {"PERSON", "ORGANIZATION", "LOCATION", "PRODUCT", "CONCEPT", "OTHER"}

# Relation types
RELATION_TYPES = {
    "owns", "uses", "works_on", "depends_on", "replaces", "related_to",
    "is_a", "part_of", "member_of", "purchased_from", "researched", "built_by",
    "visited", "lives_in", "friend_of", "happened_at",
}

# Known aliases
ALIAS_MAP = {
    "herm": "Hermes", "hermes agent": "Hermes", "hermes-agent": "Hermes",
    "hermes cli": "Hermes", "hermes ai": "Hermes",
    "hy_memory": "Hy-Memory", "hy-memory": "Hy-Memory",
    # Username aliases — override via HERMES_DISPLAY_NAME env var (default: "User")
}

SYSTEM_PROMPT = """You extract a knowledge graph from memory facts.

You will be given 1-3 memory facts. For each fact, identify:

1. Entities — concrete things mentioned (people, organizations, locations, products, concepts)
2. Relations — typed connections between entities in the same batch

Entity types: PERSON, ORGANIZATION, LOCATION, PRODUCT, CONCEPT, OTHER

Relation types: owns, uses, works_on, depends_on, replaces, related_to, is_a, part_of, member_of, purchased_from, researched, built_by, visited, lives_in, friend_of, happened_at

## Rules
- Only extract entities EXPLICITLY named in the fact text. No inference.
- Extract relations between entities in the same batch of facts (not just same fact).
- Be LENIENT with entity types — a phone is PRODUCT, a city is LOCATION.
- Skip relations where confidence < 0.6.
- Resolve obvious aliases: "Hermes Agent" = "Hermes" = "hermes-agent".
- Date/number mentions are NOT entities.
- Generic words ("the user", "the system") are NOT entities.
- Internal class/file/branch names are NOT useful entities — skip them.

## Output (strict JSON, no prose outside)
{
  "facts": [
    {
      "fact_id": "<id from input>",
      "entities": [
        {"name": "Hermes", "type": "PRODUCT", "confidence": 0.95}
      ],
      "relations": [
        {"a": "<user>", "b": "Hermes", "type": "uses", "confidence": 0.90}
      ]
    }
  ]
}"""


def _strip_think_blocks(text: str) -> str:
    """Strip LLM think-block wrappers and markdown json fences."""
    # Strip unicode think blocks (MiniMax-M3: \u22d6...\u22d7)
    text = re.sub(r"\u22d6.*?\u22d7", "", text, flags=re.DOTALL)
    # Strip XML think blocks (standard: <think>...</think>)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Strip [thinking]...[/thinking] blocks
    text = re.sub(r"\[thinking\].*?\[/thinking\]", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Fallback: if an opening think tag exists but no closing tag, strip to first {
    # This handles truncated responses where the closing tag was cut off
    if "\u22d6" in text and "{" in text:
        idx = text.index("{")
        pre = text[:idx]
        if "\u22d6" in pre:
            text = text[idx:]
    if "<think>" in text and "{" in text:
        idx = text.index("{")
        pre = text[:idx]
        if "<think>" in pre:
            text = text[idx:]
    text = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.split("\n")
            if not line.strip().startswith("```")
        )
    return text.strip()


def _parse_llm_json(text: str) -> dict | None:
    """Best-effort JSON parse for truncated or noisy LLM output."""
    text = _strip_think_blocks(text)
    if not text:
        return None

    candidates: list[str] = [text]

    # Find ALL json blocks (markdown-fenced or bare) and try largest first
    blocks = re.findall(r"\{[^{}]*\}|```json\s*(\{[\s\S]*?\})\s*```|\{[\s\S]*\}", text)
    for block in reversed(blocks):
        if block:
            candidates.append(block)

    start = text.rfind("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    start = text.find("{")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        # Brace-balance salvage (common when model truncates mid-string)
        for trim in range(len(cand), max(len(cand) - 2000, 0), -40):
            sub = cand[:trim].rstrip().rstrip(",")
            if "{" not in sub:
                continue
            need_brace = sub.count("{") - sub.count("}")
            need_bracket = sub.count("[") - sub.count("]")
            suffix = "]" * max(need_bracket, 0) + "}" * max(need_brace, 0)
            try:
                parsed = json.loads(sub + suffix)
                if isinstance(parsed, dict) and parsed.get("facts") is not None:
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


def _normalize_name(name: str) -> str:
    n = name.strip()
    n = re.sub(r"\s+", " ", n)
    return ALIAS_MAP.get(n.lower(), n)


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower()).strip("_")
    return f"l5_{s}"[:60]


def _qdrant_point_id(node_id: str) -> str:
    """Stable UUID for Qdrant (collection expects UUID-shaped ids)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hyatlas-l5:{node_id}"))


def _resolve_embed_service(s2_writer) -> Any | None:
    for obj in (s2_writer, getattr(s2_writer, "_writer", None)):
        if obj is None:
            continue
        svc = getattr(obj, "_embed_service", None) or getattr(obj, "embed_service", None)
        if svc is not None:
            return svc
    return None


def _read_watermark() -> float:
    """Read last processed timestamp from state file."""
    try:
        if _L5_WATERMARK_PATH.exists():
            state = json.loads(_L5_WATERMARK_PATH.read_text(encoding="utf-8"))
            ts = state.get("l5_watermark", 0)
            return float(ts) if ts else 0.0
    except Exception:
        pass
    return 0.0


def _write_watermark(ts: float) -> None:
    """Update watermark after successful processing."""
    try:
        state = {}
        if _L5_WATERMARK_PATH.exists():
            state = json.loads(_L5_WATERMARK_PATH.read_text(encoding="utf-8"))
        state["l5_watermark"] = ts
        state["l5_last_run"] = datetime.now().isoformat()
        _L5_WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        _L5_WATERMARK_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[L5] could not write watermark: {e}")


def _l5_user_ids(primary: str) -> list[str]:
    """All user_ids that share this Hermes memory store (facts are sharded by id)."""
    raw = os.getenv("MEMORY_L5_USER_IDS", "").strip()
    if raw:
        ids = [x.strip() for x in raw.split(",") if x.strip()]
    else:
        ids = ["hermes-user", "<discord_user_id>"]  # override via MEMORY_L5_USER_IDS env var
    if primary and primary not in ids:
        ids.insert(0, primary)
    return ids


def _fact_ts(payload: dict) -> float:
    gmt = payload.get("gmt_created") or payload.get("memory_at") or 0
    if isinstance(gmt, (int, float)):
        return float(gmt)
    if isinstance(gmt, datetime):
        return gmt.timestamp()
    if isinstance(gmt, str):
        s = gmt.strip()
        # Unix timestamp as a string
        try:
            return float(s)
        except ValueError:
            pass
        # ISO datetime string
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


async def _get_recent_l3_facts(vector_store, user_id: str, watermark: float, limit: int = 50) -> list[dict]:
    """Get L2 facts created after the watermark from the live zvec vector store.

    Replaces the legacy Qdrant scroll path (Qdrant was removed in v3.1.0; zvec
    is the only VDB). Reads all store user_ids so facts sharded across ids are
    all seen.

    NOTE: zvec's ``list_by_user`` uses a zero-vector probe; when a ``layers``
    filter is passed the combination returns nothing (the zero-vector query
    scores 0 and is dropped before the filter applies). The proven enumeration
    path used by ``list_memories`` is to fetch ALL active nodes and filter the
    layer in Python — so we do exactly that here.
    """
    try:
        from hyatlas_memory.core.models.memory import MemoryStatus

        user_ids = _l5_user_ids(user_id)
        facts: list[dict] = []

        for uid in user_ids:
            try:
                nodes = await vector_store.list_by_user(
                    user_id=uid,
                    limit=20000,
                    status_filter=[MemoryStatus.ACTIVE],
                )
            except Exception as e:
                logger.warning(f"[L5] list_by_user failed for {uid}: {e}")
                continue
            for n in nodes:
                # n.layer may be a MemoryLayer enum, the bare value "l3_fact",
                # or the enum repr string "MemoryLayer.L3_FACT" (as stored in zvec).
                layer_val = getattr(n, "layer", None)
                if hasattr(layer_val, "value"):
                    layer_val = layer_val.value
                layer_val = str(layer_val).replace("MemoryLayer.", "").lower()
                if layer_val != "l3_fact":
                    continue
                # Prefer the node attribute; custom may be a JSON string.
                gmt = getattr(n, "gmt_created", None) or getattr(n, "memory_at", None)
                if not gmt:
                    custom = getattr(n, "custom", None)
                    if isinstance(custom, dict):
                        gmt = custom.get("gmt_created") or custom.get("memory_at")
                ts = _fact_ts({"gmt_created": gmt, "memory_at": None})
                if ts > watermark:
                    facts.append({
                        "id": n.node_id,
                        "content": n.content or "",
                        "gmt_created": ts,
                    })

        facts.sort(key=lambda f: f["gmt_created"])
        return facts[:limit]

    except Exception as e:
        logger.warning(f"[L5] could not fetch L2 facts from vector store: {e}")
        return []


async def _llm_extract(facts: list[dict], llm_call) -> tuple[list[dict], list[dict]]:
    """Call LLM to extract entities + relations from facts (small batches)."""
    if not facts or not llm_call:
        return [], []

    entities: list[dict] = []
    relations: list[dict] = []
    batch_size = max(1, int(os.getenv("MEMORY_L5_BATCH_SIZE", "2")))

    for offset in range(0, len(facts), batch_size):
        batch = facts[offset : offset + batch_size]
        lines = [f"Extract entities and relations from these {len(batch)} facts:\n"]
        for f in batch:
            content = f["content"][:600]
            lines.append(f'Fact id={f["id"]}:')
            lines.append(f'  "{content}"')
        user_prompt = "\n".join(lines)

        try:
            full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"
            try:
                response = await llm_call(full_prompt)
            except TypeError:
                response = await llm_call(SYSTEM_PROMPT, user_prompt)
            if response is None:
                continue
            if isinstance(response, dict):
                text = response.get("content") or response.get("text") or json.dumps(response)
            else:
                text = str(response)

            parsed = _parse_llm_json(text)
            if not parsed:
                logger.warning("[L5] LLM JSON parse failed for batch offset=%d, text[:200]=%s", offset, repr(text[:200]))
                continue

            for fact_result in parsed.get("facts", []):
                for ent in fact_result.get("entities", []):
                    if ent.get("confidence", 0) >= _L5_ENTITY_FLOOR:
                        ent["name"] = _normalize_name(ent["name"])
                        ent["source_fact_id"] = fact_result.get("fact_id", "")
                        entities.append(ent)

                for rel in fact_result.get("relations", []):
                    if rel.get("confidence", 0) >= _L5_RELATION_FLOOR:
                        rel["a"] = _normalize_name(rel["a"])
                        rel["b"] = _normalize_name(rel["b"])
                        if rel.get("type") in RELATION_TYPES:
                            relations.append(rel)

        except TypeError:
            # Older llm_call signature: single combined prompt string
            try:
                response = await llm_call(SYSTEM_PROMPT + "\n\n" + user_prompt)
                text = _strip_think_blocks(str(response))
                parsed = _parse_llm_json(text)
                if not parsed:
                    logger.warning("[L5] LLM JSON parse failed (legacy call) offset=%d", offset)
                    continue
                for fact_result in parsed.get("facts", []):
                    for ent in fact_result.get("entities", []):
                        if ent.get("confidence", 0) >= _L5_ENTITY_FLOOR:
                            ent["name"] = _normalize_name(ent["name"])
                            ent["source_fact_id"] = fact_result.get("fact_id", "")
                            entities.append(ent)
                    for rel in fact_result.get("relations", []):
                        if rel.get("confidence", 0) >= _L5_RELATION_FLOOR:
                            rel["a"] = _normalize_name(rel["a"])
                            rel["b"] = _normalize_name(rel["b"])
                            if rel.get("type") in RELATION_TYPES:
                                relations.append(rel)
            except Exception as e:
                logger.warning(f"[L5] LLM extraction failed (legacy): {e}")
        except json.JSONDecodeError as e:
            logger.warning(f"[L5] LLM JSON parse failed: {e}")
        except Exception as e:
            logger.warning(f"[L5] LLM extraction failed: {e}")

    return entities, relations


async def _entity_embeddings(s2_writer, names: list[str]) -> dict[str, list[float]]:
    """Batch-embed entity names for Kuzu CREATE (indexed embedding column is required)."""
    if not names:
        return {}
    import asyncio

    unique = list(dict.fromkeys(names))
    embeddings: list[list[float]] | None = None
    embed_service = _resolve_embed_service(s2_writer)
    if embed_service is not None:
        embeddings = await embed_service.embed_batch(unique)
    if embeddings is None:
        try:
            from hyatlas_memory import patches as _patches  # noqa: WPS433
            model = getattr(_patches, "_local_embed_model", None)
        except Exception:
            model = None
        if model is not None:
            vecs = await asyncio.to_thread(model.encode, unique, convert_to_numpy=True)
            embeddings = [v.tolist() for v in vecs]
    if embeddings is None:
        import requests
        embed_url = os.getenv("MEMORY_L5_EMBED_URL", "http://127.0.0.1:19528/v1/embeddings")
        resp = requests.post(embed_url, json={"input": unique}, timeout=60)
        resp.raise_for_status()
        vectors = resp.json()["data"]
        embeddings = [v["embedding"] for v in vectors]
    return dict(zip(unique, embeddings, strict=False))


async def _resolve_and_write_entities(
    graph_store,
    entities: list[dict],
    user_id: str,
    agent_id: str,
    embed_by_name: dict[str, list[float]] | None = None,
) -> tuple[dict[str, str], int]:
    """Resolve entities against existing Kuzu nodes, write new ones. Returns (name→node_id, written)."""
    from hyatlas_memory.core.models.memory import MemoryLayer, MemoryNode, MemoryStatus, SourceType

    name_to_id = {}
    written = 0
    merged = 0

    for ent in entities:
        name = ent["name"]
        etype = ent.get("type", "OTHER")
        if etype not in ENTITY_TYPES:
            etype = "OTHER"

        node_id = _slugify(name)

        # Check if entity already exists
        try:
            existing = await graph_store.get_node(node_id)
            if existing:
                # Entity exists — merge (add alias, update mention count)
                name_to_id[name] = node_id
                merged += 1
                continue
        except Exception:
            pass

        # Create new entity node
        try:
            node = MemoryNode(
                node_id=node_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id="default_session",
                layer=MemoryLayer.L5_KNOWLEDGE,
                content=name,
                status=MemoryStatus.ACTIVE,
                confidence=ent.get("confidence", 0.7),
                source_type=SourceType.INFERRED,
                gmt_created=datetime.now(),
                valid_from=datetime.now(),
                custom={
                    "entity_type": etype,
                    "content_type": f"ENTITY_{etype}",
                    "source_fact_id": ent.get("source_fact_id", ""),
                },
                tags=[f"ENTITY_{etype}"],
            )
            emb = (embed_by_name or {}).get(name)
            if emb:
                node._graph_embedding = emb
            await graph_store.upsert_memory_node(node)
            name_to_id[name] = node_id
            written += 1
        except Exception as e:
            logger.warning(f"[L5] could not write entity {name}: {e}")

    logger.info(f"[L5] entities: {written} written, {merged} merged, {len(entities)} total")
    return name_to_id, written


async def _write_relations(
    graph_store, relations: list[dict], name_to_id: dict[str, str]
) -> int:
    """Write relation edges to Kuzu."""
    written = 0
    for rel in relations:
        a_id = name_to_id.get(rel["a"])
        b_id = name_to_id.get(rel["b"])
        if not a_id or not b_id:
            continue

        try:
            await graph_store.add_edge(
                a_id, b_id, "RELATED_TO",
                {
                    "relation_type": rel["type"],
                    "weight": rel.get("confidence", 0.8),
                }
            )
            written += 1
        except Exception as e:
            logger.warning(f"[L5] could not write relation {rel['a']}→{rel['b']}: {e}")

    logger.info(f"[L5] relations: {written} written, {len(relations)} total")
    return written


async def _embed_entities_to_vdb(
    s2_writer,
    entities: list[dict],
    name_to_id: dict[str, str],
    user_id: str,
    agent_id: str,
) -> bool:
    """Embed entity names and upsert them into the live zvec VDB for semantic search.

    Replaces the legacy Qdrant write (Qdrant removed in v3.1.0). Entities are
    written as ``l5_knowledge`` nodes in the same zvec collection the rest of the
    memory lives in, so they surface in normal recall. Best-effort: any failure
    is logged and returns False so the graph write is still counted as progress.
    """
    if not entities:
        return False

    vector_store = getattr(s2_writer, "_vector_store", None)
    if vector_store is None:
        logger.warning("[L5] no vector_store on s2_writer; skipping entity embed")
        return False

    from hyatlas_memory.core.models.memory import (
        MemoryLayer,
        MemoryNode,
        MemoryStatus,
        SourceType,
    )

    try:
        texts = [ent["name"] for ent in entities]
        embeddings: list[list[float]] | None = None

        embed_service = _resolve_embed_service(s2_writer)
        if embed_service is not None:
            embeddings = await embed_service.embed_batch(texts)

        if embeddings is None:
            try:
                from hyatlas_memory import patches as _patches  # noqa: WPS433
                model = getattr(_patches, "_local_embed_model", None)
            except Exception:
                model = None
            if model is not None:
                vecs = await asyncio.to_thread(model.encode, texts, convert_to_numpy=True)
                embeddings = [v.tolist() for v in vecs]

        if embeddings is None:
            embed_url = os.getenv("MEMORY_L5_EMBED_URL", "http://127.0.0.1:19528/v1/embeddings")
            import requests
            resp = requests.post(embed_url, json={"input": texts}, timeout=30)
            vectors = resp.json()["data"]
            embeddings = [v["embedding"] for v in vectors]

        written = 0
        for i, ent in enumerate(entities):
            name = ent["name"]
            node_id = name_to_id.get(name, _slugify(name))
            try:
                node = MemoryNode(
                    node_id=node_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id="default_session",
                    layer=MemoryLayer.L5_KNOWLEDGE,
                    content=name,
                    status=MemoryStatus.ACTIVE,
                    confidence=ent.get("confidence", 0.7),
                    source_type=SourceType.INFERRED,
                    gmt_created=datetime.now(),
                    valid_from=datetime.now(),
                    embedding=embeddings[i],
                    custom={
                        "entity_type": ent.get("type", "OTHER"),
                        "content_type": f"ENTITY_{ent.get('type', 'OTHER')}",
                        "source_fact_id": ent.get("source_fact_id", ""),
                    },
                    tags=[f"ENTITY_{ent.get('type', 'OTHER')}"],
                )
                await vector_store.upsert(node)
                written += 1
            except Exception as e:
                logger.warning(f"[L5] could not upsert entity {name} to zvec: {e}")

        if written:
            logger.info(f"[L5] embedded {written} entities to zvec")
        return written > 0

    except Exception as e:
        logger.warning(f"[L5] entity embedding failed: {e}")
        return False


async def run_l5_inprocess(
    s2_writer,
    user_id: str,
    agent_id: str,
    llm_call,
    request_id: str = "",
) -> dict[str, Any]:
    """Main L5 in-process extraction. Called after sweeper in S2 digest.

    Non-blocking: any failure is logged and skipped. Never raises.
    """
    if not _L5_ENABLED:
        return {"skipped": "MEMORY_L5_VERSION != 2"}

    graph_store = getattr(s2_writer, "_graph_store", None)
    if not graph_store or not getattr(graph_store, "_available", False):
        return {"skipped": "graph_store not available"}

    vector_store = getattr(s2_writer, "_vector_store", None)
    if vector_store is None:
        return {"skipped": "vector_store not available"}

    start = time.time()

    try:
        # 1. Read watermark
        watermark = _read_watermark()

        # 2. Get recent L2 facts from the live zvec vector store
        facts = await _get_recent_l3_facts(vector_store, user_id, watermark, _L5_MAX_FACTS_PER_DIGEST)
        if not facts:
            return {"skipped": "no new facts since watermark"}

        # 3. LLM extraction
        entities, relations = await _llm_extract(facts, llm_call)
        if not entities:
            return {"skipped": "no entities extracted", "facts": len(facts)}

        # 4. Resolve + write entities to Kuzu (Kuzu CREATE requires content embedding)
        embed_by_name = await _entity_embeddings(s2_writer, [e["name"] for e in entities])
        name_to_id, ent_written = await _resolve_and_write_entities(
            graph_store, entities, user_id, agent_id, embed_by_name
        )

        # 5. Write relations to Kuzu
        rel_count = await _write_relations(graph_store, relations, name_to_id)

        # 6. Embed entities into zvec VDB for semantic recall
        vdb_ok = await _embed_entities_to_vdb(s2_writer, entities, name_to_id, user_id, agent_id)

        # 7. Update watermark only when something actually persisted
        latest_ts = max(f["gmt_created"] for f in facts)
        if ent_written > 0 or rel_count > 0 or vdb_ok:
            _write_watermark(latest_ts)
            graph_store._checkpoint()
        else:
            logger.warning("[L5] watermark not advanced: no Kuzu/zvec writes succeeded")

        elapsed = time.time() - start
        result = {
            "facts": len(facts),
            "entities": len(entities),
            "relations": rel_count,
            "elapsed_ms": round(elapsed * 1000),
            "watermark": latest_ts,
        }
        logger.info(f"[L5] in-process done: {result}")
        return result

    except Exception as e:
        logger.warning(f"[L5] in-process failed (non-blocking): {e}")
        return {"error": str(e)}


def l5_rollback() -> dict[str, Any]:
    """Rollback: drop all L5 nodes from Kuzu. Leaves L1-L4/L6/L7 intact.

    v3.1.0+: L5 knowledge lives in zvec, not Qdrant. The Qdrant HTTP
    rollback path that previously lived here was removed because there
    is no Qdrant sidecar in zvec-only installs. To clear L5 nodes on a
    live zvec store, call ``ZvecVectorStore.delete_by_filter`` directly
    with ``layer = 'l5_knowledge'`` (or wipe the home directory while
    the server is stopped).

    Requires the upstream to be stopped (for Kuzu) — or run from inside the process.
    """
    results = {"kuzu": "skipped", "qdrant": "not_applicable", "watermark": "reset"}

    # Reset watermark
    try:
        _write_watermark(0.0)
        results["watermark"] = "reset to 0"
    except Exception as e:
        results["watermark"] = f"error: {e}"

    # v3.1.0+: L5 lives in zvec, no Qdrant sidecar. Result key preserved
    # for callers that still inspect the legacy "qdrant" field.
    results["qdrant"] = "no Qdrant in v3.1+; use ZvecVectorStore.delete_by_filter"

    # Kuzu deletion needs to be done from inside the server process
    # or with the server stopped. We can't do it from here safely.
    results["kuzu"] = "requires server restart with L5 disabled, then manual Kuzu cleanup"
    results["note"] = "To fully rollback: set MEMORY_L5_VERSION= (empty), restart upstream, then run: MATCH (m:Memory {layer: 'l5_knowledge'}) DETACH DELETE m"

    return results
