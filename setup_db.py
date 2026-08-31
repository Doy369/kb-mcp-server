"""数据库接入初始化：连接 Postgres + pgvector 并建表/建索引。

用法：
  1) 在 .env 设 KB_STORAGE_BACKEND=pgvector 与 KB_DATABASE_URL
  2) python setup_db.py

前提：
  - 目标 PG 已安装 vector 扩展（CREATE EXTENSION vector 可用）
  - 连接用户对该库有 CREATE 权限
"""

import sys

from kb_mcp_server.config import get_settings
from kb_mcp_server.storage import PGVectorStore


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


if __name__ == "__main__":
    main()
