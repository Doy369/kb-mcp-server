"""PG 生产存储校验：确认 schema 真实落库、数据真实写入。

存在的意义：pgvector 后端长期只被「代码就位」覆盖——没有一份自动化证明
「建表成功」和「回归结果确实落在 PG 上」。存储层若连接不上去，代码会走上
except / 降级分支，**评测照样能过**（只是悄悄跑在 memory 上），CI 一片绿但其实
什么都没验证。本脚本把这种「静默退回」变成硬失败。

用法（须先设 KB_DATABASE_URL，并由 setup_db.py / eval_run.py 先行）：
    python scripts/check_pg.py schema   # 扩展 / 表 / HNSW 索引 / 向量维度
    python scripts/check_pg.py data     # kb_chunks 非空（回归确实写进 PG）
"""

from __future__ import annotations

import os
import sys


def _dsn() -> str:
    dsn = os.environ.get("KB_DATABASE_URL")
    if not dsn:
        print("[FAIL] 未设置 KB_DATABASE_URL", file=sys.stderr)
        raise SystemExit(1)
    return dsn


def check_schema() -> None:
    import psycopg

    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector';")
        if not cur.fetchone():
            raise SystemExit("[FAIL] vector 扩展未安装")

        cur.execute("SELECT to_regclass('public.kb_chunks');")
        if not cur.fetchone()[0]:
            raise SystemExit("[FAIL] kb_chunks 表不存在")

        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'kb_chunks';")
        idx = [r[0] for r in cur.fetchall()]
        if not any("embedding" in i for i in idx):
            raise SystemExit(f"[FAIL] HNSW 向量索引缺失，现有索引：{idx}")

        cur.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'kb_chunks'::regclass AND attname = 'embedding';"
        )
        typ = cur.fetchone()[0]
        dim = os.environ.get("KB_EMBEDDING_DIM", "1024")
        if typ != f"vector({dim})":
            raise SystemExit(f"[FAIL] 向量维度不符：期望 vector({dim})，实际 {typ}")

    print(f"[OK] schema 校验通过：vector 扩展 + kb_chunks + HNSW 索引 + {typ}")


def check_data() -> None:
    import psycopg

    with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM kb_chunks;")
        n = cur.fetchone()[0]

    if n <= 0:
        raise SystemExit("[FAIL] kb_chunks 为空——回归评测并未真正写入 PG（疑似静默退回 memory）")
    print(f"[OK] PG 中已落 {n} 个片段，生产存储路径真实生效")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "schema":
        check_schema()
    elif mode == "data":
        check_data()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
