"""数据库接入初始化：连接 Postgres + pgvector（可选 + Apache AGE 图谱）并建表/建图。

用法：
  1) 在 .env 设 KB_STORAGE_BACKEND=pgvector 与 KB_DATABASE_URL
  2) python setup_db.py            # 建 kb_chunks 向量表 + HNSW 索引
     python setup_db.py --graph    # 额外初始化 AGE 图（决策 D2：与 pgvector 同库）

前提：
  - 目标 PG 已安装 vector 扩展（CREATE EXTENSION vector 可用）
  - --graph 需要 age 扩展（CREATE EXTENSION age）+ LOAD 'age' 权限
  - 连接用户对该库有 CREATE 权限

图谱初始化失败不会阻断向量库：用 KB_GRAPH_BACKEND=memory 即可离线跑通 GraphRAG。
"""

import sys

from kb_mcp_server.config import get_settings
from kb_mcp_server.storage import PGVectorStore


def init_graph() -> bool:
    """初始化 Apache AGE 图；扩展不可用则明确告知，返回 False。"""
    from kb_mcp_server.graph import AGEGraphStore

    store = AGEGraphStore()
    try:
        store.connect()
        store.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print("⚠️  图谱初始化失败（AGE 扩展不可用或权限不足）：", e)
        print("    处理①：让 DBA 在目标库执行 CREATE EXTENSION age;")
        print("    处理②：保持 KB_GRAPH_BACKEND=memory，用内置图离线跑（功能一致，零依赖）")
        return False
    print("✅ AGE 图就绪：", store.stats())
    return True


def main() -> None:
    s = get_settings()
    if s.storage_backend != "pgvector":
        print("当前 KB_STORAGE_BACKEND 不是 pgvector，无需建库。")
        print("请在 .env 设 KB_STORAGE_BACKEND=pgvector 与 KB_DATABASE_URL 后重跑本脚本。")
        return

    store = PGVectorStore()
    try:
        store.connect()
    except Exception as e:  # noqa: BLE001
        print("❌ 连接失败：", e)
        sys.exit(1)

    try:
        store.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print("❌ 建表失败（多半是 vector 扩展未安装或权限不足）：", e)
        sys.exit(1)

    print("✅ 已连接并初始化知识库 schema：", s.database_url)
    print("   - kb_chunks 表 + HNSW 向量索引就绪，当前片段数：", store.count())

    if "--graph" in sys.argv:
        init_graph()


if __name__ == "__main__":
    main()
