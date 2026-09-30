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

设计要点：**失败必须可诊断**
------------------------------
本项目在 CI 里读不到 job 日志（公开仓库也只有 admin 能下日志，本机没有 PAT）。
所以本脚本刻意做到「不靠日志也能定位」：

  · 每个阶段独立 try/except，异常**不逃逸**：一个阶段炸掉不影响其余阶段继续跑，
    最终报告里能看到「哪些过了、哪个炸了、炸在哪一行」；
  · `age_report.json` **无论成败都会写**（放在 finally 语义位置），
    产物可归档、可比对，而不是「炸了就什么都没有」；
  · 失败项通过 `::error::` **GitHub 注解**输出——注解在 run 页面直接可见，
    不需要日志权限；AGE 版本与 label 表计数用 `::notice::` 输出，作为「真跑过」的凭据。

退出码 0 = 全绿；任何一项不成立即 1（含「连不上 AGE」，绝不静默通过）。

本地（需带 AGE 的 PG）：
    KB_DATABASE_URL=postgresql://kb:kb@localhost:5432/kb python scripts/check_age.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback

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
FACTS: dict = {}          # 供 ::notice:: 输出的「真跑过」凭据


# --------------------------------------------------------------------------- #
# 输出与断言
# --------------------------------------------------------------------------- #
def _oneline(s) -> str:
    """GitHub 注解要求单行且不宜过长，否则整条被丢弃。"""
    return str(s).replace("\r", " ").replace("\n", " ")[:400]


def gha(kind: str, title: str, msg: str) -> None:
    """在工作流里输出注解；本地运行时跳过（避免污染终端输出）。

    `::error::` 会以红字出现在 run 页面上——**这就是没有日志权限时的定位通道**。
    """
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::{kind} title={_oneline(title)}::{_oneline(msg)}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(ok)


def phase(label: str, fn, *, required: bool = False):
    """执行一个阶段。异常不逃逸，转成一条失败断言 + 一行注解 + 截断的 traceback。

    为什么要隔离到「阶段」粒度：炸在第一步就把后面全跳过的话，报告里只有一条
    「失败」，看不出到底是连接、建图还是写入的问题。
    """
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc(limit=4)
        check(label, False, f"{type(e).__name__}: {e}")
        gha("error", f"AGE 校验失败：{label}", f"{type(e).__name__}: {e}")
        print("      " + tb.replace("\n", "\n      "))
        return None


def raw(sql: str, args: tuple = ()) -> list[tuple]:
    """独立连接执行原始 SQL：**与 store 内部连接分开**，构成真正的旁路对账。

    注意两个坑：
      · 必须自己 `SET search_path`——`agtype` / `ag_catalog` 下的类型在新连接里
        默认不可见，否则报 `type "agtype" does not exist`；
      · 无参数时**不要**给 `execute` 传第二个参数，免得 SQL 里的 `%` 被当占位符。
    """
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute('SET search_path = ag_catalog, "$user", public;')
            if args:
                cur.execute(sql, args)
            else:
                cur.execute(sql)
            return cur.fetchall()


def cypher_sql(graph: str, query: str, cols: str = "v agtype") -> str:
    """构造 `cypher()` 调用 SQL。

    这里**刻意不复用** `graph.py` 里的实现：本脚本的职责是「独立旁路验证」，
    与产品代码共用拼串逻辑就失去了交叉验证的意义。两处都遵守同一条 AGE 硬约束：
    图名与 Cypher 原文必须是常量字面量（不能是绑定参数），Cypher 用美元引用内联。
    """
    g = "'" + str(graph).replace("'", "''") + "'"
    tag, i = "$kb$", 0
    while tag in query:
        i += 1
        tag = "$kb" + ("q" * i) + "$"
    return f"SELECT * FROM ag_catalog.cypher({g}, {tag} {query} {tag}) as ({cols});"


