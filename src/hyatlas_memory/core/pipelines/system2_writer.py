"""
HY Memory - System 2 Writer (ultra 模式)

写入流程:
  System 1 (同步): 复用 MemoryWriter 的 write 逻辑（含意图抽取 → L7_INTENTION，
                   见 extractor + writer._store_intentions；意图属于 S1/pro，不是 S2）。
                   write 只跑 S1 即返回。
  System 2 (显式): 由 System 2 Agent（LLM + 8 tools）对 VDB 全量做认知加工，
                   归纳心智模型 → L6_SCHEMA，再跑 cross-domain sweeper。
                   Agent 是 Graph 的唯一写入者。

触发时机由调用方（App 层）通过 digest() 显式控制：
  - write 不入队、不自动触发；SDK 不维护进程内 S2 队列（重启丢任务、多 Pod
    各自一套队列、与 App 持久化 write 队列双轨等问题由此消除）。
  - digest() 一次 = 对该 user 的 VDB 全量跑一次 S2 + sweeper（同步阻塞、非幂等）。
"""

import os
from typing import Optional, Dict, Any
from datetime import datetime
import logging

from .base import WritePipeline, WriteRequest, WriteResponse, PipelineContext
from .writer import MemoryWriter
from ..config import MemoryConfig
from ..core.embed_service import EmbedService
from ..data.vector_store_base import VectorStoreBase
from ..data.graph_store_base import GraphStoreBase
from ..utils.tracer import PipelineTracer, create_tracer

import time
logger = logging.getLogger(__name__)

_SWEEPER_ENABLED = os.getenv("SWEEPER_ENABLED", "true").lower() == "true"


# ================================================================
# System 2 Trigger Mode
# ================================================================

# ================================================================
# Pro Write Pipeline
# ================================================================

