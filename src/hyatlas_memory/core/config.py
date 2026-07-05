"""
Agent Memory - 配置管理

提供灵活的配置选项，支持环境变量和配置文件
"""

import os
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from pathlib import Path


def _load_dotenv():
    """加载 .env 文件（如果存在），向上逐级查找"""
    # 从当前文件向上逐级查找 .env
    cur = Path(__file__).resolve().parent
    env_path = None
    for _ in range(5):
        candidate = cur / ".env"
        if candidate.exists():
            env_path = candidate
            break
        cur = cur.parent
    if env_path is None:
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # 不覆盖已存在的环境变量
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _get_env_float(key: str, default: float) -> float:
    """获取浮点型环境变量"""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_env_int(key: str, default: int) -> int:
    """获取整型环境变量"""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool) -> bool:
    """获取布尔型环境变量"""
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


def _default_data_dir() -> str:
    """
    返回数据根目录。

    优先级:
      1. 环境变量 MEMORY_DATA_DIR（云端部署时指定挂载路径，如 /data/memory）
      2. ~/.hy_memory（本地 pip install 用户的默认路径）

    所有本地存储（向量数据库、图数据库、SQLite、日志）均基于此目录派生。
    当配置了远程服务（如 Qdrant host、Neo4j url）时，对应的本地路径不会被使用。
    """
    return os.getenv("MEMORY_DATA_DIR", os.path.join(Path.home(), ".hy_memory"))


@dataclass
class VectorStoreConfig:
    """向量存储配置"""

    provider: str = None  # qdrant, chroma, faiss, tencent
    collection_name: str = None  # 集合名称
    persist_directory: str = None  # 持久化目录
    embedding_dims: int = None  # 向量维度
    on_disk: bool = True  # 是否持久化

    # 连接配置（远程存储）
    host: Optional[str] = None
    port: Optional[int] = None
    api_key: Optional[str] = None

    # 腾讯云向量数据库扩展
    url: Optional[str] = None  # 连接地址（tencent provider）
    username: Optional[str] = None  # 用户名（tencent provider）
    database_name: Optional[str] = None  # 数据库名（tencent provider）

    def __post_init__(self):
        if self.provider is None:
            self.provider = os.getenv("MEMORY_VECTOR_STORE", "chroma")
        if self.collection_name is None:
            self.collection_name = os.getenv("MEMORY_COLLECTION_NAME", "agent_memories")
        if self.persist_directory is None:
            self.persist_directory = os.getenv(
                "MEMORY_PERSIST_DIR",
                os.path.join(_default_data_dir(), "data", "vector_db"),
            )
        if self.embedding_dims is None:
            self.embedding_dims = _get_env_int("MEMORY_EMBEDDING_DIMS", 1536)
        if self.host is None:
            self.host = os.getenv("MEMORY_VECTOR_HOST")
        if self.port is None:
            port_str = os.getenv("MEMORY_VECTOR_PORT")
            self.port = int(port_str) if port_str else None
        if self.api_key is None:
            self.api_key = os.getenv("MEMORY_VECTOR_API_KEY")
        # 腾讯云向量数据库
        if self.url is None:
            self.url = os.getenv("MEMORY_TENCENT_VDB_URL")
        if self.username is None:
            self.username = os.getenv("MEMORY_TENCENT_VDB_USERNAME", "root")
        if self.database_name is None:
            self.database_name = os.getenv("MEMORY_TENCENT_VDB_DATABASE", "hy_memory")


