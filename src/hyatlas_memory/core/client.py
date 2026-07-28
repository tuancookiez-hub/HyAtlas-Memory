"""
HY Memory Client - 面向用户的同步客户端（无状态）

所有组件在构造函数中完成初始化，失败立即报错。
构造函数只接受连接配置，不绑定任何业务参数（user_id/agent_id 等），
一个 client 实例可被多线程共享。

    # 最简用法 — 环境变量配置
    client = HyMemoryClient()
    client.add("用户喜欢科幻电影", user_id="test_user")
    results = client.search("用户喜欢什么？", user_id="test_user")
    client.close()

    # 自定义配置（类似 mem0 风格）
    client = HyMemoryClient.from_config({
        "embedder": {"provider": "openai", "model": "text-embedding-3-small"},
        "vector_store": {"provider": "local"},
        "graph_store": {
            "provider": "kuzu",
            "path": "./data/graph",
        },
        "enable_graph": True,
    })
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional, Union

from .config import MemoryConfig
from .core.embed_service import EmbedService
from .data.cache import create_cache
from .data.history_store import HistoryStore
from .data.vector_store import create_vector_store
from .utils.log_setup import request_id_scope, set_request_id, setup_logging

if TYPE_CHECKING:
    from .runtime import SharedRuntime
from .pipelines import (
    ChatMessage,
    ComponentFactory,
    ReadRequest,
    ReadResponse,
    ToolCall,
    WriteRequest,
    WriteResponse,
)


# Coding memory 模块（lazy 初始化，仅在 messages 含 tool 且判定为 coding 时启用）
# Coding path disabled in HyAtlas v3 — all writes use normal memory path
async def classify_messages_is_coding(*a, **kw): return False

def has_any_tool_message(*a, **kw): return False
def strip_tool_messages(msgs, *a, **kw): return msgs
logger = logging.getLogger(__name__)

# add() 第一个参数的类型：字符串 或 messages 列表
MessageList = list[dict[str, str]]
AddInput = Union[str, MessageList]


def _ensure_internal_loop(method):
    """
    Decorator for async_xxx() methods on HyMemoryClient.

    Ensures the coroutine runs on _LoopThread's event loop, not the caller's.
    If already on the correct loop, executes directly.
    If on an external loop (e.g., uvicorn), delegates to _LoopThread via run_coroutine_threadsafe.
    """
    import functools

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        internal_loop = self._loop_thread._loop

        if current_loop is internal_loop:
            # Already on _LoopThread's loop — execute directly
            return await method(self, *args, **kwargs)
        else:
            # On external loop (uvicorn, etc.) — delegate to _LoopThread
            future = asyncio.run_coroutine_threadsafe(
                method(self, *args, **kwargs), internal_loop
            )
            return await asyncio.wrap_future(future)

    return wrapper


class _LoopThread:
    """
    后台线程持有一个持久的 event loop。

    解决问题：本地 Qdrant（AsyncQdrantLocal）通过文件锁绑定到创建它的 event loop，
    如果每次 asyncio.run() 创建新 loop，文件锁会冲突。
    用同一个 loop 运行所有协程即可。
    """

    def __init__(self):
        import threading
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout: float = 300):
        """在后台 loop 中运行协程，同步等待结果"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def run_async(self, coro):
        """
        在后台 loop 中运行协程，异步等待结果。

        可从任意 event loop 中安全 await — 如果已经在正确的 loop 上则直接执行，
        否则委托到 _LoopThread 的 loop 并 await。
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is self._loop:
            # 已经在 _LoopThread 的 loop 里，直接执行
            return await coro
        else:
            # 在外部 loop 里（如 uvicorn），委托到 _LoopThread
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return await asyncio.wrap_future(future)

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class HyMemoryClient:
    """
    HY Memory 无状态客户端。

    构造函数只接受连接配置和处理模式，不绑定任何业务参数。
    所有业务隔离参数（user_id/agent_id/session_id 等）在每次调用时传入，
    一个 client 实例可被多线程安全共享。

    处理模式（mode）：
    - "lite":  纯 embedding 写入，不调用 LLM，速度最快
    - "pro":   embedding + LLM agent 提取（事实/实体/意图），标准模式
    - "ultra": pro + System 2 异步认知加工（Schema 归纳/意图检测），完整模式

    数据隔离模型（三级）：
    - user_id:    一级 key — 每个用户唯一的记忆库
    - agent_id:   二级 key — 同一用户下不同 Agent 场景的隔离
    - session_id: 三级 key — 同一 Agent 下不同会话的隔离

    Usage:
        # lite: 纯向量写入，不调 LLM
        client = HyMemoryClient(mode="lite")

        # pro: 标准模式（embedding + agent 提取）
        client = HyMemoryClient(mode="pro")

        # ultra: 完整模式（pro + System 2 认知层）
        client = HyMemoryClient(mode="ultra")

        client.add("用户喜欢科幻电影", user_id="user_001")
        results = client.search("用户喜欢什么？", user_id="user_001")
        client.close()

    Args:
        config: 自定义 MemoryConfig（默认从环境变量加载）
        mode:   处理模式 ("lite" / "pro" / "ultra")，默认从环境变量 MEMORY_MODE 读取
    """

    # 合法的 mode 值
    VALID_MODES = ("lite", "pro", "ultra")

    def __init__(
        self,
        config: MemoryConfig | None = None,
        mode: str | None = None,
        coding_writer: str | None = None,
        runtime: Optional["SharedRuntime"] = None,
    ):
        # 确保日志系统已配置（幂等，多次调用安全）
        setup_logging()

        # 配置
        self._config = config or MemoryConfig.from_env()

        # coding_writer 显式传入则覆盖 config / env（默认 None → 沿用 config.coding.writer）
        # 取值: "legacy" | "agent"
        if coding_writer is not None:
            cw = str(coding_writer).strip().lower()
            if cw in ("legacy", "agent"):
                self._config.coding.writer = cw
            else:
                logger.warning(
                    f"[init] invalid coding_writer={coding_writer!r}; falling back to config default"
                )

        # 解析 mode
        import os
        self._mode = mode or os.getenv("MEMORY_MODE", "pro")
        if self._mode not in self.VALID_MODES:
            raise ValueError(
                f"Invalid mode '{self._mode}'. Must be one of: {self.VALID_MODES}"
            )

        # 图数据库开关：跟随 mode（ultra → 启用 Graph）
        self.enable_graph = (self._mode == "ultra")
        self._config.enable_graph = self.enable_graph

        # 进程级初始化日志强制不带业务 request_id（哨兵 "-"），避免污染 App
        # 请求上下文里按 request_id 的过滤。
        with request_id_scope():
            logger.info(
                f"Initializing HyMemoryClient: mode={self._mode} "
                f"enable_graph={self.enable_graph}"
            )

            # SharedRuntime 接入（多 client 部署用，避免 cross-loop bug）
            # - runtime is None  → solo 模式：自己起 _LoopThread + 自己的 cache pool
            # - runtime is given → shared 模式：复用 runtime 的 loop / cache / metrics 绑定
            self._runtime = runtime
            self._owns_runtime = runtime is None

            # 创建/复用持久 event loop
            if self._owns_runtime:
                self._loop_thread = _LoopThread()
            else:
                self._loop_thread = runtime.loop_thread

            # 同步初始化所有组件
            self._loop_thread.run(self._initialize())

            logger.info("HyMemoryClient ready")

    async def _initialize(self):
        """初始化所有核心组件"""
        # 进程级初始化日志不应继承外层（如 App 请求上下文）的业务 request_id，
        # 否则按 request_id 过滤会混入一次性 init 噪音。强制设为哨兵 "-"。
        with request_id_scope():
            await self._initialize_impl()

    async def _initialize_impl(self):
        """初始化所有核心组件（实际实现）"""

        # 0. 扩大全局默认线程池 + 各组件后续用独立池
        import concurrent.futures
        loop = asyncio.get_event_loop()
        _max_workers = int(os.getenv("MEMORY_THREAD_POOL_SIZE", "256"))
        loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers)
        )
        logger.info(f"[init] Default ThreadPoolExecutor max_workers={_max_workers}")

        # 1. EmbedService（纯配置，首次调用时真正连接 API）
        self._embed_service = EmbedService(self._config)
        logger.debug(
            f"[init] EmbedService created: model={self._config.embedder.model}"
        )

        # 2. VectorStore（根据配置选择后端）
        self._vector_store = create_vector_store(self._config)
        await self._vector_store.initialize()
        logger.info(
            f"[init] VectorStore ready: provider={self._config.vector_store.provider} "
            f"collection={self._vector_store._collection_name}"
        )

        # 3. GraphStore（可选，由 enable_graph 配置控制）
        self._graph_store = None
        if self.enable_graph:
            from .data.graph_store import create_graph_store
            self._graph_store = create_graph_store(self._config)
            await self._graph_store.initialize()
            # 检查图数据库是否真正可用（Kuzu 会静默降级为 no-op）
            if hasattr(self._graph_store, '_available') and not self._graph_store._available:
                raise RuntimeError(
                    f"GraphStore ({self._config.graph_store.provider}) init failed. "
                    f"Use mode='pro' instead of mode='ultra' to disable Graph, or check logs for details."
                )
            logger.info(
                f"[init] GraphStore ready: provider={self._config.graph_store.provider}"
            )

        # 4. ComponentFactory（注入已初始化的共享组件）
        self._registry = ComponentFactory(config=self._config)
        self._registry._shared_embed_service = self._embed_service
        self._registry._shared_vector_store = self._vector_store
        self._registry._shared_vector_store_initialized = True
        if self._graph_store is not None:
            self._registry._shared_graph_store = self._graph_store

        # 5. Cache（必须在组件预热前初始化，writer 创建时需要注入 cache）
        # - solo 模式：自己 create + initialize
        # - shared 模式：复用 runtime 的 cache（已 init + 已装 observability hook）
        if self._owns_runtime:
            self._cache = create_cache(self._config)
            await self._cache.initialize()
            logger.info(f"[init] Cache ready (backend={self._config.cache.backend})")
        else:
            self._cache = self._runtime.cache
            logger.info("[init] Cache reused from SharedRuntime")

        self._registry._shared_cache = self._cache

        # 5.1 Pipeline 可观测性 hook
        # - solo 模式：自己装一次
        # - shared 模式：runtime 已经在 shared cache 上装过了，跳过避免重复包装
        if self._owns_runtime:
            self._install_pipeline_observability_hooks()

        # 6. 预热组件（确保首次 add/search 不需要等待初始化）
        await self._registry.get_writer()
        await self._registry.get_reader()
        logger.info(f"[init] Writer & Reader ready (mode={self._mode})")

        if self._mode == "ultra":
            try:
                await self._registry.get_system2_writer()
                logger.info("[init] System2Writer ready (ultra mode)")
            except Exception as e:
                logger.warning(f"[init] System2Writer warmup failed: {e}")

        # 7. HistoryStore（可选，由 config.history.enable 控制）
        self._history_store = None
        if self._config.history.enable:
            self._history_store = HistoryStore(self._config)
            await self._history_store.initialize()
            logger.info("[init] HistoryStore ready")

        # 8. MetricsCollector
        # - solo 模式：bind cache 到 metrics 单例 + 启 background flush
        # - shared 模式：runtime 已经做过，跳过（关键！避免覆盖 _cache 触发 cross-loop）
        if self._owns_runtime:
            from .metrics import MetricsCollector
            _metrics = MetricsCollector.get()
            _metrics.bind_cache(self._cache)
            await _metrics.start_background_tasks()
            logger.info("[init] MetricsCollector ready")
        else:
            logger.info("[init] MetricsCollector already bound by SharedRuntime")

        # 9. Coding memory 链路占位（lazy 初始化）
        #    仅在 add(messages_with_tool) 首次命中 is_coding=True 时才真正初始化。
        self._coding_writer = None       # CodingWriter | None
        self._coding_store = None        # CodingMemoryStore | None
        self._coding_llm_provider = None  # LLMProvider | None
        self._coding_init_lock = asyncio.Lock()

    def _install_pipeline_observability_hooks(self) -> None:
        """
        包装 cache.store_pipeline_log：
        - Log：JSONL 文件，始终写入（环节级，不依赖 Trace 开关）
        - Trace：SQLite pipeline_logs，由 MEMORY_PIPELINE_TRACE_ENABLED 控制（Inspector）
        """
        from .utils.pipeline_log_writer import PipelineLogWriter
        from .utils.pipeline_observability import (
            is_pipeline_trace_enabled,
            resolve_pipeline_log_dir,
        )

        log_dir = resolve_pipeline_log_dir()
        log_writer = PipelineLogWriter(log_dir)
        trace_enabled = is_pipeline_trace_enabled()
        logger.info(
            f"[init] Pipeline log (JSONL) enabled: {log_dir}/<subdir>/<date>.log "
            f"(subdir from user_id prefix before '__', else default) ; "
            f"pipeline trace (DB)={'on' if trace_enabled else 'off'}"
        )

        original_store = self._cache.store_pipeline_log

        async def _store_pipeline_observability(
            *,
            request_id: str = "",
            user_id: str = "",
            agent_id: str = "",
            step: str = "",
            prompt: str = "",
            response: str = "",
            parsed: str = "",
            memory_ids=None,
            elapsed_ms: float = 0.0,
            prompt_tokens: int = 0,
            completion_tokens: int = 0,
            total_tokens: int = 0,
            **kwargs,
        ):
            # 1. Log 文件（始终）
            try:
                log_writer.write_step(
                    request_id=request_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    step=step,
                    prompt=prompt,
                    response=response,
                    parsed=parsed,
                    memory_ids=memory_ids,
                    elapsed_ms=elapsed_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )
            except Exception:
                pass

            # 2. Trace 落库（可关闭，如 OpenClaw C 端）
            if not trace_enabled:
                return True

            return await original_store(
                request_id=request_id,
                user_id=user_id,
                agent_id=agent_id,
                step=step,
                prompt=prompt,
                response=response,
                parsed=parsed,
                memory_ids=memory_ids,
                elapsed_ms=elapsed_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                **kwargs,
            )

        self._cache.store_pipeline_log = _store_pipeline_observability

    @classmethod
    def from_config(
        cls,
        config_dict: dict[str, Any],
        mode: str | None = None,
    ) -> "HyMemoryClient":
        """
        从字典创建客户端（类似 mem0 风格）。

        Usage:
            client = HyMemoryClient.from_config({
                "vector_store": {
                    "provider": "local",
                    "collection_name": "my_memories",
                },
                "graph_store": {
                    "provider": "kuzu",
                    "path": "./data/graph",
                },
                "enable_graph": True,
                "llm": {
                    "provider": "openai",
                    "model": "deepseek-chat",
                    "api_key": "sk-xxx",
                },
                "embedder": {
                    "provider": "openai",
                    "model": "text-embedding-v3",
                    "api_key": "sk-xxx",
                },
            })
        """
        config = MemoryConfig.from_dict(config_dict)
        resolved_mode = mode or config_dict.get("mode")
        return cls(config=config, mode=resolved_mode)

    # ================================================================
    # 属性
    # ================================================================

    @property
    def mode(self) -> str:
        """当前处理模式 (lite/pro/ultra)"""
        return self._mode

    # ================================================================
    # 内部
    # ================================================================

    @staticmethod
    def _build_isolation_key(uid: str, agent_id: str) -> str:
        """
        构建 history 用的 isolation_key。

        两级隔离: "{user_id}:{agent_id}"
        """
        parts = [uid or "default", agent_id or "default"]
        return ":".join(parts)

    @staticmethod
    def _parse_input(data: AddInput):
        """
        解析 add() 的输入，统一返回 (content, chat_messages, content_type)。

        str  → ("文本", [], "text")
        list → ("", [ChatMessage, ...], "messages")

        list 模式下兼容两种 tool 调用风格，规范化到统一的 ChatMessage：
        - OpenAI: assistant.tool_calls (function.arguments 是 JSON 字符串) +
                   独立 role="tool" 消息（含 tool_call_id / name）
        - Anthropic: assistant.content 为 list[content_block]，含 type=tool_use；
                     tool_result 嵌在后续 role="user" 消息的 content list 里，
                     需要拆为独立的 role="tool" 规范化消息

        一条原始消息可能产出 0~多 条 ChatMessage（Anthropic tool_result 拆分）。
        """
        if isinstance(data, str):
            return data, [], "text"

        if isinstance(data, list):
            chat_messages: list[ChatMessage] = []
            for raw in data:
                chat_messages.extend(HyMemoryClient._normalize_message(raw))
            return "", chat_messages, "messages"

        raise TypeError(
            f"add() 第一个参数应为 str 或 list[dict]，收到 {type(data).__name__}"
        )

    @staticmethod
    def _normalize_message(m: dict[str, Any]) -> list[ChatMessage]:
        """
        把单条原始消息规范化到 ChatMessage 列表。

        返回值通常是单条；当原始消息是 Anthropic 风格 user 消息且其 content
        list 含 tool_result block 时，会拆为 [user_text_msg?, tool_msg, ...]。
        """
        role = m.get("role", "user")
        raw_content = m.get("content")
        raw_tool_calls = m.get("tool_calls", []) or []

        # ── 1. OpenAI assistant.tool_calls ──
        tool_calls: list[ToolCall] = []
        for tc in raw_tool_calls:
            fn = tc.get("function", {}) or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                # OpenAI 编码为 JSON 字符串
                try:
                    args = json.loads(args) if args else {}
                except Exception:
                    args = {"_raw": args}
            elif args is None:
                args = {}
            elif not isinstance(args, dict):
                args = {"_raw": args}
            tool_calls.append(ToolCall(
                id=tc.get("id", "") or "",
                name=fn.get("name", "") or "",
                arguments=args,
            ))

        # ── 2. OpenAI 独立 tool 消息 ──
        if role == "tool":
            return [ChatMessage(
                role="tool",
                content=str(raw_content or ""),
                tool_call_id=m.get("tool_call_id"),
                tool_name=m.get("name"),
            )]

        # ── 3. Anthropic content blocks ──
        if isinstance(raw_content, list):
            text_parts: list[str] = []
            tool_result_msgs: list[ChatMessage] = []
            for block in raw_content:
                if not isinstance(block, dict):
                    # 非标准形态：当字符串处理
                    text_parts.append(str(block))
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", "") or "")
                elif btype == "tool_use":
                    # Anthropic：input 已是 dict
                    tool_input = block.get("input", {}) or {}
                    if not isinstance(tool_input, dict):
                        tool_input = {"_raw": tool_input}
                    tool_calls.append(ToolCall(
                        id=block.get("id", "") or "",
                        name=block.get("name", "") or "",
                        arguments=tool_input,
                    ))
                elif btype == "tool_result":
                    # 拆为独立 role=tool 消息
                    tc_id = block.get("tool_use_id")
                    inner = block.get("content", "")
                    if isinstance(inner, list):
                        # 嵌套 content list（通常是 [{type:text,text:...}]）
                        inner = "\n".join(
                            b.get("text", "") if isinstance(b, dict) and b.get("type") == "text"
                            else str(b)
                            for b in inner
                        )
                    elif not isinstance(inner, str):
                        inner = str(inner)
                    tool_result_msgs.append(ChatMessage(
                        role="tool",
                        content=inner,
                        tool_call_id=tc_id,
                        tool_name=None,  # Anthropic tool_result 没带 name
                    ))
                # 其他未知 block 类型静默忽略
            text = "\n".join(p for p in text_parts if p).strip()

            outputs: list[ChatMessage] = []
            # 即使 text 为空，只要 tool_calls 非空也保留 assistant 消息
            if text or tool_calls:
                outputs.append(ChatMessage(
                    role=role,
                    content=text,
                    tool_calls=tool_calls,
                ))
            outputs.extend(tool_result_msgs)
            return outputs

        # ── 4. 普通 string content ──
        content_str = "" if raw_content is None else str(raw_content)
        return [ChatMessage(
            role=role,
            content=content_str,
            tool_calls=tool_calls,
            tool_call_id=m.get("tool_call_id"),
            tool_name=m.get("name"),
        )]

    # ================================================================
    # Coding memory lazy init
    # ================================================================

    async def _get_coding_writer(self):
        """Coding path disabled in HyAtlas v3."""
        return None

    async def _search_coding(
        self,
        target_query: str,
        *,
        user_ids: list[str],
        workspace_id: str | None,
        branch: str | None,
        limit: int,
        min_score: float,
        request_id: str,
        t0: float,
    ) -> dict[str, Any]:
        """
        Coding 召回路径：
          1. 拿 store（lazy init），ensure 已 initialize
          2. embed(target_query) → store.search_by_query_embedding
          3. boundary filter 已在 store 内做；这里只做 min_score 过滤 + 详情 fetch
          4. 返回 {"scene": "productivity", "coding_memories": [...], ...}

        失败时 fail-safe：返回 success=True + 空 coding_memories（不抛错）。
        """
        try:
            await self._get_coding_writer()  # 副作用：初始化 store
        except Exception as e:
            logger.warning(f"[search-coding] init failed: {e}")
            return {
                "request_id": request_id,
                "scene": "productivity",
                "coding_memories": [],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            }

        # 多 user_ids 时取首位（coding memory 一般跟单 user 绑定；
        # 跨 user 的 coding memory 检索属未来扩展）
        primary_user = user_ids[0]

        try:
            embedding = await self._embed_service.embed(target_query)
        except Exception as e:
            logger.warning(f"[search-coding] embed failed: {e}")
            return {
                "request_id": request_id,
                "scene": "productivity",
                "coding_memories": [],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            }

        try:
            hits = await self._coding_store.search_by_query_embedding(
                embedding,
                user_id=primary_user,
                workspace_id=workspace_id,
                branch=branch,
                top=max(limit * 4, 8),  # 过采样为 dedup 留余量
            )
        except Exception as e:
            logger.warning(f"[search-coding] vdb search failed: {e}")
            return {
                "request_id": request_id,
                "scene": "productivity",
                "coding_memories": [],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            }

        # min_score 过滤
        hits = [h for h in hits if h.get("score", 0.0) >= min_score]
        # 取 top-K
        hits = hits[:limit]

        # 拉详情
        memory_ids = [h["memory_id"] for h in hits]
        try:
            details = await self._coding_store.get_many(memory_ids, user_id=primary_user)
        except Exception as e:
            logger.warning(f"[search-coding] fetch details failed: {e}")
            details = []
        details_by_id = {d.memory_id: d for d in details}

        # 组装返回
        out_memories: list[dict[str, Any]] = []
        for h in hits:
            d = details_by_id.get(h["memory_id"])
            if d is None:
                continue  # SQLite 与 VDB 不一致，跳过
            out_memories.append({
                "memory_id": d.memory_id,
                "task": d.task,
                "search_keys": list(d.search_keys),
                "solution": d.solution,
                "boundary_envs": d.boundary_envs,
                "boundary_scope": d.boundary_scope,
                "workspace_id": d.workspace_id,
                "branch": d.branch,
                "files": list(d.files),
                "matched_key": h.get("matched_key_text"),
                "matched_key_kind": h.get("matched_key_kind"),
                "score": round(float(h.get("score", 0.0)), 4),
                "confidence": d.confidence,
                "updated_at": d.updated_at.isoformat() if d.updated_at else None,
                "source": d.source,
            })

        elapsed = round((time.perf_counter() - t0) * 1000, 2)
        logger.info(
            f"[search-coding] hits={len(out_memories)} elapsed_ms={elapsed} "
            f"workspace_id={workspace_id!r} branch={branch!r}"
        )
        return {
            "request_id": request_id,
            "scene": "productivity",
            "coding_memories": out_memories,
            "elapsed_ms": elapsed,
        }

    # ================================================================
    # 同步 API
    # ================================================================

    def add(
        self,
        data: AddInput,
        *,
        user_id: str = "",
        agent_id: str = "default_agent",
        session_id: str = "default_session",
        metadata: dict[str, Any] | None = None,
        memory_at: datetime | None = None,
        enable_summary: bool | None = None,
        workspace_id: str | None = None,
        branch: str | None = None,
        request_id: str | None = None,
        extract_scene: str | None = None,
    ) -> dict[str, Any]:
        """
        写入记忆。

        处理深度由 Client 初始化时的 mode 决定：
        - lite:  纯 embedding 写入
        - pro:   embedding + LLM agent 提取（事实/实体/reconcile）
        - ultra: pro + System 2 异步认知加工

        Args:
            data: 文本字符串，或 messages 列表
                  - "用户喜欢科幻电影"
                  - [{"role": "user", "content": "..."}, ...]
            user_id:      用户 ID（一级隔离 key）
            agent_id:     Agent ID（二级隔离 key）
            session_id:   Session ID（三级隔离 key）
            metadata:     自定义元数据
            memory_at:    记忆时间戳（不传则用当前时间），用于导入历史记忆
            enable_summary: 本次写入是否生成 L3_SUMMARY。
                            None（默认）= 沿用 LLMConfig.enable_summary（全局默认 False）；
                            True/False = 仅对此次调用生效，覆盖默认。
                            仅 pro/ultra 模式有效（lite 模式不调 LLM）。
            workspace_id: Coding memory 路径专用。repo 标识（建议 git remote URL 规范化）。
                          没传时 scope=strict/project 的 coding memory 会被拒写。chat 路径忽略。
            branch:       Coding memory 路径专用。分支名。仅 boundary_scope=strict 时启用且必填。
                          chat 路径忽略。

        Returns:
            chat 链路：{"success": True, "memory_id": "...", "request_id": "...", "elapsed_ms": ...}
            coding 链路：{"success": True, "scene": "productivity", "ops": [...], "memory_ids": [...], ...}
        """
        return self._loop_thread.run(
            self.async_add(
                data, user_id=user_id, agent_id=agent_id, session_id=session_id,
                metadata=metadata, memory_at=memory_at,
                enable_summary=enable_summary,
                workspace_id=workspace_id, branch=branch,
                request_id=request_id,
                extract_scene=extract_scene,
            )
        )

    def search(
        self,
        query: str | None = None,
        *,
        queries: list[str] | None = None,
        scene: str | None = None,
        user_ids: list[str] | None = None,
        agent_ids: list[str] | None = None,
        session_ids: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.4,
        profile_min_score: float = 0.4,
        profile_limit: int = 10,
        intention_limit: int = 0,
        created_after: float | None = None,
        reader: str = "",
        workspace_id: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """
        搜索记忆。

        三路召回（chat 路径）：
        - **Profile 路**: L0_BASIC_INFO + L6_SCHEMA（用户画像）
        - **Proactive 路**: L7_INTENTION（主动意图，默认关闭）
        - **Normal 路**: L2_FACT + L3_SUMMARY + L4_IDENTITY + L5_KNOWLEDGE 等

        Coding 路径（自动判定）：
        - 当 queries 上下文判定为 coding 时，走 CodingMemoryStore（task + search_keys
          多 key 召回 + boundary 过滤），返回结构含 "scene": "productivity" 和 "coding_memories"。
        - 否则走 chat 路径，返回与历史完全一致。

        Args:
            query:              搜索查询（兼容旧调用；与 queries 二选一）
            queries:            多条 queries（最后一条为 target；前置作为 coding judge 上下文）
            user_ids:           用户 ID 列表（一级隔离 key，支持跨用户搜索）
            agent_ids:          Agent ID 列表（空列表 = 搜索所有 agent）
            session_ids:        Session ID 列表（空列表 = 搜索所有 session）
            limit:              Normal 路的 topk 数量，默认 10
            min_score:          Normal 路的最低分数阈值，默认 0.4
            profile_min_score:  Profile 路的最低分数阈值，默认 0.4
            profile_limit:      Profile 路的最大返回数，默认 10
            intention_limit:    Proactive 路的 topk 数量，默认 0（关闭）
            created_after:      Unix timestamp (float)，只返回 gmt_created >= 此值的记忆
            reader:             Reader 类型，动态切换。可选: legacy/hybrid/hybrid_v2/tencent_hybrid。
                                空字符串 = 使用环境变量 HY_MEMORY_READER 或默认 legacy。
            workspace_id:       Coding 召回的 boundary filter（chat 路径忽略）
            branch:             Coding 召回的 strict scope filter（chat 路径忽略）

        Returns:
            chat 路径：{"request_id": "...", "memories": [...], "elapsed_ms": ...}
            coding 路径：{"request_id": "...", "scene": "productivity",
                         "coding_memories": [...], "elapsed_ms": ...}
        """
        return self._loop_thread.run(self.async_search(
            query, queries=queries, scene=scene,
            user_ids=user_ids, agent_ids=agent_ids, session_ids=session_ids,
            limit=limit, min_score=min_score,
            profile_min_score=profile_min_score, profile_limit=profile_limit,
            intention_limit=intention_limit, created_after=created_after,
            reader=reader,
            workspace_id=workspace_id, branch=branch,
        ))

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """
        获取单条记忆。

        Args:
            memory_id: 记忆 ID

        Returns:
            {"memory_id", "content", "layer", "request_id", "elapsed_ms"} 或 None（不存在时）
        """
        return self._loop_thread.run(self.async_get(memory_id))

    def update(
        self,
        memory_id: str,
        content: str,
    ) -> dict[str, Any]:
        """
        更新记忆内容。

        Args:
            memory_id: 记忆 ID
            content:   新内容

        Returns:
            {"success": True, "memory_id": "...", "request_id": "...", "elapsed_ms": ...}
        """
        return self._loop_thread.run(self.async_update(memory_id, content))

    def delete(self, memory_id: str) -> dict[str, Any]:
        """
        删除单条记忆。

        Args:
            memory_id: 记忆 ID

        Returns:
            {"success": True, "deleted_count": 1, "request_id": "...", "elapsed_ms": ...}
        """
        return self._loop_thread.run(self.async_delete(memory_id))

    def delete_all(
        self,
        *,
        user_id: str = "",
        agent_ids: list[str] | None = None,
        session_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        删除用户的记忆。

        默认删除该用户的所有记忆（agent_ids/session_ids 都不传时）。
        可通过 agent_ids/session_ids 缩小删除范围。

        Args:
            user_id:      用户 ID（一级隔离 key）
            agent_ids:    Agent ID 列表（空列表 = 所有 agent）
            session_ids:  Session ID 列表（空列表 = 所有 session）

        Returns:
            {"success": True, "deleted_count": N, "request_id": "...", "elapsed_ms": ...}
        """
        return self._loop_thread.run(
            self.async_delete_all(user_id=user_id, agent_ids=agent_ids, session_ids=session_ids)
        )

    def list_memories(
        self,
        *,
        user_id: str = "",
        agent_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str = "desc",
        include_raw: bool = True,
    ) -> dict[str, Any]:
        """
        列出用户的记忆（同步）。

        从向量库直接读取，只返回 ACTIVE 状态的记忆。
        按 created_at 排序，支持分页。

        Args:
            user_id:  用户 ID
            agent_id: 可选，限定某个 agent
            limit:    每页条数（默认 100）
            offset:   偏移量（默认 0）
            order:    排序方式，"desc"（默认，最新在前）或 "asc"
            include_raw: 是否包含 L1_RAW（未提取的原始记忆）。默认 True。

        Returns:
            {"vdb": {...}, "graph": {...} | 省略, "elapsed_ms": ...}
        """
        return self._loop_thread.run(
            self.async_list_memories(
                user_id=user_id, agent_id=agent_id,
                limit=limit, offset=offset, order=order,
                include_raw=include_raw,
            )
        )

    def clone_user(
        self,
        src_user_id: str,
        dst_user_id: str,
        *,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """
        克隆用户记忆。

        将 src_user_id 下的全部记忆深拷贝到 dst_user_id 下，
        两者此后完全独立互不影响。

        Args:
            src_user_id: 源用户 ID
            dst_user_id: 目标用户 ID
            agent_id:    可选，限定克隆某个 agent 下的记忆

        Returns:
            {"success": True, "request_id": "...", "cloned_count": N, "elapsed_ms": ...}
        """
        return self._loop_thread.run(
            self.async_clone_user(
                src_user_id, dst_user_id, agent_id=agent_id,
            )
        )

    # ================================================================
    # System 2 Digest API
    # ================================================================

    def digest(
        self,
        user_id: str,
        agent_id: str = "default",
    ) -> dict[str, Any]:
        """
        手动触发 System 2 认知加工（ultra 模式专用，同步阻塞直到完成）。

        digest 一次 = 对该 user 的 VDB 全量跑一次 System 2（Schema 归纳 → L6_SCHEMA
        + cross-domain sweeper）。System 2 的输入是 VDB 全量扫描，与「期间 add 了几次」
        无关，因此 digest 不消费写入队列、跑一次完整认知周期。

        非幂等：每次 digest 看到的是当前最新 VDB 全量，连续多次 digest 有意义
        （add 一批 → digest → 再 add 一批 → 再 digest）。

        注意：意图（L7_INTENTION）由 System 1 在 add/write 时即时抽取落库（pro/ultra
        模式都有），不在 digest 里处理。digest 只负责 System 2 的 Schema 层。

        触发时机由调用方控制：System 2 不在 add/write 里自动触发，也无进程内队列。
        典型 eval 用法：批量 add()（只跑 S1）→ 调一次 digest() 做全量 Schema 归纳。

        仅 ultra 模式可调用。lite/pro 模式调用会抛出 RuntimeError。

        Args:
            user_id:  用户 ID
            agent_id: Agent ID

        Returns:
            {"success": True, "request_id": "...", "tasks_processed": 1, "results": {...},
             "cross_domain_sweeper": {...}, "elapsed_ms": ...}
        """
        if self._mode != "ultra":
            raise RuntimeError(
                f"digest() requires mode='ultra', but this client is mode='{self._mode}'. "
                f"Create a new client with HyMemoryClient(mode='ultra') to use System 2."
            )
        return self._loop_thread.run(
            self.async_digest(user_id=user_id, agent_id=agent_id)
        )

    @_ensure_internal_loop
    async def async_digest(
        self,
        user_id: str,
        agent_id: str = "default",
    ) -> dict[str, Any]:
        """手动触发 System 2 认知加工（异步）"""
        t0 = __import__("time").perf_counter()

        # 获取 System2Writer
        try:
            writer = await self._registry.get_system2_writer()
        except (ValueError, KeyError):
            return {
                "success": False,
                "error": "System2Writer not available.",
                "tasks_processed": 0,
            }

        # 检查是否支持 digest
        if not hasattr(writer, "digest"):
            return {
                "success": False,
                "error": f"Writer '{writer.VERSION}' does not support digest()",
                "tasks_processed": 0,
            }

        result = await writer.digest(user_id=user_id, agent_id=agent_id)
        result["elapsed_ms"] = round((__import__("time").perf_counter() - t0) * 1000, 2)
        return result

    def sweep(
        self,
        user_ids: list[str],
        agent_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        手动触发 cross-domain sweeper（同步）。

        独立于 digest 流程，不执行 S2 Agent，只做：
        1. 补齐 schema 的 beh abstraction + beh_embedding
        2. 碰撞扫描 + core 归纳

        对每个 user_id × agent_id 组合执行一次 sweeper。

        Args:
            user_ids:  SDK user_id 列表
            agent_ids: Agent ID 列表（默认 ["default_agent"]）

        Returns:
            {"success": True, "results": [{...}, ...]}
        """
        if self._mode != "ultra":
            raise RuntimeError(
                f"sweep() requires mode='ultra', but this client is mode='{self._mode}'."
            )
        return self._loop_thread.run(
            self.async_sweep(user_ids=user_ids, agent_ids=agent_ids)
        )

    @_ensure_internal_loop
    async def async_sweep(
        self,
        user_ids: list[str],
        agent_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """手动触发 cross-domain sweeper（异步）

        对每个 user_id × agent_id 组合执行一次 sweeper。
        """
        t0 = __import__("time").perf_counter()

        try:
            writer = await self._registry.get_system2_writer()
        except (ValueError, KeyError):
            return {"success": False, "error": "System2Writer not available.", "results": []}

        effective_agents = agent_ids or ["default_agent"]
        results = []
        for uid in user_ids:
            for aid in effective_agents:
                r = await writer.run_sweeper(user_id=uid, agent_id=aid)
                results.append({"user_id": uid, "agent_id": aid, **r})

        elapsed_ms = round((__import__("time").perf_counter() - t0) * 1000, 2)
        return {"success": True, "results": results, "elapsed_ms": elapsed_ms}

    # ================================================================
    # System Metrics
    # ================================================================

    def get_metrics(self, minutes: int = 5) -> dict[str, Any]:
        """
        获取系统整体负载指标。

        Args:
            minutes: 查询最近 N 分钟的数据（默认 5 分钟）

        Returns:
            {
                "uptime_seconds": float,
                "requests": {"total", "active_sys1", "active_sys2", "queued_sys2", "completed", "failed"},
                "avg_latency_ms": {"sys1_waiting", "sys1_l1_process", "sys1_workflow", ...},
                "throughput": {"sys1_completed_last_60s", "sys2_completed_last_60s"},
                "storage_ops": {"vdb_ops_total", "vdb_ops_avg_ms", "graph_ops_total", "graph_ops_avg_ms"},
            }
        """
        return self._loop_thread.run(self.async_get_metrics(minutes=minutes))

    @_ensure_internal_loop
    async def async_get_metrics(self, minutes: int = 5) -> dict[str, Any]:
        """获取系统整体负载指标（异步）"""
        from .metrics import MetricsCollector
        return await MetricsCollector.get().get_snapshot(minutes=minutes)

    def close(self):
        """关闭客户端，释放所有资源"""
        if self._registry is not None:
            self._loop_thread.run(self._close_async())
        # solo 模式：本 client 拥有 loop_thread，可以安全 stop
        # shared 模式：loop_thread 由 runtime 管，留给 runtime.aclose() 关
        if hasattr(self, '_loop_thread') and self._owns_runtime:
            self._loop_thread.stop()

    def history(self, memory_id: str) -> list[dict[str, Any]]:
        """
        查询某条记忆的变更历史。

        Args:
            memory_id: 记忆 ID

        Returns:
            按时间排序的历史记录列表，每条包含 event/old_memory/new_memory/created_at 等字段。
            如果未启用历史记录，返回空列表。
        """
        return self._loop_thread.run(self.async_history(memory_id))

    def get_recent_history(
        self,
        user_id: str = "",
        agent_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        查询最近的操作历史。

        Args:
            user_id:    用户 ID
            agent_ids:  Agent ID 列表（空列表 = 查所有 agent 的历史）
            limit:      返回数量上限

        Returns:
            按时间倒序的最近操作列表。
            如果未启用历史记录，返回空列表。
        """
        return self._loop_thread.run(
            self.async_get_recent_history(user_id=user_id, agent_ids=agent_ids, limit=limit)
        )

    async def _close_async(self):
        # 先关 coding 链路（不依赖其他组件）
        if getattr(self, "_coding_store", None) is not None:
            try:
                await self._coding_store.close()
            except Exception as e:
                logger.warning(f"[close] coding store close failed: {e}")
            self._coding_store = None
            self._coding_writer = None
            self._coding_llm_provider = None
        if self._registry:
            await self._registry.close()
            self._registry = None
        if self._graph_store:
            await self._graph_store.close()
            self._graph_store = None
        if self._history_store:
            await self._history_store.close()
            self._history_store = None
        # cache：solo 模式下 client 拥有，关；shared 模式下 runtime 拥有，留给 runtime.aclose()
        if self._cache and self._owns_runtime:
            await self._cache.close()
            self._cache = None
        elif self._cache and not self._owns_runtime:
            # shared 模式：仅断引用，不真正关 pool
            self._cache = None
        logger.info("HyMemoryClient closed")

    # ================================================================
    # Delete helpers — 演化链修复 + Graph evidence 清理
    # ================================================================

    async def _repair_evolution_chain_on_delete(self, node) -> None:
        """
        删除节点前修复演化链。

        场景 1: 删除链头（is_latest=True + supersedes 非空）
          → 将 supersedes 列表中的前驱恢复为 is_latest=True + ACTIVE
          → 从前驱的 superseded_by 中移除本节点 ID

        场景 2: 删除链中间节点（is_latest=False + superseded_by 非空 + supersedes 非空）
          → 将前驱的 superseded_by 改为指向后继
          → 将后继的 supersedes 改为指向前驱（跳过本节点）

        场景 3: 删除链尾（is_latest=False + supersedes 为空）
          → 只需从后继的 supersedes 中移除本节点 ID
        """
        from .models.memory import MemoryStatus

        node_id = node.node_id
        supersedes = node.supersedes or []        # 本节点取代的旧节点
        superseded_by = node.superseded_by or []  # 取代本节点的新节点
        is_latest = node.is_latest

        if not supersedes and not superseded_by:
            return  # 不在任何链上

        try:
            # 场景 1: 删除链头 — 恢复前驱
            if is_latest and supersedes:
                for pred_id in supersedes:
                    pred = await self._vector_store.get_by_id(pred_id)
                    if pred is None:
                        continue
                    # 从前驱的 superseded_by 中移除本节点
                    pred_sb = [x for x in (pred.superseded_by or []) if x != node_id]
                    # 如果前驱没有其他后继，恢复为链头
                    if not pred_sb:
                        await self._vector_store.update_payload(pred_id, {
                            "is_latest": True,
                            "status": MemoryStatus.ACTIVE.value,
                            "superseded_by": [],
                        })
                        logger.info(f"[delete] chain repair: restored {pred_id} as chain head")
                    else:
                        await self._vector_store.update_payload(pred_id, {
                            "superseded_by": pred_sb,
                        })

            # 场景 2: 删除链中间 — 跳接前驱和后继
            elif not is_latest and supersedes and superseded_by:
                # 后继的 supersedes: 把本节点替换为本节点的前驱
                for succ_id in superseded_by:
                    succ = await self._vector_store.get_by_id(succ_id)
                    if succ is None:
                        continue
                    succ_sup = list(succ.supersedes or [])
                    if node_id in succ_sup:
                        succ_sup.remove(node_id)
                        succ_sup.extend(supersedes)
                    await self._vector_store.update_payload(succ_id, {
                        "supersedes": succ_sup,
                    })

                # 前驱的 superseded_by: 把本节点替换为本节点的后继
                for pred_id in supersedes:
                    pred = await self._vector_store.get_by_id(pred_id)
                    if pred is None:
                        continue
                    pred_sb = list(pred.superseded_by or [])
                    if node_id in pred_sb:
                        pred_sb.remove(node_id)
                        pred_sb.extend(superseded_by)
                    await self._vector_store.update_payload(pred_id, {
                        "superseded_by": pred_sb,
                    })
                logger.info(f"[delete] chain repair: bridged {supersedes} → {superseded_by}")

            # 场景 3: 删除链尾 — 从后继的 supersedes 中移除
            elif not is_latest and not supersedes and superseded_by:
                for succ_id in superseded_by:
                    succ = await self._vector_store.get_by_id(succ_id)
                    if succ is None:
                        continue
                    succ_sup = [x for x in (succ.supersedes or []) if x != node_id]
                    await self._vector_store.update_payload(succ_id, {
                        "supersedes": succ_sup,
                    })

        except Exception as e:
            logger.warning(f"[delete] chain repair failed for {node_id}: {e}")

    async def _cleanup_graph_evidence(self, deleted_node_id: str) -> None:
        """
        清理已删除 VDB 节点在 Graph 中的 evidence 引用。

        当一个 VDB 节点被删除后，如果它作为 VdbRef 影子节点被某个
        Schema/Intention 通过 DERIVED_FROM 引用，需要清理悬空引用。

        注意：VdbRef 是独立的 node table，不能用 delete_node（那是删 Memory）。
        直接用底层 execute 删 VdbRef + DERIVED_FROM 边。
        """
        if self._graph_store is None:
            return
        try:
            gs = self._graph_store
            # Kuzu backend
            if hasattr(gs, '_execute') and hasattr(gs, '_available'):
                if not gs._available:
                    return
                gs._execute(
                    "MATCH (v:VdbRef {node_id: $nid}) DETACH DELETE v;",
                    {"nid": deleted_node_id},
                )
            # Neo4j backend
            elif hasattr(gs, '_run_write'):
                await gs._run_write(
                    "MATCH (v:VdbRef {node_id: $nid}) DETACH DELETE v",
                    {"nid": deleted_node_id},
                )
            logger.debug(f"[delete] VdbRef {deleted_node_id} cleaned from Graph")
        except Exception as e:
            logger.debug(f"[delete] VdbRef cleanup for {deleted_node_id}: {e}")

    # ================================================================
    # 异步 API
    # ================================================================

    @_ensure_internal_loop
    async def async_add(
        self,
        data: AddInput,
        *,
        user_id: str = "",
        agent_id: str = "default_agent",
        session_id: str = "default_session",
        metadata: dict[str, Any] | None = None,
        memory_at: datetime | None = None,
        enable_summary: bool | None = None,
        workspace_id: str | None = None,
        branch: str | None = None,
        request_id: str | None = None,
        extract_scene: str | None = None,
    ) -> dict[str, Any]:
        """写入记忆（异步）

        Args:
            enable_summary: 本次是否生成 L3_SUMMARY；None = 沿用 LLMConfig.enable_summary（全局默认 False）。
                            仅 pro/ultra 模式有效。
            workspace_id / branch: Coding memory 路径专用，详见 add() docstring。
        """
        # 生成 request_id 并注入日志上下文（队列消费可传入固定 id 保证幂等与追溯）
        request_id = request_id or str(uuid.uuid4())
        set_request_id(request_id)
        t0 = time.perf_counter()

        content, chat_messages, content_type = self._parse_input(data)
        uid = user_id

        # 纯 input token 数（tiktoken 精确估算；不含 system/few-shot/历史等 prompt 开销）
        from .utils.token_count import count_input_tokens
        input_tokens = count_input_tokens(content, chat_messages)

        # ────────────────────────────────────────────────────────────
        # Coding 链路分流（仅 messages 含 tool 消息且 LLM 判定为 coding）
        # 不含 tool: O(1) 短路 → chat 链
        # 含 tool 但判 chat: strip tool 后走 chat 链
        # 含 tool 且判 coding: 走 coding 链路
        # 关键：不影响现有 chat 路径
        # ────────────────────────────────────────────────────────────
        if (
            content_type == "messages"
            and getattr(self._config, "coding", None) is not None
            and self._config.coding.enable
            and has_any_tool_message(chat_messages)
        ):
            # lazy 初始化 coding LLM provider（用于 judge）
            if self._coding_llm_provider is None:
                from .agent.llm_provider import LLMProvider
                self._coding_llm_provider = LLMProvider(self._config)

            try:
                is_coding = await classify_messages_is_coding(
                    chat_messages, self._coding_llm_provider
                )
            except Exception as e:
                logger.warning(f"[add] coding judge failed, fallback to chat: {e}")
                is_coding = False

            if is_coding:
                logger.info(
                    f"[add] routing to CODING path: user={uid} agent_id={agent_id} "
                    f"workspace_id={workspace_id!r} branch={branch!r}"
                )
                writer = await self._get_coding_writer()
                resp = await writer.write(
                    chat_messages,
                    user_id=uid,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    branch=branch,
                    session_id=session_id,
                    request_id=request_id,
                )
                # 同步刷新 elapsed
                resp["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                resp["input_tokens"] = input_tokens
                return resp

            # 判定为 chat：strip tool 消息再继续
            logger.info(
                "[add] tool messages found but classified as CHAT; stripping tools"
            )
            chat_messages = strip_tool_messages(chat_messages)

        if content_type == "messages":
            preview = f"messages({len(chat_messages)} turns)"
            logger.info(
                f"[add] writing {preview} user={uid} "
                f"agent_id={agent_id} session_id={session_id} mode={self._mode}"
            )
            logger.info(f"[add] messages detail: {[m.to_dict() for m in chat_messages]}")
        else:
            logger.info(
                f"[add] writing text({len(content)} chars) user={uid} "
                f"agent_id={agent_id} session_id={session_id} mode={self._mode} "
                f"content={content}"
            )

        # mode → agent_mode 映射
        # lite: 不调 LLM agent
        # pro/ultra: 调 LLM agent 做事实提取
        agent_mode = "disabled" if self._mode == "lite" else "full"
        extra = dict(metadata) if metadata else {}
        extra["agent_mode"] = agent_mode
        extra["mode"] = self._mode

        write_req = WriteRequest(
            content=content,
            messages=chat_messages,
            user_id=uid,
            agent_id=agent_id,
            session_id=session_id,
            content_type=content_type,
            extra=extra,
            memory_at=memory_at,
            enable_summary=enable_summary,
            extract_scene=extract_scene,
            # 显式透传 request_id（本地变量，不受 contextvar 污染）作为整条写入链路
            # 落库 memory_ops / DIGEST_SUMMARY / pipeline_logs 的单一真相源。
            request_id=request_id,
        )

        # mode → writer:
        #   lite/pro → MemoryWriter (区别在 agent_mode)
        #   ultra    → System2Writer (System 1 + System 2 auto-enqueue)
        if self._mode == "ultra":
            writer = await self._registry.get_system2_writer()
        else:
            writer = await self._registry.get_writer()
        logger.debug(f"[add] using writer: {writer.VERSION} (mode={self._mode})")
        result: WriteResponse = await writer.write(write_req)

        resp = {
            "success": result.success,
            "scene": "chat",
            "memory_id": result.memory_id,
            "request_id": request_id,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "error_code": result.error_code if result.error_code else None,
            "error_message": result.error_message or None,
        }

        # timing breakdown
        _timing = result.extra.get("timing")
        if _timing:
            resp["timing"] = _timing

        # reconcile op counts + stored memory ids (analytics passthrough)
        if result.extra.get("reconcile_ops"):
            resp["ops_summary"] = result.extra["reconcile_ops"]
        if result.extra.get("agent_stored_ids"):
            resp["memory_ids"] = result.extra["agent_stored_ids"]

        # Extraction visibility: surface agent outcome so callers know whether
        # the memory was fully processed or left as unextracted l1_raw.
        _agent_status = result.extra.get("agent_status", "")
        if _agent_status:
            resp["extraction_status"] = _agent_status
            if _agent_status == "failed":
                resp["extraction_error"] = result.extra.get("agent_error", "")
                resp["extraction_error_code"] = result.extra.get("agent_error_code", "")

        # token usage：纯 input（tiktoken）+ extract / reconcile 两条 LLM 链路的
        # prompt/completion 消耗（lite 模式无 LLM，extract/reconcile 为 0）。
        resp["input_tokens"] = input_tokens
        resp["extract_tokens"] = result.extra.get(
            "extract_tokens", {"prompt": 0, "completion": 0, "total": 0}
        )
        resp["reconcile_tokens"] = result.extra.get(
            "reconcile_tokens", {"prompt": 0, "completion": 0, "total": 0}
        )

        if result.success:
            logger.info(f"[add] done: memory_id={result.memory_id} elapsed={result.elapsed_ms:.0f}ms")

            # 记录历史（非阻塞，失败仅 warning）
            if self._history_store is not None:
                try:
                    isolation_key = self._build_isolation_key(uid, agent_id)
                    # 从 content 或 messages 中提取用于记录的文本
                    history_content = content or "; ".join(
                        f"{m.role}: {m.content}" for m in chat_messages
                    )
                    await self._history_store.record_add(
                        memory_id=result.memory_id,
                        content=history_content,
                        layer=result.layer or "",
                        isolation_key=isolation_key,
                    )
                except Exception as e:
                    logger.warning(f"[add] history record failed: {e}")
        else:
            logger.warning(f"[add] failed: {result.error_message}")

        return resp

    def _spawn_search_dedup(
        self,
        all_mems: list[dict[str, Any]],
        *,
        request_id: str,
        user_id: str,
        agent_id: str,
    ) -> None:
        """对 search 结果中的 L2_FACT + L4_IDENTITY 做去重删库（fire-and-forget）。

        只取这两层（重复重灾区）；带 evolution_chain 的项视为链头，其 chain node_ids
        从 evolution_chain 取出供连带删除。整体后台执行，不阻塞 search 返回。
        """
        import asyncio

        from .pipelines._retrieval.dedup import DedupItem, execute_dedup

        _DEDUP_LAYERS = {"l2_fact", "l4_identity"}
        targets = [m for m in all_mems if (m.get("layer") or "").lower() in _DEDUP_LAYERS and m.get("memory_id")]
        if len(targets) < 2:
            return

        vector_store = self._vector_store
        cache = getattr(self, "_cache", None)

        async def _run():
            try:
                node_ids = [m["memory_id"] for m in targets]
                embs = await vector_store.get_embeddings(node_ids)
                items: list[DedupItem] = []
                for m in targets:
                    mid = m["memory_id"]
                    emb = embs.get(mid)
                    if not emb:
                        continue
                    chain = m.get("evolution_chain") or []
                    chain_ids = [c.get("node_id") for c in chain if c.get("node_id")] if chain else [mid]
                    items.append(DedupItem(
                        node_id=mid,
                        embedding=emb,
                        content=m.get("content", ""),
                        is_latest=True,
                        is_chain_head=bool(chain),
                        gmt_created=(float(m["gmt_created"]) if m.get("gmt_created") else None),
                        chain_node_ids=chain_ids or [mid],
                    ))
                if len(items) >= 2:
                    await execute_dedup(
                        items,
                        vector_store=vector_store,
                        cache=cache,
                        trigger="search",
                        request_id=request_id,
                        user_id=user_id,
                        agent_id=agent_id,
                        delete_from_store=True,
                    )
            except Exception as e:
                logger.debug(f"[search] background dedup failed: {e}")

        try:
            asyncio.ensure_future(_run())
        except RuntimeError:
            # 无事件循环（极少见）：同步跳过，不影响主流程
            logger.debug("[search] no running loop for dedup; skipped")

    @_ensure_internal_loop
    async def async_search(
        self,
        query: str | None = None,
        *,
        queries: list[str] | None = None,
        scene: str | None = None,
        user_ids: list[str] | None = None,
        agent_ids: list[str] | None = None,
        session_ids: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.4,
        profile_min_score: float = 0.4,
        profile_limit: int = 10,
        intention_limit: int = 0,
        created_after: float | None = None,
        reader: str = "",
        workspace_id: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """搜索记忆（异步）。详见 search() docstring。"""
        # ── 入参归一化 ──
        if queries is not None and query is not None:
            raise ValueError("query and queries are mutually exclusive; choose one")
        if queries is None and query is None:
            raise ValueError("either query or queries is required")
        if queries is None:
            queries_list: list[str] = [query]
        else:
            if not isinstance(queries, list) or not queries:
                raise ValueError("queries must be a non-empty list[str]")
            queries_list = [str(q) for q in queries]
        target_query = queries_list[-1]

        # scene 校验
        scene_norm: str | None = None
        if scene is not None:
            sn = str(scene).strip().lower()
            if sn not in ("productivity", "normal"):
                raise ValueError(
                    f"scene must be 'productivity' or 'normal', got {scene!r}"
                )
            scene_norm = sn

        # 生成 request_id 并注入日志上下文
        request_id = str(uuid.uuid4())
        set_request_id(request_id)
        t0 = time.perf_counter()

        effective_user_ids = user_ids if user_ids is not None else []
        effective_agent_ids = agent_ids if agent_ids is not None else []
        effective_session_ids = session_ids if session_ids is not None else []

        # 校验: 必须传 user_ids
        if not effective_user_ids:
            raise ValueError("user_ids is required and cannot be empty")

        # 校验: 多 agent_ids + 多 session_ids 非法
        if len(effective_agent_ids) > 1 and len(effective_session_ids) > 1:
            raise ValueError(
                "Cannot specify multiple agent_ids and multiple session_ids simultaneously. "
                "Use single agent_id with multiple session_ids, or multiple agent_ids without session_ids."
            )

        # ────────────────────────────────────────────────────────────
        # Scene 路由（决定走 coding 还是 chat 召回）
        #
        # 三分支：
        #   1) 显式 scene 指定（productivity/normal）→ 直接用，**不调 LLM**（低时延）
        #   2) 未指定 scene + 多 queries        → 调 LLM 同时判类 + 改写
        #                                        effective_query = rewrite_query
        #   3) 未指定 scene + 单 query         → 默认 normal（不调 LLM）
        # ────────────────────────────────────────────────────────────
        coding_enabled = (
            getattr(self._config, "coding", None) is not None
            and self._config.coding.enable
        )
        effective_target_query = target_query  # 默认用 queries[-1] 或 query
        decided_scene: str  # 最终决定的 scene
        rewrite_used: bool = False
        scene_mode: str = "default"          # explicit / llm / default
        scene_is_coding: bool = False
        _t_scene = t0  # 场景判定耗时起点（沿用 search 入口时间）

        if scene_norm is not None:
            # 1) 显式指定
            decided_scene = scene_norm
            scene_mode = "explicit"
            scene_is_coding = (decided_scene == "productivity")
            logger.info(
                f"[search] scene explicit={decided_scene} (no LLM judge); "
                f"target={effective_target_query!r}"
            )
        elif queries is not None and coding_enabled:
            # 2) 多 queries → LLM 同时判类 + 改写
            if self._coding_llm_provider is None:
                from .agent.llm_provider import LLMProvider
                self._coding_llm_provider = LLMProvider(self._config)
            try:
                async def classify_and_rewrite_queries(*a, **kw): return {"is_coding": False, "rewrite_query": "", "ok": False}
                jr = await classify_and_rewrite_queries(
                    queries_list, self._coding_llm_provider
                )
            except Exception as e:
                logger.warning(
                    f"[search] classify_and_rewrite failed, fallback to normal: {e}"
                )
                jr = {"is_coding": False, "rewrite_query": target_query, "ok": False}

            if jr.get("ok"):
                decided_scene = "productivity" if jr.get("is_coding") else "normal"
                scene_mode = "llm"
                scene_is_coding = bool(jr.get("is_coding"))
                new_q = jr.get("rewrite_query") or target_query
                if new_q and new_q != target_query:
                    effective_target_query = new_q
                    rewrite_used = True
                logger.info(
                    f"[search] scene auto={decided_scene} via LLM; "
                    f"orig_target={target_query!r} effective={effective_target_query!r} "
                    f"rewrite_used={rewrite_used}"
                )
            else:
                # parse / LLM 失败 → 默认 normal，原 query
                decided_scene = "normal"
                scene_mode = "llm"
                logger.info(
                    f"[search] scene auto fallback=normal; target={target_query!r}"
                )
        else:
            # 3) 单 query 或 productivity 链路关闭 → 默认 normal
            decided_scene = "normal"
            logger.info(
                f"[search] scene default=normal (single query / productivity disabled); "
                f"target={target_query!r}"
            )

        # ────────────────────────────────────────────────────────────
        # 场景判定落 pipeline log（search 的第一步，在 reader 之前）
        # ────────────────────────────────────────────────────────────
        if getattr(self, "_cache", None) is not None:
            try:
                from .pipelines._retrieval.trace import ReadTraceLogger
                _scene_trace = ReadTraceLogger(
                    cache=self._cache,
                    request_id=request_id,
                    user_id=(effective_user_ids[0] if effective_user_ids else ""),
                    agent_id=(effective_agent_ids[0] if effective_agent_ids else "default_agent"),
                    reader_version="client",
                )
                await _scene_trace.log_scene_judge(
                    query=target_query or "",
                    scene=decided_scene,
                    mode=scene_mode,
                    is_coding=scene_is_coding,
                    rewrite_query=(effective_target_query if rewrite_used else None),
                    rewrite_used=rewrite_used,
                    elapsed_ms=(time.perf_counter() - _t_scene) * 1000 if _t_scene else 0.0,
                )
            except Exception as _e:
                logger.debug(f"[search] scene judge trace failed: {_e}")

        # ────────────────────────────────────────────────────────────
        # 按决定的 scene 分流
        # ────────────────────────────────────────────────────────────
        if decided_scene == "productivity":
            if not coding_enabled:
                logger.warning(
                    "[search] scene=productivity requested but productivity link disabled; "
                    "falling back to normal"
                )
                decided_scene = "normal"
            else:
                logger.info(
                    f"[search] routing to PRODUCTIVITY path: target={effective_target_query!r} "
                    f"workspace_id={workspace_id!r} branch={branch!r}"
                )
                return await self._search_coding(
                    effective_target_query,
                    user_ids=effective_user_ids,
                    workspace_id=workspace_id,
                    branch=branch,
                    limit=limit,
                    min_score=min_score,
                    request_id=request_id,
                    t0=t0,
                )

        # ────────────────────────────────────────────────────────────
        # Chat 召回链路（原有逻辑）
        # ────────────────────────────────────────────────────────────
        logger.info(
            f"[search] query='{effective_target_query}' user_ids={effective_user_ids} "
            f"agent_ids={effective_agent_ids} session_ids={effective_session_ids} limit={limit}"
        )

        read_req = ReadRequest(
            query=effective_target_query,
            user_ids=effective_user_ids,
            agent_ids=effective_agent_ids,
            session_ids=effective_session_ids,
            limit=limit,
            min_score=min_score,
            profile_min_score=profile_min_score,
            profile_limit=profile_limit,
            intention_limit=intention_limit,
            request_id=request_id,
            created_after=created_after,
        )

        reader_impl = await self._registry.get_reader(reader_name=reader)
        logger.debug(f"[search] using reader: {reader_impl.VERSION}")
        result: ReadResponse = await reader_impl.read(read_req)

        if not result.success:
            logger.warning(f"[search] failed: {result.error_message}")
            return {
                "request_id": request_id,
                "memories": {"profile": [], "proactive": [], "normal": []},
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            }

        _all_mems = []
        for mem in result.memories:
            score = mem.get("score", 0.0)
            item = {
                "memory_id": mem.get("memory_id", ""),
                "content": mem.get("content", ""),
                "score": round(score, 4),
                "layer": mem.get("layer", ""),
                "owner": mem.get("owner"),
                "speculate": mem.get("speculate"),
                "source_raw_memory_id": mem.get("source_raw_memory_id"),
                "tags": mem.get("tags") or [],
                "memory_at": mem.get("memory_at"),
                "gmt_created": mem.get("gmt_created"),
            }
            if mem.get("schema_type"):
                item["schema_type"] = mem["schema_type"]
            if mem.get("evolution_chain"):
                item["evolution_chain"] = mem["evolution_chain"]
            _all_mems.append(item)

        # search 链路去重：仅对 L2_FACT + L4_IDENTITY（重复重灾区）判重删库。
        # L0 及其他层不参与。fire-and-forget，不阻塞本次返回（清理面向后续查询）。
        try:
            self._spawn_search_dedup(
                _all_mems,
                request_id=request_id,
                user_id=(effective_user_ids[0] if effective_user_ids else ""),
                agent_id=(effective_agent_ids[0] if effective_agent_ids else ""),
            )
        except Exception as e:
            logger.debug(f"[search] spawn dedup failed (non-fatal): {e}")

        # Memory Strength：搜索后 best-effort 回写 access_count+1 / last_accessed_at=now
        # （fire-and-forget，不阻塞响应；失败静默）。用 reader 透出的 access_count 计算 +1，
        # 避免再读一次。受总开关 strength_enabled 约束（默认关闭），并可用
        # MEMORY_RECALL_ACCESS_TRACKING=false 单独关闭回写。
        _recall_cfg = self._config.recall
        if (getattr(_recall_cfg, "strength_enabled", False)
                and getattr(_recall_cfg, "access_tracking_enabled", True)):
            _bump_items = [
                (mem.get("memory_id", ""), mem.get("access_count", 0))
                for mem in result.memories
                if mem.get("memory_id")
            ]
            if _bump_items:
                from .pipelines._retrieval.strength import bump_access as _bump_access

                async def _do_bump():
                    try:
                        await _bump_access(self._vector_store, _bump_items)
                    except Exception as _be:
                        logger.debug(f"[search] access bump failed: {_be}")

                try:
                    asyncio.ensure_future(_do_bump())
                except Exception as _be:
                    logger.debug(f"[search] access bump schedule failed: {_be}")

        # 按 channel 分组
        # profile = L0_BASIC_INFO + L6_SCHEMA（用户画像类）；proactive = intention；
        # 其余（含 L4_IDENTITY / L2_FACT / L3_SUMMARY）归 normal。
        # L4_IDENTITY 从 SDK 视角按 VDB 普通记忆处理（走主语义+BM25 路），不再算 profile，
        # 避免同节点被 profile 路与主路双重召回、profile 高分被主路融合低分顶掉。
        # profile 桶仍额外卡 profile_min_score（对 L0/L6 有效），不达标降级 normal。
        _PROFILE_LAYERS = {"l0_basic_info", "l6_schema"}
        _PROACTIVE_LAYERS = {"l7_intention"}
        memories = {"profile": [], "proactive": [], "normal": []}
        total = 0
        for mem in _all_mems:
            layer = mem["layer"]
            if layer in _PROFILE_LAYERS:
                if mem.get("score", 0.0) >= profile_min_score:
                    memories["profile"].append(mem)
                else:
                    memories["normal"].append(mem)
            elif layer in _PROACTIVE_LAYERS:
                memories["proactive"].append(mem)
            else:
                memories["normal"].append(mem)
            total += 1

        logger.info(
            f"[search] done: {total} results "
            f"(profile={len(memories['profile'])} proactive={len(memories['proactive'])} "
            f"normal={len(memories['normal'])}), elapsed={result.elapsed_ms:.0f}ms"
        )
        for ch, mems in memories.items():
            for i, mem in enumerate(mems):
                logger.debug(
                    f"[search] {ch}#{i} score={mem['score']:.4f} "
                    f"[{mem['layer']}] {mem['content']}"
                )

        # 记录搜索历史（可选，非阻塞）
        if self._history_store is not None and self._config.history.record_searches:
            try:
                hist_user = effective_user_ids[0] if effective_user_ids else "default"
                hist_agent = effective_agent_ids[0] if effective_agent_ids else "default"
                isolation_key = self._build_isolation_key(hist_user, hist_agent)
                await self._history_store.record_search(
                    query=effective_target_query,
                    isolation_key=isolation_key,
                    results_count=total,
                )
            except Exception as e:
                logger.warning(f"[search] history record failed: {e}")

        return {
            "request_id": request_id,
            "memories": memories,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    # ================================================================
    # CRUD API（异步）— get / update / delete / delete_all
    # ================================================================

    @_ensure_internal_loop
    async def async_get(self, memory_id: str) -> dict[str, Any] | None:
        """
        获取单条记忆（异步）。

        VDB + Graph 双路并行查询，哪个有结果返回哪个。

        Returns:
            {"memory_id", "content", "layer", "source", "request_id", "elapsed_ms"} 或 None
        """
        import asyncio as _asyncio_get

        request_id = str(uuid.uuid4())
        set_request_id(request_id)
        t0 = time.perf_counter()

        logger.info(f"[get] memory_id={memory_id}")

        # 双路并行
        async def _get_vdb():
            return await self._vector_store.get_by_id(memory_id)

        async def _get_graph():
            if self._graph_store is None:
                return None
            try:
                return await self._graph_store.get_node(memory_id)
            except Exception as e:
                logger.debug(f"[get] graph_store query failed: {e}")
                return None

        vdb_node, graph_node = await _asyncio_get.gather(_get_vdb(), _get_graph())

        elapsed = round((time.perf_counter() - t0) * 1000, 2)

        # VDB 优先（如果两边都有，以 VDB 为准）
        if vdb_node is not None:
            return {
                "memory_id": vdb_node.node_id,
                "content": vdb_node.content,
                "layer": vdb_node.layer.value,
                "status": vdb_node.status.value if vdb_node.status else "active",
                "is_latest": vdb_node.is_latest,
                "supersedes": vdb_node.supersedes or [],
                "superseded_by": vdb_node.superseded_by or [],
                "tags": vdb_node.tags or [],
                "memory_at": int(vdb_node.memory_at.timestamp()) if isinstance(vdb_node.memory_at, datetime) else None,
                "gmt_created": int(vdb_node.gmt_created.timestamp()) if isinstance(vdb_node.gmt_created, datetime) else None,
                "speculate": getattr(vdb_node, "speculate", None),
                "source_raw_memory_id": getattr(vdb_node, "source_raw_memory_id", None),
                "user_id": vdb_node.user_id,
                "agent_id": vdb_node.agent_id,
                "confidence": vdb_node.confidence,
                "custom": vdb_node.custom or {},
                "source": "vdb",
                "request_id": request_id,
                "elapsed_ms": elapsed,
            }

        if graph_node is not None:
            return {
                "memory_id": graph_node.node_id,
                "content": graph_node.content,
                "layer": graph_node.layer.value if graph_node.layer else "",
                "status": graph_node.status.value if graph_node.status else "active",
                "is_latest": getattr(graph_node, "is_latest", True),
                "supersedes": graph_node.supersedes or [],
                "superseded_by": graph_node.superseded_by or [],
                "tags": graph_node.tags or [],
                "memory_at": int(graph_node.memory_at.timestamp()) if isinstance(graph_node.memory_at, datetime) else None,
                "gmt_created": int(graph_node.gmt_created.timestamp()) if isinstance(graph_node.gmt_created, datetime) else None,
                "speculate": getattr(graph_node, "speculate", None),
                "source_raw_memory_id": getattr(graph_node, "source_raw_memory_id", None),
                "user_id": graph_node.user_id,
                "agent_id": graph_node.agent_id,
                "confidence": graph_node.confidence,
                "custom": getattr(graph_node, "custom", {}) or {},
                "source": "graph",
                "request_id": request_id,
                "elapsed_ms": elapsed,
            }

        logger.info(f"[get] not found: memory_id={memory_id}")
        return None

    @_ensure_internal_loop
    async def async_update(self, memory_id: str, content: str) -> dict[str, Any]:
        """
        更新记忆内容（异步）。

        流程: get_by_id → 404 校验 → embed → 更新节点字段 → upsert → 记录历史

        Returns:
            {"success", "memory_id", "request_id", "elapsed_ms"}
        """
        request_id = str(uuid.uuid4())
        set_request_id(request_id)
        t0 = time.perf_counter()

        logger.info(f"[update] memory_id={memory_id} new_content({len(content)} chars)")

        # 获取旧节点
        node = await self._vector_store.get_by_id(memory_id)
        if node is None:
            logger.warning(f"[update] not found: memory_id={memory_id}")
            return {
                "success": False,
                "memory_id": memory_id,
                "request_id": request_id,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                "error_message": f"memory_id '{memory_id}' not found",
            }

        old_content = node.content
        old_status = node.status.value

        # 生成新 embedding
        new_embedding = await self._embed_service.embed(content)

        # 更新节点
        node.content = content
        node.embedding = new_embedding

        await self._vector_store.upsert(node)
        logger.info(f"[update] done: memory_id={memory_id}")

        # 记录历史
        if self._history_store is not None:
            try:
                uid = node.user_id or ""
                agent_id = node.agent_id or "default"
                isolation_key = self._build_isolation_key(uid, agent_id)
                await self._history_store.record_update(
                    memory_id=memory_id,
                    old_content=old_content,
                    new_content=content,
                    old_status=old_status,
                    new_status=node.status.value,
                    change_reason="client.update",
                    isolation_key=isolation_key,
                )
            except Exception as e:
                logger.warning(f"[update] history record failed: {e}")

        return {
            "success": True,
            "memory_id": memory_id,
            "request_id": request_id,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    @_ensure_internal_loop
    async def async_delete(self, memory_id: str) -> dict[str, Any]:
        """
        删除单条记忆（异步）。

        同时删除向量库和图数据库（如有）中的对应节点。

        演化链处理：
        - 如果删除的是链头（is_latest=True + supersedes 非空），
          将前驱节点恢复为 is_latest=True + status=ACTIVE
        - 如果删除的是链中间节点，将其 supersedes 指向的前驱重新挂到后继的 supersedes 上

        Graph evidence 处理：
        - 删除 Graph 中该节点 + 关联的 VdbRef 影子节点 + DERIVED_FROM 边

        Returns:
            {"success", "deleted_count", "request_id", "elapsed_ms"}
        """
        request_id = str(uuid.uuid4())
        set_request_id(request_id)
        t0 = time.perf_counter()

        logger.info(f"[delete] memory_id={memory_id}")

        # 先获取节点信息（用于链修复 + history + tag 清理）
        old_content = ""
        old_tags: list[str] = []
        old_user_id = ""
        old_isolation_key = ""
        node = await self._vector_store.get_by_id(memory_id)
        if node is not None:
            old_content = node.content
            try:
                old_tags = list(node.tags or [])
            except Exception:
                old_tags = []
            old_user_id = node.user_id or ""
            try:
                old_isolation_key = node.get_isolation_key() or ""
            except Exception:
                old_isolation_key = ""

        # ── 演化链修复（VDB 删除前执行）──
        if node is not None:
            await self._repair_evolution_chain_on_delete(node)

        # 删除向量库
        deleted = await self._vector_store.delete(memory_id)

        # 删除图数据库节点 + 清理 evidence（如有）
        if self._graph_store is not None:
            try:
                await self._graph_store.delete_node(memory_id)
            except Exception as e:
                logger.warning(f"[delete] graph_store delete failed: {e}")
            # 清理该节点作为 VdbRef 被其他 Schema 引用的情况
            try:
                await self._cleanup_graph_evidence(memory_id)
            except Exception as e:
                logger.debug(f"[delete] graph evidence cleanup failed (non-fatal): {e}")

        deleted_count = 1 if deleted else 0

        # 清理 tag_index：此 memory 删除后，检查其每个 tag 是否还有其他 memory 引用；
        # 无引用则从 tag_index 中移除。失败静默降级，不影响 delete 语义。
        if deleted and old_tags and old_user_id:
            try:
                from .pipelines._retrieval import tag_index as _tag_index_helper
                removed = await _tag_index_helper.cleanup_tags_on_delete(
                    vector_store=self._vector_store,
                    user_id=old_user_id,
                    tags=old_tags,
                    isolation_key=old_isolation_key,
                )
                if removed:
                    logger.debug(f"[delete] tag_index cleaned {removed} orphan tags")
            except Exception as e:
                logger.debug(f"[delete] tag_index cleanup failed (non-fatal): {e}")

        # 记录历史
        if self._history_store is not None and deleted:
            try:
                uid = (node.user_id if node else "") or ""
                agent_id = (node.agent_id if node else "") or "default"
                isolation_key = self._build_isolation_key(uid, agent_id)
                await self._history_store.record_delete(
                    memory_id=memory_id,
                    content=old_content,
                    isolation_key=isolation_key,
                )
            except Exception as e:
                logger.warning(f"[delete] history record failed: {e}")

        logger.info(f"[delete] done: deleted_count={deleted_count}")

        return {
            "success": deleted,
            "deleted_count": deleted_count,
            "request_id": request_id,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    async def _acquire_graph_store_for_purge(self):
        """
        获取用于 purge 的 GraphStore（与 mode 无关）。

        ultra 模式复用常驻连接；pro/lite 在检测到遗留图数据时临时打开并 purge 后关闭。
        Returns:
            (graph_store | None, ephemeral: bool) — ephemeral=True 时调用方须 close。
        """
        if self._graph_store is not None:
            return self._graph_store, False

        from .data.graph_store import create_graph_store, graph_data_may_exist

        if not graph_data_may_exist(self._config):
            return None, False

        gs = create_graph_store(self._config)
        await gs.initialize()
        if getattr(gs, "_available", True) is False:
            logger.warning(
                "[purge] ephemeral graph store unavailable (e.g. Kuzu file lock); "
                "skip graph purge"
            )
            await gs.close()
            return None, False

        provider = getattr(self._config.graph_store, "provider", None) or "kuzu"
        logger.info(
            f"[purge] opened ephemeral GraphStore for delete_all "
            f"(mode={self._mode}, provider={provider})"
        )
        return gs, True

    async def _purge_graph_data(
        self,
        user_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """按 user 范围清理图数据（VDB 与 mode 无关，图库亦同）。"""
        gs, ephemeral = await self._acquire_graph_store_for_purge()
        if gs is None:
            return 0
        try:
            n = await gs.delete_by_metadata(
                user_id=user_id, agent_id=agent_id, session_id=session_id,
            )
            logger.info(
                f"[delete_all] graph purged: user={user_id} agent={agent_id} "
                f"session={session_id} memory_nodes={n} ephemeral={ephemeral}"
            )
            return n
        except Exception as e:
            logger.warning(f"[delete_all] graph_store purge failed: {e}")
            return 0
        finally:
            if ephemeral:
                await gs.close()

    @_ensure_internal_loop
    async def async_delete_all(
        self,
        *,
        user_id: str = "",
        agent_ids: list[str] | None = None,
        session_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        删除用户的记忆（异步）。

        默认删除该用户的所有记忆。可通过 agent_ids/session_ids 缩小范围。

        - agent_ids=[], session_ids=[] → 删除 user 下所有 agent、所有 session
        - agent_ids=["a1"] → 删除 user 下 a1 的所有 session
        - agent_ids=["a1"], session_ids=["s1","s2"] → 删除指定组合

        Returns:
            {"success", "deleted_count", "request_id", "elapsed_ms"}
        """
        request_id = str(uuid.uuid4())
        set_request_id(request_id)
        t0 = time.perf_counter()

        uid = user_id
        effective_agent_ids = agent_ids if agent_ids is not None else []
        effective_session_ids = session_ids if session_ids is not None else []

        total_deleted = 0
        graph_deleted = 0

        if not effective_agent_ids and not effective_session_ids:
            # 删除用户所有记忆：按 user_id 字段过滤
            logger.info(f"[delete_all] deleting all for user={uid}")
            count = await self._vector_store.delete_by_metadata(user_id=uid)
            if count > 0:
                total_deleted += count
            graph_deleted = await self._purge_graph_data(uid)

        elif effective_agent_ids and not effective_session_ids:
            # 删除指定 agent 下所有 session
            for aid in effective_agent_ids:
                logger.info(f"[delete_all] deleting user={uid} agent={aid}")
                count = await self._vector_store.delete_by_metadata(
                    user_id=uid, agent_id=aid,
                )
                if count > 0:
                    total_deleted += count
                graph_deleted += await self._purge_graph_data(uid, agent_id=aid)

        else:
            # 构建完整的 agent × session 组合
            a_ids = effective_agent_ids or ["default_agent"]
            for aid in a_ids:
                for sid in effective_session_ids:
                    logger.info(f"[delete_all] deleting user={uid} agent={aid} session={sid}")
                    count = await self._vector_store.delete_by_metadata(
                        user_id=uid, agent_id=aid, session_id=sid,
                    )
                    if count > 0:
                        total_deleted += count
                    graph_deleted += await self._purge_graph_data(
                        uid, agent_id=aid, session_id=sid,
                    )

        logger.info(
            f"[delete_all] done: vdb_deleted={total_deleted} graph_memory_nodes={graph_deleted} "
            f"mode={self._mode}"
        )

        return {
            "success": True,
            "deleted_count": total_deleted,
            "graph_deleted_count": graph_deleted,
            "request_id": request_id,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    # ================================================================
    # List Memories API（异步）
    # ================================================================

    @staticmethod
    def _memory_node_to_list_item(node) -> dict[str, Any]:
        """将 MemoryNode 序列化为 list API 条目。"""
        _memory_at = None
        if isinstance(node.memory_at, datetime):
            _memory_at = int(node.memory_at.timestamp())
        _gmt_created = None
        if isinstance(node.gmt_created, datetime):
            _gmt_created = int(node.gmt_created.timestamp())
        if _gmt_created is None and isinstance(getattr(node, "valid_from", None), datetime):
            _gmt_created = int(node.valid_from.timestamp())

        from .models.memory import MemoryLayer
        layer_value = node.layer.value if node.layer else ""
        extracted = node.layer != MemoryLayer.L1_RAW if node.layer else True

        return {
            "memory_id": node.node_id,
            "content": node.content,
            "layer": layer_value,
            "status": node.status.value if node.status else "active",
            "memory_at": _memory_at,
            "gmt_created": _gmt_created,
            "user_id": node.user_id,
            "agent_id": node.agent_id,
            "session_id": getattr(node, "session_id", None),
            "speculate": getattr(node, "speculate", None),
            "source_raw_memory_id": getattr(node, "source_raw_memory_id", None),
            "tags": node.tags or [],
            "custom": node.custom or {},
            "extracted": extracted,
        }

    @staticmethod
    def _sort_memory_nodes(nodes: list, *, order: str) -> list:
        reverse = (order.lower() != "asc")

        def _sort_key(n):
            val = n.memory_at if n.memory_at is not None else n.gmt_created
            if val is None:
                val = getattr(n, "valid_from", None)
            if val is None:
                return datetime.min
            if isinstance(val, datetime):
                return val
            return datetime.min

        nodes.sort(key=_sort_key, reverse=reverse)
        return nodes

    async def _list_vdb_bucket(
        self,
        *,
        user_id: str,
        agent_id: str | None,
        limit: int,
        offset: int,
        order: str,
        include_raw: bool = True,
    ) -> dict[str, Any]:
        from .models.memory import MemoryLayer, MemoryStatus

        all_nodes = await self._vector_store.list_by_user(
            user_id=user_id,
            agent_id=agent_id,
            status_filter=[MemoryStatus.ACTIVE],
        )
        if not include_raw:
            all_nodes = [n for n in all_nodes if n.layer != MemoryLayer.L1_RAW]
        all_nodes = self._sort_memory_nodes(all_nodes, order=order)
        total = len(all_nodes)
        page_nodes = all_nodes[offset: offset + limit]
        return {
            "memories": [self._memory_node_to_list_item(n) for n in page_nodes],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def _list_graph_bucket(
        self,
        *,
        user_id: str,
        agent_id: str | None,
        limit: int,
        offset: int,
        order: str,
    ) -> dict[str, Any] | None:
        """列出图库 L6/L7 节点；无图数据或不可用时返回 None。"""
        from .models.memory import MemoryLayer, MemoryNode

        gs, ephemeral = await self._acquire_graph_store_for_purge()
        if gs is None:
            return None

        aid = agent_id or "default_agent"
        ik = MemoryNode.build_isolation_key(user_id, aid)

        try:
            graph_nodes = []
            for layer in (MemoryLayer.L6_SCHEMA, MemoryLayer.L7_INTENTION):
                try:
                    chunk = await gs.get_all_nodes(ik, layer=layer, limit=10_000)
                    graph_nodes.extend(chunk)
                except Exception as e:
                    logger.warning(f"[list] graph list layer={layer.value} failed: {e}")

            graph_nodes = self._sort_memory_nodes(graph_nodes, order=order)
            total = len(graph_nodes)
            page_nodes = graph_nodes[offset: offset + limit]
            return {
                "nodes": [self._memory_node_to_list_item(n) for n in page_nodes],
                "total": total,
                "limit": limit,
                "offset": offset,
                "isolation_key": ik,
            }
        finally:
            if ephemeral:
                await gs.close()

    @_ensure_internal_loop
    async def async_list_memories(
        self,
        *,
        user_id: str = "",
        agent_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str = "desc",
        include_raw: bool = True,
    ) -> dict[str, Any]:
        """
        列出用户的记忆（异步）。

        VDB 与图库分桶返回（图库仅在存在 Kuzu/Neo4j 数据时包含 graph 键）。

        Args:
            include_raw: 是否包含 L1_RAW（未提取的原始记忆）。默认 True。
                设为 False 可获得与之前相同的「仅已提取」视图。

        Returns:
            {
                "vdb": {"memories": [...], "total", "limit", "offset"},
                "graph": {"nodes": [...], "total", ...} | 省略,
                "elapsed_ms": ...,
            }
        """
        t0 = time.perf_counter()

        vdb = await self._list_vdb_bucket(
            user_id=user_id, agent_id=agent_id,
            limit=limit, offset=offset, order=order,
            include_raw=include_raw,
        )
        graph = await self._list_graph_bucket(
            user_id=user_id, agent_id=agent_id,
            limit=limit, offset=offset, order=order,
        )

        elapsed = round((time.perf_counter() - t0) * 1000, 2)
        logger.info(
            f"[list] user={user_id} agent={agent_id} "
            f"vdb_total={vdb['total']} graph_total={graph['total'] if graph else 'n/a'} "
            f"elapsed={elapsed}ms"
        )

        out: dict[str, Any] = {"vdb": vdb, "elapsed_ms": elapsed}
        if graph is not None:
            out["graph"] = graph
        return out

    # ================================================================
    # Entity Store 构建 / 迁移 API
    # ================================================================

    def build_entity_store(
        self,
        user_id: str,
        *,
        agent_id: str = "default_agent",
        rebuild: bool = False,
    ) -> dict[str, Any]:
        """为指定 user 的全部 L2_FACT（含旧数据）抽取 entity 刷入 entity store（同步）。

        用于给历史 collection 补建 entity store，使 reader=mem0 的 entity boost 生效。
        与 mode 无关（pro 也可调）。

        Args:
            user_id:  用户 ID
            agent_id: Agent ID（默认 default_agent）
            rebuild:  True 时先清空该 user 现有 entity 再重建（默认 False，增量合并）

        Returns:
            {"success", "memories_scanned", "entities_indexed", "elapsed_ms"}
        """
        return self._loop_thread.run(
            self.async_build_entity_store(
                user_id=user_id, agent_id=agent_id, rebuild=rebuild,
            )
        )

    @_ensure_internal_loop
    async def async_build_entity_store(
        self,
        user_id: str,
        *,
        agent_id: str = "default_agent",
        rebuild: bool = False,
    ) -> dict[str, Any]:
        """build_entity_store 的异步实现。"""
        from .models.memory import MemoryLayer, MemoryStatus
        from .pipelines._retrieval.entity_store import index_memory_entities

        t0 = time.perf_counter()
        vs = self._vector_store

        # 扫描该 user 的全部 ACTIVE L2_FACT
        try:
            nodes = await vs.list_by_user(
                user_id=user_id,
                agent_id=agent_id,
                limit=100000,
                status_filter=[MemoryStatus.ACTIVE],
                layers=[MemoryLayer.L2_FACT],
            )
        except Exception as e:
            logger.error(f"[build_entity_store] list_by_user failed: {e}")
            return {"success": False, "error": str(e), "memories_scanned": 0, "entities_indexed": 0}

        # rebuild：先剔除这些 memory 在 entity store 里的关联（best-effort）
        if rebuild:
            for n in nodes:
                try:
                    await vs.delete_entities_for_memory(
                        memory_id=n.node_id, user_id=user_id, agent_ids=[agent_id] if agent_id else None,
                    )
                except NotImplementedError:
                    return {
                        "success": False,
                        "error": "entity store not supported by this vector store backend",
                        "memories_scanned": 0, "entities_indexed": 0,
                    }
                except Exception as e:
                    logger.debug(f"[build_entity_store] rebuild cleanup failed for {n.node_id}: {e}")

        indexed = 0
        for n in nodes:
            cnt = await index_memory_entities(
                vector_store=vs,
                embed_service=self._embed_service,
                memory_id=n.node_id,
                content=n.content or "",
                user_id=user_id,
                agent_id=agent_id or "default_agent",
            )
            indexed += cnt

        elapsed = round((time.perf_counter() - t0) * 1000, 2)
        logger.info(
            f"[build_entity_store] user={user_id} agent={agent_id} "
            f"scanned={len(nodes)} entities_indexed={indexed} rebuild={rebuild} elapsed={elapsed}ms"
        )
        return {
            "success": True,
            "memories_scanned": len(nodes),
            "entities_indexed": indexed,
            "elapsed_ms": elapsed,
        }

    # ================================================================
    # Clone User API（异步）
    # ================================================================

    @_ensure_internal_loop
    async def async_clone_user(
        self,
        src_user_id: str,
        dst_user_id: str,
        *,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """
        克隆用户记忆（异步）。

        深拷贝 src 的全部记忆到 dst，embedding 直接复制不重新计算。
        dst 必须为空，否则返回错误。操作同步完成，失败时回滚已写入的数据。

        Returns:
            {"success": True, "request_id": "...", "cloned_count": N, "elapsed_ms": ...}
        """
        request_id = str(uuid.uuid4())
        set_request_id(request_id)
        t0 = time.perf_counter()

        if not src_user_id or not dst_user_id:
            return {
                "success": False,
                "request_id": request_id,
                "cloned_count": 0,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                "error_message": "src_user_id and dst_user_id are required",
            }

        if src_user_id == dst_user_id:
            return {
                "success": False,
                "request_id": request_id,
                "cloned_count": 0,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                "error_message": "src_user_id and dst_user_id must be different",
            }

        logger.info(
            f"[clone] src={src_user_id} → dst={dst_user_id} agent_id={agent_id}"
        )

        try:
            # 1. 检查 dst 是否为空
            dst_nodes = await self._vector_store.list_by_user(
                dst_user_id, agent_id=agent_id, limit=1,
            )
            if dst_nodes:
                return {
                    "success": False,
                    "request_id": request_id,
                    "cloned_count": 0,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                    "error_message": (
                        f"dst_user_id '{dst_user_id}' already has memories. "
                        "Clone requires an empty destination."
                    ),
                }

            # 2. 列出 src 的全量记忆（含 embedding）
            src_nodes = await self._vector_store.list_by_user(
                src_user_id, agent_id=agent_id,
            )
            if not src_nodes:
                return {
                    "success": True,
                    "request_id": request_id,
                    "cloned_count": 0,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                    "error_message": None,
                }

            logger.info(f"[clone] found {len(src_nodes)} nodes to clone")

            # 3. 深拷贝：第一遍——生成新 node_id，建完整映射表
            from .models.memory import MemoryNode
            id_map: dict[str, str] = {}  # old_node_id → new_node_id

            for src_node in src_nodes:
                id_map[src_node.node_id] = str(uuid.uuid4())

            # 第二遍——用完整的 id_map 做深拷贝 + 指针重映射
            new_nodes: list = []
            for src_node in src_nodes:
                new_id = id_map[src_node.node_id]

                # 使用 to_dict + from_dict 做深拷贝
                node_dict = src_node.to_dict()
                node_dict["node_id"] = new_id
                node_dict["user_id"] = dst_user_id
                # 重置检索元数据
                node_dict["access_count"] = 0
                node_dict["last_accessed_at"] = None

                # 映射演化链指针（supersedes / superseded_by）
                # 指向映射表外的 ID（不在本次 clone 范围内）则清空
                if node_dict.get("supersedes"):
                    node_dict["supersedes"] = [
                        id_map[sid] for sid in node_dict["supersedes"]
                        if sid in id_map
                    ]
                if node_dict.get("superseded_by"):
                    node_dict["superseded_by"] = [
                        id_map[sid] for sid in node_dict["superseded_by"]
                        if sid in id_map
                    ]
                # 映射 evidence_chain 中的 ID
                if node_dict.get("evidence_chain"):
                    node_dict["evidence_chain"] = [
                        id_map[eid] for eid in node_dict["evidence_chain"]
                        if eid in id_map
                    ]
                # 映射 source_raw_memory_id
                if node_dict.get("source_raw_memory_id"):
                    node_dict["source_raw_memory_id"] = id_map.get(
                        node_dict["source_raw_memory_id"],
                        node_dict["source_raw_memory_id"],
                    )

                new_node = MemoryNode.from_dict(node_dict)
                # 保留原始 embedding（content 不变，无需重新计算）
                new_node.embedding = src_node.embedding
                # 保留原始时间（不要被 __post_init__ 覆盖）
                new_node.memory_at = src_node.memory_at
                new_node.gmt_created = src_node.gmt_created
                new_node.valid_from = src_node.valid_from
                new_nodes.append(new_node)

            # 5. 批量写入向量库
            written_ids = await self._vector_store.upsert_batch(new_nodes)
            logger.info(f"[clone] upsert_batch: {len(written_ids)} nodes written")

            # 6. 图数据库克隆（如启用）
            # 正确流程：从源 Graph 读节点 + 边，用 id_map 重映射后写入目标 Graph
            if self._graph_store is not None:
                await self._clone_graph(
                    src_user_id=src_user_id,
                    dst_user_id=dst_user_id,
                    agent_id=agent_id,
                    id_map=id_map,
                )

            # 7. 复制 memory_operations 记录（src → dst，用 id_map 重映射）
            if self._cache:
                try:
                    await self._clone_memory_operations(
                        src_user_id=src_user_id,
                        dst_user_id=dst_user_id,
                        clone_request_id=request_id,
                        id_map=id_map,
                    )
                except Exception as ops_err:
                    logger.warning(f"[clone] clone memory_operations failed (non-fatal): {ops_err}")

            logger.info(
                f"[clone] done: {len(written_ids)} nodes cloned "
                f"from {src_user_id} to {dst_user_id}"
            )

            return {
                "success": True,
                "request_id": request_id,
                "cloned_count": len(written_ids),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                "error_message": None,
            }

        except Exception as e:
            logger.error(f"[clone] failed: {e}", exc_info=True)
            # 回滚：删除已写入 dst 的数据
            try:
                await self._vector_store.delete_by_metadata(user_id=dst_user_id)
                await self._purge_graph_data(dst_user_id)
                logger.info(f"[clone] rollback: cleaned dst_user_id={dst_user_id}")
            except Exception as rollback_err:
                logger.warning(f"[clone] rollback failed: {rollback_err}")

            return {
                "success": False,
                "request_id": request_id,
                "cloned_count": 0,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                "error_message": str(e),
            }

    # ================================================================
    # Clone Graph Helper
    # ================================================================

    async def _clone_graph(
        self,
        src_user_id: str,
        dst_user_id: str,
        agent_id: str | None,
        id_map: dict[str, str],
    ) -> None:
        """
        从源用户的 Graph 深拷贝到目标用户。

        流程：
        1. 读源 Graph 的所有 Memory 节点（按 isolation_key）
        2. 用 id_map 重映射节点 ID + user_id + isolation_key，写入目标 Graph
        3. 复制 VdbRef 影子节点（ID 重映射）+ DERIVED_FROM 边
        4. 复制 Memory→Memory 关系边（RELATED_TO）
        5. 复制 TAGGED_WITH 边（Topic tag）
        """
        from .models.memory import MemoryNode
        gs = self._graph_store
        if gs is None:
            return

        effective_agent = agent_id or "default_agent"

        # 构建源/目标 isolation_key
        # 源用户可能有多个 session，用 get_all_nodes 按 user_id 前缀匹配
        # 但 get_all_nodes 需要精确 isolation_key，所以遍历 id_map 里的源节点找 isolation_key
        try:
            # Step 1: 读源 Graph 的所有 Memory 节点
            # 用 Cypher 按 user_id 查（不依赖 isolation_key，更通用）
            src_graph_nodes = []
            if hasattr(gs, '_execute') and hasattr(gs, '_available'):
                # Kuzu
                if not gs._available:
                    return
                result = gs._execute(
                    "MATCH (m:Memory) WHERE m.user_id = $uid RETURN m;",
                    {"uid": src_user_id},
                )
                while result is not None and result.has_next():
                    row = result.get_next()
                    src_graph_nodes.append(gs._row_to_memory_node(row[0]))
            elif hasattr(gs, '_run'):
                # Neo4j — 同时取 embedding / beh_embedding 以便 clone 时一并复制
                rows = await gs._run(
                    "MATCH (m:Memory) WHERE m.user_id = $uid "
                    "RETURN m, m.embedding AS _emb, m.beh_embedding AS _beh_emb",
                    {"uid": src_user_id},
                )
                for r in rows:
                    node = gs._record_to_memory_node(r["m"])
                    # 把 embedding 暂存到 node 上，供后续 upsert 使用
                    if r.get("_emb") is not None:
                        node._graph_embedding = list(r["_emb"])
                    if r.get("_beh_emb") is not None:
                        node._graph_beh_embedding = list(r["_beh_emb"])
                    src_graph_nodes.append(node)

            if not src_graph_nodes:
                logger.info("[clone] no Graph nodes to clone")
                return

            logger.info(f"[clone] Graph: found {len(src_graph_nodes)} source nodes")

            # 记住每个源节点的 old_id，确保 id_map 覆盖
            graph_old_ids = []  # 与 src_graph_nodes 一一对应
            for src_node in src_graph_nodes:
                old_nid = src_node.node_id
                graph_old_ids.append(old_nid)
                if old_nid not in id_map:
                    id_map[old_nid] = str(uuid.uuid4())

            # Step 2: 写入目标 Graph Memory 节点（深拷贝 + ID 重映射）
            from .models.memory import MemoryNode as _MN
            for i, src_node in enumerate(src_graph_nodes):
                old_nid = graph_old_ids[i]
                new_id = id_map[old_nid]
                # 深拷贝，不修改源 node
                d = src_node.to_dict()
                d["node_id"] = new_id
                d["user_id"] = dst_user_id
                dst_node = _MN.from_dict(d)
                # 携带源节点的 embedding（to_dict 不含 embedding，需手动传递）
                src_emb = getattr(src_node, '_graph_embedding', None)
                src_beh = getattr(src_node, '_graph_beh_embedding', None)
                if src_emb:
                    dst_node._graph_embedding = src_emb
                try:
                    await gs.upsert_memory_node(dst_node)
                    # upsert 后再补 beh_embedding（upsert 只写 embedding，不写 beh）
                    if src_beh:
                        await gs.update_embedding(new_id, beh_embedding=src_beh)
                except Exception as e:
                    logger.warning(f"[clone] Graph node upsert failed {new_id}: {e}")

            # Step 3: 复制 VdbRef + DERIVED_FROM 边
            for i, old_nid in enumerate(graph_old_ids):
                new_id = id_map[old_nid]
                try:
                    evidence = await gs.get_evidence_vdbrefs(old_nid)
                except Exception:
                    evidence = []

                for ev in evidence:
                    old_vdbref_id = ev["node_id"]
                    new_vdbref_id = id_map.get(old_vdbref_id, old_vdbref_id)
                    try:
                        await gs.ensure_vdbref(new_vdbref_id, ev.get("layer", ""))
                        await gs.add_derived_from(new_id, new_vdbref_id)
                    except Exception as e:
                        logger.debug(f"[clone] DERIVED_FROM edge failed: {e}")

            # Step 4: 复制 Memory→Memory 关系边
            edge_types = ["RELATED_TO", "CROSS_ABSTRACTS_TO"]

            for i, old_nid in enumerate(graph_old_ids):
                try:
                    if hasattr(gs, '_execute'):
                        result = gs._execute(
                            """
                            MATCH (a:Memory {node_id: $nid})-[r]->(b:Memory)
                            RETURN b.node_id, label(r) AS edge_type;
                            """,
                            {"nid": old_nid},
                        )
                        edges = []
                        while result is not None and result.has_next():
                            row = result.get_next()
                            edges.append({"target": row[0], "edge_type": row[1]})
                    elif hasattr(gs, '_run'):
                        rows = await gs._run(
                            """
                            MATCH (a:Memory {node_id: $nid})-[r]->(b:Memory)
                            RETURN b.node_id AS target, type(r) AS edge_type
                            """,
                            {"nid": old_nid},
                        )
                        edges = [{"target": r["target"], "edge_type": r["edge_type"]} for r in rows]
                    else:
                        edges = []
                except Exception:
                    edges = []

                for edge in edges:
                    et = edge["edge_type"]
                    if et not in edge_types:
                        continue
                    new_src = id_map.get(old_nid)
                    new_tgt = id_map.get(edge["target"])
                    if new_src and new_tgt:
                        try:
                            await gs.add_edge(new_src, new_tgt, et, {})
                        except Exception as e:
                            logger.debug(f"[clone] edge {et} failed: {e}")

            # Step 5: 复制 TAGGED_WITH 边
            for i, old_nid in enumerate(graph_old_ids):
                new_id = id_map[old_nid]
                src_node = src_graph_nodes[i]
                dst_ik = MemoryNode.build_isolation_key(
                    dst_user_id,
                    src_node.agent_id or "default_agent",
                    src_node.session_id or "default_session",
                )

                try:
                    if hasattr(gs, '_execute'):
                        result = gs._execute(
                            "MATCH (m:Memory {node_id: $nid})-[:TAGGED_WITH]->(t:Topic) RETURN t.name;",
                            {"nid": old_nid},
                        )
                        tag_rows = []
                        while result is not None and result.has_next():
                            tag_rows.append({"name": result.get_next()[0], "embedding": None})
                    elif hasattr(gs, '_run'):
                        tag_rows = await gs._run(
                            "MATCH (m:Memory {node_id: $nid})-[:TAGGED_WITH]->(t:Topic) "
                            "RETURN t.name AS name, t.embedding AS embedding",
                            {"nid": old_nid},
                        )
                    else:
                        tag_rows = []
                except Exception:
                    tag_rows = []

                for tr in tag_rows:
                    tag_name = tr["name"]
                    tag_emb = tr.get("embedding")
                    try:
                        await gs.add_topic_tag(new_id, tag_name, dst_ik)
                        # 把源 Topic 的 embedding 写到目标 Topic（add_topic_tag 按 name
                        # 精确匹配时不设 embedding，需要补写）
                        if tag_emb is not None and hasattr(gs, '_run_write'):
                            await gs._run_write(
                                "MATCH (t:Topic {isolation_key: $ik, name: $name}) "
                                "WHERE t.embedding IS NULL "
                                "SET t.embedding = $emb",
                                {"ik": dst_ik, "name": tag_name, "emb": list(tag_emb)},
                            )
                    except Exception as e:
                        logger.debug(f"[clone] TAGGED_WITH failed: {e}")

            logger.info(f"[clone] Graph: cloned {len(src_graph_nodes)} nodes with edges")

        except Exception as e:
            logger.warning(f"[clone] Graph clone failed (non-fatal): {e}")

    async def _clone_memory_operations(
        self,
        src_user_id: str,
        dst_user_id: str,
        clone_request_id: str,
        id_map: dict[str, str],
    ) -> None:
        """
        复制源用户的 memory_operations 到目标用户。

        - user_id 替换为 dst_user_id
        - memory_id / old_memory_id 按 id_map 重映射（找不到映射的保留原值）
        - supersedes 列表中的 ID 也做重映射
        - request_id 保留源的（保持 per_digest 分组语义）
        - 额外追加一条 CLONE op 记录（标记 clone 来源）
        """
        import json as _json

        # 1. 查出 src 的所有 ops
        src_ops = await self._cache.get_memory_operations(user_id=src_user_id, limit=50000)
        if not src_ops:
            logger.info("[clone] no memory_operations to clone")
            return

        # 2. 逐条写入 dst（ID 重映射）
        cloned_count = 0
        for op in src_ops:
            # 重映射 memory_id
            src_mem_id = op.get("memory_id", "")
            dst_mem_id = id_map.get(src_mem_id, src_mem_id)

            # 重映射 old_memory_id
            src_old_id = op.get("old_memory_id") or ""
            dst_old_id = id_map.get(src_old_id, src_old_id) if src_old_id else None

            # 重映射 supersedes 列表
            raw_supersedes = op.get("supersedes", [])
            if isinstance(raw_supersedes, str):
                try:
                    raw_supersedes = _json.loads(raw_supersedes)
                except Exception:
                    raw_supersedes = []
            dst_supersedes = [id_map.get(sid, sid) for sid in (raw_supersedes or [])]

            await self._cache.store_memory_operation(
                request_id=op.get("request_id", clone_request_id),
                user_id=dst_user_id,
                agent_id=op.get("agent_id", ""),
                op=op.get("op", "ADD"),
                memory_id=dst_mem_id,
                content=op.get("content", ""),
                layer=op.get("layer", ""),
                old_memory_id=dst_old_id,
                reason=op.get("reason", ""),
                supersedes=dst_supersedes,
            )
            cloned_count += 1

        # 3. 追加一条 CLONE 元记录（标记 clone 来源，方便回溯）
        await self._cache.store_memory_operation(
            request_id=clone_request_id,
            user_id=dst_user_id,
            agent_id="",
            op="CLONE",
            memory_id="",
            content=_json.dumps({
                "src_user_id": src_user_id,
                "dst_user_id": dst_user_id,
                "ops_cloned": cloned_count,
            }, ensure_ascii=False),
            layer="",
            reason=f"cloned from {src_user_id}",
        )

        logger.info(f"[clone] memory_operations: cloned {cloned_count} ops from {src_user_id} → {dst_user_id}")

    # ================================================================
    # History API（异步）
    # ================================================================

    @_ensure_internal_loop
    async def async_history(self, memory_id: str) -> list[dict[str, Any]]:
        """查询某条记忆的变更历史（异步）"""
        if self._history_store is None:
            return []
        return await self._history_store.get_history(memory_id)

    @_ensure_internal_loop
    async def async_get_recent_history(
        self,
        user_id: str = "",
        agent_ids: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        查询最近的操作历史（异步）。

        agent_ids 为空时，查询该用户所有 agent 的历史。
        """
        if self._history_store is None:
            return []

        uid = user_id
        effective_agent_ids = agent_ids if agent_ids is not None else []

        if not effective_agent_ids:
            # 查所有 agent 的历史：用 "user_id:" 前缀匹配
            isolation_key = self._build_isolation_key(uid, "default")
            return await self._history_store.get_recent(isolation_key, limit=limit)
        else:
            # 查指定 agent 的历史
            all_results = []
            for aid in effective_agent_ids:
                isolation_key = self._build_isolation_key(uid, aid)
                results = await self._history_store.get_recent(isolation_key, limit=limit)
                all_results.extend(results)
            # 按时间倒序排列，取 top limit
            all_results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return all_results[:limit]

    # ================================================================
    # Context manager
    # ================================================================

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._close_async()
