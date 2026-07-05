"""
HY Memory - Pipeline Tracer (用户级调试日志)

简单粗暴的请求级 trace 工具，记录：
1. 每个环节的耗时 (ms)
2. 每个环节的关键中间结果 (截断到合理长度)
3. 整条请求的完整时间线

使用方式：
    tracer = PipelineTracer(
        operation="ProcessMemory",
        pipeline_version="system1",
        uid="user_123",
        agent_id="chat_scene",
    )
    
    with tracer.span("embed") as s:
        embedding = await embed_service.embed(text)
        s.set_output({"dims": len(embedding), "cache_hit": False})
    
    with tracer.span("qdrant_upsert") as s:
        memory_id = await vector_store.upsert(node)
        s.set_output({"memory_id": memory_id})
    
    tracer.finish()
    # → 自动写入日志文件 + Python logger

日志文件位置 (按 uid 分目录，按 uid+agent_id+日期 滚动):
    {MEMORY_TRACE_LOG_DIR}/{uid}/{uid}_{agent_id}_{date}.log

示例:
    /memory/logs/traces/user_123/user_123_chat_scene_2026-03-17.log

每行是一个完整的 JSON trace：
{
    "trace_id": "abc123",
    "operation": "ProcessMemory",
    "pipeline": "system1",
    "uid": "user_123",
    "agent_id": "chat_scene",
    "start_time": "2026-03-11T14:30:00.123",
    "total_ms": 1720.5,
    "spans": [
        {"name": "route", "start_ms": 0.1, "end_ms": 0.3, "duration_ms": 0.2, "output": {"layer": "profile"}},
        {"name": "embed", "start_ms": 0.3, "end_ms": 1500.0, "duration_ms": 1499.7, "output": {"dims": 1024}},
        {"name": "qdrant_upsert", "start_ms": 1500.0, "end_ms": 1700.0, "duration_ms": 200.0, "output": {"memory_id": "xxx"}},
    ],
    "input_summary": "用户喜欢科幻电影...",
    "output_summary": {"success": true, "layer": "profile"},
    "error": null
}
"""

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 日志根目录
# 云端部署: 设置 MEMORY_TRACE_LOG_DIR 或 MEMORY_DATA_DIR 指向挂载路径
# 本地使用: 默认写到 ~/.hy_memory/logs/traces/
def _default_trace_log_dir() -> str:
    explicit = os.getenv("MEMORY_TRACE_LOG_DIR")
    if explicit:
        return explicit
    data_dir = os.getenv("MEMORY_DATA_DIR", os.path.join(Path.home(), ".hy_memory"))
    return os.path.join(data_dir, "logs", "traces")

_TRACE_LOG_DIR = _default_trace_log_dir()

# 单个 output 字段的最大字符数 (避免日志爆炸)
# 增大到 10000 以支持完整的文本信息打印 (向量等大数据仍需外部过滤)
_MAX_OUTPUT_LEN = 10000

# 是否启用 trace (可通过环境变量关闭)
_TRACE_ENABLED = os.getenv("MEMORY_TRACE_ENABLED", "true").lower() in ("true", "1", "yes")


def _truncate(obj: Any, max_len: int = _MAX_OUTPUT_LEN) -> Any:
    """截断过长的输出"""
    if obj is None:
        return None
    if isinstance(obj, str):
        if len(obj) > max_len:
            return obj[:max_len] + f"...(truncated, total {len(obj)})"
        return obj
    if isinstance(obj, (list, tuple)):
        # 如果列表元素是 float (可能是向量)，立即截断
        if len(obj) > 10 and all(isinstance(x, (int, float)) for x in obj[:5]):
            return {"_type": "vector", "_len": len(obj), "_sample": list(obj[:3])}
        if len(obj) > 100:
            return {"_type": type(obj).__name__, "_len": len(obj), "_sample": list(obj[:5])}
        return [_truncate(item, max_len) for item in obj]
    if isinstance(obj, dict):
        return {k: _truncate(v, max_len) for k, v in obj.items()}
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, (int, bool)):
        return obj
    # fallback: str 表示
    s = str(obj)
    if len(s) > max_len:
        return s[:max_len] + "...(truncated)"
    return s


