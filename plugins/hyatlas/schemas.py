"""Tool schemas for the HyAtlas plugin.

These get registered as agent-callable tools via ``ctx.register_tool``
in the plugin's ``register()`` function. Each tool has a JSON Schema
description that the model reads to decide when to call it.
"""

from __future__ import annotations

from typing import Any, Dict


HYATLAS_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "hyatlas_status",
    "description": (
        "Get the health and layer-counts of the HyAtlas memory server. "
        "Returns: vdb, embed, llm, write_pipeline status; per-layer counts; "
        "graph nodes/edges. Use this to confirm the memory system is healthy "
        "before recalling or writing."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


HYATLAS_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "hyatlas_search",
    "description": (
        "Semantic search the HyAtlas memory store for memories relevant "
        "to the query. Returns the channels pages, profile, proactive, "
        "experiences and normal, each hit with score, layer, content, timestamp "
        "and, when known, its event date and the older versions it replaced. "
        "Queries may name a time ('last week', '2026-10-05'). Prefer this over "
        "relying on prefetched context when you need explicit, up-to-date recall."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query",
            },
            "user_id": {
                "type": "string",
                "description": "User scope (defaults to active profile's user_id)",
            },
            "agent_id": {
                "type": "string",
                "description": "Agent scope (defaults to active profile's agent_id)",
            },
            "layer": {
                "type": "string",
                "description": "Restrict to one layer: l1_profile, l2_raw, l3_fact, l4_summary, l5_knowledge, l6_schema, l7_intention, l8_entity",
                "enum": [
                    "l1_profile", "l2_raw", "l3_fact", "l4_summary",
                    "l5_knowledge", "l6_schema", "l7_intention", "l8_entity",
                ],
            },
            "limit": {
                "type": "integer",
                "description": "Max results per channel (default 10)",
                "default": 10,
            },
        },
        "required": ["query"],
    },
}


HYATLAS_RECENT_SCHEMA: Dict[str, Any] = {
    "name": "hyatlas_recent",
    "description": (
        "List the most recent HyAtlas memories, optionally filtered by "
        "layer. Use this to see what was just saved or to browse the latest "
        "activity without a semantic query."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "User scope"},
            "agent_id": {"type": "string", "description": "Agent scope"},
            "layer": {
                "type": "string",
                "description": "Restrict to one layer",
                "enum": [
                    "l1_profile", "l2_raw", "l3_fact", "l4_summary",
                    "l5_knowledge", "l6_schema", "l7_intention", "l8_entity",
                ],
            },
            "limit": {
                "type": "integer",
                "description": "Max items (default 20)",
                "default": 20,
            },
            "include_raw": {
                "type": "boolean",
                "description": "Include L2 raw (the unprocessed input, what was just typed). Omit it to use the server's mode: lite includes raw (its only stored layer), other modes do not. Set true or false to force it.",
            },
        },
        "required": [],
    },
}


HYATLAS_ADD_SCHEMA: Dict[str, Any] = {
    "name": "hyatlas_add",
    "description": (
        "Save a memory directly to HyAtlas. Use this to record durable "
        "facts the agent wants remembered. The text is stored raw (L2), and in "
        "pro and ultra mode the server's LLM extracts facts with dates, "
        "preferences, intentions, entity pages and task experiences from it; "
        "summaries and the knowledge graph follow in the background. Prefer "
        "the built-in memory tool (`memory` / `memory add`) for atomic facts; "
        "this tool is for richer context dumps."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Memory text to store (will be LLM-extracted)",
            },
            "user_id": {"type": "string", "description": "User scope"},
            "agent_id": {"type": "string", "description": "Agent scope"},
            "session_id": {"type": "string", "description": "Session scope"},
        },
        "required": ["text"],
    },
}


ALL_SCHEMAS = [
    HYATLAS_STATUS_SCHEMA,
    HYATLAS_SEARCH_SCHEMA,
    HYATLAS_RECENT_SCHEMA,
    HYATLAS_ADD_SCHEMA,
]