@dataclass
class LLMConfig:
    """LLM 配置（用于记忆提取和处理）"""

    provider: str = None  # openai | eval_platform
    model: str = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None

    # 生成参数
    temperature: float = None
    max_tokens: int = None  # 默认 max_tokens（通用）
    agent_max_tokens: int = None  # Agent 场景统一 max_tokens

    # 调用参数
    timeout: int = None  # LLM 请求超时（秒）
    max_retries: int = None  # 最大重试次数
    retry_delay: float = None  # 重试间隔（秒）

    # 功能开关
    enable_summary: bool = None  # 是否生成 L3_SUMMARY（默认关闭；可通过 LLMConfig(enable_summary=True) 或 client.add(enable_summary=True) 开启）
    few_shot_enabled: bool = None  # 是否在 extractor / reconcile prompt 中附加 few-shot 示例（默认关闭，env: MEMORY_FEW_SHOT_ENABLED）
    extract_scene: str = None  # 抽取场景：'chat'（默认，实时对话提取）| 'migration'（从已沉淀记忆迁移抽取，保真优先）。env: MEMORY_EXTRACT_SCENE

    # 内部平台扩展（extra_headers / extra_body 会透传给 OpenAI client）
    extra_headers: Optional[Dict[str, str]] = None
    extra_body: Optional[Dict[str, Any]] = None

    # 蒸馏平台专用
    eval_user: Optional[str] = None
    eval_apikey: Optional[str] = None

    def __post_init__(self):
        if self.provider is None:
            self.provider = os.getenv("MEMORY_LLM_PROVIDER", "openai")
        if self.model is None:
            self.model = os.getenv("MEMORY_LLM_MODEL", "gpt-4.1-nano")
        if self.api_key is None:
            self.api_key = os.getenv(
                "MEMORY_LLM_API_KEY",
                os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY")),
            )
        if self.base_url is None:
            self.base_url = os.getenv("MEMORY_LLM_BASE_URL", os.getenv("LLM_BASE_URL"))
        if self.temperature is None:
            self.temperature = _get_env_float("MEMORY_LLM_TEMPERATURE", 0.1)
        if self.max_tokens is None:
            self.max_tokens = _get_env_int("MEMORY_LLM_MAX_TOKENS", 1024)
        if self.agent_max_tokens is None:
            self.agent_max_tokens = _get_env_int("MEMORY_AGENT_MAX_TOKENS", 2000)
        if self.eval_user is None:
            self.eval_user = os.getenv("MEMORY_LLM_EVAL_USER")
        if self.eval_apikey is None:
            self.eval_apikey = os.getenv("MEMORY_LLM_EVAL_APIKEY")
        if self.timeout is None:
            self.timeout = _get_env_int("MEMORY_LLM_TIMEOUT", 180)
        if self.max_retries is None:
            self.max_retries = _get_env_int("MEMORY_LLM_MAX_RETRIES", 5)
        if self.retry_delay is None:
            self.retry_delay = _get_env_float("MEMORY_LLM_RETRY_DELAY", 1.0)
        if self.enable_summary is None:
            # 默认关闭 summary；如需开启，请通过 LLMConfig(enable_summary=True)
            # 或 client.add(..., enable_summary=True) 显式开启。
            self.enable_summary = False
        if self.few_shot_enabled is None:
            # 默认关闭 few-shot；开启后 extractor / reconcile prompt 会附加示例。
            self.few_shot_enabled = _get_env_bool("MEMORY_FEW_SHOT_ENABLED", False)
        if self.extract_scene is None:
            # 默认 'chat'（实时对话提取，行为与历史版本完全一致）。
            # 设为 'migration' 时，extractor 改用迁移专用 prompt（保真优先、原子化、
            # 不做价值过滤），用于把用户已沉淀的历史记忆迁移进来；不影响默认对话提取。
            self.extract_scene = (
                os.getenv("MEMORY_EXTRACT_SCENE", "chat").strip().lower() or "chat"
            )


