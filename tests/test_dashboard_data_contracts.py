from __future__ import annotations

import importlib.util
from pathlib import Path

PATH = (
    Path(__file__).parents[1] / "src" / "hyatlas_memory" / "server" / "dashboard" / "dashboard.py"
)
APP = PATH.with_name("app.js")


def load():
    spec = importlib.util.spec_from_file_location("hyatlas_dashboard_contract_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_memory_extraction_excludes_graph_nodes():
    mod = load()
    payload = {
        "vdb": {
            "memories": [
                {
                    "memory_id": "vdb-1",
                    "layer": "l2_fact",
                    "content": "persisted fact",
                    "gmt_created": 10,
                }
            ]
        },
        "graph": {
            "nodes": [
                {
                    "node_id": "graph-1",
                    "layer": "l5_knowledge",
                    "content": "derived entity",
                    "created_at": "2026-07-29T10:00:00",
                }
            ]
        },
    }

    items = mod._extract_memories(payload)

    assert [item["memory_id"] for item in items] == ["vdb-1"]
    assert items[0]["layer"] == "l2_fact"
    assert items[0]["content"] == "persisted fact"


def test_memory_extraction_never_returns_graph_layers():
    mod = load()
    payload = {
        "vdb": {
            "memories": [
                {"memory_id": "raw", "layer": "l1_raw", "content": "raw"},
                {"memory_id": "fact", "layer": "l2_fact", "content": "fact"},
            ]
        },
        "graph": {
            "nodes": [
                {"node_id": "l5", "layer": "l5_knowledge", "content": "knowledge"},
                {"node_id": "l6", "layer": "l6_schema", "content": "schema"},
                {"node_id": "l7", "layer": "l7_intention", "content": "intention"},
            ]
        },
    }

    layers = {item["layer"] for item in mod._extract_memories(payload)}

    assert layers == {"l1_raw", "l2_fact"}


def test_memory_page_applies_offset_after_merge_and_deduplication():
    mod = load()
    items = [
        {"memory_id": "new", "gmt_created": 30},
        {"memory_id": "middle", "gmt_created": 20},
        {"memory_id": "old", "gmt_created": 10},
        {"memory_id": "middle", "gmt_created": 20},
    ]

    page, total = mod._memory_page(items, offset=1, limit=1)

    assert total == 3
    assert [item["memory_id"] for item in page] == ["middle"]


def test_memory_page_returns_empty_after_total():
    mod = load()
    items = [{"memory_id": "one", "gmt_created": 10}]

    page, total = mod._memory_page(items, offset=1, limit=5)

    assert total == 1
    assert page == []


def test_frontend_keeps_graph_nodes_out_of_activity_records():
    text = APP.read_text(encoding="utf-8")

    assert "let vdbMemories = [];" in text
    assert "let codingMemories = [];" in text
    assert "let graphNodes = [];" in text
    assert "let activityMemories = [];" in text
    assert "let observatoryMemories = [];" in text
    assert "activityMemories = [...vdbMemories, ...codingMemories];" in text
    assert "observatoryMemories = [...vdbMemories, ...graphNodes];" in text
    assert "allMemories" not in text
    assert 'if path == "/api/memories":' in PATH.read_text(encoding="utf-8")


def test_frontend_payload_names_do_not_shadow_state_stores():
    text = APP.read_text(encoding="utf-8")

    assert "codingPayload" in text
    assert "codingMemories.memories" not in text


def test_frontend_activity_surfaces_use_activity_records_only():
    text = APP.read_text(encoding="utf-8")

    chart = text[text.index("function renderActivityChart") : text.index("function renderOperations")]
    today = text[text.index("function renderToday()") : text.index("// Settings / System")]
    sidebar = text[text.index("function renderOverviewSidebar") : text.index("function getMostActiveLayer")]

    assert "activityMemories.forEach" in chart
    assert "activityMemories.filter" in today
    assert "activityMemories.filter" in sidebar
    assert "graphNodes" not in chart
    assert "graphNodes" not in today
    assert "graphNodes" not in sidebar


def test_today_sidebar_uses_the_same_rolling_24_hour_window_as_timeline():
    text = APP.read_text(encoding="utf-8")
    sidebar = text[text.index("function updateRightSidebar") : text.index("let recentIngestionTab")]

    assert "Date.now() - 24 * 60 * 60 * 1000" in sidebar
    assert "created.getTime() >= since" in sidebar
    assert "today.setHours(0, 0, 0, 0)" not in sidebar


def test_observatory_uses_its_explicit_dataset():
    text = (APP.parent / "js" / "observatory.js").read_text(encoding="utf-8")

    assert "observatoryMemories" in text
    assert "allMemories" not in text


def test_observatory_uses_stored_graph_relations_only():
    app = APP.read_text(encoding="utf-8")
    text = (APP.parent / "js" / "observatory.js").read_text(encoding="utf-8")

    assert "let graphRelations = [];" in app
    assert "graphRelations = l5Graph?.relations || [];" in app
    assert "buildStoredObservatoryEdges" in text
    assert "graphRelations" in text
    assert "keyword-based edges" not in text
    assert "structural cross-layer edges" not in text
    assert "computeObservatoryEdges" not in text


def test_observatory_loads_all_live_graph_layers_without_synthetic_timestamps():
    text = APP.read_text(encoding="utf-8")

    assert "/api/l5/graph?layer=l6_schema&n=500&rels=false" in text
    assert "/api/l5/graph?layer=l7_intention&n=500&rels=false" in text
    assert "new Date(n.created_at || 0)" in text
    assert "mention_count || 0" not in text


def test_observatory_uses_canonical_layer_counts_and_reports_subset():
    text = (APP.parent / "js" / "observatory.js").read_text(encoding="utf-8")

    assert "buildCanonicalLayerSummary" in text
    assert "layerCountsData" in text
    assert "renderedCount" in text
    assert "canonicalTotal" in text
    assert "subsetMissing" in text


def test_open_observatory_rebuilds_after_profile_reload():
    text = APP.read_text(encoding="utf-8")

    assert "if (sceneInitialized && currentPage === 'observatory') updateGraph(obsCurrentScope);" in text


def test_observatory_field_note_names_real_sources_and_metrics():
    text = (APP.parent / "js" / "observatory.js").read_text(encoding="utf-8")

    assert "graphRelations.filter" in text
    assert "Stored connections" in text
    assert "Stored linked layers" in text
    assert "Kuzu confidence" in text
    assert "Retrieval score" in text
    assert "Kuzu graph" in text
    assert "Not available" in text
    assert "High detail" not in text
    assert ">Importance<" not in text
