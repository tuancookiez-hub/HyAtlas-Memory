"""
基础画像（L0_BASIC_INFO）upsert 模块。

把用户稳定的结构化属性（name / age / location / occupation / employer，可配置）
合并写入 L0_BASIC_INFO memory 演化链。

特点：
- 每次更新只存 diff 字段，创建新版本节点。
- 旧版本标记为 superseded，通过 supersedes/superseded_by 链关联。
- 用户真实 search 时，reader 把 L0_BASIC_INFO 纳入 Profile 路召回。
- 字段表（fields schema）由 MemoryConfig.basic_profile.fields 配置。

设计变更（2026-06）：
- 删除原 LLM function-calling tool `update_basic_user_profile`（弱模型乱填）。
- 改为：extractor prompt 直接要求 LLM 在 JSON 输出中返回 basic_info；writer
  收到 basic_info 后调用本模块 upsert_basic_profile() 入库。
- 字段表可配置，效果不绑死 5 个固定字段。

公共 API：
- upsert_basic_profile(...)        — 主入口，writer 直接调
- basic_info_node_id(...)          — deterministic id（仍保留，便于回退查询）
- render_l0_evolution_chain(...)   — Profile 路渲染
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ================================================================
# 默认字段顺序（用于 _render_content 内容拼接顺序）
# 注：实际接受哪些字段由调用方传入 allowed_fields 决定。
# ================================================================

BASIC_FIELDS: List[str] = ["name", "age", "location", "occupation", "employer"]


# ================================================================
# deterministic node id（保留，便于回退查询历史数据）
# ================================================================

_BASIC_INFO_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-000000babb1e")  # stable sentinel


def basic_info_node_id(user_id: str, agent_id: str) -> str:
    """同一 (user_id, agent_id) 对应同一 deterministic id（仅用于回退查询旧数据）。"""
    key = f"basic_info::{user_id or 'default'}::{agent_id or 'default_agent'}"
    return str(uuid.uuid5(_BASIC_INFO_NAMESPACE, key))


# ================================================================
# content 渲染
# ================================================================

def _render_content(kv: Dict[str, Any], field_order: Optional[List[str]] = None) -> str:
    """
    把 KV 渲染成人类可读的自然语言串。
    与其他 memory 保持 "The user's ..." 风格一致。

    field_order: 可选，控制字段拼接顺序。未指定时按 kv 字典序。
    """
    order = field_order or sorted(kv.keys())
    parts: List[str] = []
    for key in order:
        if key not in kv:
            continue
        value = kv[key]
        if value is None:
            continue
        s = str(value).strip()
        if not s:
            continue
        parts.append(f"{key} is {s}")
    if not parts:
        return ""
    return "The user's " + ", ".join(parts) + "."


def render_l0_evolution_chain(l0_nodes: list) -> str:
    """
    按属性分组、时间正序展示 L0 全部历史版本。

    输入：L0_BASIC_INFO 节点列表（含 superseded 的旧版本）
    输出：格式化的字符串，例如：
        用户姓名: 张三
        用户年龄: (2026-01-15 用户称年龄25) → (2026-05-10 用户称年龄26)
        用户职业: (2026-01-15 用户说自己是程序员) → (2026-03-20 用户说自己是架构师)
    """
    from datetime import datetime as _dt

    # 按时间正序排列
    sorted_nodes = sorted(
        l0_nodes,
        key=lambda n: (getattr(n, 'gmt_created', None) or getattr(n, 'valid_from', None) or _dt.min),
    )

    # 收集每个属性的历史版本（动态发现 key，不再绑死 BASIC_FIELDS）
    field_history: Dict[str, List[tuple]] = {}
    for node in sorted_nodes:
        kv = {}
        if hasattr(node, 'custom') and isinstance(node.custom, dict):
            kv = node.custom.get("basic_info_kv") or {}
        ts = getattr(node, 'gmt_created', None) or getattr(node, 'valid_from', None)
        ts_str = ts.strftime("%Y-%m-%d") if ts else "unknown"
        for field_name, val in kv.items():
            field_history.setdefault(field_name, []).append((ts_str, val))

    # 渲染：内置 5 字段使用中文 label，其余使用原 key
    field_labels = {
        "name": "用户姓名",
        "age": "用户年龄",
        "location": "用户所在地",
        "occupation": "用户职业",
        "employer": "用户雇主",
    }
    # 顺序：先 BASIC_FIELDS 中已知的，再剩下的按字母序
    ordered_keys: List[str] = [k for k in BASIC_FIELDS if k in field_history]
    for k in sorted(field_history.keys()):
        if k not in ordered_keys:
            ordered_keys.append(k)

    lines = []
    for field_name in ordered_keys:
        history = field_history[field_name]
        if not history:
            continue
        label = field_labels.get(field_name, field_name)
        if len(history) == 1:
            lines.append(f"{label}: {history[0][1]}")
        else:
            chain = " → ".join(f"({ts} {v})" for ts, v in history)
            lines.append(f"{label}: {chain}")

    return "\n".join(lines) if lines else ""


# ================================================================
# 输入 sanitize
# ================================================================

def _sanitize_kv(
    kv: Dict[str, Any],
    allowed_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    只保留 allowed_fields 中的 key；丢弃 null / 空串 / "null" / "none"。
    age 字段强制 int。
    """
    if allowed_fields is None:
        allowed_fields = BASIC_FIELDS
    allowed_set = set(allowed_fields)

    result: Dict[str, Any] = {}
    for k, v in kv.items():
        if k not in allowed_set:
            continue
        if v is None:
            continue
        if isinstance(v, str):
            s = v.strip()
            if not s or s.lower() in ("null", "none"):
                continue
            result[k] = s
        elif isinstance(v, bool):
            # bool 是 int 子类，特判排在前面
            result[k] = "yes" if v else "no"
        elif isinstance(v, int):
            result[k] = int(v)
        elif isinstance(v, float):
            try:
                if k == "age":
                    result[k] = int(v)
                else:
                    result[k] = float(v)
            except (TypeError, ValueError):
                continue
        else:
            # 其他非预期类型（list/dict）忽略
            continue
    return result