@dataclass
class EmbedderConfig:
    """Embedder 配置（统一走 OpenAI 兼容协议）"""

    provider: str = None  # openai（兼容 DashScope、DeepSeek 等）
    model: str = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    embedding_dims: int = None

    # 重试参数
    max_retries: int = None  # 最大重试次数
    retry_delay: float = None  # 重试间隔（秒）
    timeout: int = None  # 请求超时（秒）

    # 内部平台扩展（extra_headers / extra_body 会透传给 OpenAI client）
    extra_headers: Optional[Dict[str, str]] = None
    extra_body: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.provider is None:
            self.provider = os.getenv("MEMORY_EMBEDDER_PROVIDER", "openai")
        if self.model is None:
            self.model = os.getenv("MEMORY_EMBEDDER_MODEL", "text-embedding-3-small")
        if self.api_key is None:
            self.api_key = os.getenv(
                "MEMORY_EMBEDDER_API_KEY",
                os.getenv("EMBEDDING_API_KEY", os.getenv("OPENAI_API_KEY")),
            )
        if self.base_url is None:
            self.base_url = os.getenv(
                "MEMORY_EMBEDDER_BASE_URL", os.getenv("EMBEDDING_BASE_URL")
            )
        if self.embedding_dims is None:
            val = os.getenv("MEMORY_EMBEDDING_DIMS")
            self.embedding_dims = int(val) if val else None
        if self.max_retries is None:
            self.max_retries = _get_env_int("MEMORY_EMBEDDER_MAX_RETRIES", 3)
        if self.retry_delay is None:
            self.retry_delay = _get_env_float("MEMORY_EMBEDDER_RETRY_DELAY", 2.0)
        if self.timeout is None:
            self.timeout = _get_env_int("MEMORY_EMBEDDER_TIMEOUT", 60)
        if self.extra_headers is None:
            val = os.getenv("MEMORY_EMBEDDER_EXTRA_HEADERS")
            if val:
                import json as _json

                try:
                    self.extra_headers = _json.loads(val)
                except Exception:
                    pass
        if self.extra_body is None:
            val = os.getenv("MEMORY_EMBEDDER_EXTRA_BODY")
            if val:
                import json as _json

                try:
                    self.extra_body = _json.loads(val)
                except Exception:
                    pass


@dataclass
class RecallConfig:
    """召回配置"""

    # 默认召回数量
    default_limit: int = 10
    max_limit: int = 100
    default_limit_per_layer: int = 5

    # 评分权重
    semantic_weight: float = 0.5
    recency_weight: float = 0.3
    importance_weight: float = 0.15
    access_weight: float = 0.05

    # 时间衰减
    recency_decay_days: int = 30  # 衰减周期（天）
    recency_decay_factor: float = 0.9  # 衰减因子

    # Memory Strength（基于闲置时长的时间衰减排序）
    # strength = (1 + log(access_count)) * exp(-idle_days / tau)，乘进 normal 通道分数
    strength_enabled: bool = False        # 总开关（默认关闭）：关闭时不做 strength 重排，也不回写 access
    strength_tau: float = 180.0           # 衰减时间常数（天）
    access_tracking_enabled: bool = True  # 搜索后是否回写 access_count / last_accessed_at（受总开关约束）

    # Entity Store（对齐 mem0：独立 {collection}_entities collection + entity boost）
    # 此开关**只控制写入侧**：开启后写入 L2_FACT 落库时自动抽 entity 刷 store。
    # reader_mem0 的 entity boost 不受此开关控制——总是自动尝试（对齐 mem0），
    # 靠 entity store 里有没有数据自然决定是否生效。
    entity_store_enabled: bool = False    # 写入侧开关（默认关闭），env: MEMORY_ENTITY_STORE_ENABLED

    # 最小分数阈值
    min_score_threshold: float = 0.0

    def __post_init__(self):
        self.default_limit = _get_env_int("MEMORY_RECALL_DEFAULT_LIMIT", 10)
        self.max_limit = _get_env_int("MEMORY_RECALL_MAX_LIMIT", 100)
        self.semantic_weight = _get_env_float("MEMORY_RECALL_SEMANTIC_WEIGHT", 0.5)
        self.recency_weight = _get_env_float("MEMORY_RECALL_RECENCY_WEIGHT", 0.3)
        self.importance_weight = _get_env_float("MEMORY_RECALL_IMPORTANCE_WEIGHT", 0.15)
        self.access_weight = _get_env_float("MEMORY_RECALL_ACCESS_WEIGHT", 0.05)
        self.recency_decay_days = _get_env_int("MEMORY_RECALL_DECAY_DAYS", 30)
        self.strength_enabled = _get_env_bool("MEMORY_RECALL_STRENGTH_ENABLED", False)
        self.strength_tau = _get_env_float("MEMORY_RECALL_STRENGTH_TAU", 180.0)
        self.access_tracking_enabled = _get_env_bool("MEMORY_RECALL_ACCESS_TRACKING", True)
        self.entity_store_enabled = _get_env_bool("MEMORY_ENTITY_STORE_ENABLED", False)