def count_labels(graph: str) -> tuple[int, int, dict]:
    """绕开 store，直接数 AGE 为各 label 建的表里的行数。返回 (顶点数, 边数, 明细)。

    **必须排除 `_ag_label_vertex` / `_ag_label_edge`**：AGE 用它们作
    「所有顶点/边 label 表的继承父表」（每个图都有），父表 `count(*)` 会把子表行
    一并算进来（PostgreSQL 继承语义）。若把父表也累加，顶点/边数量会**翻倍**
    ——真机上表现为 `stats()` 报 7 顶点而原始表算成 14，这条对账断言直接红。
    """
    rows = raw(
        """
        SELECT l.name, l.kind::text
        FROM ag_catalog.ag_label l
        JOIN ag_catalog.ag_graph g ON l.graph = g.graphid
        WHERE g.name = %s
          AND l.name NOT IN ('_ag_label_vertex', '_ag_label_edge')
        """,
        (graph,),
    )
    verts = edges = 0
    detail: dict = {}
    for name, kind in rows:
        try:
            n = int(raw(f'SELECT count(*) FROM "{graph}"."{name}"')[0][0])
        except Exception as e:  # noqa: BLE001
            detail[name] = {"kind": kind, "error": _oneline(e)[:80]}
            continue
        detail[name] = {"kind": kind, "rows": n}
        if kind == "v":
            verts += n
        elif kind == "e":
            edges += n
    return verts, edges, detail


