"""L5 in-process knowledge graph extraction.

Runs inside S2's digest cycle as a peer step after the cross-domain sweeper.
Extracts entities + relations from L2 facts, writes to Kuzu (same process,
same connection — no lock conflict), and embeds entities to Qdrant for
semantic search.

Feature flag: MEMORY_L5_VERSION=2 enables this. =1 keeps old subprocess.
Unset = L5 off.
"""
from __future__ import annotations

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
_L5_VERSION = os.getenv("MEMORY_L5_VERSION", "").strip()
_L5_ENABLED = _L5_VERSION == "2"
_L5_WATERMARK_PATH = Path(os.getenv(
    "MEMORY_L5_WATERMARK_PATH",
    str(Path.home() / "AppData" / "Local" / "hermes" / "logs" / "l5_pipeline_state.json"),
))
_L5_ENTITY_FLOOR = float(os.getenv("MEMORY_L5_ENTITY_FLOOR", "0.4"))
_L5_RELATION_FLOOR = float(os.getenv("MEMORY_L5_RELATION_FLOOR", "0.6"))
_L5_DEDUP_MERGE = float(os.getenv("MEMORY_L5_DEDUP_MERGE", "0.92"))
_L5_DEDUP_REVIEW = float(os.getenv("MEMORY_L5_DEDUP_REVIEW", "0.75"))
_L5_MAX_FACTS_PER_DIGEST = int(os.getenv("MEMORY_L5_MAX_FACTS", "50"))
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
    "tuanc": "TunaCookie", "tuna cookie": "TunaCookie",
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
        {"a": "TunaCookie", "b": "Hermes", "type": "uses", "confidence": 0.90}
      ]
    }
  ]
}"""


def _strip_think_blocks(text: str) -> str:
    """Strip LLM think-block wrappers and markdown json fences."""
    text = re.sub(r"⋖.*?⋗", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
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
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    if start >= 0:
        candidates.append(text[start:])

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
        ids = ["hermes-user", "tuanc", "221727702992945152"]
    if primary and primary not in ids:
        ids.insert(0, primary)
    return ids


def _fact_ts(payload: dict) -> float:
    gmt = payload.get("gmt_created") or payload.get("memory_at") or 0
    try:
        return float(gmt)
    except (TypeError, ValueError):
        return 0.0


async def _get_recent_l2_facts(graph_store, user_id: str, watermark: float, limit: int = 50) -> list[dict]:
    """Get L2 facts created after the watermark from Qdrant (all store user_ids)."""
    import requests
    try:
        user_ids = _l5_user_ids(user_id)
        must = [
            {"key": "layer", "match": {"value": "l2_fact"}},
            {"key": "status", "match": {"value": "active"}},
        ]
        if len(user_ids) == 1:
            must.append({"key": "user_id", "match": {"value": user_ids[0]}})
        else:
            must.append({"key": "user_id", "match": {"any": user_ids}})

        facts: list[dict] = []
        offset = None
        page_limit = min(256, max(limit * 4, 64))

        while len(facts) < limit:
            body: dict = {
                "limit": page_limit,
                "with_payload": True,
                "with_vector": False,
                "filter": {"must": must},
            }
            if offset is not None:
                body["offset"] = offset
            resp = requests.post(
                f"{_L5_QDRANT_URL}/collections/{_L5_COLLECTION}/points/scroll",
                json=body,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json().get("result") or {}
            points = data.get("points") or []
            if not points:
                break
            for p in points:
                payload = p.get("payload", {})
                ts = _fact_ts(payload)
                if ts > watermark:
                    facts.append({
                        "id": p["id"],
                        "content": payload.get("content", ""),
                        "gmt_created": ts,
                    })
            offset = data.get("next_page_offset")
            if offset is None:
                break

        facts.sort(key=lambda f: f["gmt_created"])
        return facts[:limit]

    except Exception as e:
        logger.warning(f"[L5] could not fetch L2 facts from Qdrant: {e}")
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
                logger.warning("[L5] LLM JSON parse failed for batch offset=%d", offset)
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


async def _embed_entities_to_qdrant(
    s2_writer,
    entities: list[dict],
    name_to_id: dict[str, str],
    user_id: str,
    agent_id: str,
):
    """Embed entity content and write to Qdrant for semantic search."""
    import asyncio

    import requests

    if not entities:
        return False

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
            resp = requests.post(embed_url, json={"input": texts}, timeout=30)
            vectors = resp.json()["data"]
            embeddings = [v["embedding"] for v in vectors]

        points = []
        for i, ent in enumerate(entities):
            name = ent["name"]
            node_id = name_to_id.get(name, _slugify(name))
            points.append({
                "id": _qdrant_point_id(node_id),
                "vector": embeddings[i],
                "payload": {
                    "layer": "l5_knowledge",
                    "status": "active",
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "content": name,
                    "content_type": f"ENTITY_{ent.get('type', 'OTHER')}",
                    "node_id": node_id,
                    "gmt_created": int(time.time()),
                },
            })

        resp = requests.put(
            f"{_L5_QDRANT_URL}/collections/{_L5_COLLECTION}/points",
            json={"points": points},
            timeout=30,
        )
        if resp.status_code == 200:
            logger.info(f"[L5] embedded {len(points)} entities to Qdrant")
            return True
        logger.warning(
            f"[L5] Qdrant embed write failed: {resp.status_code} body={resp.text[:300]}"
        )
        return False

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

    start = time.time()

    try:
        # 1. Read watermark
        watermark = _read_watermark()

        # 2. Get recent L2 facts
        facts = await _get_recent_l2_facts(graph_store, user_id, watermark, _L5_MAX_FACTS_PER_DIGEST)
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

        # 6. Embed entities to Qdrant
        qdrant_ok = await _embed_entities_to_qdrant(s2_writer, entities, name_to_id, user_id, agent_id)

        # 7. Update watermark only when something actually persisted
        latest_ts = max(f["gmt_created"] for f in facts)
        if ent_written > 0 or rel_count > 0 or qdrant_ok:
            _write_watermark(latest_ts)
        else:
            logger.warning("[L5] watermark not advanced: no Kuzu/Qdrant writes succeeded")

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
    """Rollback: drop all L5 nodes from Kuzu + Qdrant. Leaves L0-L4/L6/L7 intact.

    Requires the upstream to be stopped (for Kuzu) — or run from inside the process.
    """
    import requests

    results = {"kuzu": "skipped", "qdrant": "skipped", "watermark": "reset"}

    # Reset watermark
    try:
        _write_watermark(0.0)
        results["watermark"] = "reset to 0"
    except Exception as e:
        results["watermark"] = f"error: {e}"

    # Delete L5 points from Qdrant
    try:
        resp = requests.post(
            f"{_L5_QDRANT_URL}/collections/{_L5_COLLECTION}/points/delete",
            json={
                "filter": {
                    "must": [{"key": "layer", "match": {"value": "l5_knowledge"}}]
                }
            },
            timeout=30,
        )
        results["qdrant"] = f"deleted (status={resp.status_code})"
    except Exception as e:
        results["qdrant"] = f"error: {e}"

    # Kuzu deletion needs to be done from inside the server process
    # or with the server stopped. We can't do it from here safely.
    results["kuzu"] = "requires server restart with L5 disabled, then manual Kuzu cleanup"
    results["note"] = "To fully rollback: set MEMORY_L5_VERSION= (empty), restart upstream, then run: MATCH (m:Memory {layer: 'l5_knowledge'}) DETACH DELETE m"

    return results