# ================================================================
# 公共入口：upsert_basic_profile
# ================================================================

@dataclass
class BasicProfileUpsertResult:
    """upsert 结果。failed 时 node_id="" 且 error 非 None。"""
    success: bool
    node_id: str = ""
    content: str = ""
    diff_kv: Dict[str, Any] = None  # type: ignore[assignment]
    changed: bool = False
    created: bool = False
    supersedes: Optional[str] = None
    reason: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "node_id": self.node_id,
            "content": self.content,
            "diff_kv": self.diff_kv or {},
            "changed": self.changed,
            "created": self.created,
            "supersedes": self.supersedes,
            "reason": self.reason,
            "error": self.error,
        }


async def upsert_basic_profile(
    *,
    user_id: str,
    agent_id: str,
    session_id: str,
    kv: Dict[str, Any],
    vector_store,
    embed_service,
    allowed_fields: Optional[List[str]] = None,
) -> BasicProfileUpsertResult:
    """
    把 LLM 提取出的 basic_info kv 入库到 L0_BASIC_INFO 演化链。

    kv 中字段不在 allowed_fields 里的会被丢弃（防止 LLM 编出多余字段）。
    新版本节点：deterministic node_id，新 UUID。
    旧版本：is_latest=False, status=SUPERSEDED, superseded_by 指向新节点。
    """
    user_id = (user_id or "").strip()
    agent_id = (agent_id or "default_agent").strip() or "default_agent"
    session_id = (session_id or "default_session").strip() or "default_session"

    if not user_id:
        return BasicProfileUpsertResult(success=False, error="user_id required")

    new_kv = _sanitize_kv(kv or {}, allowed_fields=allowed_fields)
    if not new_kv:
        return BasicProfileUpsertResult(
            success=True, changed=False, reason="no valid fields after sanitize"
        )

    # 找最新版本节点 + 计算 diff
    latest_node = await _find_latest_l0(vector_store, user_id, agent_id)
    existing_full_kv = await _assemble_full_kv(vector_store, user_id, agent_id, allowed_fields)

    diff_kv: Dict[str, Any] = {}
    for k, v in new_kv.items():
        old_v = existing_full_kv.get(k)
        if old_v is None or str(v) != str(old_v):
            diff_kv[k] = v

    if not diff_kv:
        return BasicProfileUpsertResult(
            success=True, changed=False, reason="no diff from existing"
        )

    # 渲染：按 allowed_fields 顺序拼接（更稳定）
    content = _render_content(diff_kv, field_order=allowed_fields)
    if not content:
        return BasicProfileUpsertResult(success=False, error="rendered content is empty")

    # 重算 embedding
    try:
        embedding = await embed_service.embed_queued(content)
    except Exception as e:
        return BasicProfileUpsertResult(success=False, error=f"embed failed: {e}")

    from ...models.memory import MemoryNode, MemoryLayer, MemoryStatus, SourceType

    new_node_id = str(uuid.uuid4())
    supersedes_id = latest_node.node_id if latest_node else None

    new_node = MemoryNode(
        node_id=new_node_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        layer=MemoryLayer.L0_BASIC_INFO,
        content=content,
        source_type=SourceType.EXPLICIT,
        status=MemoryStatus.ACTIVE,
        is_latest=True,
        supersedes=[supersedes_id] if supersedes_id else None,
        embedding=embedding,
        custom={"basic_info_kv": diff_kv},  # 只存 diff
        tags=["basic_info"],
    )

    try:
        await vector_store.upsert(new_node)
    except Exception as e:
        return BasicProfileUpsertResult(success=False, error=f"upsert failed: {e}")

    # 旧版本标记 superseded
    if latest_node:
        try:
            await vector_store.update_payload(
                latest_node.node_id,
                {
                    "is_latest": False,
                    "superseded_by": [new_node_id],
                    "status": MemoryStatus.SUPERSEDED.value,
                },
            )
        except Exception as e:
            logger.warning(f"[basic_profile] failed to supersede old L0 node: {e}")

    logger.info(
        f"[basic_profile] user={user_id} agent={agent_id} "
        f"new L0 version={new_node_id} diff={diff_kv} supersedes={supersedes_id}"
    )

    return BasicProfileUpsertResult(
        success=True,
        node_id=new_node_id,
        content=content,
        diff_kv=diff_kv,
        changed=True,
        created=True,
        supersedes=supersedes_id,
    )