@dataclass
class TimeWindowConfig:
    """时间窗口配置"""

    # 滚动删除
    enable_rolling_delete: bool = False
    rolling_window_days: int = 90  # 滚动窗口（天）
    rolling_check_interval: int = 3600  # 检查间隔（秒）

    # 按层配置不同的窗口
    layer_windows: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.enable_rolling_delete = _get_env_bool("MEMORY_ROLLING_DELETE", False)
        self.rolling_window_days = _get_env_int("MEMORY_ROLLING_WINDOW_DAYS", 90)

        # 默认各层窗口
        if not self.layer_windows:
            self.layer_windows = {
                "raw": _get_env_int("MEMORY_RAW_WINDOW_DAYS", 30),
                "dialogue": _get_env_int("MEMORY_DIALOGUE_WINDOW_DAYS", 60),
                "summary": _get_env_int("MEMORY_SUMMARY_WINDOW_DAYS", 180),
                "profile": _get_env_int("MEMORY_PROFILE_WINDOW_DAYS", 365),
                "knowledge": _get_env_int("MEMORY_KNOWLEDGE_WINDOW_DAYS", 365),
            }


@dataclass
class ExtractorConfig:
    """记忆提取器配置"""

    # 自动路由
    enable_auto_routing: bool = True

    # 自动提取
    enable_auto_extract: bool = False
    extract_entities: bool = True  # 提取实体
    extract_summary: bool = True  # 提取摘要
    extract_profile: bool = True  # 提取画像

    # 提取阈值
    min_content_length: int = 10  # 最小内容长度
    summary_trigger_length: int = 500  # 触发摘要的长度

    def __post_init__(self):
        self.enable_auto_routing = _get_env_bool("MEMORY_AUTO_ROUTING", True)
        self.enable_auto_extract = _get_env_bool("MEMORY_AUTO_EXTRACT", False)
        self.min_content_length = _get_env_int("MEMORY_MIN_CONTENT_LENGTH", 10)


@dataclass
class GraphStoreConfig:
    """图存储配置 (Kuzu / Neo4j / Memgraph)"""

    provider: str = None  # "kuzu" | "neo4j" | "memgraph"
    db_path: str = None  # Kuzu 数据库路径
    url: str = None  # Neo4j/Memgraph bolt URL
    username: str = None  # Neo4j/Memgraph 用户名
    password: str = None  # Neo4j/Memgraph 密码
    database: str = None  # Neo4j 数据库名

    def __post_init__(self):
        if self.provider is None:
            self.provider = os.getenv("MEMORY_GRAPH_PROVIDER", "kuzu")
        if self.db_path is None:
            self.db_path = os.getenv(
                "MEMORY_GRAPH_DB_PATH",
                os.path.join(_default_data_dir(), "data", "kuzu_db"),
            )
        if self.url is None:
            self.url = os.getenv(
                "NEO4J_URL", os.getenv("MEMGRAPH_URL", "bolt://localhost:7687")
            )
        if self.username is None:
            self.username = os.getenv(
                "NEO4J_USERNAME", os.getenv("MEMGRAPH_USERNAME", "")
            )
        if self.password is None:
            self.password = os.getenv(
                "NEO4J_PASSWORD", os.getenv("MEMGRAPH_PASSWORD", "")
            )
        if self.database is None:
            self.database = os.getenv("NEO4J_DATABASE", "neo4j")


