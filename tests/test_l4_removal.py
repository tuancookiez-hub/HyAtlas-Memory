"""L4 layer full-removal regression tests (2026-08-06) + 2026-08-08 renumber."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from hyatlas_memory.core.models.memory import MemoryLayer

SRC = Path(__file__).parents[1] / "src"

DASHBOARD = (
    SRC / "hyatlas_memory" / "server" / "dashboard" / "dashboard.py"
)
APPJS = SRC / "hyatlas_memory" / "server" / "dashboard" / "app.js"
OBSJS = SRC / "hyatlas_memory" / "server" / "dashboard" / "js" / "observatory.js"


def _load_py(path):
    spec = importlib.util.spec_from_file_location("l4removal_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_l4_member_removed():
    assert not hasattr(MemoryLayer, "L4_IDENTITY")
    assert not hasattr(MemoryLayer, "PROFILE")


def test_all_layers_excludes_l4():
    """Renumbered 7-layer model: L1..L7 with no L4_IDENTITY gap."""
    values = [layer.value for layer in MemoryLayer.all_layers()]
    assert "l4_identity" not in values
    assert "l0_basic_info" not in values  # renumbered away
    assert "l1_profile" in values
    assert "l2_raw" in values
    assert "l3_fact" in values
    assert "l4_summary" in values
    assert "l5_knowledge" in values
    assert "l6_schema" in values
    assert "l7_intention" in values


def test_historical_identity_aliases_map_to_l3_fact():
    """Identity is fully retired; all identity aliases map to L3_FACT (renumbered L2_FACT)."""
    assert MemoryLayer.from_string("l4_identity").value == "l3_fact"
    assert MemoryLayer.from_string("profile").value == "l3_fact"
    assert MemoryLayer.from_string("l6_identity").value == "l3_fact"
    assert MemoryLayer.from_string("l5_identity").value == "l3_fact"


def test_pre_renumber_aliases_route_through_from_string():
    """Pre-2026-08-08 layer names should deserialize cleanly to the new
    7-layer numbering without crashing."""
    assert MemoryLayer.from_string("l0_basic_info").value == "l1_profile"
    assert MemoryLayer.from_string("l1_raw").value == "l2_raw"
    assert MemoryLayer.from_string("l2_fact").value == "l3_fact"
    assert MemoryLayer.from_string("l3_summary").value == "l4_summary"
    # L5/L6/L7 storage values did not change
    assert MemoryLayer.from_string("l5_knowledge").value == "l5_knowledge"
    assert MemoryLayer.from_string("l6_schema").value == "l6_schema"
    assert MemoryLayer.from_string("l7_intention").value == "l7_intention"


def _dashboard_text():
    return DASHBOARD.read_text(encoding="utf-8", errors="replace")


def _appjs_text():
    return APPJS.read_text(encoding="utf-8", errors="replace")


def test_dashboard_omits_l4_layer():
    text = _dashboard_text()
    assert "L4_IDENTITY" not in text
    # The renumbered L4_SUMMARY is fine — that's a different concept.
    # Legacy layer literal strings should not appear:
    for legacy in ("l0_basic_info", "l4_identity"):
        assert legacy not in text


def test_dashboard_sources_now_l1_l4():
    text = _dashboard_text()
    assert '"l1_l4": "vdb"' in text
    assert '"l5_l7": "graph"' in text
    assert '"l0_l3"' not in text
    assert '"l0_l4"' not in text


def test_appjs_omits_l4_and_uses_new_labels():
    text = _appjs_text()
    # Old L4 layer key/label completely gone from JS:
    assert "Identity (retired)" not in text
    assert "L4 retired" not in text
    # New layer labels present (renumbered):
    assert "L1 Profile" in text or "l1_profile" in text
    assert "L2 Raw" in text or "l2_raw" in text
    assert "L3 Fact" in text or "l3_fact" in text
    assert "L4 Summary" in text or "l4_summary" in text


def test_observatory_omits_l4():
    text = OBSJS.read_text(encoding="utf-8", errors="replace")
    # Legacy L4 layer key should not appear
    assert "l4_identity" not in text
    # Renumbered L4 Summary key may appear (different concept)
