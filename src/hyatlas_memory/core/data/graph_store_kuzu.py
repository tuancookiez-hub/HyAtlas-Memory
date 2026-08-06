"""
Agent Memory V2 - KuzuGraphStore

基于 Kuzu 嵌入式图数据库的图存储层实现。

Kuzu 优势:
- 嵌入式部署 (无需额外服务进程)
- 支持 Cypher 查询语言
- 高性能图遍历
- 支持属性图模型

注意: Kuzu 嵌入式 DB 只支持单进程访问。在多进程环境中会降级为 no-op 模式。
"""

from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
from pathlib import Path
import logging
import json
import re

from ..models.memory import MemoryNode, MemoryLayer, MemoryStatus
from ..config import MemoryConfig
from .graph_store_base import GraphStoreBase

logger = logging.getLogger(__name__)

# Kuzu Cypher schema DDL
# 注意: Kuzu < 0.4.0 不支持 IF NOT EXISTS，需要先检查表是否存在再创建
# embedding / beh_embedding 列使用 FLOAT[{dims}]，dims 在 init_schema 时动态替换
_SCHEMA_DDL_TABLES = {
    # Node tables
    "User": """
CREATE NODE TABLE User(
    isolation_key STRING,
    user_id STRING,
    agent_id STRING,
    created_at TIMESTAMP,
    PRIMARY KEY (isolation_key)
)""",
    "Memory": None,  # 动态生成，依赖 embedding_dims
    "Topic": """
CREATE NODE TABLE Topic(
    topic_id STRING,
    isolation_key STRING,
    name STRING,
    created_at TIMESTAMP,
    PRIMARY KEY (topic_id)
)""",
    # Rel tables
    "HAS_MEMORY": """
CREATE REL TABLE HAS_MEMORY(
    FROM User TO Memory,
    created_at TIMESTAMP
)""",
    "TAGGED_WITH": """
CREATE REL TABLE TAGGED_WITH(
    FROM Memory TO Topic,
    created_at TIMESTAMP
)""",
    "RELATED_TO": """
CREATE REL TABLE RELATED_TO(
    FROM Memory TO Memory,
    relation_type STRING,
    weight DOUBLE,
    created_at TIMESTAMP
)""",
    # VdbRef shadow node + DERIVED_FROM edge
    "VdbRef": """
CREATE NODE TABLE VdbRef(
    node_id STRING,
    layer STRING,
    PRIMARY KEY (node_id)
)""",
    "DERIVED_FROM": """
CREATE REL TABLE DERIVED_FROM(
    FROM Memory TO VdbRef,
    created_at TIMESTAMP
)""",
    # Cross-domain schema induction: L6 basic → L6 core (单向)
    "CROSS_ABSTRACTS_TO": """
CREATE REL TABLE CROSS_ABSTRACTS_TO(
    FROM Memory TO Memory,
    created_at TIMESTAMP
)""",
}


def _make_memory_ddl(dims: int) -> str:
    """生成 Memory 表 DDL，embedding 列维度由配置决定"""
    return f"""
CREATE NODE TABLE Memory(
    node_id STRING,
    isolation_key STRING,
    user_id STRING,
    agent_id STRING,
    layer STRING,
    content STRING,
    content_type STRING,
    status STRING,
    version INT64,
    confidence DOUBLE,
    source_type STRING,
    emotional_valence DOUBLE,
    emotional_arousal DOUBLE,
    specificity_score DOUBLE,
    rarity_score DOUBLE,
    longtail_flag BOOLEAN,
    meta_tags STRING,
    source_session_id STRING,
    source_turn_index INT64,
    temporal_anchor STRING,
    access_count INT64,
    created_at TIMESTAMP,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    last_accessed_at TIMESTAMP,
    previous_version_id STRING,
    superseded_by_id STRING,
    change_reason STRING,
    evidence_chain STRING,
    custom_json STRING,
    tags STRING,
    extra_json STRING,
    embedding FLOAT[{dims}],
    beh_embedding FLOAT[{dims}],
    PRIMARY KEY (node_id)
)"""