# --------------------------------------------------------------------------- #
# 各阶段
# --------------------------------------------------------------------------- #
def _stage_connect():
    """连接 + 扩展 + 建图（含幂等）。返回 store；失败返回 None。"""
    from kb_mcp_server.graph import AGEGraphStore

    store = AGEGraphStore()
    store.connect()
    check("连接 AGE 数据库", True, "psycopg 连接成功 + CREATE EXTENSION age")

    ver = raw("SELECT extversion FROM pg_extension WHERE extname = 'age'")
    ext = raw("SELECT count(*) FROM pg_extension WHERE extname = 'age'")[0][0]
    FACTS["age_version"] = ver[0][0] if ver else "?"
    check("age 扩展已安装", int(ext) == 1, f"版本 = {FACTS['age_version']}")

    store.ensure_schema()
    n1 = raw("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (GRAPH,))[0][0]
    check("图已创建", int(n1) == 1, f"ag_catalog.ag_graph 中 {GRAPH} 条目 = {n1}")

    store.ensure_schema()  # 幂等
    n2 = raw("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (GRAPH,))[0][0]
    check("ensure_schema 幂等", int(n2) == 1, f"二次调用后条目仍为 {n2}")
    return store


def _stage_no_silent_fallback():
    """最重要的一条：AGE 配置下绝不能悄悄退回 memory 图。"""
    from kb_mcp_server.graph import get_graph_store

    g = get_graph_store()
    check(
        "get_graph_store() 未静默降级",
        getattr(g, "backend", "?") == "age",
        f"返回 backend = {getattr(g, 'backend', '?')!r}"
        "（应为 'age'；若为 'memory' 说明 AGE 连接异常被吞掉、降级了）",
    )


# --------------------------------------------------------------------------- #
# AGE 语法能力探针
#
# 为什么单独做一层：`AGEGraphStore` 的实现（MERGE / SET / DETACH DELETE / 列定义列表）
# 此前**从未在真实 AGE 上跑过**，而不同 AGE 版本对子句与参数绑定的支持并不一致。
# 逐条探针把「失败」变成「哪一条语法在这版不支持」，一轮 CI 就能定位——
# 在本项目「CI 日志读不到」的约束下，这一点尤其重要。
# 每条探针独立捕获异常：一条不支持不影响其余探针。
# --------------------------------------------------------------------------- #
def _probe_capabilities() -> None:
    def probe(label: str, query: str, cols: str = "v agtype") -> bool:
        try:
            raw(cypher_sql(GRAPH, query, cols))
            check(label, True, "")
            return True
        except Exception as e:  # noqa: BLE001
            check(label, False, f"{type(e).__name__}: {_oneline(e)}")
            return False

    probe("探针：RETURN 常量", "RETURN 1")
    probe("探针：MATCH 计数", "MATCH (n) RETURN count(n)")
    # ↓ 多列 RETURN：AGE 的 cypher() 要求列定义列表与 RETURN 表达式数逐一对齐，
    #   否则报 "return row and column definition list do not match"。这正是
    #   find_entities / neighbors / paths / export_graph 在真机上会踩的坑——
    #   单独探一条，失败时一眼看得出是「列数没对齐」而不是别的。
    probe("探针：多列 RETURN（find_entities / export_graph 依赖）",
          "MATCH (n) RETURN n.name, labels(n), n.props",
          "a agtype, b agtype, c agtype")
    probe("探针：5 列 RETURN（export_graph 形状）",
          "MATCH (n)-[r]->(m) RETURN n.name, labels(n), type(r), m.name, labels(m)",
          "a agtype, b agtype, c agtype, d agtype, e agtype")
    probe("探针：CREATE 节点", "CREATE (n:Document {name: 'probe_doc'}) RETURN n")
    # ↓ MERGE 是 store.upsert_entity 的核心，若这版 AGE 不支持则整条写入路径不成立
    probe("探针：MERGE（upsert_entity 依赖）",
          "MERGE (n:Document {name: 'probe_doc'}) RETURN n")
    # ↓ upsert_relation 用的是 `MERGE (a)-[r:REL {}]->(b)`（空属性表），
    #   这个形状在部分 AGE 版本上单独出过问题，所以跟节点 MERGE 分开探。
    probe("探针：MERGE 关系 + 空属性表（upsert_relation 依赖）",
          "MERGE (a:Document {name: 'probe_a'}) MERGE (b:IssueCategory {name: 'probe_b'}) "
          "MERGE (a)-[r:MENTIONS {}]->(b) RETURN r")
    probe("探针：SET 字符串属性（props 序列化依赖）",
          "MATCH (n:Document {name: 'probe_doc'}) SET n.props = '{}' RETURN n")
    probe("探针：MATCH 关系计数", "MATCH ()-[r]->() RETURN count(r)")
    # ↓ DETACH DELETE 是 store.clear() 依赖；无 RETURN 的语句能否配合列定义列表是个已知坑
    probe("探针：DETACH DELETE（clear 依赖）", "MATCH (n) DETACH DELETE n")


def _stage_primitives(store):
    """写入原语：直接调 store 的 upsert，异常会原样抛出，是最可诊断的一层。

    与下一阶段的「真实摄取链路」互为对照：
      · 两个都过 → 链路与实现都 OK；
      · 只有这个过 → 摄取 hook 把异常吞了（去看它的 stderr）；
      · 两个都炸 → 问题在 AGE 兼容性（MERGE / SET / 类型），异常就在下面。
    """
    store.clear()
    store.upsert_entity("Document", DOC_ID, {"doc_id": DOC_ID})
    store.upsert_entity("IssueCategory", "退款退货", {"hits": 2})
    store.upsert_relation("Document", DOC_ID, "MENTIONS", "IssueCategory", "退款退货",
                          {"doc_id": DOC_ID})
    st = store.stats()
    check(
        "写入原语可落图（upsert_entity / upsert_relation）",
        int(st.get("nodes", 0)) >= 2 and int(st.get("edges", 0)) >= 1,
        f"stats = {st}",
    )


def _stage_real_pipeline(store):
    """真实写入：走真实摄取链路（正文 → 规则三元组 → 落图）。"""
    import app  # noqa: E402  触发真实接线：_ingestor 持有 AGE 图

    store.clear()
    n_chunks = app._ingestor.ingest_text(DOC_ID, DOC_TEXT)
    gres = app._ingestor.last_graph_result or {}
    check("摄取写入向量库", n_chunks >= 1, f"{n_chunks} 个片段")
    check(
        "摄取触发建图且后端为 age",
        gres.get("backend") == "age",
        "last_graph_result = "
        + json.dumps({k: gres.get(k) for k in ("backend", "added", "triples", "ok")},
                     ensure_ascii=False),
    )


def _stage_reconcile(store):
    """原始表对账 + Cypher 读回。返回 (顶点数, 边数)。"""
    verts, edges, detail = count_labels(GRAPH)
    FACTS["labels"] = detail
    check("原始表存在顶点", verts >= 1, f"顶点行数 = {verts}")
    check("原始表存在边（关系）", edges >= 1, f"边行数 = {edges}")
    doc_rows = detail.get("Document", {}).get("rows", 0)
    check("Document 节点已落库", doc_rows >= 1, f'kb_graph."Document" 行数 = {doc_rows}')

    st = store.stats()
    check(
        "store.stats() 与原始表口径一致",
        int(st.get("nodes", -1)) == verts and int(st.get("edges", -1)) == edges,
        f"stats = {st}（原始表 {verts} 顶点 / {edges} 边）",
    )

    ents = store.find_entities(limit=20)
    check("find_entities 能读回实体", len(ents) >= 1,
          f"命中 {len(ents)} 个：{[e.get('name') for e in ents][:6]}")

    ex = store.export_graph()
    check(
        "export_graph 可用于前端可视化",
        len(ex.get("nodes", [])) >= 1 and ex.get("backend") == "age",
        f"backend={ex.get('backend')} nodes={len(ex.get('nodes', []))} "
        f"edges={len(ex.get('edges', []))}",
    )

    nbr = store.neighbors(DOC_ID, node_type="Document")
    n_names = [n.get("name") for n in nbr.get("neighbors", [])]
    check("neighbors 能走通关系", len(n_names) >= 1, f"Document 的邻居：{n_names[:6]}")

    store.clear()
    v2, e2, _ = count_labels(GRAPH)
    check("clear() 后图已清空", v2 == 0 and e2 == 0, f"清空后 {v2} 顶点 / {e2} 边")
    return verts, edges


def _write_report(failed: list[str]) -> None:
    """报告无论成败都要落盘——否则 CI 失败时连「失败在哪一步」都拿不到。"""
    report = {
        "dsn": DSN,
        "graph": GRAPH,
        "age_version": FACTS.get("age_version", "?"),
        "passed": len(CHECKS) - len(failed),
        "total": len(CHECKS),
        "failures": failed,
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS],
        "labels": FACTS.get("labels", {}),
    }
    rp = os.path.join(_HERE, "age_report.json")
    try:
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[age] 报告已写入：{rp}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 报告写入失败：{e}", file=sys.stderr)


def main() -> int:
    print("=" * 68)
    print("Apache AGE 图侧真跑验证")
    print(f"  DSN   = {DSN}")
    print(f"  graph = {GRAPH}")
    print("=" * 68)

    try:
        store = phase("阶段一：连接 / 扩展 / 建图", _stage_connect)
        if store is None:
            # 连不上就到此为止：后面的阶段没有意义，但报告仍然要写
            print("\n[FAIL] 无法连接 AGE 或建图失败，跳过后续阶段", file=sys.stderr)
        else:
            phase("阶段二：不静默降级", _stage_no_silent_fallback)
            phase("阶段三：AGE 语法能力探针", _probe_capabilities)
            phase("阶段四：写入原语", lambda: _stage_primitives(store))
            phase("阶段五：真实摄取链路", lambda: _stage_real_pipeline(store))
            phase("阶段六：原始表对账与读回", lambda: _stage_reconcile(store))
    except BaseException as e:  # noqa: BLE001  兜底：报告必须产出
        check("整体执行", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    failed = [n for n, ok, _ in CHECKS if not ok]
    _write_report(failed)

    # 把结论顶到 run 页面可见的位置（无需日志权限）
    for name, ok, detail in CHECKS:
        if not ok:
            gha("error", f"AGE 断言失败：{name}", detail)
    if FACTS.get("labels"):
        gha("notice", "AGE 原始表计数（真跑凭据）",
            f"age={FACTS.get('age_version')} " + json.dumps(FACTS["labels"], ensure_ascii=False))

    print("-" * 68)
    if failed:
        print(f"[FAIL] AGE 验证未通过（{len(failed)}/{len(CHECKS)} 项失败）：{failed}",
              file=sys.stderr)
    else:
        print(f"[OK] AGE 验证全部通过（{len(CHECKS)} 项）："
              "扩展 / 建图 / 不降级 / 写入 / 原始表对账 / 读回 / 清理")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
