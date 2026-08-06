"""
System 2 Agent Tools

8 个工具供 System 2 Agent (LLM) 操作 VDB + Graph 双层存储。

Read (4):
  1. search_vdb     — 向量语义检索 VDB (L0-L4)
  2. search_graph   — 按内容/标签/层级查 Graph (L5-L7)
  3. get_node       — 统一获取节点详情 (VDB or Graph)，渐进式披露
  4. expand_node    — 图遍历，查看某节点的关联节点

Write (4): — Graph 专用
  5. create_graph_node   — 创建 L6 SCHEMA / L7 INTENTION 节点
  6. update_graph_node   — 更新已有 Graph 节点
  7. add_edge            — 建立关系 (RELATED_TO)
  8. delete_graph_node   — 删除节点
"""

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from ..core.embed_service import EmbedService
from ..data.graph_store_base import GraphStoreBase
from ..data.vector_store_base import VectorStoreBase
from ..models.memory import MemoryLayer, MemoryNode, MemoryStatus, SourceType

logger = logging.getLogger(__name__)

# Evidence tree truncation length for content preview
_TRUNCATE_LEN = 80


def _truncate(text: str, max_len: int = _TRUNCATE_LEN) -> str:
    if not text:
        return ""
    return text[:max_len] + ("..." if len(text) > max_len else "")


# ====================================================================
# Tool definitions (OpenAI function-calling format)
# ====================================================================

SYSTEM2_TOOL_DEFINITIONS = [
    # --- Read tools ---
    {
        "type": "function",
        "function": {
            "name": "search_vdb",
            "description": "Semantic search in VDB. Returns list: content, score, layer. Only searches L2_FACT and L3_SUMMARY layers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "layers": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["l2_fact", "l3_summary"]},
                        "description": "Layer filter. Options: 'l2_fact', 'l3_summary'. Omit to search both."
                    },
                    "limit": {"type": "integer", "description": "Max results, default 10"}
                },
                "required": ["query"]
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_graph",
            "description": "Search Graph nodes. Filter by content keyword, tags, or layer. Only L6_SCHEMA and L7_INTENTION exist in Graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Content keyword (fuzzy match)"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by tags"
                    },
                    "layer": {"type": "string", "enum": ["l6_schema", "l7_intention"], "description": "Layer filter. Options: 'l6_schema', 'l7_intention'"},
                },
                "required": [],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_node",
            "description": "Get node details: content, layer, confidence, tags. Works for both VDB and Graph nodes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Node ID"},
                },
                "required": ["node_id"]
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "expand_node",
            "description": "Graph traversal: see related nodes (RELATED_TO edges) from a given node.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Start node ID"},
                    "hops": {"type": "integer", "description": "Hops, default 1"},
                },
                "required": ["node_id"]
            },
        }
    },
    # --- Write tools ---
    {
        "type": "function",
        "function": {
            "name": "create_graph_node",
            "description": "Create an L6_SCHEMA or L7_INTENTION node. Content must be a single sentence for Schema (circumstance + pattern + insight). Automatically creates VdbRef + DERIVED_FROM + Tag edges.",
            "parameters": {
                "type": "object",
                "properties": {
                    "layer": {
                        "type": "string",
                        "enum": ["l6_schema", "l7_intention"],
                        "description": "Node layer"
                    },
                    "content": {"type": "string", "description": "Node content (single sentence for Schema)"},
                    "evidence_list": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "VDB node_id list as evidence"
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tag list (prefer reusing existing tags)"
                    },
                    "confidence": {"type": "number", "description": "Confidence 0-1, default 0.8"},
                },
                "required": ["layer", "content", "evidence_list"]
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_evidence",
            "description": "Add evidence to an EXISTING Schema or Intention. Links new VDB facts to the node via DERIVED_FROM edges. Does NOT change the node content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Existing Graph node ID to add evidence to"},
                    "evidence_list": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "VDB node_id list to add as new evidence"
                    },
                },
                "required": ["node_id", "evidence_list"]
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_edge",
            "description": "Create a RELATED_TO relationship between two Graph nodes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "target_id": {"type": "string"},
                    "reason": {"type": "string", "description": "Reason for the relationship"},
                },
                "required": ["source_id", "target_id"]
            },
        }
    },
]


# ====================================================================
# Tool executor
# ====================================================================

