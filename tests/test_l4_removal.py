"""L4 layer full-removal regression tests (2026-08-06)."""
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
    values = [layer.value for layer in MemoryLayer.all_layers()]
    assert "l4_identity" not in values
    assert "l0_basic_info" in values
    assert "l3_summary" in values
    assert "l5_knowledge" in values


def test_historical_identity_aliases_map_to_l2():
    assert MemoryLayer.from_string("l4_identity").value == "l2_fact"
    assert MemoryLayer.from_string("profile").value == "l2_fact"
    assert MemoryLayer.from_string("l6_identity").value == "l2_fact"
    assert MemoryLayer.from_string("l5_identity").value == "l2_fact"


def _dashboard_text():
    return DASHBOARD.read_text(encoding="utf-8", errors="replace")


def _appjs_text():
    return APPJS.read_text(encoding="utf-8", errors="replace")


def test_dashboard_omits_l4_layer():
    text = _dashboard_text()
    assert "l4_identity" not in text.split("\n")  # no direct layer key ref
    assert "L4_IDENTITY" not in text


def test_dashboard_sources_now_l0_l3():
    text = _dashboard_text()
    assert '"l0_l3": "vdb"' in text
    assert '"l0_l4"' not in text
    assert "L4 retired" not in text


def test_appjs_omits_l4_and_updates_coverage():
    text = _appjs_text()
    # `l4_identity` appears only inside the defensive coverage guard
    # (k !== 'l4_identity'), which is intentionally kept so a stray historical
    # row never inflates the layer-coverage count. No UI label/card/order ref.
    assert "Identity (retired)" not in text
    assert "L4 retired" not in text
    assert "'l4_identity':" not in text            # no LAYERS label entry
    assert "'l4_identity'" not in text.replace("k !== 'l4_identity'", "")
    assert "L0-L3 VDB + L5-L7 graph" in text
    assert "k !== 'l4_identity'" in text            # defensive coverage guard retained


def test_observatory_omits_l4():
    text = OBSJS.read_text(encoding="utf-8", errors="replace")
    assert "l4_identity" not in text
    assert "l4_identity" not in text.lower()
