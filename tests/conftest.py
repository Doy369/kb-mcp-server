"""pytest 全局配置：在导入任何 kb_mcp_server 模块之前隔离全部持久化路径。

**为什么必须在这里做**：`kb_mcp_server.config` 在模块导入时就求值
`DATA_DIR` / `_MEM_STORE_PATH` / `_GRAPH_STORE_PATH` 等常量，之后再改环境变量无效。
因此必须在 conftest 顶部（任何 app / kb_mcp_server 导入之前）设置环境变量。

隔离目标：测试绝不读写项目根的真实数据（kb_store.json / runtime_config.json /
kb_audit.jsonl），也不依赖用户页面配置，保证「本地跑」与「CI 跑」结果一致。
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ---- 1) 隔离数据目录（必须在 import kb_mcp_server 之前）----
_TMP = tempfile.mkdtemp(prefix="kb_test_")
os.environ["KB_DATA_DIR"] = _TMP

# ---- 2) 全部走离线确定性后端，零外部依赖（不下载 bge 模型、不连 Postgres）----
os.environ["KB_STORAGE_BACKEND"] = "memory"
os.environ["KB_EMBEDDING_BACKEND"] = "dev"
os.environ["KB_GRAPH_BACKEND"] = "memory"
os.environ["KB_GRAPH_ENABLED"] = "1"
os.environ["KB_CHUNK_STRATEGY"] = "fine"
os.environ["KB_LLM_ENABLED"] = "0"
os.environ["KB_AGENT_MODE"] = "deterministic"
os.environ["KB_API_MOCK"] = "1"
os.environ["KB_AUDIT_LOG"] = "off"          # 不污染审计文件
os.environ["KB_ACTION_LOG"] = "off"         # P2-9 动作审计默认关闭；需要断言的用例自行开
os.environ["KB_RATE_LIMIT"] = "0"
os.environ["KB_API_TOKEN"] = ""
os.environ["KB_ACTION_GRAPH"] = "0"         # P2-9 收尾：图谱回写默认关；需要断言的用例自行开


@pytest.fixture
def clean_store(tmp_path, monkeypatch):
    """给单个测试一个全新的内存向量库（独立 JSON 路径，测试后自动清理）。"""
    from kb_mcp_server import config
    from kb_mcp_server import storage

    store_path = str(tmp_path / "kb_store_test.json")
    monkeypatch.setattr(storage, "_MEM_STORE_PATH", store_path)
    fresh = storage.MemoryVectorStore()
    monkeypatch.setattr(storage, "_store_instance", fresh, raising=False)
    return fresh


@pytest.fixture
def clean_graph(tmp_path, monkeypatch):
    """给单个测试一个全新的内存图存储。"""
    from kb_mcp_server import graph

    graph_path = str(tmp_path / "kb_graph_test.json")
    monkeypatch.setattr(graph, "_GRAPH_STORE_PATH", graph_path)
    monkeypatch.setattr(graph, "_graph_instance", None, raising=False)
    fresh = graph.MemoryGraphStore()
    monkeypatch.setattr(graph, "_graph_instance", fresh, raising=False)
    return fresh
