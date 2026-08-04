from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
APP = ROOT / "src" / "hyatlas_memory" / "server" / "dashboard" / "app.js"
L5 = ROOT / "src" / "hyatlas_memory" / "server" / "dashboard" / "js" / "l5.js"


def function(text: str, name: str, next_name: str) -> str:
    start = text.index(f"async function {name}")
    end = text.index(f"function {next_name}", start)
    return text[start:end]


def test_explore_search_sends_selected_profile_and_visible_controls():
    text = APP.read_text(encoding="utf-8")
    body = function(text, "performSearch", "renderSearchResults")

    assert "agent_ids" in body
    assert "searchMode" in body
    assert "filter-layer" in body
    assert "filter-time" in body
    assert "sort-by" in body


def test_l5_graph_request_uses_selected_profile_scope():
    text = L5.read_text(encoding="utf-8")

    assert "scopedPath('/api/l5/graph'" in text
    assert "currentAgentId" in text


def test_l5_cache_is_invalidated_when_profile_changes():
    app = APP.read_text(encoding="utf-8")
    selector_start = app.index("function initAgentSelector")
    selector_end = app.index("async function loadAllData", selector_start)
    body = app[selector_start:selector_end]

    assert "l5State.data = null" in body


def test_dashboard_graph_proxy_forwards_observatory_filters():
    text = (ROOT / "src" / "hyatlas_memory" / "server" / "dashboard" / "dashboard.py").read_text(encoding="utf-8")

    assert 'layer = qs.get("layer", [None])[0]' in text
    assert 'limit = qs.get("n", [None])[0]' in text
    assert 'rels = qs.get("rels", [None])[0]' in text
    assert 'upstream_qs.append(f"layer={layer}")' in text
    assert 'upstream_qs.append(f"n={limit}")' in text
    assert 'upstream_qs.append(f"rels={rels}")' in text


def test_profiles_reuse_canonical_layer_count_contract():
    text = (ROOT / "src" / "hyatlas_memory" / "server" / "dashboard" / "dashboard.py").read_text(encoding="utf-8")
    profiles = text[text.index('if path == "/api/profiles":') : text.index('if path == "/api/memories":')]

    assert "_layer_counts(agent_id)" in profiles
    assert '"display_total": counts["display_total"]' in profiles
    assert '"memory_count"' not in profiles


def test_all_profile_scope_is_explicit_in_requests():
    text = APP.read_text(encoding="utf-8")
    search = text[text.index("async function performSearch") : text.index("function renderSearchResults")]

    assert "function scopeQuery" in text
    assert "agent_id=all" in text
    assert "return agentId === 'all' ? [] : [agentId];" in text
    assert "PROFILE_IDS.slice(1)" not in search


def test_l5_controls_come_from_payload_and_include_relation_filter():
    text = L5.read_text(encoding="utf-8")

    assert "Object.keys(d.type_distribution || {})" in text
    assert "selectedRelation" in text
    assert "relation_type_distribution" in text
    assert "LOADED AT" in text
    assert "EXPORTED AT" not in text


def test_explore_search_has_loading_empty_error_and_score_help():
    text = APP.read_text(encoding="utf-8")
    body = function(text, "performSearch", "renderSearchResults")

    assert "Searching…" in body
    assert "Search failed:" in body
    assert "No memories matched" in text
    assert "scoreLabel" in text


def test_open_scoped_pages_refresh_when_profile_changes():
    text = APP.read_text(encoding="utf-8")
    load = text[text.index("async function loadAllData") : text.index("function updateGlobalStatus")]

    assert "if (currentPage === 'explore') performSearch();" in load
    assert "if (currentPage === 'l5') initL5Page();" in load


def test_explore_filter_controls_trigger_search():
    observatory = (APP.parent / "js" / "observatory.js").read_text(encoding="utf-8")

    assert "['filter-layer', 'filter-time', 'sort-by']" in observatory
    assert "addEventListener('change', performSearch)" in observatory


def test_l5_entities_show_stored_provenance_when_available():
    text = L5.read_text(encoding="utf-8")

    assert "l5-source" in text
    assert "n.source" in text
    assert "n.created_at" in text


def test_specialist_scope_uses_scoped_vdb_total_before_global_status_points():
    text = APP.read_text(encoding="utf-8")
    start = text.index("function getVdbPoints()")
    end = text.index("function getGraphRelationCount()")
    helper = text[start:end]

    assert "currentAgentId !== 'all'" in helper
    assert helper.index("layerCountsData?.vdb_total") < helper.index("statusData?.vdb_points")


def test_auto_refresh_waits_for_the_previous_load_to_finish():
    text = APP.read_text(encoding="utf-8")

    assert "async function refreshLoop" in text
    assert "await loadAllData()" in text
    assert "setTimeout(refreshLoop, REFRESH_S * 1000)" in text
    assert "setInterval(loadAllData" not in text