class KuzuGraphStore(GraphStoreBase):
    """
    Kuzu 图存储

    使用嵌入式 Kuzu 数据库，通过 Cypher 查询实现图操作。

    注意: Kuzu 嵌入式 DB 只支持单进程访问。在多进程（如 tRPC 多 worker）环境中，
    只有第一个获取到锁的进程能正常使用 GraphStore，其他进程会降级为 no-op 模式。
    """

    def __init__(self, config: MemoryConfig):
        super().__init__(config)
        self._db = None
        self._conn = None
        self._available = False  # 是否成功初始化

        # embedding 维度（从 VectorStore 或 Embedder 配置中取）
        vs_dims = getattr(getattr(config, 'vector_store', None), 'embedding_dims', None)
        emb_dims = getattr(getattr(config, 'embedder', None), 'embedding_dims', None)
        self._embedding_dims = vs_dims or emb_dims or 1536
        self._embedding_property = "embedding"
        self._beh_embedding_property = "beh_embedding"
        self._embedding_index = "memory_content_idx"
        self._legacy_embedding_dims = []
        self._vector_schema_compatible = False

        # 图数据库存储路径
        graph_config = getattr(config, 'graph_store', None)
        if graph_config and hasattr(graph_config, 'db_path'):
            self._db_path = graph_config.db_path
        else:
            self._db_path = str(
                Path(config.vector_store.persist_directory).parent / "kuzu_db"
            )

    async def initialize(self) -> None:
        """初始化 Kuzu 数据库并创建 Schema
        
        在多进程环境中，如果 Kuzu 文件被其他进程锁定，会降级为 no-op 模式。
        使用线程池 + 超时来防止 Kuzu Database() 构造函数阻塞事件循环。
        """
        try:
            import kuzu
        except ImportError:
            raise ImportError(
                "kuzu is required. Install with: pip install kuzu"
            )

        db_path = Path(self._db_path)
        # Kuzu >= 0.11 自行管理数据库文件，不接受已存在的空目录
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.is_dir():
            # 如果路径是一个空目录，删除它让 Kuzu 自己创建
            try:
                if not any(db_path.iterdir()):
                    logger.info(f"Removing empty directory at {db_path} for Kuzu initialization")
                    db_path.rmdir()
                else:
                    # 非空目录可能是旧版 Kuzu 数据，尝试直接使用
                    logger.info(f"Non-empty directory at {db_path}, attempting Kuzu open")
            except Exception as rm_err:
                logger.warning(f"Could not clean up {db_path}: {rm_err}")

        import asyncio
        import concurrent.futures

        def _open_kuzu_db():
            """在线程池中执行 Kuzu Database 构造（可能阻塞获取文件锁）"""
            db = kuzu.Database(self._db_path)
            conn = kuzu.Connection(db)
            return db, conn

        try:
            loop = asyncio.get_event_loop()
            # 用线程池执行，超时 5 秒防止文件锁阻塞卡住事件循环
            db, conn = await asyncio.wait_for(
                loop.run_in_executor(None, _open_kuzu_db),
                timeout=5.0,
            )
            self._db = db
            self._conn = conn
        except asyncio.TimeoutError:
            logger.warning(
                f"GraphStore init timed out after 5s (Kuzu file lock contention). "
                f"Degrading to no-op mode. This is expected in multi-worker environments."
            )
            self._db = None
            self._conn = None
            self._available = False
            return
        except Exception as db_err:
            # Kuzu 初始化失败 — 降级为 no-op，记录完整诊断信息
            import os as _os
            try:
                import kuzu as _kuzu_mod
                _kuzu_ver = getattr(_kuzu_mod, '__version__', 'unknown')
            except Exception:
                _kuzu_ver = 'import_failed'
            logger.error(
                f"GraphStore init failed (will degrade to no-op): {db_err}. "
                f"db_path={self._db_path}, "
                f"path_exists={_os.path.exists(self._db_path)}, "
                f"kuzu_version={_kuzu_ver}",
            )
            logger.debug("GraphStore init traceback:", exc_info=True)
            self._db = None
            self._conn = None
            self._available = False
            return

        # 获取 embedding 维度 → 动态生成 Memory 表 DDL
        dims = self._embedding_dims
        ddl_tables = dict(_SCHEMA_DDL_TABLES)
        ddl_tables["Memory"] = _make_memory_ddl(dims)

        # 执行 Schema DDL (先检查表是否存在，不存在才创建)
        ddl_errors = []
        for table_name, ddl_stmt in ddl_tables.items():
            if ddl_stmt is None:
                continue
            if self._table_exists(table_name):
                logger.debug(f"Table {table_name} already exists, skipping")
                continue
            try:
                self._conn.execute(ddl_stmt + ";")
                logger.debug(f"Created table {table_name}")
            except Exception as e:
                err_msg = str(e)
                # "already exists" 类错误可以忽略
                if "already exist" in err_msg.lower() or "duplicate" in err_msg.lower():
                    logger.debug(f"Schema DDL note (ignored): {e}")
                else:
                    ddl_errors.append((table_name, err_msg))
                    logger.warning(f"Schema DDL failed for {table_name}: {e}")

        # 检查关键表是否存在
        critical_tables = ["User", "Memory", "Topic"]
        missing = [t for t in critical_tables if not self._table_exists(t)]
        if missing:
            logger.error(
                f"GraphStore schema incomplete: missing tables {missing}. "
                f"DDL errors: {ddl_errors}. Degrading to no-op."
            )
            self._db = None
            self._conn = None
            self._available = False
            return

        # Existing Kuzu tables keep their original fixed ARRAY dimensions.
        # Add a dimension-specific lane when config dimensions change; never
        # rewrite or delete historical graph vectors during normal startup.
        self._ensure_embedding_lane(dims)
        self._ensure_vector_indexes()

        self._available = True
        logger.info(f"GraphStore initialized (Kuzu), db_path={self._db_path}, embedding_dims={dims}")

    def _table_properties(self, table: str) -> dict[str, str]:
        """Return property name → Kuzu type for a table."""
        props = {}
        result = self._conn.execute(f"CALL table_info('{table}') RETURN *;")
        while result.has_next():
            row = result.get_next()
            props[str(row[1])] = str(row[2])
        return props

    @staticmethod
    def _array_dims(kind: str | None) -> int | None:
        match = re.fullmatch(r"FLOAT\[(\d+)\]", kind or "")
        return int(match.group(1)) if match else None

    def _ensure_embedding_lane(self, dims: int) -> None:
        """Select or add the graph-vector properties for active dimensions."""
        props = self._table_properties("Memory")
        canonical = self._array_dims(props.get("embedding"))
        legacy = {
            found
            for name, kind in props.items()
            if (name == "embedding" or name.startswith("embedding_"))
            and (found := self._array_dims(kind)) is not None
            and found != dims
        }

        beh_canonical = self._array_dims(props.get("beh_embedding"))
        if canonical == dims and beh_canonical == dims:
            self._embedding_property = "embedding"
            self._beh_embedding_property = "beh_embedding"
            self._embedding_index = "memory_content_idx"
        else:
            self._embedding_property = f"embedding_{dims}"
            self._beh_embedding_property = f"beh_embedding_{dims}"
            self._embedding_index = f"memory_content_idx_{dims}"
            expected = f"FLOAT[{dims}]"
            for name in (self._embedding_property, self._beh_embedding_property):
                if name not in props:
                    self._conn.execute(f"ALTER TABLE Memory ADD {name} {expected};")
                    props[name] = expected
                    logger.info(f"Added Kuzu Memory.{name} ({expected})")
                if props[name] != expected:
                    raise RuntimeError(
                        f"Kuzu Memory.{name} has type {props[name]}, expected {expected}"
                    )

        self._legacy_embedding_dims = sorted(legacy)
        self._vector_schema_compatible = True

    def _ensure_vector_indexes(self) -> None:
        """确保 Memory 表上的 HNSW 向量索引存在

        注意: Kuzu 不允许 SET 被索引的列。因此：
        - embedding (V_con): 建索引，CREATE 时一次性写入，不做后续 SET
        - beh_embedding (V_beh): 不建索引，允许 sweeper 后续 SET 写入
        beh_embedding 的检索走内存矩阵计算（N < 100，毫秒级）
        """
        try:
            self._conn.execute(
                f"CALL CREATE_VECTOR_INDEX('Memory', '{self._embedding_index}', "
                f"'{self._embedding_property}', metric := 'cosine');"
            )
            logger.info(
                f"Created vector index {self._embedding_index} on "
                f"Memory.{self._embedding_property}"
            )
        except Exception as e:
            err_msg = str(e).lower()
            if "already exist" in err_msg or "duplicate" in err_msg:
                logger.debug(f"Vector index {self._embedding_index} already exists")
            else:
                self._vector_schema_compatible = False
                logger.warning(f"Failed to create vector index {self._embedding_index}: {e}")

    def _execute(self, query: str, params: dict[str, Any] | None = None) -> Any:
        """执行 Cypher 查询 (GraphStore 不可用时返回 None)"""
        if not self._available or self._conn is None:
            return None
        if params:
            return self._conn.execute(query, params)
        return self._conn.execute(query)

    def _table_exists(self, table_name: str) -> bool:
        """检查 Kuzu 中某个 node/rel table 是否存在
        
        兼容不同 Kuzu 版本:
        - Kuzu 0.4.x: show_tables() 返回 [name, type, comment]
        - Kuzu 0.11+: show_tables() 返回 [id, name, type, database, comment]
        """
        if self._conn is None:
            return False
        try:
            result = self._conn.execute("CALL show_tables() RETURN *;")
            while result.has_next():
                row = result.get_next()
                # 遍历所有列查找表名（兼容不同版本）
                for col in row:
                    if col == table_name:
                        return True
            return False
        except Exception as e:
            logger.debug(f"_table_exists check failed: {e}")
            return False

    # ================================================================
    # User 节点管理
    # ================================================================

    async def ensure_user_node(self, isolation_key: str, user_id: str = "",
                                agent_id: str = "") -> None:
        """确保 User 节点存在
        
        Kuzu 0.4.x 不支持 MERGE ... ON CREATE SET，改用 exists check + CREATE/UPDATE
        """
        if not self._available:
            return
        try:
            # 检查是否已存在
            result = self._execute(
                "MATCH (u:User {isolation_key: $ik}) RETURN u.isolation_key;",
                {"ik": isolation_key}
            )
            exists = result is not None and result.has_next()
            
            if not exists:
                self._execute(
                    "CREATE (u:User {isolation_key: $ik, user_id: $uid, "
                    "agent_id: $sid, created_at: $now});",
                    {"ik": isolation_key, "uid": user_id,
                     "sid": agent_id, "now": datetime.now()}
                )
        except Exception as e:
            logger.warning(f"ensure_user_node failed: {e}")

    # ================================================================
    # Memory 节点 CRUD
    # ================================================================

    async def upsert_memory_node(self, node: MemoryNode) -> str:
        """创建或更新 Memory 节点 + User→Memory 边"""
        if not self._available:
            return node.node_id
        isolation_key = node.get_isolation_key()
        await self.ensure_user_node(
            isolation_key, node.user_id, node.agent_id
        )

        now = datetime.now()

        # 安全获取属性（MemoryNode 在不同版本/场景下可能缺少某些字段）
        def _val(attr, default=""):
            v = getattr(node, attr, default)
            if v is None:
                return default
            return v.value if hasattr(v, 'value') else v

        meta_tags_raw = getattr(node, 'meta_tags', []) or []
        meta_tags_strs = [
            (t.value if hasattr(t, 'value') else str(t)) for t in meta_tags_raw
        ]

        params = {
            "nid": node.node_id,
            "ik": isolation_key,
            "uid": node.user_id,
            "sid": node.agent_id,
            "layer": node.layer.value,
            "content": node.content,
            "ctype": _val('content_type', ''),
            "status": node.status.value,
            "ver": getattr(node, 'version', 1) or 1,
            "conf": node.confidence,
            "stype": node.source_type.value,
            "eval": getattr(node, 'emotional_valence', 0.0) or 0.0,
            "earou": getattr(node, 'emotional_arousal', 0.0) or 0.0,
            "spec": getattr(node, 'specificity_score', 0.0) or 0.0,
            "rar": getattr(node, 'rarity_score', 0.0) or 0.0,
            "lt": getattr(node, 'longtail_flag', False) or False,
            "mtags": json.dumps(meta_tags_strs),
            "ssid": getattr(node, 'source_session_id', '') or '',
            "stidx": getattr(node, 'source_turn_index', 0) or 0,
            "tanc": getattr(node, 'temporal_anchor', '') or '',
            "ac": getattr(node, 'access_count', 0) or 0,
            "cat": getattr(node, 'created_at', None) or now,
            "vf": node.valid_from or now,
            "vu": getattr(node, 'valid_until', None),
            "laat": getattr(node, 'last_accessed_at', None),
            "pvid": getattr(node, 'previous_version_id', '') or '',
            "sbid": getattr(node, 'superseded_by_id', '') or '',
            "cr": getattr(node, 'change_reason', '') or '',
            "ec": json.dumps(getattr(node, 'evidence_chain', []) or []),
            "cust": json.dumps(getattr(node, 'custom', {}) or {}),
            "tags": json.dumps(getattr(node, 'tags', []) or []),
            "extra": "{}",
        }

        # 向量属性：从 MemoryNode._graph_embedding / _graph_beh_embedding 获取
        # 这些是瞬态属性，不序列化，仅在 create_graph_node 流程中传递
        embedding = getattr(node, '_graph_embedding', None)
        beh_embedding = getattr(node, '_graph_beh_embedding', None)

        # Kuzu 0.4.x 不支持 MERGE ... ON CREATE/MATCH SET，改用 exists check + CREATE/UPDATE
        exists_result = self._execute(
            "MATCH (m:Memory {node_id: $nid}) RETURN m.node_id;",
            {"nid": node.node_id}
        )
        node_exists = exists_result is not None and exists_result.has_next()
        
        if not node_exists:
            # CREATE 新节点（Kuzu 被索引的列只能在 CREATE 时写入，不能 SET）
            params["emb"] = embedding
            params["beh_emb"] = beh_embedding
            query = """
                CREATE (m:Memory {
                    node_id: $nid, isolation_key: $ik, user_id: $uid,
                    agent_id: $sid, layer: $layer, content: $content,
                    content_type: $ctype, status: $status, version: $ver,
                    confidence: $conf, source_type: $stype,
                    emotional_valence: $eval, emotional_arousal: $earou,
                    specificity_score: $spec, rarity_score: $rar,
                    longtail_flag: $lt, meta_tags: $mtags,
                    source_session_id: $ssid, source_turn_index: $stidx,
                    temporal_anchor: $tanc, access_count: $ac,
                    created_at: $cat, valid_from: $vf, valid_until: $vu,
                    last_accessed_at: $laat,
                    previous_version_id: $pvid, superseded_by_id: $sbid,
                    change_reason: $cr, evidence_chain: $ec,
                    custom_json: $cust, tags: $tags, extra_json: $extra,
                    __EMBEDDING__: $emb, __BEH_EMBEDDING__: $beh_emb
                });
                """
            query = query.replace("__EMBEDDING__", self._embedding_property)
            query = query.replace("__BEH_EMBEDDING__", self._beh_embedding_property)
            self._execute(query, params)
        else:
            # UPDATE 已有节点（content embedding 有索引，不能 SET，见 update_embedding）
            update_sets = [
                "m.content = $content", "m.status = $status", "m.version = $ver",
                "m.confidence = $conf", "m.emotional_valence = $eval",
                "m.emotional_arousal = $earou", "m.meta_tags = $mtags",
                "m.access_count = $ac", "m.last_accessed_at = $laat",
                "m.valid_until = $vu", "m.superseded_by_id = $sbid",
                "m.change_reason = $cr", "m.tags = $tags",
            ]
            if beh_embedding is not None:
                params["beh_emb"] = beh_embedding
                update_sets.append(f"m.{self._beh_embedding_property} = $beh_emb")
            if embedding is not None:
                logger.debug(
                    f"Kuzu upsert: node {node.node_id} exists; content embedding "
                    f"ignored on MATCH (set at CREATE via _graph_embedding only)"
                )
            self._execute(
                f"""
                MATCH (m:Memory {{node_id: $nid}})
                SET {", ".join(update_sets)};
                """,
                params,
            )

        # 确保 HAS_MEMORY 边 (Kuzu 0.4.x 不支持 MERGE edge，用 CREATE)
        try:
            self._execute(
                """
                MATCH (u:User {isolation_key: $ik}), (m:Memory {node_id: $nid})
                CREATE (u)-[:HAS_MEMORY {created_at: $now}]->(m);
                """,
                {"ik": isolation_key, "nid": node.node_id, "now": now},
            )
        except Exception as e:
            logger.debug(f"HAS_MEMORY edge note: {e}")

        return node.node_id

    async def get_node(self, node_id: str) -> MemoryNode | None:
        """获取单个 Memory 节点"""
        if not self._available:
            return None
        result = self._execute(
            "MATCH (m:Memory {node_id: $nid}) RETURN m;",
            {"nid": node_id},
        )
        rows = []
        while result.has_next():
            rows.append(result.get_next())
        if not rows:
            return None
        return self._row_to_memory_node(rows[0][0])

    async def get_nodes_by_ids(self, node_ids: list[str]) -> list[MemoryNode]:
        """批量获取"""
        if not self._available or not node_ids:
            return []
        nodes = []
        for nid in node_ids:
            n = await self.get_node(nid)
            if n:
                nodes.append(n)
        return nodes

    async def get_all_nodes(
        self,
        isolation_key: str,
        layer: MemoryLayer | None = None,
        status: MemoryStatus | None = None,
        limit: int = 100,
    ) -> list[MemoryNode]:
        """按条件获取节点列表"""
        if not self._available:
            return []
        conditions = ["m.isolation_key = $ik"]
        params: dict[str, Any] = {"ik": isolation_key}

        if layer:
            conditions.append("m.layer = $layer")
            params["layer"] = layer.value
        if status:
            conditions.append("m.status = $status")
            params["status"] = status.value

        where = " AND ".join(conditions)
        # Kuzu 0.4.x 不支持参数化 LIMIT，使用字符串拼接
        query = f"MATCH (m:Memory) WHERE {where} RETURN m LIMIT {int(limit)};"
        result = self._execute(query, params)

        nodes = []
        while result.has_next():
            row = result.get_next()
            nodes.append(self._row_to_memory_node(row[0]))
        return nodes

    async def get_profile(self, isolation_key: str) -> MemoryNode | None:
        """获取 Profile 节点 (identity retired; profile lives in L0_BASIC_INFO VDB)."""
        return None

    async def update_node(self, node_id: str, updates: dict[str, Any]) -> bool:
        """更新节点属性"""
        if not self._available:
            return False
        if not updates:
            return True
        set_clauses = []
        params: dict[str, Any] = {"nid": node_id}
        for key, value in updates.items():
            safe_key = key.replace("-", "_")
            set_clauses.append(f"m.{safe_key} = ${safe_key}")
            params[safe_key] = value

        set_str = ", ".join(set_clauses)
        try:
            self._execute(
                f"MATCH (m:Memory {{node_id: $nid}}) SET {set_str};",
                params,
            )
            return True
        except Exception as e:
            logger.warning(f"update_node failed for {node_id}: {e}")
            return False

    async def delete_node(self, node_id: str) -> bool:
        """删除 Memory 节点及其关联边"""
        if not self._available:
            return False
        try:
            self._execute(
                "MATCH (m:Memory {node_id: $nid}) DETACH DELETE m;",
                {"nid": node_id},
            )
            return True
        except Exception as e:
            logger.warning(f"delete_node failed for {node_id}: {e}")
            return False

    async def delete_all_nodes(self, isolation_key: str) -> int:
        """删除某隔离键下所有 Memory 节点"""
        if not self._available:
            return 0
        try:
            # 先统计
            count_result = self._execute(
                "MATCH (m:Memory) WHERE m.isolation_key = $ik RETURN count(m);",
                {"ik": isolation_key},
            )
            count = 0
            while count_result.has_next():
                count = count_result.get_next()[0]

            self._execute(
                "MATCH (m:Memory) WHERE m.isolation_key = $ik DETACH DELETE m;",
                {"ik": isolation_key},
            )
            return count
        except Exception as e:
            logger.warning(f"delete_all_nodes failed for {isolation_key}: {e}")
            return 0

    async def delete_by_metadata(
        self,
        user_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """按 metadata 删除该用户范围内的图数据（Memory + User + Topic + 孤立 VdbRef）"""
        if not self._available:
            return 0
        try:
            conditions = ["m.user_id = $uid"]
            params: dict[str, Any] = {"uid": user_id}
            if agent_id is not None:
                conditions.append("m.agent_id = $aid")
                params["aid"] = agent_id
            if session_id is not None:
                ik = f"{user_id}::{agent_id or 'default_agent'}::{session_id}"
                conditions.append("m.isolation_key = $ik")
                params["ik"] = ik

            where = " AND ".join(conditions)
            total = 0

            count_result = self._execute(
                f"MATCH (m:Memory) WHERE {where} RETURN count(m);",
                params,
            )
            mem_count = 0
            while count_result.has_next():
                mem_count = count_result.get_next()[0]
            self._execute(
                f"MATCH (m:Memory) WHERE {where} DETACH DELETE m;",
                params,
            )
            total += mem_count

            # User 节点（按 user_id / agent_id / session 粒度）
            user_conds = ["u.user_id = $uid"]
            user_params: dict[str, Any] = {"uid": user_id}
            if session_id is not None:
                ik = f"{user_id}::{agent_id or 'default_agent'}::{session_id}"
                user_conds.append("u.isolation_key = $ik")
                user_params["ik"] = ik
            elif agent_id is not None:
                user_conds.append("u.agent_id = $aid")
                user_params["aid"] = agent_id
            user_where = " AND ".join(user_conds)
            self._execute(
                f"MATCH (u:User) WHERE {user_where} DETACH DELETE u;",
                user_params,
            )

            # Topic 节点（isolation_key 与 User 同格式）
            topic_conds = ["t.isolation_key STARTS WITH $pfx"]
            topic_params: dict[str, Any] = {"pfx": f"{user_id}::"}
            if agent_id is not None and session_id is None:
                topic_params["pfx"] = f"{user_id}::{agent_id}::"
            elif session_id is not None:
                topic_params["pfx"] = f"{user_id}::{agent_id or 'default_agent'}::{session_id}"
                topic_conds = ["t.isolation_key = $pfx"]
            topic_where = " AND ".join(topic_conds)
            self._execute(
                f"MATCH (t:Topic) WHERE {topic_where} DETACH DELETE t;",
                topic_params,
            )

            # 孤立 VdbRef（DERIVED_FROM 已随 Memory 删除）
            self._execute(
                "MATCH (v:VdbRef) "
                "OPTIONAL MATCH ()-[r:DERIVED_FROM]->(v) "
                "WITH v, count(r) AS cnt WHERE cnt = 0 "
                "DETACH DELETE v;",
                {},
            )

            logger.info(
                f"[graph-store] delete_by_metadata user={user_id} agent={agent_id} "
                f"session={session_id} memory_nodes={mem_count}"
            )
            return total
        except Exception as e:
            logger.warning(f"delete_by_metadata failed (user_id={user_id}): {e}")
            return 0

    # ================================================================
    # 边操作
    # ================================================================

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        properties: dict[str, Any] | None = None,
    ) -> bool:
        """添加 Memory→Memory 关系边 (RELATED_TO)，自动建双向边"""
        if not self._available:
            return False
        props = properties or {}
        now = datetime.now()

        edge_type_upper = edge_type.upper()
        if edge_type_upper != "RELATED_TO":
            logger.warning(f"Unsupported edge type: {edge_type}, only RELATED_TO is supported")
            return False

        params = {
            "src": source_id, "tgt": target_id,
            "rtype": props.get("relation_type", "related"),
            "w": props.get("weight", 1.0),
            "now": now,
        }

        try:
            # 正向 A→B
            self._execute(
                """
                MATCH (a:Memory {node_id: $src}), (b:Memory {node_id: $tgt})
                CREATE (a)-[:RELATED_TO {
                    relation_type: $rtype, weight: $w, created_at: $now
                }]->(b);
                """,
                params,
            )
            # 反向 B→A
            self._execute(
                """
                MATCH (a:Memory {node_id: $tgt}), (b:Memory {node_id: $src})
                CREATE (a)-[:RELATED_TO {
                    relation_type: $rtype, weight: $w, created_at: $now
                }]->(b);
                """,
                params,
            )
            return True
        except Exception as e:
            logger.warning(f"add_edge failed ({source_id})-[{edge_type}]->({target_id}): {e}")
            return False

    async def add_topic_tag(
        self,
        memory_node_id: str,
        topic_name: str,
        isolation_key: str,
        embed_service=None,
    ) -> str:
        """记忆节点关联主题 (Memory→Topic)"""
        if not self._available:
            return ""
        import uuid as _uuid
        topic_id = f"topic_{_uuid.uuid5(_uuid.NAMESPACE_URL, f'{isolation_key}:{topic_name}').hex[:12]}"
        now = datetime.now()

        try:
            # Kuzu 0.4.x 不支持 MERGE ... ON CREATE SET，改用 exists check + CREATE
            topic_result = self._execute(
                "MATCH (t:Topic {topic_id: $tid}) RETURN t.topic_id;",
                {"tid": topic_id}
            )
            topic_exists = topic_result is not None and topic_result.has_next()
            
            if not topic_exists:
                self._execute(
                    """
                    CREATE (t:Topic {topic_id: $tid, isolation_key: $ik, name: $name, created_at: $now});
                    """,
                    {"tid": topic_id, "ik": isolation_key, "name": topic_name, "now": now},
                )
            self._execute(
                """
                MATCH (m:Memory {node_id: $mid}), (t:Topic {topic_id: $tid})
                CREATE (m)-[:TAGGED_WITH {created_at: $now}]->(t);
                """,
                {"mid": memory_node_id, "tid": topic_id, "now": now},
            )
        except Exception as e:
            logger.debug(f"add_topic_tag note: {e}")

        return topic_id

    # ================================================================
    # V3: VdbRef (shadow node) + DERIVED_FROM edge
    # ================================================================

    async def ensure_vdbref(self, node_id: str, layer: str) -> None:
        """确保 VdbRef 影子节点存在"""
        if not self._available:
            return
        try:
            result = self._execute(
                "MATCH (v:VdbRef {node_id: $nid}) RETURN v.node_id;",
                {"nid": node_id}
            )
            if result is not None and result.has_next():
                return  # 已存在
            self._execute(
                "CREATE (v:VdbRef {node_id: $nid, layer: $layer});",
                {"nid": node_id, "layer": layer},
            )
        except Exception as e:
            logger.debug(f"ensure_vdbref note: {e}")

    async def add_derived_from(self, memory_node_id: str, vdbref_node_id: str) -> bool:
        """添加 DERIVED_FROM 边: Memory → VdbRef"""
        if not self._available:
            return False
        try:
            now = datetime.now()
            self._execute(
                """
                MATCH (m:Memory {node_id: $mid}), (v:VdbRef {node_id: $vid})
                CREATE (m)-[:DERIVED_FROM {created_at: $now}]->(v);
                """,
                {"mid": memory_node_id, "vid": vdbref_node_id, "now": now},
            )
            return True
        except Exception as e:
            logger.warning(f"add_derived_from failed ({memory_node_id})->({vdbref_node_id}): {e}")
            return False

    async def find_referencing_memories(
        self,
        vdb_node_ids: list[str],
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """反向查找: VDB node_id → 引用它的 Graph Memory 节点"""
        if not self._available or not vdb_node_ids:
            return []
        results = []
        for vdb_id in vdb_node_ids:
            try:
                res = self._execute(
                    f"""
                    MATCH (v:VdbRef {{node_id: $vid}})<-[:DERIVED_FROM]-(m:Memory)
                    WHERE m.status = 'active'
                    RETURN m.node_id, m.content, m.layer, m.confidence
                    LIMIT {int(limit)};
                    """,
                    {"vid": vdb_id},
                )
                while res is not None and res.has_next():
                    row = res.get_next()
                    results.append({
                        "node_id": row[0],
                        "content": row[1],
                        "layer": row[2],
                        "confidence": row[3],
                        "evidence_vdb_id": vdb_id,
                        "source": "reverse_evidence",
                    })
            except Exception as e:
                logger.debug(f"find_referencing_memories for {vdb_id}: {e}")
        return results[:limit]

    async def get_evidence_vdbrefs(self, memory_node_id: str) -> list[dict[str, Any]]:
        """获取一个 Graph Memory 的全部 DERIVED_FROM evidence VdbRef"""
        if not self._available:
            return []
        try:
            result = self._execute(
                """
                MATCH (m:Memory {node_id: $mid})-[:DERIVED_FROM]->(v:VdbRef)
                RETURN v.node_id, v.layer;
                """,
                {"mid": memory_node_id},
            )
            refs = []
            while result is not None and result.has_next():
                row = result.get_next()
                refs.append({"node_id": row[0], "layer": row[1]})
            return refs
        except Exception as e:
            logger.debug(f"get_evidence_vdbrefs for {memory_node_id}: {e}")
            return []

    # ================================================================
    # 图检索
    # ================================================================

    async def expand_from_anchors(
        self,
        anchor_ids: list[str],
        hop: int = 1,
        max_nodes: int = 50,
    ) -> list[dict[str, Any]]:
        """从锚点节点出发，沿图边扩展 N 跳"""
        if not self._available or not anchor_ids:
            return []

        expanded = []
        seen = set(anchor_ids)

        for anchor_id in anchor_ids:
            try:
                # 正向边
                result = self._execute(
                    f"""
                    MATCH (a:Memory {{node_id: $aid}})-[r]->(b:Memory)
                    WHERE b.status = 'active'
                    RETURN b.node_id, b.content, b.layer, b.confidence,
                           label(r) AS edge_type
                    LIMIT {int(max_nodes)};
                    """,
                    {"aid": anchor_id},
                )
                while result.has_next():
                    row = result.get_next()
                    nid = row[0]
                    if nid not in seen:
                        seen.add(nid)
                        expanded.append({
                            "node_id": nid,
                            "content": row[1],
                            "layer": row[2],
                            "confidence": row[3],
                            "edge_type": row[4],
                            "from_anchor": anchor_id,
                            "source": "graph_expand",
                        })

                # 反向边
                result2 = self._execute(
                    f"""
                    MATCH (b:Memory)-[r]->(a:Memory {{node_id: $aid}})
                    WHERE b.status = 'active'
                    RETURN b.node_id, b.content, b.layer, b.confidence,
                           label(r) AS edge_type
                    LIMIT {int(max_nodes)};
                    """,
                    {"aid": anchor_id},
                )
                while result2.has_next():
                    row = result2.get_next()
                    nid = row[0]
                    if nid not in seen:
                        seen.add(nid)
                        expanded.append({
                            "node_id": nid,
                            "content": row[1],
                            "layer": row[2],
                            "confidence": row[3],
                            "edge_type": row[4],
                            "from_anchor": anchor_id,
                            "source": "graph_expand",
                        })

            except Exception as e:
                logger.debug(f"expand_from_anchors for {anchor_id}: {e}")

            if len(expanded) >= max_nodes:
                break

        return expanded[:max_nodes]

    async def expand_with_tags(
        self,
        anchor_ids: list[str],
        hop: int = 2,
        max_nodes: int = 500,
        isolation_key: str = "",
    ) -> list[dict[str, Any]]:
        """BFS 展开：RELATED_TO + TAGGED_WITH(双向)，N 跳。"""
        if not self._available or not anchor_ids:
            return []

        seen = set(anchor_ids)
        all_expanded = []
        frontier = list(anchor_ids)

        ik_filter = f"AND m.isolation_key = '{isolation_key}'" if isolation_key else ""

        for current_hop in range(1, hop + 1):
            if not frontier or len(all_expanded) >= max_nodes:
                break

            next_frontier = []

            for aid in frontier:
                # --- RELATED_TO 邻居（双向）---
                try:
                    result = self._execute(
                        f"""
                        MATCH (a:Memory {{node_id: $aid}})-[:RELATED_TO]-(m:Memory)
                        WHERE m.status = 'active' {ik_filter}
                        RETURN DISTINCT m.node_id, m.content, m.layer, m.confidence
                        LIMIT {int(max_nodes)};
                        """,
                        {"aid": aid},
                    )
                    while result.has_next():
                        row = result.get_next()
                        nid = row[0]
                        if nid not in seen:
                            seen.add(nid)
                            all_expanded.append({
                                "node_id": nid,
                                "content": row[1],
                                "layer": row[2],
                                "confidence": row[3],
                                "hop": current_hop,
                                "source": "related_to",
                            })
                            next_frontier.append(nid)
                except Exception as e:
                    logger.debug(f"expand_with_tags RELATED_TO {aid}: {e}")

                # --- TAGGED_WITH 桥接 ---
                try:
                    result = self._execute(
                        f"""
                        MATCH (a:Memory {{node_id: $aid}})-[:TAGGED_WITH]->(t:Topic)<-[:TAGGED_WITH]-(m:Memory)
                        WHERE m.status = 'active' AND m.node_id <> $aid {ik_filter}
                        RETURN DISTINCT m.node_id, m.content, m.layer, m.confidence, t.name
                        LIMIT {int(max_nodes)};
                        """,
                        {"aid": aid},
                    )
                    while result.has_next():
                        row = result.get_next()
                        nid = row[0]
                        if nid not in seen:
                            seen.add(nid)
                            all_expanded.append({
                                "node_id": nid,
                                "content": row[1],
                                "layer": row[2],
                                "confidence": row[3],
                                "hop": current_hop,
                                "source": f"tag:{row[4]}",
                            })
                            next_frontier.append(nid)
                except Exception as e:
                    logger.debug(f"expand_with_tags TAGGED_WITH {aid}: {e}")

                if len(all_expanded) >= max_nodes:
                    break

            frontier = next_frontier

        return all_expanded[:max_nodes]

    # ================================================================
    # 向量检索 (HNSW)
    # ================================================================

    async def vector_search(
        self,
        query_embedding: list[float],
        isolation_key: str,
        layers: list[str] | None = None,
        limit: int = 10,
        score_threshold: float = 0.0,
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        """V_con 向量检索 Graph Memory 节点"""
        if not self._available or not query_embedding:
            return []

        # 确定过滤模式
        use_prefix = False
        if not isolation_key and user_id:
            use_prefix = True
            ik_prefix = user_id + "::"

        try:
            # Kuzu QUERY_VECTOR_INDEX 支持直接传参数化 list
            r = self._execute(
                f"CALL QUERY_VECTOR_INDEX('Memory', '{self._embedding_index}', "
                f"$vec, {int(limit * 3)}) "
                f"RETURN node.node_id, node.content, node.layer, node.confidence, "
                f"node.isolation_key, node.status, node.custom_json, distance;",
                {"vec": query_embedding},
            )
            results = []
            while r is not None and r.has_next():
                row = r.get_next()
                ik = row[4]
                status = row[5]
                layer_val = row[2]
                # 过滤：isolation_key（精确或前缀）+ status + layers
                if use_prefix:
                    if not ik or not ik.startswith(ik_prefix):
                        continue
                else:
                    if ik != isolation_key or status != "active":
                        continue
                if status != "active":
                    continue
                if layers and layer_val not in layers:
                    continue
                score = 1.0 - row[7]  # cosine distance → similarity
                if score < score_threshold:
                    continue
                results.append({
                    "node_id": row[0],
                    "content": row[1],
                    "layer": layer_val,
                    "confidence": row[3],
                    "score": score,
                    "custom_json": row[6],
                    "source": "graph_vector_search",
                })
                if len(results) >= limit:
                    break
            return results
        except Exception as e:
            logger.warning(f"vector_search failed: {e}")
            return []

    async def beh_vector_search(
        self,
        query_beh_embedding: list[float],
        isolation_key: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """V_beh 向量检索 L6 basic 节点 (sweeper 用)

        beh_embedding 不建 HNSW 索引（Kuzu 限制：被索引列不能 SET），
        所以用全量扫描 + 内存 cosine 计算。N < 100 时毫秒级。
        """
        if not self._available or not query_beh_embedding:
            return []

        try:
            import numpy as np

            # 全量取 L6 basic 的 beh_embedding + embedding
            r = self._execute(
                "MATCH (m:Memory) WHERE m.isolation_key = $ik "
                "AND m.layer = 'l6_schema' AND m.status = 'active' "
                f"AND m.{self._beh_embedding_property} IS NOT NULL "
                f"RETURN m.node_id, m.content, m.{self._embedding_property}, "
                f"m.{self._beh_embedding_property};",
                {"ik": isolation_key},
            )
            candidates = []
            while r is not None and r.has_next():
                row = r.get_next()
                candidates.append({
                    "node_id": row[0],
                    "content": row[1],
                    "embedding": row[2],
                    "beh_embedding": row[3],
                })

            if not candidates:
                return []

            # 内存 cosine 计算
            q_vec = np.array(query_beh_embedding, dtype=np.float32)
            q_norm = q_vec / (np.linalg.norm(q_vec) + 1e-10)

            scored = []
            for c in candidates:
                beh = np.array(c["beh_embedding"], dtype=np.float32)
                beh_norm = beh / (np.linalg.norm(beh) + 1e-10)
                beh_sim = float(np.dot(q_norm, beh_norm))
                scored.append((beh_sim, c))

            # 按 beh_sim 降序，取 topk
            scored.sort(key=lambda x: x[0], reverse=True)
            results = []
            for beh_sim, c in scored[:limit]:
                results.append({
                    "node_id": c["node_id"],
                    "content": c["content"],
                    "embedding": c["embedding"],
                    "beh_distance": 1.0 - beh_sim,
                })
            return results
        except Exception as e:
            logger.warning(f"beh_vector_search failed: {e}")
            return []

    async def update_embedding(
        self,
        node_id: str,
        embedding: list[float] | None = None,
        beh_embedding: list[float] | None = None,
    ) -> bool:
        """更新节点的 embedding / beh_embedding 向量属性

        Kuzu 限制：被 HNSW 索引的列（embedding）不能 SET。
        - beh_embedding：无索引，可直接 SET
        - embedding：有索引，需在 CREATE 时写入（不支持后续更新）
          如果确实需要更新 embedding，调用方应 delete + upsert（带新 embedding）
        """
        if not self._available:
            return False
        try:
            if beh_embedding is not None:
                self._execute(
                    f"MATCH (m:Memory {{node_id: $nid}}) "
                    f"SET m.{self._beh_embedding_property} = $vec;",
                    {"nid": node_id, "vec": beh_embedding},
                )
            if embedding is not None:
                # Kuzu 被索引列不能 SET；应在 upsert CREATE 时传 _graph_embedding
                logger.debug(
                    f"Kuzu: skip SET indexed column 'embedding' for {node_id} "
                    f"(use upsert_memory_node with _graph_embedding on CREATE)"
                )
                if beh_embedding is None:
                    return False
            return True
        except Exception as e:
            logger.warning(f"update_embedding failed for {node_id}: {e}")
            return False

    # ================================================================
    # 跨域归纳 (Cross-Domain)
    # ================================================================

    async def add_cross_abstracts_to(self, basic_id: str, core_id: str) -> bool:
        """添加 CROSS_ABSTRACTS_TO 边: L6 basic → L6 core (单向)"""
        if not self._available:
            return False
        try:
            now = datetime.now()
            self._execute(
                """
                MATCH (a:Memory {node_id: $src}), (b:Memory {node_id: $tgt})
                CREATE (a)-[:CROSS_ABSTRACTS_TO {created_at: $now}]->(b);
                """,
                {"src": basic_id, "tgt": core_id, "now": now},
            )
            return True
        except Exception as e:
            logger.warning(f"add_cross_abstracts_to failed ({basic_id})->({core_id}): {e}")
            return False

    async def find_cores_from_basics(
        self,
        basic_ids: list[str],
        max_hops: int = 2,
    ) -> list[dict[str, Any]]:
        """从 L6 basic 出发，沿 RELATED_TO / CROSS_ABSTRACTS_TO 找 L6 core"""
        if not self._available or not basic_ids:
            return []

        seen = set()
        results = []
        for nid in basic_ids:
            try:
                r = self._execute(
                    f"""
                    MATCH (start:Memory {{node_id: $nid}})
                          -[:CROSS_ABSTRACTS_TO*1..{int(max_hops)}]->
                          (core:Memory)
                    WHERE core.status = 'active'
                    RETURN DISTINCT core.node_id, core.content, core.confidence;
                    """,
                    {"nid": nid},
                )
                while r is not None and r.has_next():
                    row = r.get_next()
                    cid = row[0]
                    if cid not in seen:
                        seen.add(cid)
                        results.append({
                            "node_id": cid,
                            "content": row[1],
                            "confidence": row[2],
                        })
            except Exception as e:
                logger.debug(f"find_cores_from_basics for {nid}: {e}")
        return results

    async def get_cross_abstracts_targets(self, basic_id: str) -> list[str]:
        """查询某个 L6 basic 已有的 CROSS_ABSTRACTS_TO → core node_id 列表"""
        if not self._available:
            return []
        try:
            r = self._execute(
                """
                MATCH (a:Memory {node_id: $nid})-[:CROSS_ABSTRACTS_TO]->(b:Memory)
                RETURN b.node_id;
                """,
                {"nid": basic_id},
            )
            ids = []
            while r is not None and r.has_next():
                ids.append(r.get_next()[0])
            return ids
        except Exception as e:
            logger.debug(f"get_cross_abstracts_targets for {basic_id}: {e}")
            return []

    # ================================================================
    # 统计
    # ================================================================

    async def get_stats(self) -> dict[str, Any]:
        """获取图统计"""
        if not self._available:
            return {"backend": "kuzu", "available": False, "reason": "file lock not acquired"}
        try:
            mem_count = self._execute("MATCH (m:Memory) RETURN count(m);")
            user_count = self._execute("MATCH (u:User) RETURN count(u);")

            mc = 0
            while mem_count.has_next():
                mc = mem_count.get_next()[0]
            uc = 0
            while user_count.has_next():
                uc = user_count.get_next()[0]

            return {
                "backend": "kuzu",
                "db_path": self._db_path,
                "memory_nodes": mc,
                "user_nodes": uc,
                "vector_schema_compatible": self._vector_schema_compatible,
                "embedding_dims": self._embedding_dims,
                "embedding_property": self._embedding_property,
                "embedding_index": self._embedding_index,
                "legacy_embedding_dims": self._legacy_embedding_dims,
            }
        except Exception as e:
            return {"backend": "kuzu", "error": str(e)}

    def _checkpoint(self) -> None:
        """Flush WAL to main DB file. Best-effort — never crash."""
        if not self._available or self._conn is None:
            return
        try:
            self._conn.execute("CHECKPOINT;")
        except Exception as e:
            logger.warning(f"Kuzu checkpoint failed: {e}")

    async def close(self) -> None:
        """Flush WAL and release the file lock."""
        if self._conn is not None:
            try:
                self._conn.execute("CHECKPOINT;")
            except Exception as e:
                logger.warning(f"Kuzu checkpoint on close failed: {e}")
        if self._db is not None:
            try:
                self._db.close()
            except Exception as e:
                logger.warning(f"Kuzu db.close failed: {e}")
        self._conn = None
        self._db = None
        self._available = False
        logger.info("GraphStore (Kuzu) closed")

    # ================================================================
    # 内部工具
    # ================================================================

    @staticmethod
    def _row_to_memory_node(row_data: Any) -> MemoryNode:
        """将 Kuzu 查询结果行转为 MemoryNode"""
        if isinstance(row_data, dict):
            d = row_data
        else:
            # Kuzu Node 对象有 dict-like 接口
            d = dict(row_data) if hasattr(row_data, '__iter__') else {}

        # 解析 JSON 字段
        meta_tags_raw = d.get("meta_tags", "[]")
        if isinstance(meta_tags_raw, str):
            try:
                meta_tags_raw = json.loads(meta_tags_raw)
            except (json.JSONDecodeError, TypeError):
                meta_tags_raw = []

        evidence_chain = d.get("evidence_chain", "[]")
        if isinstance(evidence_chain, str):
            try:
                evidence_chain = json.loads(evidence_chain)
            except (json.JSONDecodeError, TypeError):
                evidence_chain = []

        custom = d.get("custom_json", "{}")
        if isinstance(custom, str):
            try:
                custom = json.loads(custom)
            except (json.JSONDecodeError, TypeError):
                custom = {}

        tags = d.get("tags", "[]")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []

        return MemoryNode.from_dict({
            "node_id": d.get("node_id", ""),
            "user_id": d.get("user_id", ""),
            "agent_id": d.get("agent_id", ""),
            "layer": d.get("layer", "l1_raw"),
            "content": d.get("content", ""),
            "content_type": d.get("content_type", "raw"),
            "status": d.get("status", "active"),
            "version": d.get("version", 1),
            "confidence": d.get("confidence", 1.0),
            "source_type": d.get("source_type", "explicit"),
            "emotional_valence": d.get("emotional_valence", 0.0),
            "emotional_arousal": d.get("emotional_arousal", 0.0),
            "specificity_score": d.get("specificity_score", 0.0),
            "rarity_score": d.get("rarity_score", 0.0),
            "longtail_flag": d.get("longtail_flag", False),
            "meta_tags": meta_tags_raw,
            "source_session_id": d.get("source_session_id", ""),
            "source_turn_index": d.get("source_turn_index"),
            "temporal_anchor": d.get("temporal_anchor"),
            "access_count": d.get("access_count", 0),
            "created_at": d.get("created_at"),
            "valid_from": d.get("valid_from"),
            "valid_until": d.get("valid_until"),
            "last_accessed_at": d.get("last_accessed_at"),
            "previous_version_id": d.get("previous_version_id"),
            "superseded_by_id": d.get("superseded_by_id"),
            "change_reason": d.get("change_reason"),
            "evidence_chain": evidence_chain,
            "custom": custom,
            "tags": tags,
        })