@dataclass
class CacheConfig:
    """
    缓存后端配置

    支持的后端:
    - "sqlite"   : SQLite 本地数据库（默认，零依赖）
    - "mysql"    : 腾讯云 MySQL (CDB) 分布式持久化（需 aiomysql 包）

    细粒度控制：可以启用/禁用特定的缓存功能
    - enable_profile_cache   : Profile 热点缓存（读优化）
    - enable_system2_queue   : System2 任务队列（后台任务）
    - enable_intention_cache : Intention 队列（触发器）
    - enable_write_records   : Write record（状态查询）
    """

    backend: str = None  # "sqlite" | "mysql"
    db_path: str = None  # SQLite 路径

    # MySQL 连接参数（backend="mysql" 时使用）
    mysql_host: str = None
    mysql_port: int = None
    mysql_user: str = None
    mysql_password: str = None
    mysql_database: str = None
    mysql_pool_size: int = None
    mysql_pool_recycle: int = None

    # 细粒度缓存控制（所有后端均支持）
    enable_profile_cache: bool = None  # Profile 缓存
    enable_system2_queue: bool = None  # System2 任务队列
    enable_intention_cache: bool = None  # Intention 队列
    enable_write_records: bool = None  # Write 记录

    def __post_init__(self):
        if self.backend is None:
            self.backend = os.getenv("MEMORY_CACHE_BACKEND", "sqlite")
        backend_norm = str(self.backend).lower().strip()
        if backend_norm not in ("sqlite", "mysql"):
            raise ValueError(
                f"Unsupported cache backend {self.backend!r}; "
                f"must be one of: sqlite / mysql"
            )
        self.backend = backend_norm
        if self.db_path is None:
            self.db_path = os.getenv(
                "MEMORY_CACHE_DB_PATH",
                os.path.join(_default_data_dir(), "data", "cache.db"),
            )

        # MySQL 连接参数
        if self.mysql_host is None:
            self.mysql_host = os.getenv("MEMORY_MYSQL_HOST", "localhost")
        if self.mysql_port is None:
            self.mysql_port = _get_env_int("MEMORY_MYSQL_PORT", 3306)
        if self.mysql_user is None:
            self.mysql_user = os.getenv("MEMORY_MYSQL_USER", "root")
        if self.mysql_password is None:
            self.mysql_password = os.getenv("MEMORY_MYSQL_PASSWORD", "")
        if self.mysql_database is None:
            self.mysql_database = os.getenv("MEMORY_MYSQL_DATABASE", "hy_memory")
        if self.mysql_pool_size is None:
            self.mysql_pool_size = _get_env_int("MEMORY_MYSQL_POOL_SIZE", 10)
        if self.mysql_pool_recycle is None:
            self.mysql_pool_recycle = _get_env_int("MEMORY_MYSQL_POOL_RECYCLE", 3600)

        # 细粒度缓存控制 - 默认均为 True（启用所有功能）
        if self.enable_profile_cache is None:
            self.enable_profile_cache = _get_env_bool(
                "MEMORY_CACHE_ENABLE_PROFILE", True
            )
        if self.enable_system2_queue is None:
            self.enable_system2_queue = _get_env_bool(
                "MEMORY_CACHE_ENABLE_SYSTEM2_QUEUE", True
            )
        if self.enable_intention_cache is None:
            self.enable_intention_cache = _get_env_bool(
                "MEMORY_CACHE_ENABLE_INTENTION_CACHE", True
            )
        if self.enable_write_records is None:
            self.enable_write_records = _get_env_bool(
                "MEMORY_CACHE_ENABLE_WRITE_RECORDS", True
            )