class System2ToolExecutor:
    """
    执行 System 2 Agent 的 tool calls。

    持有 VDB + Graph store 引用，所有操作自动记 pipeline log。
    """

    def __init__(
        self,
        vector_store: VectorStoreBase,
        graph_store: GraphStoreBase,
        embed_service: EmbedService,
        user_id: str,
        agent_id: str,
    ):
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.embed_service = embed_service
        self.user_id = user_id
        self.agent_id = agent_id
        self._isolation_key = MemoryNode.build_isolation_key(user_id, agent_id)
        self.tool_call_log: list[dict[str, Any]] = []

    async def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        """执行一个 tool call，返回 JSON string 结果"""
        start_time = time.perf_counter()

        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            result = {"error": f"Unknown tool: {tool_name}"}
        else:
            try:
                result = await handler(args)
            except Exception as e:
                logger.error(f"[S2-tool] {tool_name} failed: {e}", exc_info=True)
                result = {"error": f"{type(e).__name__}: {str(e)}"}

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # Log
        result_json = json.dumps(result, ensure_ascii=False, default=str)
        self.tool_call_log.append({
            "tool": tool_name,
            "args": args,
            "result": result_json,  # 完整结果
            "result_preview": _truncate(result_json, 200),
            "elapsed_ms": round(elapsed_ms, 2),
        })

        # Metrics: Graph ops（write tools 记录到 graph_ops）
        _GRAPH_WRITE_TOOLS = {"create_graph_node", "add_evidence", "add_edge"}
        if tool_name in _GRAPH_WRITE_TOOLS:
            try:
                from ..metrics import MetricsCollector
                MetricsCollector.get().record_graph_op(elapsed_ms)
            except Exception:
                pass

        return result_json

    # ================================================================
    # Read tools
    # ================================================================

    async def _tool_search_vdb(self, args: dict) -> Any:
        query = args["query"]
        layers_str = args.get("layers")
        limit = args.get("limit", 10)

        layers = None
        if layers_str:
            layers = [MemoryLayer.from_string(l) for l in layers_str]

        query_embedding = await self.embed_service.embed_queued(query)
        results = await self.vector_store.search(
            query_embedding=query_embedding,
            user_id=self.user_id,
            agent_ids=[self.agent_id] if self.agent_id else None,
            layers=layers,
            limit=limit,
            status_filter=[MemoryStatus.ACTIVE],
        )

        return [
            {
                "node_id": r["node_id"],
                "content": r["node"].content,
                "score": round(r["score"], 4),
                "layer": r["node"].layer.value,
                "evidence_count": len(r["node"].evidence_chain) if r["node"].evidence_chain else 0,
            }
            for r in results
        ]

    async def _tool_search_graph(self, args: dict) -> Any:
        query = args.get("query")
        tags = args.get("tags")
        layer = args.get("layer")

        # Search by content (list all active nodes with layer filter)
        layer_filter = MemoryLayer.from_string(layer) if layer else None
        all_nodes = await self.graph_store.get_all_nodes(
            isolation_key=self._isolation_key,
            layer=layer_filter,
            status=MemoryStatus.ACTIVE,
            limit=50,
        )

        # Filter by query keyword
        if query:
            query_lower = query.lower()
            all_nodes = [n for n in all_nodes if query_lower in n.content.lower()]

        # Filter by tags
        if tags and all_nodes:
            tag_set = {t.lower() for t in tags}
            all_nodes = [
                n for n in all_nodes
                if any(t.lower() in tag_set for t in (n.tags or []))
            ] or all_nodes  # fallback to unfiltered if no tag match

        results = []
        for n in all_nodes:
            evidence_refs = await self.graph_store.get_evidence_vdbrefs(n.node_id)
            results.append({
                "node_id": n.node_id,
                "content": n.content,
                "layer": n.layer.value,
                "confidence": n.confidence,
                "evidence_count": len(evidence_refs),
            })
        return results

    async def _tool_get_node(self, args: dict) -> Any:
        node_id = args["node_id"]

        # Try VDB first
        vdb_node = await self.vector_store.get_by_id(node_id)
        if vdb_node:
            result = {
                "node_id": vdb_node.node_id,
                "layer": vdb_node.layer.value,
                "content": vdb_node.content,
                "status": vdb_node.status.value if hasattr(vdb_node.status, 'value') else str(vdb_node.status),
                "tags": vdb_node.tags or [],
                "gmt_created": str(vdb_node.gmt_created) if vdb_node.gmt_created else None,
            }
            # Add source_raw_memory_id if present (L3/L4)
            if vdb_node.source_raw_memory_id:
                result["source_raw_memory_id"] = vdb_node.source_raw_memory_id
            # Add supersedes info
            if vdb_node.supersedes:
                result["supersedes"] = vdb_node.supersedes
            if not vdb_node.is_latest:
                result["is_latest"] = False
            return result

        # Try Graph
        graph_node = await self.graph_store.get_node(node_id)
        if graph_node:
            evidence_refs = await self.graph_store.get_evidence_vdbrefs(node_id)
            return {
                "node_id": graph_node.node_id,
                "layer": graph_node.layer.value,
                "content": graph_node.content,
                "confidence": graph_node.confidence,
                "status": graph_node.status.value if hasattr(graph_node.status, 'value') else str(graph_node.status),
                "tags": graph_node.tags or [],
                "evidence_count": len(evidence_refs),
            }

        return {"error": f"Node {node_id} not found in VDB or Graph"}

    async def _tool_expand_node(self, args: dict) -> Any:
        node_id = args["node_id"]
        hops = args.get("hops", 1)

        expanded = await self.graph_store.expand_from_anchors(
            anchor_ids=[node_id],
            hop=hops,
            max_nodes=30,
        )

        return [
            {
                "node_id": item["node_id"],
                "content": item.get("content", ""),
                "layer": item.get("layer", ""),
                "edge_type": item.get("edge_type", ""),
                "confidence": item.get("confidence"),
            }
            for item in expanded
        ]

    # ================================================================
    # Write tools
    # ================================================================

    async def _tool_create_graph_node(self, args: dict) -> Any:
        layer_str = args["layer"]
        content = args["content"]
        evidence_list = args["evidence_list"]
        tags = args.get("tags", [])
        confidence = args.get("confidence", 0.8)

        layer = MemoryLayer.from_string(layer_str)
        node_id = str(uuid.uuid4())

        # Embed content → V_con（Graph 向量检索用）
        try:
            content_embedding = await self.embed_service.embed_queued(content)
        except Exception as e:
            logger.warning(f"create_graph_node: embed failed, node will have no embedding: {e}")
            content_embedding = None

        # Create the Graph Memory node
        now = datetime.now()
        node = MemoryNode(
            node_id=node_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            layer=layer,
            content=content,
            confidence=confidence,
            status=MemoryStatus.ACTIVE,
            source_type=SourceType.INFERRED,
            tags=tags,
            memory_at=now,
            custom={"schema_type": "basic"} if layer == MemoryLayer.L6_SCHEMA else {},
        )
        if content_embedding:
            node._graph_embedding = content_embedding
        # embedding 必须在 upsert CREATE 时写入（Kuzu 索引列不可 SET）。
        # Neo4j 可在 upsert 后 SET，但 _graph_embedding 已在 upsert 中处理，无需再调 update_embedding。
        await self.graph_store.upsert_memory_node(node)

        # Create VdbRef + DERIVED_FROM edges for each evidence
        for eid in evidence_list:
            vdb_node = await self.vector_store.get_by_id(eid)
            ev_layer = vdb_node.layer.value if vdb_node else "unknown"
            await self.graph_store.ensure_vdbref(eid, ev_layer)
            await self.graph_store.add_derived_from(node_id, eid)
            # 递增 VDB fact 的 s2_evidence_count（用于 preprocess 过滤已消化 facts）
            await self._increment_evidence_count(eid, vdb_node)

        # Create Tag + TAGGED_WITH edges (with embedding normalization)
        for tag in tags:
            await self.graph_store.add_topic_tag(
                node_id, tag, self._isolation_key, embed_service=self.embed_service,
            )

        return {"node_id": node_id, "created": True, "evidence_count": len(evidence_list)}

    async def _tool_add_evidence(self, args: dict) -> Any:
        """Add evidence to an existing Schema/Intention (content stays immutable)"""
        node_id = args["node_id"]
        evidence_list = args["evidence_list"]

        added = 0
        for eid in evidence_list:
            vdb_node = await self.vector_store.get_by_id(eid)
            ev_layer = vdb_node.layer.value if vdb_node else "unknown"
            await self.graph_store.ensure_vdbref(eid, ev_layer)
            await self.graph_store.add_derived_from(node_id, eid)
            # 递增 VDB fact 的 s2_evidence_count
            await self._increment_evidence_count(eid, vdb_node)
            added += 1

        return {"node_id": node_id, "evidence_added": added}

    async def _increment_evidence_count(self, vdb_id: str, vdb_node=None) -> None:
        """递增 VDB fact 的 s2_evidence_count（存在 custom 子字典中，best-effort）"""
        try:
            # 读出当前 custom dict
            custom = {}
            if vdb_node and hasattr(vdb_node, "custom") and isinstance(vdb_node.custom, dict):
                custom = dict(vdb_node.custom)
            current = custom.get("s2_evidence_count", 0)
            custom["s2_evidence_count"] = current + 1
            # 写回 custom 子字典（_payload_to_node 通过 MemoryNode.from_dict 读 custom）
            await self.vector_store.update_payload(vdb_id, {"custom": custom})
        except Exception as e:
            logger.debug(f"[S2-tools] increment s2_evidence_count for {vdb_id} failed: {e}")

    async def _tool_add_edge(self, args: dict) -> Any:
        source_id = args["source_id"]
        target_id = args["target_id"]
        reason = args.get("reason", "")

        props = {"relation_type": reason or "related"}

        success = await self.graph_store.add_edge(source_id, target_id, "RELATED_TO", props)
        return {"success": success, "edge": f"({source_id})-[RELATED_TO]->({target_id})"}
