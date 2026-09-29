"""Apache AGE 图侧「真跑」验证：把「从未在真实 PG 上验证过」变成硬证据。

为什么需要这一步（ROADMAP P2-10 补）
--------------------------------------
向量侧（pgvector）早在 CI 的 `pg` job 上跑通了，但**图侧一直是空白**：
`AGEGraphStore` 的实现、`setup_db.py --graph`、乃至 `graph.py` 自己都写着
「生产启用前请在目标 AGE 版本上跑一遍验证」。原因很实在——开发机没有 docker daemon，
起不了带 AGE 扩展的库；而 CI 用的 `pgvector/pgvector:pg16` 镜像**不含 AGE**，
`--graph` 会按设计降级成 memory 图。于是「AGE 图能用」一直是未验证假设。

本脚本在**真实 Apache AGE**（service container）上验证四件事：

  1) 扩展与建图      CREATE EXTENSION age / create_graph 真的生效（原始 SQL 查 ag_catalog）
  2) 不静默降级      `KB_GRAPH_BACKEND=age` 时 `get_graph_store()` 必须返回 age 后端
                     ——它内部 try/except 会把连不上 AGE 悄悄换成 memory 图，
                     这正是项目最警惕的「静默降级」缺陷类别，必须被断言顶住
  3) 真实写入        经**真实摄取链路**（正文 → 规则三元组 → 落图），不是直接调 store API
  4) 原始表对账      绕开 store，直接数 AGE 建出来的 label 表行数（顶点/边）
                     ——「副作用真的发生了」要能被 store 之外的路径看见

退出码 0 = 全绿；任何一项不成立即 1（含「连不上 AGE」，绝不静默通过）。

本地（需带 AGE 的 PG）：
    KB_DATABASE_URL=postgresql://kb:kb@localhost:5432/kb python scripts/check_age.py
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 本脚本 import 项目包（不像 check_pg.py 只用原始 SQL），而 `python scripts/xxx.py`
# 的 sys.path[0] 是 scripts/ 而非项目根 → 必须显式补根目录。
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# --------------------------------------------------------------------------- #
# 环境隔离：必须在 import app 之前设置（app 会在模块级初始化图存储）
# --------------------------------------------------------------------------- #
DSN = os.environ.get("KB_DATABASE_URL", "postgresql://kb:kb@localhost:5432/kb")
GRAPH = os.environ.get("KB_GRAPH_NAME", "kb_graph")

os.environ.setdefault("KB_STORAGE_BACKEND", "memory")   # 本例只验图，向量侧用 memory
os.environ["KB_GRAPH_BACKEND"] = "age"                  # ← 被测目标
os.environ["KB_DATABASE_URL"] = DSN
os.environ["KB_GRAPH_NAME"] = GRAPH
os.environ["KB_GRAPH_ENABLED"] = "1"
os.environ["KB_LLM_ENABLED"] = "0"                      # 规则抽取，零外部依赖
os.environ.setdefault("KB_EMBEDDING_BACKEND", "dev")
os.environ["KB_AUDIT_LOG"] = "off"
os.environ.setdefault("KB_API_MOCK", "1")
os.environ["KB_DATA_DIR"] = os.path.join(_HERE, ".age_runtime")
os.environ.setdefault("KB_MEM_STORE", os.path.join(_HERE, ".age_runtime", "store.json"))

DOC_ID = "age_check_doc"
DOC_TEXT = (
    "客户反馈发货延迟，物流配送异常，包裹超过 24 小时未送达。"
    "客户要求退款退货，并升级工单处理。若核实为仓储缺货，按 SLA 承诺 2 小时内响应。"
    "该问题涉及退款退货流程与物流配送时效。"
)

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    mark = "OK  " if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" —— {detail}"
    print(line)
    return bool(ok)


def raw(sql: str, args: tuple = ()) -> list[tuple]:
    """独立连接执行原始 SQL：**与 store 内部连接分开**，构成真正的旁路对账。"""
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()


def count_labels(graph: str) -> tuple[int, int, dict]:
    """绕开 store，直接数 AGE 为各 label 建的表里的行数。返回 (顶点数, 边数, 明细)。"""
    rows = raw(
        """
        SELECT l.name, l.kind::text
        FROM ag_catalog.ag_label l
        JOIN ag_catalog.ag_graph g ON l.graph = g.graphid
        WHERE g.name = %s
        """,
        (graph,),
    )
    verts = edges = 0
    detail: dict = {}
    for name, kind in rows:
        try:
            n = int(raw(f'SELECT count(*) FROM "{graph}"."{name}"')[0][0])
        except Exception as e:  # noqa: BLE001
            detail[name] = {"kind": kind, "error": str(e)[:80]}
            continue
        detail[name] = {"kind": kind, "rows": n}
        if kind == "v":
            verts += n
        elif kind == "e":
            edges += n
    return verts, edges, detail


def main() -> int:
    print("=" * 68)
    print("Apache AGE 图侧真跑验证")
    print(f"  DSN   = {DSN}")
    print(f"  graph = {GRAPH}")
    print("=" * 68)

    # ------------------------------------------------------------------ #
    # 1) 连接与扩展：连不上就明确失败（本脚本绝不降级）
    # ------------------------------------------------------------------ #
    from kb_mcp_server.graph import AGEGraphStore

    store = AGEGraphStore()
    try:
        store.connect()
    except Exception as e:  # noqa: BLE001
        check("连接 AGE 数据库", False, f"{type(e).__name__}: {e}")
        print("\n[FAIL] 无法连接 AGE：请确认服务已就绪且 KB_DATABASE_URL 正确", file=sys.stderr)
        return 1
    check("连接 AGE 数据库", True, "psycopg 连接成功 + CREATE EXTENSION age")

    ext = int(raw("SELECT count(*) FROM pg_extension WHERE extname = 'age'")[0][0])
    check("age 扩展已安装", ext == 1, f"pg_extension 中 age 条目 = {ext}")

    store.ensure_schema()
    graphs = raw("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (GRAPH,))[0][0]
    check("图已创建", int(graphs) == 1, f"ag_catalog.ag_graph 中 {GRAPH} 条目 = {graphs}")

    # 幂等：重复 ensure_schema 不应重复建图 / 报错
    store.ensure_schema()
    graphs2 = raw("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (GRAPH,))[0][0]
    check("ensure_schema 幂等", int(graphs2) == 1, f"二次调用后条目仍为 {graphs2}")

    # ------------------------------------------------------------------ #
    # 2) 不静默降级：这是本脚本最重要的一条断言
    # ------------------------------------------------------------------ #
    from kb_mcp_server.graph import get_graph_store

    g = get_graph_store()
    check(
        "get_graph_store() 未静默降级",
        getattr(g, "backend", "?") == "age",
        f"返回 backend = {getattr(g, 'backend', '?')!r}（应为 'age'；若为 'memory' 说明 AGE 连接被吞掉降级了）",
    )

    # 清空历史，保证下面的计数是本次写入的结果
    store.clear()

    # ------------------------------------------------------------------ #
    # 3) 真实写入：走真实摄取链路（正文 → 规则三元组 → 落图）
    # ------------------------------------------------------------------ #
    import app  # noqa: E402  触发真实接线：_ingestor 持有 AGE 图

    n_chunks = app._ingestor.ingest_text(DOC_ID, DOC_TEXT)
    gres = app._ingestor.last_graph_result or {}
    check("摄取写入向量库", n_chunks >= 1, f"{n_chunks} 个片段")
    check(
        "摄取触发建图且后端为 age",
        gres.get("backend") == "age",
        f"last_graph_result = {json.dumps({k: gres.get(k) for k in ('backend', 'added', 'triples')}, ensure_ascii=False)}",
    )

    # ------------------------------------------------------------------ #
    # 4) 原始表对账：绕开 store，直接数 AGE 自己建的表
    # ------------------------------------------------------------------ #
    verts, edges, detail = count_labels(GRAPH)
    check("原始表存在顶点", verts >= 1, f"顶点行数 = {verts}")
    check("原始表存在边（关系）", edges >= 1, f"边行数 = {edges}")
    doc_rows = detail.get("Document", {}).get("rows", 0)
    check("Document 节点已落库", doc_rows >= 1, f'kb_graph."Document" 行数 = {doc_rows}')
    print(f"       label 明细：{json.dumps(detail, ensure_ascii=False)}")

    # ------------------------------------------------------------------ #
    # 5) Cypher 读回（store 自身 API 也要能读，且与原始表口径一致）
    # ------------------------------------------------------------------ #
    st = store.stats()
    check(
        "store.stats() 与原始表一致",
        int(st.get("nodes", -1)) == verts and int(st.get("edges", -1)) == edges,
        f"stats = {st}（原始表 {verts} 顶点 / {edges} 边）",
    )

    ents = store.find_entities(limit=20)
    check("find_entities 能读回实体", len(ents) >= 1, f"命中 {len(ents)} 个：{[e.get('name') for e in ents][:6]}")

    ex = store.export_graph()
    check(
        "export_graph 可用于前端可视化",
        len(ex.get("nodes", [])) >= 1 and ex.get("backend") == "age",
        f"backend={ex.get('backend')} nodes={len(ex.get('nodes', []))} edges={len(ex.get('edges', []))}",
    )

    # 邻居查询：Document --MENTIONS--> IssueCategory
    nbr = store.neighbors(DOC_ID, node_type="Document")
    n_names = [n.get("name") for n in nbr.get("neighbors", [])]
    check("neighbors 能走通关系", len(n_names) >= 1, f"Document 的邻居：{n_names[:6]}")

    # ------------------------------------------------------------------ #
    # 6) 清理：DETACH DELETE 在目标 AGE 版本上是否真的可用
    # ------------------------------------------------------------------ #
    store.clear()
    v2, e2, _ = count_labels(GRAPH)
    check("clear() 后图已清空", v2 == 0 and e2 == 0, f"清空后 {v2} 顶点 / {e2} 边")

    # ------------------------------------------------------------------ #
    failed = [n for n, ok, _ in CHECKS if not ok]
    print("-" * 68)
    if failed:
        print(f"[FAIL] AGE 验证未通过（{len(failed)}/{len(CHECKS)} 项）：{failed}", file=sys.stderr)
    else:
        print(f"[OK] AGE 验证全部通过（{len(CHECKS)} 项）：扩展 / 建图 / 不降级 / 写入 / 原始表对账 / 读回 / 清理")

    report = {
        "dsn": DSN,
        "graph": GRAPH,
        "passed": len(CHECKS) - len(failed),
        "total": len(CHECKS),
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS],
        "counts": {"vertices": verts, "edges": edges, "labels": detail},
    }
    rp = os.path.join(_HERE, "age_report.json")
    try:
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[age] 报告已写入：{rp}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 报告写入失败：{e}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