@dataclass
class Span:
    """一个 trace 中的单个环节"""
    name: str
    start_time: float = 0.0  # time.perf_counter()
    end_time: float = 0.0
    output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    
    # 相对于 trace 起始的偏移 (ms)
    start_ms: float = 0.0
    end_ms: float = 0.0
    
    def set_output(self, data: Dict[str, Any]) -> None:
        """设置该环节的关键输出"""
        self.output.update(data)
    
    def set_error(self, err: str) -> None:
        """设置错误信息"""
        self.error = err
    
    @property
    def duration_ms(self) -> float:
        if self.end_time <= 0:
            return 0.0
        return (self.end_time - self.start_time) * 1000
    
    def to_dict(self) -> Dict[str, Any]:
        d = {
            "name": self.name,
            "start_ms": round(self.start_ms, 2),
            "end_ms": round(self.end_ms, 2),
            "duration_ms": round(self.duration_ms, 2),
        }
        if self.output:
            d["output"] = _truncate(self.output)
        if self.error:
            d["error"] = self.error
        return d


class PipelineTracer:
    """
    Pipeline 请求级 Tracer
    
    记录一次请求的完整时间线和关键中间结果。
    支持嵌套 span 和 context manager 用法。
    """
    
    def __init__(
        self,
        operation: str,
        pipeline_version: str = "",
        uid: str = "",
        agent_id: str = "",
        request_id: str = "",
        content_preview: str = "",
    ):
        self.trace_id = uuid.uuid4().hex[:12]
        self.operation = operation
        self.pipeline_version = pipeline_version
        self.uid = uid
        self.agent_id = agent_id
        self.request_id = request_id
        self.content_preview = content_preview or ""
        
        self._start_time = time.perf_counter()
        self._start_datetime = datetime.now()
        self._spans: List[Span] = []
        self._current_span: Optional[Span] = None
        self._output_summary: Dict[str, Any] = {}
        self._error: Optional[str] = None
        self._finished = False
        
        if _TRACE_ENABLED:
            logger.debug(
                f"[trace] START {self.operation} "
                f"pipeline={self.pipeline_version} uid={self.uid} "
                f"content={self.content_preview}"
            )
    
    @contextmanager
    def span(self, name: str):
        """
        Context manager 方式记录一个环节。
        
        Usage:
            with tracer.span("embed") as s:
                result = await embed(text)
                s.set_output({"dims": len(result)})
        """
        s = Span(name=name)
        s.start_time = time.perf_counter()
        s.start_ms = (s.start_time - self._start_time) * 1000
        
        prev_span = self._current_span
        self._current_span = s
        
        try:
            yield s
        except Exception as e:
            s.set_error(str(e))
            raise
        finally:
            s.end_time = time.perf_counter()
            s.end_ms = (s.end_time - self._start_time) * 1000
            self._spans.append(s)
            self._current_span = prev_span
            
            if _TRACE_ENABLED:
                err_tag = f" ERROR={s.error}" if s.error else ""
                out_tag = ""
                if s.output:
                    # 简短版输出
                    short_out = {k: v for k, v in s.output.items() if not isinstance(v, (list, dict)) or len(str(v)) < 100}
                    if short_out:
                        out_tag = f" → {short_out}"
                logger.debug(
                    f"[trace] {name}: {s.duration_ms:.1f}ms{out_tag}{err_tag}"
                )
    
    def start_span(self, name: str) -> Span:
        """
        手动 start/end 方式 (不用 with 时使用)
        
        Usage:
            s = tracer.start_span("embed")
            result = await embed(text)
            s.set_output({"dims": len(result)})
            tracer.end_span(s)
        """
        s = Span(name=name)
        s.start_time = time.perf_counter()
        s.start_ms = (s.start_time - self._start_time) * 1000
        self._current_span = s
        return s
    
    def end_span(self, s: Span) -> None:
        """结束一个手动 span"""
        s.end_time = time.perf_counter()
        s.end_ms = (s.end_time - self._start_time) * 1000
        self._spans.append(s)
        self._current_span = None
        
        if _TRACE_ENABLED:
            err_tag = f" ERROR={s.error}" if s.error else ""
            logger.debug(
                f"[trace] {s.name}: {s.duration_ms:.1f}ms{err_tag}"
            )
    
    def set_output(self, data: Dict[str, Any]) -> None:
        """设置请求级的输出摘要"""
        self._output_summary.update(data)
    
    def set_error(self, error: str) -> None:
        """设置请求级错误"""
        self._error = error
    
    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self._start_time) * 1000
    
    def to_dict(self) -> Dict[str, Any]:
        """生成完整的 trace JSON"""
        return {
            "trace_id": self.trace_id,
            "operation": self.operation,
            "pipeline": self.pipeline_version,
            "uid": self.uid,
            "agent_id": self.agent_id,
            "request_id": self.request_id,
            "start_time": self._start_datetime.isoformat(),
            "total_ms": round(self.total_ms, 2),
            "spans": [s.to_dict() for s in self._spans],
            "input_summary": self.content_preview,
            "output_summary": _truncate(self._output_summary),
            "error": self._error,
        }
    
    def to_summary_line(self) -> str:
        """生成一行人类可读的 trace 摘要"""
        spans_info = " → ".join(
            f"{s.name}({s.duration_ms:.0f}ms)" for s in self._spans
        )
        status = "✅" if not self._error else f"❌ {self._error}"
        return (
            f"[{self.trace_id}] {self.operation} {self.pipeline_version} "
            f"total={self.total_ms:.0f}ms | {spans_info} | {status}"
        )
    
    def finish(self, *, write_file: bool = False) -> Dict[str, Any]:
        """
        结束 trace。

        请求级 timeline 由 writer 写入 SQLite（WRITE_TIMELINE，Inspector 用）。
        默认不再写 uid 维度旧 trace 文件；write_file=True 时保留兼容行为。

        Returns:
            完整的 trace dict
        """
        if self._finished:
            return self.to_dict()
        self._finished = True

        trace_data = self.to_dict()

        if _TRACE_ENABLED:
            logger.debug(f"[trace] FINISH {self.to_summary_line()}")
            if write_file:
                self._write_to_file(trace_data)

        return trace_data
    
    def _write_to_file(self, trace_data: Dict[str, Any]) -> None:
        """
        写入 JSONL 日志文件。
        
        文件路径格式:
            {TRACE_LOG_DIR}/{uid}/{uid}_{agent_id}_{date}.log

        示例:
            /memory/logs/traces/user_123/user_123_chat_scene_2026-03-17.log

        每行一条 JSON trace，按天滚动。
        """
        try:
            uid = self.uid or "_unknown"
            agent_id = self.agent_id or "_default"
            date_str = self._start_datetime.strftime("%Y-%m-%d")

            # 目录: {trace_log_dir}/{uid}/
            log_dir = Path(_TRACE_LOG_DIR) / uid
            log_dir.mkdir(parents=True, exist_ok=True)
            
            # 文件: {uid}_{agent_id}_{date}.log
            log_file = log_dir / f"{uid}_{agent_id}_{date_str}.log"
            
            # append 一行 JSON
            line = json.dumps(trace_data, ensure_ascii=False, default=str)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                
        except Exception as e:
            logger.warning(f"[TRACE:{self.trace_id}] Failed to write trace file: {e}")


def create_tracer(
    operation: str,
    pipeline_version: str = "",
    uid: str = "",
    agent_id: str = "",
    request_id: str = "",
    content_preview: str = "",
) -> PipelineTracer:
    """
    创建一个 PipelineTracer 的便捷工厂函数。

    如果 MEMORY_TRACE_ENABLED=false，返回的 tracer 仍然可用，
    只是不输出日志和写文件。
    """
    return PipelineTracer(
        operation=operation,
        pipeline_version=pipeline_version,
        uid=uid,
        agent_id=agent_id,
        request_id=request_id,
        content_preview=content_preview,
    )
