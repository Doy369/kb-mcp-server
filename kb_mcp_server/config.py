import json
import os
import sys

from dotenv import load_dotenv
from pydantic import BaseModel


def _is_frozen() -> bool:
    """是否被 PyInstaller 等工具冻结为可执行文件。"""
    return getattr(sys, "frozen", False)


def get_data_dir() -> str:
    """exe 冻结模式下返回用户可写数据目录（%APPDATA%/kb-mcp-server）；
    开发模式返回空串（沿用项目根）。"""
    if _is_frozen():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "kb-mcp-server")
        os.makedirs(d, exist_ok=True)
        return d
    return ""


# exe 模式下所有运行时数据（kb_store.json / runtime_config.json / .env）落在用户目录，
# 避免写进只读的临时解压目录导致重启即丢。
DATA_DIR = get_data_dir()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DATA_DIR:
    RUNTIME_CONFIG_PATH = os.path.join(DATA_DIR, "runtime_config.json")
    _ENV_PATH = os.path.join(DATA_DIR, ".env")
else:
    RUNTIME_CONFIG_PATH = os.path.join(PROJECT_ROOT, "runtime_config.json")
    _ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

load_dotenv(dotenv_path=_ENV_PATH)

_runtime_cfg: "dict | None" = None


class Settings(BaseModel):
    # 存储后端：pgvector（生产，需 Postgres+pgvector）| memory（本地离线开发，纯 Python）
    storage_backend: str = os.getenv("KB_STORAGE_BACKEND", "memory")

    # Postgres + pgvector 连接串（仅 pgvector 后端需要）
    database_url: str = os.getenv("KB_DATABASE_URL", "postgresql://kb:kb@localhost:5432/kb")

    # 图谱后端（P6 · GraphRAG 关系增强层）：
    #   memory：纯 Python 内存图 + JSON 落盘，离线零依赖
    #   age   ：Postgres + Apache AGE，与 pgvector 同库；不可用时自动回退 memory
    graph_backend: str = os.getenv("KB_GRAPH_BACKEND", "memory")
    graph_enabled: bool = os.getenv("KB_GRAPH_ENABLED", "1").lower() in ("1", "true", "yes")

    # 嵌入后端：bge（sentence-transformers 本地模型）| dev（纯 Python 离线条目哈希嵌入，仅开发验证）
    embedding_backend: str = os.getenv("KB_EMBEDDING_BACKEND", "dev")
    embedding_model: str = os.getenv("KB_EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")
    embedding_dim: int = int(os.getenv("KB_EMBEDDING_DIM", "1024"))

    # 本地合成 LLM（OpenAI 兼容接口，如 Ollama / vLLM）。KB_LLM_ENABLED=1 时启用合成，失败自动回退模板。
    llm_base_url: str = os.getenv("KB_LLM_BASE_URL", "http://localhost:11434/v1")
    llm_model: str = os.getenv("KB_LLM_MODEL", "qwen2.5:7b")
    llm_enabled: bool = os.getenv("KB_LLM_ENABLED", "0").lower() in ("1", "true", "yes")

    # P5 加固：Web 控制台鉴权 / 限流 / 日志
    api_token: str = os.getenv("KB_API_TOKEN", "")          # 非空则 /api/* 需 Bearer；空=不鉴权
    rate_limit: int = int(os.getenv("KB_RATE_LIMIT", "0"))  # 每 IP 每分钟最大请求数；0=不限流
    log_file: str = os.getenv("KB_LOG_FILE", "")            # 结构化访问日志文件路径；空=仅 stdout


_settings: "Settings | None" = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


# --------------------------------------------------------------------------- #
# 运行时配置（页面可改、持久化，优先于环境变量）
# --------------------------------------------------------------------------- #
def load_runtime_config() -> dict:
    """加载页面写入的运行时配置；首次调用时从文件读取并缓存。"""
    global _runtime_cfg
    if _runtime_cfg is None:
        try:
            if os.path.exists(RUNTIME_CONFIG_PATH):
                with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
                    _runtime_cfg = json.load(f)
            else:
                _runtime_cfg = {}
        except Exception:
            _runtime_cfg = {}
    return _runtime_cfg


def get_cfg(key: str, default: str = "") -> str:
    """读取配置：运行时配置优先，其次环境变量，最后 default。"""
    rc = load_runtime_config()
    v = rc.get(key)
    if v not in (None, ""):
        return v
    return os.getenv(key, default)


def set_cfg(key: str, value: str | None) -> None:
    """写入一项运行时配置并持久化（value 为空/None 则删除该项）。"""
    rc = load_runtime_config()
    if value is None or value == "":
        rc.pop(key, None)
    else:
        rc[key] = value
    _persist_runtime_config(rc)


def _persist_runtime_config(rc: dict) -> None:
    global _runtime_cfg
    _runtime_cfg = rc
    try:
        with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(rc, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
