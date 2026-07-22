"""Reconciler JSON-parse robustness tests.

The reconcile pipeline parses LLM output into ReconcileOp lists. LLMs emit
messy JSON (code fences, trailing commas, reasoning blocks, bare prose).
These tests pin the parse contract so a regression that silently drops ops
(data loss) gets caught. No mocks — exercises the real MemoryReconciler._parse_ops.
"""

from hyatlas_memory.core.agent.reconciler import MemoryReconciler
from hyatlas_memory.core.config import MemoryConfig


def _rec() -> MemoryReconciler:
    return MemoryReconciler(MemoryConfig())


def test_clean_flat_array():
    ops = _rec()._parse_ops('[{"op": "ADD", "content": "x"}]')
    assert len(ops) == 1
    assert ops[0].op == "ADD"


def test_code_fenced_json():
    ops = _rec()._parse_ops('```json\n[{"op": "ADD", "content": "x"}]\n```')
    assert len(ops) == 1
    assert ops[0].op == "ADD"


def test_trailing_comma_array():
    # The most common LLM defect — strict json.loads rejects this.
    ops = _rec()._parse_ops('[{"op": "ADD", "content": "x"},]')
    assert len(ops) == 1
    assert ops[0].op == "ADD"


def test_trailing_comma_nested():
    # Two ADD ops with trailing commas on each object and the array.
    raw = '[{"op": "ADD", "content": "x",},\n{"op": "ADD", "content": "y",},]'
    ops = _rec()._parse_ops(raw)
    assert len(ops) == 2
    assert {o.content for o in ops} == {"x", "y"}


def test_think_block_stripped():
    raw = '\n[{"op": "ADD", "content": "x"}]'
    ops = _rec()._parse_ops(raw)
    assert len(ops) == 1
    assert ops[0].op == "ADD"


def test_group_format_backward_compat():
    raw = '[{"reason": "r", "ops": [{"op": "ADD", "content": "x"}]}]'
    ops = _rec()._parse_ops(raw)
    assert len(ops) == 1
    assert ops[0].op == "ADD"


def test_empty_returns_no_ops():
    assert _rec()._parse_ops("") == []
    assert _rec()._parse_ops("[]") == []


def test_bare_prose_returns_no_ops():
    # LLM sometimes answers in prose instead of JSON — must not raise.
    assert _rec()._parse_ops("I could not find anything to reconcile.") == []


def test_unrecoverable_garbage_returns_no_ops():
    assert _rec()._parse_ops("{not json at all") == []