@dataclass
class APIConfig:
    """API 配置"""

    host: str = "0.0.0.0"
    port: int = 8000
    prefix: str = "/api/v1/memory"

    # 认证
    enable_auth: bool = False
    api_keys: List[str] = field(default_factory=list)

    # 限流
    enable_rate_limit: bool = False
    rate_limit_per_minute: int = 100

    # CORS
    enable_cors: bool = True
    cors_origins: List[str] = field(default_factory=lambda: ["*"])

    def __post_init__(self):
        self.host = os.getenv("MEMORY_API_HOST", "0.0.0.0")
        self.port = _get_env_int("MEMORY_API_PORT", 8000)
        self.prefix = os.getenv("MEMORY_API_PREFIX", "/api/v1/memory")
        self.enable_auth = _get_env_bool("MEMORY_API_AUTH", False)

        # API Keys
        api_keys_str = os.getenv("MEMORY_API_KEYS", "")
        if api_keys_str:
            self.api_keys = [k.strip() for k in api_keys_str.split(",")]


@dataclass
class HistoryConfig:
    """历史记录配置（SQLite 审计追踪）"""

    enable: bool = None  # 是否启用历史记录
    db_path: str = None  # SQLite 数据库路径
    record_searches: bool = None  # 是否记录搜索操作

    def __post_init__(self):
        if self.enable is None:
            self.enable = _get_env_bool("MEMORY_HISTORY_ENABLE", True)
        if self.db_path is None:
            self.db_path = os.getenv(
                "MEMORY_HISTORY_DB_PATH",
                os.path.join(_default_data_dir(), "data", "history.db"),
            )
        if self.record_searches is None:
            self.record_searches = _get_env_bool(
                "MEMORY_HISTORY_RECORD_SEARCHES", False
            )


@dataclass
class CodingConfig:
    """
    Coding Memory 配置（生产力 / 编码场景独立链路）

    详见 docs/coding_memory_mvp_design.md。
    """

    enable: bool = None  # 是否启用 coding 链路（默认 True；关闭后所有 add 走 chat 链）
    db_path: str = None  # coding_memory.db 路径
    tool_result_max_bytes: int = None  # 单条 tool_result 截断字节上限
    writer: str = None  # "legacy" | "agent"；默认 "legacy"，env: MEMORY_CODING_WRITER_KIND

    def __post_init__(self):
        if self.enable is None:
            self.enable = _get_env_bool("MEMORY_CODING_ENABLED", True)
        if self.db_path is None:
            self.db_path = os.getenv(
                "MEMORY_CODING_DB_PATH",
                os.path.join(_default_data_dir(), "data", "coding_memory.db"),
            )
        if self.tool_result_max_bytes is None:
            self.tool_result_max_bytes = _get_env_int(
                "MEMORY_CODING_TOOL_RESULT_MAX_BYTES", 2048
            )
        if self.writer is None:
            # 默认 legacy（保持向后兼容）；agent = CodingCurator
            val = (os.getenv("MEMORY_CODING_WRITER_KIND") or "legacy").strip().lower()
            if val not in ("legacy", "agent"):
                val = "legacy"
            self.writer = val


@dataclass
class BasicProfileConfig:
    """
    用户基础画像（L0_BASIC_INFO）的字段 schema 配置。

    设计原则：
    - 不再使用 LLM function-calling tool（会让弱模型乱填字段）。
    - extractor 直接把 fields 字段表 format 到 EXTRACT prompt 里，要求 LLM
      在 JSON 输出中以 `basic_info` 字段返回（仅当对话明确出现时才填）。
    - 不同 client 可以配置不同的 schema（例如车机场景关注 vehicle_model，
      医疗场景关注 medical_history），SDK 默认提供 5 个通用字段。
    - 如果配置为空（fields == {}），SDK 回退到 default_factory 给出的默认 5 字段。

    用法：
        config.basic_profile.fields["hobby"] = "User's main hobby."
        # 或整体替换
        config.basic_profile.fields = {
            "name": "User's full or preferred name.",
            "vehicle_model": "User's primary vehicle model.",
        }
    """

    fields: Dict[str, str] = field(
        default_factory=lambda: {
            "name": "User's full or preferred name.",
            "age": "User's age in years (integer).",
            "location": "User's primary city or region of residence.",
            "occupation": "User's job title or role.",
            "employer": "User's employer / company name.",
        }
    )

    def effective_fields(self) -> Dict[str, str]:
        """配置为空时回落到默认；否则用配置值。"""
        if not self.fields:
            return BasicProfileConfig().fields
        return dict(self.fields)