class System2Writer(WritePipeline):
    """
    System 2 写入器 (ultra 模式)

    特点:
    - System 1 同步路径完全复用 MemoryWriter；write 只跑 S1 即返回。
    - System 2 不在 write 里自动触发、不维护进程内队列。触发时机完全由调用方
      （App 层）通过显式调用 digest() 控制 —— 避免 SDK 内存队列重启丢任务、
      多 Pod 各自一套队列、与 App 持久化 write 队列双轨等问题。
    - digest() 一次 = 对该 user 的 VDB 全量跑一次 S2 Agent + sweeper（非幂等）。
    """

    VERSION = "system2"

    def __init__(
        self,
        config: MemoryConfig,
        embed_service: Optional[EmbedService] = None,
        vector_store: Optional[VectorStoreBase] = None,
        graph_store: Optional[GraphStoreBase] = None,
        cache=None,
    ):
        self.config = config
        self._embed_service = embed_service
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._cache = cache

        # System 1: 复用 MemoryWriter
        self._writer: Optional[MemoryWriter] = None

        self._initialized = False

    async def initialize(self) -> None:
        """初始化 Pro Pipeline"""
        if self._initialized:
            return

        # 初始化 MemoryWriter (System 1)
        self._writer = MemoryWriter(
            config=self.config,
            embed_service=self._embed_service,
            vector_store=self._vector_store,
            cache=self._cache,
        )
        await self._writer.initialize()

        self._initialized = True
        logger.info("System2Writer initialized (S2 triggered explicitly via digest)")

    async def write(
        self,
        request: WriteRequest,
        ctx: Optional[PipelineContext] = None,
        tracer: Optional[PipelineTracer] = None,
    ) -> WriteResponse:
        """
        执行 Pro 写入流程:
        1. System 1 同步写入 (复用 Lite)，写完即返回。
        2. System 2 不在此触发 —— 由调用方显式调用 digest()。
        """
        start_time = datetime.now()

        # 创建 tracer
        if tracer is None:
            tracer = create_tracer(
                operation="system2_write",
                pipeline_version="system2",
                uid=request.user_id,
                agent_id=request.agent_id,
                request_id=request.request_id,
                content_preview=request.content,
            )

        # 确保已初始化
        if not self._initialized:
            await self.initialize()

        # ============================================
        # System 1: 复用 Lite 的完整写入流程
        # ============================================
        with tracer.span("system1_write") as s:
            response = await self._writer.write(request, ctx=ctx)
            s.set_output({
                "success": response.success,
                "memory_id": response.memory_id,
                "layer": response.layer,
            })

        if not response.success:
            response.elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
            return response

        # System 2 由调用方通过 digest() 显式触发，write 不入队、不自动跑。
        response.extra["pipeline_version"] = "system2"
        response.extra["system2_task_ids"] = []

        response.elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
        return response

    # ================================================================
    # System 2 认知加工
    # ================================================================

    async def _run_system2_workers(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        System 2 认知加工 — System 2 Agent + Graph

        全面使用 Agent（LLM + 8 tools）替代旧的独立 Workers。
        Agent 是 Graph 的唯一写入者。
        """
        return await self._run_system2_agent(payload)

    async def _run_system2_agent_with_timing(self, payload: Dict[str, Any]) -> Dict[str, float]:
        """
        包裹 _run_system2_agent，返回细分 timing:
          - preprocess_ms
          - agent_generate_ms（纯 LLM 时间，= agent elapsed - tools 总耗时）
          - tools_avg_ms（tool 平均执行耗时）
        """
        from .system2_agent import prepare_materials, run_system2_agent, s2_agent_skip_reason
        from .system2_tools import System2ToolExecutor

        user_id = payload["user_id"]
        agent_id = payload["agent_id"]
        request_id = payload.get("request_id", "")

        vector_store = await self._writer._get_vector_store()
        embed_service = self._writer.embed_service

        # Phase 1: preprocess
        _t = time.time()
        materials = await prepare_materials(
            user_id=user_id,
            agent_id=agent_id,
            vector_store=vector_store,
            graph_store=self._graph_store,
            embed_service=embed_service,
            config=self.config,
        )
        preprocess_ms = (time.time() - _t) * 1000

        # Log preprocessing
        await self._store_system2_log(
            request_id=request_id,
            user_id=user_id,
            agent_id=agent_id,
            step="SYSTEM2_PREPROCESS",
            result={
                "success": True,
                **materials.get("stats", {}),
                "clusters": materials.get("clusters", []),
                "graph_forward": materials.get("graph_forward", []),
                "graph_reverse": materials.get("graph_reverse", []),
                "unprocessed_facts": materials.get("unprocessed_facts", []),
            },
        )

        skip_reason = s2_agent_skip_reason(materials)
        if skip_reason:
            stats = materials.get("stats", {})
            logger.info(
                f"[S2-agent] skipping agent: {skip_reason} "
                f"(pool={stats.get('total_facts_pool', 0)} "
                f"clusters={stats.get('clusters_found', 0)} "
                f"fresh={stats.get('fresh_facts', 0)})"
            )
            return {"preprocess_ms": preprocess_ms, "agent_generate_ms": 0, "tools_avg_ms": 0}

        # Phase 2: Agent
        tool_executor = System2ToolExecutor(
            vector_store=vector_store,
            graph_store=self._graph_store,
            embed_service=embed_service,
            user_id=user_id,
            agent_id=agent_id,
        )

        agent_result = await run_system2_agent(
            materials=materials,
            tool_executor=tool_executor,
            config=self.config,
        )

        # 计算 timing
        agent_elapsed_ms = agent_result.get("elapsed_ms", 0)
        tool_call_log = agent_result.get("tool_call_log", [])
        tools_total_ms = sum(t.get("elapsed_ms", 0) for t in tool_call_log)
        tools_count = len(tool_call_log)
        agent_generate_ms = max(0, agent_elapsed_ms - tools_total_ms)
        tools_avg_ms = tools_total_ms / tools_count if tools_count > 0 else 0

        # Log agent result
        await self._store_system2_log(
            request_id=request_id,
            user_id=user_id,
            agent_id=agent_id,
            step="SYSTEM2_AGENT",
            result={
                "success": agent_result.get("success", False),
                "tool_calls_count": len(agent_result.get("tool_calls", [])),
                "iterations": agent_result.get("iterations", 0),
                "total_tokens": agent_result.get("total_tokens", 0),
                "elapsed_ms": agent_elapsed_ms,
                "agent_generate_ms": round(agent_generate_ms, 1),
                "tools_avg_ms": round(tools_avg_ms, 1),
            },
        )

        # Step 2: SYSTEM2_AGENT_TRAJECTORY — 完整 tool_calls
        import json as _json_s2_t
        tool_calls = agent_result.get("tool_calls", [])
        if self._cache and tool_calls:
            try:
                await self._cache.store_pipeline_log(
                    request_id=request_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    step="SYSTEM2_AGENT_TRAJECTORY",
                    prompt="",
                    response=_json_s2_t.dumps({
                        "tool_calls": tool_calls,
                        "agent_reasoning": agent_result.get("agent_reasoning", ""),
                    }, ensure_ascii=False, default=str),
                    parsed=_json_s2_t.dumps({
                        "tool_calls": tool_calls,
                        "tool_calls_count": len(tool_calls),
                        "iterations": agent_result.get("iterations", 0),
                        "has_reasoning": bool(agent_result.get("agent_reasoning")),
                        "total_tokens": agent_result.get("total_tokens", 0),
                    }, ensure_ascii=False, default=str),
                )
            except Exception as e:
                logger.debug(f"[S2] store SYSTEM2_AGENT_TRAJECTORY failed: {e}")

        # Step 3: SYSTEM2_AGENT_MESSAGES — 完整 LLM 对话历史
        messages = agent_result.get("messages")
        if messages and self._cache:
            try:
                clean_messages = []
                for m in messages:
                    clean_msg = {"role": m.get("role", "")}
                    if m.get("content"):
                        clean_msg["content"] = m["content"]
                    if m.get("tool_calls"):
                        clean_msg["tool_calls"] = m["tool_calls"]
                    if m.get("tool_call_id"):
                        clean_msg["tool_call_id"] = m["tool_call_id"]
                    clean_messages.append(clean_msg)

                await self._cache.store_pipeline_log(
                    request_id=request_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    step="SYSTEM2_AGENT_MESSAGES",
                    prompt=clean_messages[1]["content"] if len(clean_messages) > 1 else "",
                    response=_json_s2_t.dumps(clean_messages, ensure_ascii=False, default=str),
                    parsed=_json_s2_t.dumps({
                        "message_count": len(clean_messages),
                        "roles": [m["role"] for m in clean_messages],
                    }, ensure_ascii=False, default=str),
                )
            except Exception as e:
                logger.debug(f"[S2] store SYSTEM2_AGENT_MESSAGES failed: {e}")

        return {
            "preprocess_ms": preprocess_ms,
            "agent_generate_ms": agent_generate_ms,
            "tools_avg_ms": tools_avg_ms,
        }

    async def _run_system2_agent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        System 2 Agent — 预处理 + LLM Agent + Graph tools
        """
        from .system2_agent import prepare_materials, run_system2_agent, s2_agent_skip_reason
        from .system2_tools import System2ToolExecutor

        user_id = payload["user_id"]
        agent_id = payload["agent_id"]
        request_id = payload.get("request_id", "")

        vector_store = await self._writer._get_vector_store()
        embed_service = self._writer.embed_service

        # Phase 1: 预处理
        materials = await prepare_materials(
            user_id=user_id,
            agent_id=agent_id,
            vector_store=vector_store,
            graph_store=self._graph_store,
            embed_service=embed_service,
            config=self.config,
        )

        # Log preprocessing
        await self._store_system2_log(
            request_id=request_id,
            user_id=user_id,
            agent_id=agent_id,
            step="SYSTEM2_PREPROCESS",
            result={
                "success": True,
                **materials.get("stats", {}),
                "clusters": materials.get("clusters", []),
                "graph_forward": materials.get("graph_forward", []),
                "graph_reverse": materials.get("graph_reverse", []),
                "unprocessed_facts": materials.get("unprocessed_facts", []),
            },
        )

        skip_reason = s2_agent_skip_reason(materials)
        if skip_reason:
            stats = materials.get("stats", {})
            logger.info(
                f"[S2-agent] skipping agent: {skip_reason} "
                f"(pool={stats.get('total_facts_pool', 0)} "
                f"clusters={stats.get('clusters_found', 0)})"
            )
            return {
                "system2_agent": {
                    "success": True,
                    "skipped": True,
                    "reason": skip_reason,
                }
            }

        # Phase 2: Agent
        tool_executor = System2ToolExecutor(
            vector_store=vector_store,
            graph_store=self._graph_store,
            embed_service=embed_service,
            user_id=user_id,
            agent_id=agent_id,
        )

        agent_result = await run_system2_agent(
            materials=materials,
            tool_executor=tool_executor,
            config=self.config,
        )

        # Log agent result — 完整 trajectory
        import json as _json_s2

        # Step 1: SYSTEM2_AGENT 主日志（聚合统计）
        await self._store_system2_log(
            request_id=request_id,
            user_id=user_id,
            agent_id=agent_id,
            step="SYSTEM2_AGENT",
            result={
                "success": agent_result.get("success", False),
                "tool_calls_count": len(agent_result.get("tool_calls", [])),
                "iterations": agent_result.get("iterations", 0),
                "total_tokens": agent_result.get("total_tokens", 0),
                "prompt_tokens": agent_result.get("total_prompt_tokens", 0),
                "completion_tokens": agent_result.get("total_completion_tokens", 0),
                "elapsed_ms": agent_result.get("elapsed_ms", 0),
            },
        )

        # Step 2: SYSTEM2_AGENT_TRAJECTORY — 完整 trajectory（tool_calls + reasoning）
        # 无条件存储：即使没有 tool calls，Agent 的推理过程本身就是 trajectory
        tool_calls = agent_result.get("tool_calls", [])
        if self._cache:
            try:
                await self._cache.store_pipeline_log(
                    request_id=request_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    step="SYSTEM2_AGENT_TRAJECTORY",
                    prompt="",
                    response=_json_s2.dumps({
                        "tool_calls": tool_calls,
                        "agent_reasoning": agent_result.get("agent_reasoning", ""),
                    }, ensure_ascii=False, default=str),
                    parsed=_json_s2.dumps({
                        "tool_calls": tool_calls,
                        "tool_calls_count": len(tool_calls),
                        "iterations": agent_result.get("iterations", 0),
                        "has_reasoning": bool(agent_result.get("agent_reasoning")),
                        "total_tokens": agent_result.get("total_tokens", 0),
                    }, ensure_ascii=False, default=str),
                )
            except Exception as e:
                logger.debug(f"[S2] store SYSTEM2_AGENT_TRAJECTORY failed: {e}")

        # Step 3: SYSTEM2_AGENT_MESSAGES — 完整 LLM 对话历史（可重放）
        messages = agent_result.get("messages")
        if messages and self._cache:
            try:
                # 过滤掉 embedding 等大字段，只保留文本
                clean_messages = []
                for m in messages:
                    clean_msg = {"role": m.get("role", "")}
                    if m.get("content"):
                        clean_msg["content"] = m["content"]
                    if m.get("tool_calls"):
                        clean_msg["tool_calls"] = m["tool_calls"]
                    if m.get("tool_call_id"):
                        clean_msg["tool_call_id"] = m["tool_call_id"]
                    clean_messages.append(clean_msg)

                await self._cache.store_pipeline_log(
                    request_id=request_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    step="SYSTEM2_AGENT_MESSAGES",
                    prompt=clean_messages[1]["content"] if len(clean_messages) > 1 else "",  # user msg (materials)
                    response=_json_s2.dumps(clean_messages, ensure_ascii=False, default=str),
                    parsed=_json_s2.dumps({
                        "message_count": len(clean_messages),
                        "roles": [m["role"] for m in clean_messages],
                    }, ensure_ascii=False, default=str),
                )
            except Exception as e:
                logger.debug(f"[S2] store SYSTEM2_AGENT_MESSAGES failed: {e}")

        # Step 4: Graph 写入操作记 memory_operations（可追溯）
        tool_call_log = agent_result.get("tool_call_log", [])
        if tool_call_log and self._cache:
            for tc in tool_call_log:
                tool_name = tc.get("tool", "")
                if tool_name in ("create_graph_node", "update_graph_node", "delete_graph_node", "add_edge"):
                    try:
                        result_preview = tc.get("result_preview", "")
                        # 尝试提取 node_id
                        try:
                            result_data = _json_s2.loads(result_preview) if result_preview else {}
                        except Exception:
                            result_data = {}
                        memory_id = result_data.get("node_id", "")

                        op_name = {
                            "create_graph_node": "GRAPH_CREATE",
                            "update_graph_node": "GRAPH_UPDATE",
                            "delete_graph_node": "GRAPH_DELETE",
                            "add_edge": "GRAPH_ADD_EDGE",
                        }.get(tool_name, "GRAPH_OP")

                        await self._cache.store_memory_operation(
                            request_id=request_id,
                            user_id=user_id,
                            agent_id=agent_id,
                            op=op_name,
                            memory_id=memory_id,
                            content=_json_s2.dumps(tc.get("args", {}), ensure_ascii=False, default=str)[:500],
                            layer="graph",
                            reason=f"System 2 Agent tool: {tool_name}",
                        )
                    except Exception as e:
                        logger.debug(f"[S2] store graph memory_operation failed: {e}")

        return {"system2_agent": agent_result}

    async def _run_cross_domain_sweeper(
        self,
        user_id: str,
        agent_id: str,
        llm_call,
        request_id: str = "",
    ) -> Dict[str, Any]:
        """在 System2 Agent 之后运行跨域突破性归纳"""
        if not _SWEEPER_ENABLED:
            return {"skipped": True, "reason": "SWEEPER_ENABLED=false"}

        try:
            from .cross_domain_sweeper import CrossDomainSweeper

            embed_service = self._writer.embed_service
            sweeper = CrossDomainSweeper(
                graph_store=self._graph_store,
                embed_service=embed_service,
                llm_call=llm_call,
                user_id=user_id,
                agent_id=agent_id,
                request_id=request_id,
                cache=self._cache,
            )
            result = await sweeper.sweep()
            logger.info(f"[S2] Cross-domain sweeper: {result}")
            return result
        except Exception as e:
            logger.warning(f"[S2] Cross-domain sweeper failed: {e}", exc_info=True)
            return {"error": str(e)}

    def _get_llm_call(self):
        """返回 sweeper 需要的 async LLM callable: async (prompt: str) -> str"""
        from ..agent.llm_provider import LLMProvider

        provider = LLMProvider(self.config)
        # 使用配置的 temperature（兼容 Kimi K2.5 等限制 temperature 的模型）
        temperature = self.config.llm.temperature if self.config.llm.temperature is not None else 0.3

        async def _call(prompt: str) -> str:
            resp = await provider._call_llm(prompt, max_tokens=1024, temperature=temperature)
            return resp.content

        return _call

    async def _store_system2_log(
        self,
        request_id: str,
        user_id: str,
        agent_id: str,
        step: str,
        result: Dict[str, Any],
    ) -> None:
        """Write a pipeline log entry for a System 2 worker execution."""
        if not self._cache or not request_id:
            return
        try:
            import json as _json
            await self._cache.store_pipeline_log(
                request_id=request_id,
                user_id=user_id,
                agent_id=agent_id,
                step=step,
                prompt=result.get("prompt", ""),
                response=result.get("llm_response", ""),
                parsed=_json.dumps(result, ensure_ascii=False, default=str),
                elapsed_ms=result.get("elapsed_ms", 0),
                prompt_tokens=result.get("prompt_tokens", 0),
                completion_tokens=result.get("completion_tokens", 0),
                total_tokens=result.get("total_tokens", 0),
            )
        except Exception as e:
            logger.debug(f"[S2] store {step} pipeline log failed: {e}")

    # ================================================================
    # 公开 API: digest (手动触发 System 2)
    # ================================================================

    async def run_sweeper(
        self,
        user_id: str,
        agent_id: str = "default_agent",
    ) -> Dict[str, Any]:
        """
        手动触发 cross-domain sweeper（同步等待完成）。

        独立于 digest 流程，不执行 S2 Agent，只做：
        1. 补齐 schema 的 beh abstraction + beh_embedding
        2. 碰撞扫描 + core 归纳

        Args:
            user_id: SDK user_id（已含 app_id 前缀）
            agent_id: Agent ID

        Returns:
            {"request_id": "...", "basics_count": N, "new_beh_embeddings": N,
             "collisions": N, "cores_created": N, "cores_merged": N,
             "created_memory_ids": [...]}
        """
        import uuid
        request_id = str(uuid.uuid4())
        logger.info(f"[S2] manual sweeper triggered: user={user_id} agent={agent_id} request_id={request_id}")

        result = await self._run_cross_domain_sweeper(
            user_id=user_id,
            agent_id=agent_id,
            llm_call=self._get_llm_call(),
            request_id=request_id,
        )
        result["request_id"] = request_id
        return result

    async def digest(
        self,
        user_id: str,
        agent_id: str = "default_agent",
    ) -> Dict[str, Any]:
        """
        手动触发 System 2 认知加工（由调用方/App 层控制触发时机）。

        语义：digest 一次 = 对该 user 的 VDB 全量跑一次 System 2（S2 Agent +
        cross-domain sweeper）。S2 的输入是 prepare_materials 全量扫描的 VDB
        （L2/L4 聚类 + 已有 Graph schema），与写入次数无关，只跑一次完整认知周期。

        非幂等：每次 digest 看到的是当前最新的 VDB 全量，连续多次 digest 有意义
        （例如先 add 一批 → digest → 再 add 一批 → 再 digest）。

        Args:
            user_id: 用户 ID
            agent_id: Agent ID

        Returns:
            {"success": True, "request_id": "...", "tasks_processed": 1, "results": {...}}
        """
        import uuid
        request_id = str(uuid.uuid4())
        logger.info(f"[S2] digest triggered: user={user_id} agent={agent_id} request_id={request_id}")

        payload = {
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": "default_session",
            "memory_id": "",
            "request_id": request_id,
            "agent_stored_ids": [],
            "content": "",
            "created_at": datetime.now().isoformat(),
        }
        results = await self._run_system2_workers(payload)

        # Cross-domain sweeper（在 System2 之后执行一次）
        sweeper_result = await self._run_cross_domain_sweeper(
            user_id=user_id,
            agent_id=agent_id,
            llm_call=self._get_llm_call(),
            request_id=request_id,
        )

        return {
            "success": True,
            "request_id": request_id,
            "tasks_processed": 1,
            "results": results,
            "cross_domain_sweeper": sweeper_result,
        }

    # ================================================================
    # Lifecycle
    # ================================================================

    async def close(self) -> None:
        """释放 Pro 写入资源"""
        if self._writer:
            await self._writer.close()

        logger.debug("System2Writer closed")
