"""
Agent Memory V2 - SqliteCache

基于 Python 标准库 sqlite3 的零依赖审计/观测落库后端。
单机开箱即用。

设计模式跟随 history_store.py:
- 标准库 sqlite3 + _run_in_sqlite_pool()
- WAL 模式 + check_same_thread=False
- threading.Lock 保护写操作

三张表:
- memory_operations / pipeline_logs / system_metrics: 审计与观测
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any

from .cache_base import CacheBase
from ..config import MemoryConfig

logger = logging.getLogger(__name__)

# SQLite 独立线程池（不与 VDB/Graph 竞争）
_SQLITE_POOL_SIZE = 32
_sqlite_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_SQLITE_POOL_SIZE, thread_name_prefix="sqlite"
)


def _run_in_sqlite_pool(func, *args, **kwargs):
    """在 SQLite 独立线程池中执行同步函数"""
    import functools
    loop = asyncio.get_event_loop()
    if args or kwargs:
        return loop.run_in_executor(_sqlite_executor, functools.partial(func, *args, **kwargs))
    return loop.run_in_executor(_sqlite_executor, func)


# DDL
# 表名 / 列与 MySQL 分区版对齐（含 created_date），SQLite 不支持分区，仅普通建表。
_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS memory_operations_v2 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL DEFAULT '',
    op           TEXT NOT NULL,       -- ADD / UPDATE
    memory_id    TEXT NOT NULL,       -- 操作后的节点 ID（ADD: 新节点 ID；UPDATE: 新节点 ID）
    old_memory_id TEXT,               -- UPDATE 时的旧节点 ID
    content      TEXT NOT NULL,       -- 最终写入的内容
    layer        TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    supersedes   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    created_date TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memop_request ON memory_operations_v2(request_id);
CREATE INDEX IF NOT EXISTS idx_memop_memory  ON memory_operations_v2(memory_id);
CREATE INDEX IF NOT EXISTS idx_memop_user    ON memory_operations_v2(user_id);

CREATE TABLE IF NOT EXISTS pipeline_logs_v2 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    agent_id     TEXT NOT NULL DEFAULT '',
    step         TEXT NOT NULL,       -- EXTRACT / SEARCH_QUERY / RECONCILE / SUMMARY
    prompt       TEXT NOT NULL,       -- LLM prompt 原文
    response     TEXT NOT NULL,       -- LLM response 原文
    parsed       TEXT NOT NULL DEFAULT '',  -- 解析后的结构化结果 (JSON)
    memory_ids   TEXT NOT NULL DEFAULT '',  -- 关联的 memory_id 列表 (JSON array)，reconcile 阶段有值
    elapsed_ms   REAL NOT NULL DEFAULT 0,
    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    created_date TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_plog_request ON pipeline_logs_v2(request_id);
CREATE INDEX IF NOT EXISTS idx_plog_user    ON pipeline_logs_v2(user_id);

CREATE TABLE IF NOT EXISTS system_metrics (
    minute_ts  TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON system_metrics(minute_ts);

CREATE TABLE IF NOT EXISTS digest_runs (
    run_id          TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT '',
    agent_id        TEXT NOT NULL DEFAULT '',
    trigger         TEXT NOT NULL DEFAULT '',   -- per_write|manual|scheduled|night|catchup
    status          TEXT NOT NULL DEFAULT '',   -- running|completed|partial|failed
    clusters_total  INTEGER NOT NULL DEFAULT 0,
    batches         INTEGER NOT NULL DEFAULT 0,
    tasks_total     INTEGER NOT NULL DEFAULT 0,
    tasks_succeeded INTEGER NOT NULL DEFAULT 0,
    tasks_failed    INTEGER NOT NULL DEFAULT 0,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT NOT NULL DEFAULT '',
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL DEFAULT '',
    completed_at    TEXT NOT NULL DEFAULT '',
    elapsed_ms      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_drun_user ON digest_runs(user_id);
CREATE INDEX IF NOT EXISTS idx_drun_started ON digest_runs(started_at DESC);
"""