@dataclass
class PipelineRouteConfig:
    """
    Pipeline 路由配置

    控制不同业务方/请求使用哪个版本的 Pipeline (Lite, Pro, ...)。
    """

    # 默认 Pipeline 版本
    default_version: str = None

    # 业务方 → Pipeline 版本映射
    # 例: {"business_a": "pro", "experiment_group": "pro"}
    business_version_map: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.default_version is None:
            self.default_version = os.getenv("MEMORY_PIPELINE_DEFAULT_VERSION", "lite")

        # 从环境变量加载业务方映射
        # 格式: MEMORY_PIPELINE_BUSINESS_MAP="business_a:pro,business_b:lite"
        map_str = os.getenv("MEMORY_PIPELINE_BUSINESS_MAP", "")
        if map_str and not self.business_version_map:
            for pair in map_str.split(","):
                pair = pair.strip()
                if ":" in pair:
                    biz, ver = pair.split(":", 1)
                    self.business_version_map[biz.strip()] = ver.strip()


@dataclass
class MemoryConfig:
    """
    Agent Memory 主配置

    整合所有子配置，提供统一的配置入口
    """

    # 子配置
    vector_store: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    graph_store: GraphStoreConfig = field(default_factory=GraphStoreConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    embedder: EmbedderConfig = field(default_factory=EmbedderConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)
    time_window: TimeWindowConfig = field(default_factory=TimeWindowConfig)
    extractor: ExtractorConfig = field(default_factory=ExtractorConfig)
    api: APIConfig = field(default_factory=APIConfig)
    pipeline: PipelineRouteConfig = field(default_factory=PipelineRouteConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    coding: CodingConfig = field(default_factory=CodingConfig)
    basic_profile: BasicProfileConfig = field(default_factory=BasicProfileConfig)

    # 全局配置
    enable_graph: bool = False
    enable_agent: bool = False
    debug: bool = False
    metrics_enabled: bool = True

    def __post_init__(self):
        # enable_graph 跟随 mode 走：mode=ultra 时自动启用，无需独立环境变量
        # 保留 from_dict() 可显式覆盖
        self.enable_agent = _get_env_bool("MEMORY_ENABLE_AGENT", False)
        self.debug = _get_env_bool("MEMORY_DEBUG", False)
        self.metrics_enabled = _get_env_bool("MEMORY_METRICS", True)

    @classmethod
    def from_env(cls) -> "MemoryConfig":
        """从环境变量创建配置"""
        return cls()

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "MemoryConfig":
        """从字典创建配置"""
        config = cls()

        _sections = [
            "vector_store",
            "graph_store",
            "cache",
            "llm",
            "embedder",
            "recall",
            "time_window",
            "extractor",
            "api",
            "pipeline",
            "history",
        ]
        for section in _sections:
            if section in config_dict:
                sub = getattr(config, section)
                for key, value in config_dict[section].items():
                    if hasattr(sub, key):
                        setattr(sub, key, value)

        # basic_profile: fields 是 Dict[str, str]，整体替换（非空时）
        if "basic_profile" in config_dict:
            bp = config_dict["basic_profile"] or {}
            fields = bp.get("fields")
            if isinstance(fields, dict) and fields:
                # 强制 str→str；过滤掉空 description
                config.basic_profile.fields = {
                    str(k): str(v).strip()
                    for k, v in fields.items()
                    if str(k).strip() and str(v).strip()
                }

        config.enable_graph = config_dict.get("enable_graph", config.enable_graph)
        config.enable_agent = config_dict.get("enable_agent", config.enable_agent)
        config.debug = config_dict.get("debug", config.debug)
        config.metrics_enabled = config_dict.get(
            "metrics_enabled", config.metrics_enabled
        )

        return config

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "vector_store": {
                "provider": self.vector_store.provider,
                "collection_name": self.vector_store.collection_name,
                "persist_directory": self.vector_store.persist_directory,
                "embedding_dims": self.vector_store.embedding_dims,
            },
            "graph_store": {
                "provider": self.graph_store.provider,
                "db_path": self.graph_store.db_path,
                "url": self.graph_store.url,
                "database": self.graph_store.database,
            },
            "cache": {
                "backend": self.cache.backend,
                "db_path": self.cache.db_path,
            },
            "llm": {
                "provider": self.llm.provider,
                "model": self.llm.model,
                "base_url": self.llm.base_url,
            },
            "embedder": {
                "provider": self.embedder.provider,
                "model": self.embedder.model,
                "base_url": self.embedder.base_url,
                "embedding_dims": self.embedder.embedding_dims,
            },
            "recall": {
                "default_limit": self.recall.default_limit,
                "semantic_weight": self.recall.semantic_weight,
                "recency_weight": self.recall.recency_weight,
                "importance_weight": self.recall.importance_weight,
            },
            "pipeline": {
                "default_version": self.pipeline.default_version,
                "business_version_map": self.pipeline.business_version_map,
            },
            "history": {
                "enable": self.history.enable,
                "db_path": self.history.db_path,
                "record_searches": self.history.record_searches,
            },
            "basic_profile": {
                "fields": dict(self.basic_profile.fields),
            },
            "debug": self.debug,
            "enable_graph": self.enable_graph,
            "enable_agent": self.enable_agent,
            "metrics_enabled": self.metrics_enabled,
        }

    def print_config(self) -> None:
        """打印配置信息"""
        print("=" * 60)
        print("Agent Memory Configuration")
        print("=" * 60)
        print(f"[Vector Store]")
        print(f"  Provider: {self.vector_store.provider}")
        print(f"  Collection: {self.vector_store.collection_name}")
        print(f"  Persist Dir: {self.vector_store.persist_directory}")
        print(f"  Embedding Dims: {self.vector_store.embedding_dims}")
        print(f"[Graph Store]")
        print(f"  Provider: {self.graph_store.provider}")
        if self.graph_store.provider == "neo4j":
            print(f"  URL: {self.graph_store.url}")
            print(f"  Database: {self.graph_store.database}")
        else:
            print(f"  DB Path: {self.graph_store.db_path}")
        print(f"[Cache]")
        print(f"  Backend: {self.cache.backend}")
        if self.cache.backend == "sqlite":
            print(f"  DB Path: {self.cache.db_path}")
        else:
            print(f"  MySQL: {self.cache.mysql_host}:{self.cache.mysql_port}/{self.cache.mysql_database}")
        print(f"[LLM]")
        print(f"  Provider: {self.llm.provider}")
        print(f"  Model: {self.llm.model}")
        print(f"  Base URL: {self.llm.base_url}")
        print(f"[Embedder]")
        print(f"  Provider: {self.embedder.provider}")
        print(f"  Model: {self.embedder.model}")
        print(f"  Base URL: {self.embedder.base_url}")
        print(f"[Recall]")
        print(f"  Default Limit: {self.recall.default_limit}")
        print(
            f"  Weights: semantic={self.recall.semantic_weight}, "
            f"recency={self.recall.recency_weight}, "
            f"importance={self.recall.importance_weight}"
        )
        print(f"[Pipeline]")
        print(f"  Default Version: {self.pipeline.default_version}")
        print(f"  Business Map: {self.pipeline.business_version_map}")
        print(f"[API]")
        print(f"  Prefix: {self.api.prefix}")
        print(f"  Auth: {self.api.enable_auth}")
        print(f"[History]")
        print(f"  Enable: {self.history.enable}")
        print(f"  DB Path: {self.history.db_path}")
        print(f"  Record Searches: {self.history.record_searches}")
        print("=" * 60)