# ================================================================
# 内部：查找 L0 节点 / 组装 full kv
# ================================================================

def _is_valid_l0_head(node) -> bool:
    """链头必须是合法 L0 节点：layer==L0_BASIC_INFO 且带 basic_info_kv 结构。

    防御 defense-in-depth：即便上游查询（list_by_user / get_by_id）因后端
    layer 过滤失效等原因返回了非 L0 节点（如 l1_raw），也不把它当链头去
    supersede，避免生成跨层脏链（raw 被串进 L0 演化链）。
    """
    from ...models.memory import MemoryLayer
    if node is None:
        return False
    if getattr(node, "layer", None) != MemoryLayer.L0_BASIC_INFO:
        return False
    custom = getattr(node, "custom", None)
    return isinstance(custom, dict) and "basic_info_kv" in custom


async def _find_latest_l0(vector_store, user_id: str, agent_id: str):
    """找到当前 is_latest=True 的合法 L0 链头；找不到返回 None（= 新链起点）。"""
    from ...models.memory import MemoryLayer, MemoryStatus
    nodes = await vector_store.list_by_user(
        user_id=user_id,
        agent_id=agent_id,
        status_filter=[MemoryStatus.ACTIVE],
        layers=[MemoryLayer.L0_BASIC_INFO],
        limit=10,
    )
    # 只认合法 L0 节点（layer + kv 结构校验），过滤掉脏节点
    valid = [n for n in nodes if _is_valid_l0_head(n)]
    for n in valid:
        if n.is_latest:
            return n
    if valid:
        valid.sort(key=lambda x: x.gmt_created or x.valid_from, reverse=True)
        return valid[0]
    # 向后兼容：尝试旧 deterministic id（同样需过 L0 校验）
    old_id = basic_info_node_id(user_id, agent_id)
    legacy = await vector_store.get_by_id(old_id)
    return legacy if _is_valid_l0_head(legacy) else None



async def _assemble_full_kv(
    vector_store,
    user_id: str,
    agent_id: str,
    allowed_fields: Optional[List[str]],
) -> Dict[str, Any]:
    """沿演化链组装完整 KV。"""
    from ...models.memory import MemoryLayer
    all_nodes = await vector_store.list_by_user(
        user_id=user_id,
        agent_id=agent_id,
        layers=[MemoryLayer.L0_BASIC_INFO],
        limit=100,
    )
    all_nodes.sort(key=lambda x: x.gmt_created or x.valid_from)

    allowed_set = set(allowed_fields) if allowed_fields else None
    full_kv: Dict[str, Any] = {}
    for n in all_nodes:
        if not isinstance(n.custom, dict):
            continue
        kv = n.custom.get("basic_info_kv") or {}
        if not isinstance(kv, dict):
            continue
        for k, v in kv.items():
            if allowed_set is not None and k not in allowed_set:
                continue
            full_kv[k] = v
    return full_kv
