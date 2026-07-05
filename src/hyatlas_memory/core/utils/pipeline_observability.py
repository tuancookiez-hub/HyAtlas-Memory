# -*- coding: utf-8 -*-
"""
Pipeline 可观测性：Log（文件 JSONL，始终开启）与 Trace（SQLite pipeline_logs，可关闭）。

- Log：运维 tail/grep，每个环节即时一行，不依赖后续步骤成功与否。
- Trace：Inspector / 调试，落库 pipeline_logs（需 MEMORY_CACHE_BACKEND=sqlite 等持久化后端）。

环境变量：
  MEMORY_PIPELINE_TRACE_ENABLED  — Trace 落库，默认 true；OpenClaw C 端可设 false
  MEMORY_MEMORY_OPERATIONS_ENABLED — memory_operations 审计表落库，默认 false
  MEMORY_LOG_DIR                 — Log 目录（优先）
  MEMORY_TRACE_LOG_DIR           — Log 目录（兼容旧名）

子目录（{log_dir}/{subdir}/{date}.log）由 PipelineLogWriter 从每条记录的
user_id 前缀（第一个 "__" 左侧）切分；无前缀回落 "default"。SDK 不感知
"app_id/租户"等业务概念，多租户拆分由上层（App）通过拼接 user_id 实现
（同时避免单个日志文件过大、便于日志采集）。
"""

from __future__ import annotations

import os
from pathlib import Path


def is_pipeline_trace_enabled() -> bool:
    """是否将 pipeline step 写入 cache（SQLite pipeline_logs），供 Inspector 使用。"""
    return os.getenv("MEMORY_PIPELINE_TRACE_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )


def is_memory_operations_enabled() -> bool:
    """是否将知识库变动写入 memory_operations 审计表。默认关闭（false）。"""
    return os.getenv("MEMORY_MEMORY_OPERATIONS_ENABLED", "false").lower() in (
        "true",
        "1",
        "yes",
    )


def resolve_pipeline_log_dir() -> str:
    """
    JSONL pipeline log 根目录（始终使用，有默认值）。

    优先级: MEMORY_LOG_DIR > MEMORY_TRACE_LOG_DIR > {MEMORY_DATA_DIR}/logs/pipeline
    """
    explicit = os.getenv("MEMORY_LOG_DIR") or os.getenv("MEMORY_TRACE_LOG_DIR")
    if explicit:
        return explicit
    data_dir = os.getenv("MEMORY_DATA_DIR", os.path.join(Path.home(), ".hy_memory"))
    return os.path.join(data_dir, "logs", "pipeline")