class SqliteCache(CacheBase):
    """
    SQLite 审计/观测落库后端（memory_operations / pipeline_logs / system_metrics）。

    零依赖本地后端，适用于单机 / 开发环境。
    使用标准库 sqlite3，通过 _run_in_sqlite_pool() 包装为异步。
    """

    def __init__(self, config: MemoryConfig):
        self.config = config
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

        # 读取 db_path
        cache_cfg = getattr(config, "cache", None)
        if cache_cfg and getattr(cache_cfg, "db_path", None):
            self._db_path = cache_cfg.db_path
        else:
            from ..config import _default_data_dir
            self._db_path = os.getenv(
                "MEMORY_CACHE_DB_PATH",
                os.path.join(_default_data_dir(), "data", "cache.db"),
            )

    # ================================================================
    # 生命周期
    # ================================================================

    async def initialize(self) -> None:
        await _run_in_sqlite_pool(self._init_sync)
        logger.debug(f"SqliteCache initialized: {self._db_path}")

    def _init_sync(self) -> None:
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_CREATE_TABLES_SQL)
        self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await _run_in_sqlite_pool(self._close_sync)
        logger.info("SqliteCache closed")

    def _close_sync(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ================================================================
    # 统计
    # ================================================================

    async def get_stats(self) -> Dict[str, Any]:
        return {
            "backend": "sqlite",
            "db_path": self._db_path,
        }

    # ================================================================
    # Memory Operations Log
    # ================================================================

    async def store_memory_operation(
        self,
        request_id: str,
        user_id: str,
        agent_id: str,
        op: str,
        memory_id: str,
        content: str,
        layer: str = "",
        old_memory_id: Optional[str] = None,
        reason: str = "",
        supersedes: Optional[List[str]] = None,
    ) -> bool:
        """记录一条知识库变动操作（ADD / EVOLVE）"""
        from ..utils.pipeline_observability import is_memory_operations_enabled
        if not is_memory_operations_enabled():
            return True
        try:
            import json as _json
            from datetime import datetime as dt
            now = dt.now()
            created_at = now.isoformat()
            created_date = now.strftime("%Y-%m-%d")
            supersedes_str = _json.dumps(supersedes or [], ensure_ascii=False)

            def _insert():
                with self._lock:
                    self._conn.execute(
                        """INSERT INTO memory_operations_v2
                           (request_id, user_id, agent_id, op, memory_id, old_memory_id,
                            content, layer, reason, supersedes, created_at, created_date)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (request_id, user_id, agent_id, op, memory_id,
                         old_memory_id, content, layer, reason, supersedes_str,
                         created_at, created_date),
                    )
                    self._conn.commit()

            await _run_in_sqlite_pool(_insert)
            return True
        except Exception as e:
            logger.warning(f"store_memory_operation failed: {e}")
            return False

    async def get_memory_operations(
        self,
        request_id: Optional[str] = None,
        memory_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        查询知识库变动记录，支持按 request_id / memory_id / user_id 过滤。
        至少指定一个过滤条件。
        """
        try:
            def _query():
                conditions = []
                params = []
                if request_id:
                    conditions.append("request_id = ?")
                    params.append(request_id)
                if memory_id:
                    conditions.append("memory_id = ?")
                    params.append(memory_id)
                if user_id:
                    conditions.append("user_id = ?")
                    params.append(user_id)

                if not conditions:
                    return []

                where = " AND ".join(conditions)
                rows = self._conn.execute(
                    f"SELECT * FROM memory_operations_v2 WHERE {where} "
                    f"ORDER BY id DESC LIMIT ?",
                    params + [limit],
                ).fetchall()
                result = []
                for r in rows:
                    row = dict(r)
                    # 反序列化 supersedes JSON 字符串 → list
                    import json as _json
                    raw_sup = row.get("supersedes", "")
                    try:
                        row["supersedes"] = _json.loads(raw_sup) if raw_sup else []
                    except Exception:
                        row["supersedes"] = []
                    result.append(row)
                return result

            return await _run_in_sqlite_pool(_query)
        except Exception as e:
            logger.warning(f"get_memory_operations failed: {e}")
            return []

    # ================================================================
    # Pipeline Logs (LLM 调用链中间结果)
    # ================================================================

    async def store_pipeline_log(
        self,
        request_id: str,
        user_id: str,
        agent_id: str,
        step: str,
        prompt: str,
        response: str,
        parsed: str = "",
        memory_ids: Optional[List[str]] = None,
        elapsed_ms: float = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> bool:
        """
        记录一条 pipeline 中间结果（EXTRACT / SEARCH_QUERY / RECONCILE / SUMMARY）。
        """
        try:
            from datetime import datetime as dt
            now = dt.now()
            created_at = now.isoformat()
            created_date = now.strftime("%Y-%m-%d")
            mem_ids_json = json.dumps(memory_ids or [])

            def _insert():
                with self._lock:
                    self._conn.execute(
                        """INSERT INTO pipeline_logs_v2
                           (request_id, user_id, agent_id, step, prompt, response,
                            parsed, memory_ids, elapsed_ms,
                            prompt_tokens, completion_tokens, total_tokens,
                            created_at, created_date)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (request_id, user_id, agent_id, step, prompt, response,
                         parsed, mem_ids_json, elapsed_ms,
                         prompt_tokens, completion_tokens, total_tokens,
                         created_at, created_date),
                    )
                    self._conn.commit()

            await _run_in_sqlite_pool(_insert)
            return True
        except Exception as e:
            logger.warning(f"store_pipeline_log failed: {e}")
            return False

    async def get_pipeline_logs(
        self,
        request_id: Optional[str] = None,
        user_id: Optional[str] = None,
        step: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        查询 pipeline 中间结果日志。

        支持按 request_id / user_id / step 过滤，至少指定一个。
        """
        try:
            def _query():
                conditions = []
                params = []
                if request_id:
                    conditions.append("request_id = ?")
                    params.append(request_id)
                if user_id:
                    conditions.append("user_id = ?")
                    params.append(user_id)
                if step:
                    conditions.append("step = ?")
                    params.append(step)

                if not conditions:
                    return []

                where = " AND ".join(conditions)
                rows = self._conn.execute(
                    f"SELECT * FROM pipeline_logs_v2 WHERE {where} "
                    f"ORDER BY id ASC LIMIT ?",
                    params + [limit],
                ).fetchall()
                return [dict(r) for r in rows]

            return await _run_in_sqlite_pool(_query)
        except Exception as e:
            logger.warning(f"get_pipeline_logs failed: {e}")
            return []

    # ================================================================
    # Digest Runs（System 2 顶层执行记录）
    # ================================================================

    _DIGEST_RUN_COLS = (
        "run_id", "user_id", "agent_id", "trigger", "status",
        "clusters_total", "batches", "tasks_total", "tasks_succeeded",
        "tasks_failed", "retry_count", "error_message", "total_tokens",
        "started_at", "completed_at", "elapsed_ms",
    )

    async def store_digest_run(self, run_id: str, record: Dict[str, Any]) -> bool:
        """新建一条 digest run 顶层记录（INSERT OR REPLACE）。"""
        try:
            rec = dict(record)
            rec["run_id"] = run_id
            values = [rec.get(c, "" if c in ("user_id", "agent_id", "trigger", "status",
                                             "error_message", "started_at", "completed_at")
                              else 0) for c in self._DIGEST_RUN_COLS]
            placeholders = ", ".join("?" for _ in self._DIGEST_RUN_COLS)
            cols = ", ".join(self._DIGEST_RUN_COLS)

            def _insert():
                with self._lock:
                    self._conn.execute(
                        f"INSERT OR REPLACE INTO digest_runs ({cols}) VALUES ({placeholders})",
                        values,
                    )
                    self._conn.commit()

            await _run_in_sqlite_pool(_insert)
            return True
        except Exception as e:
            logger.warning(f"store_digest_run failed: {e}")
            return False

    async def update_digest_run(self, run_id: str, **fields: Any) -> bool:
        """部分更新一条 digest run 记录（只覆盖给定字段）。"""
        valid = [k for k in fields if k in self._DIGEST_RUN_COLS and k != "run_id"]
        if not valid:
            return False
        try:
            set_clause = ", ".join(f"{k} = ?" for k in valid)
            params = [fields[k] for k in valid] + [run_id]

            def _update():
                with self._lock:
                    self._conn.execute(
                        f"UPDATE digest_runs SET {set_clause} WHERE run_id = ?",
                        params,
                    )
                    self._conn.commit()

            await _run_in_sqlite_pool(_update)
            return True
        except Exception as e:
            logger.warning(f"update_digest_run failed: {e}")
            return False

    async def get_digest_runs(
        self,
        user_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """查询最近的 digest run 记录（按 started_at 倒序）。"""
        try:
            def _query():
                if user_id:
                    rows = self._conn.execute(
                        "SELECT * FROM digest_runs WHERE user_id = ? "
                        "ORDER BY started_at DESC LIMIT ?",
                        (user_id, limit),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT * FROM digest_runs ORDER BY started_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(r) for r in rows]

            return await _run_in_sqlite_pool(_query)
        except Exception as e:
            logger.warning(f"get_digest_runs failed: {e}")
            return []

    # ================================================================
    # System Metrics（分钟粒度落盘）
    # ================================================================

    async def store_metrics_minute(self, minute_ts: str, data: dict) -> None:
        """存储一个分钟桶的增量指标数据（UPSERT: 同一分钟多次写入会合并）"""
        try:
            def _store():
                now = datetime.now().isoformat()
                # UPSERT: 如果同一分钟已存在，合并数据
                existing = self._conn.execute(
                    "SELECT data FROM system_metrics WHERE minute_ts = ?",
                    (minute_ts,),
                ).fetchone()
                if existing:
                    import json as _json
                    old_data = _json.loads(existing["data"])
                    merged = self._merge_metric_buckets(old_data, data)
                    self._conn.execute(
                        "UPDATE system_metrics SET data = ?, created_at = ? WHERE minute_ts = ?",
                        (_json.dumps(merged, default=str), now, minute_ts),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO system_metrics (minute_ts, data, created_at) VALUES (?, ?, ?)",
                        (minute_ts, json.dumps(data, default=str), now),
                    )
                self._conn.commit()
            await _run_in_sqlite_pool(_store)
        except Exception as e:
            logger.warning(f"store_metrics_minute failed: {e}")

    async def load_metrics_range(self, start_ts: str, end_ts: str) -> list:
        """读取指定时间范围内的分钟指标数据"""
        try:
            def _load():
                rows = self._conn.execute(
                    "SELECT data FROM system_metrics WHERE minute_ts >= ? AND minute_ts <= ? ORDER BY minute_ts",
                    (start_ts, end_ts),
                ).fetchall()
                results = []
                for row in rows:
                    try:
                        results.append(json.loads(row["data"]))
                    except (json.JSONDecodeError, TypeError):
                        pass
                return results
            return await _run_in_sqlite_pool(_load)
        except Exception as e:
            logger.warning(f"load_metrics_range failed: {e}")
            return []

    async def cleanup_old_metrics(self, before_ts: str) -> None:
        """删除指定时间之前的 metrics 数据"""
        try:
            def _cleanup():
                self._conn.execute(
                    "DELETE FROM system_metrics WHERE minute_ts < ?",
                    (before_ts,),
                )
                self._conn.commit()
            await _run_in_sqlite_pool(_cleanup)
        except Exception as e:
            logger.warning(f"cleanup_old_metrics failed: {e}")

    @staticmethod
    def _merge_metric_buckets(old: dict, new: dict) -> dict:
        """合并两个 metric bucket（同一分钟多次 flush）"""
        merged = dict(old)
        for key in ("sys1_started", "sys1_completed", "sys1_failed",
                    "sys2_started", "sys2_completed", "sys2_failed",
                    "vdb_ops", "graph_ops"):
            merged[key] = merged.get(key, 0) + new.get(key, 0)
        for key in ("vdb_ops_sum_ms", "graph_ops_sum_ms"):
            merged[key] = merged.get(key, 0) + new.get(key, 0)
        # timing sums
        for bucket_key in ("sys1_timing_sums", "sys2_timing_sums"):
            old_sums = merged.get(bucket_key, {})
            new_sums = new.get(bucket_key, {})
            for k, v in new_sums.items():
                old_sums[k] = old_sums.get(k, 0) + v
            merged[bucket_key] = old_sums
        return merged
