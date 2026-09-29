"""`AGEGraphStore._cypher()` 必须遵守 AGE 的两条硬约束。

AGE 在 `post_parse_analyze_hook` 里拦截 `cypher()` 做语法改写，于是：

1) **列定义列表列数 == `RETURN` 表达式数**。`cypher()` 返回 SETOF record，
   PostgreSQL 逐一比对，不符即报
   `return row and column definition list do not match`。
   ——曾在真机把 `find_entities` / `neighbors` / `paths` / `export_graph` 全数打挂。

2) **图名与 Cypher 原文必须是常量字面量**（不能是绑定参数 `$2`）。
   否则 AGE 读不到原文，报 `a name constant is expected`。
   ——这条同样只在真机暴露：本地 memory 后端根本不会生成 SQL。

本文件用**假连接**把生成的 SQL 抓下来断言，不依赖任何真实 PG。
"""

from __future__ import annotations

import re

import pytest

from kb_mcp_server.graph import AGEGraphStore


class _FakeCursor:
    def __init__(self, sink: list):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, args=None):
        self._sink.append((sql, args))

    def fetchall(self):
        return []

    def fetchone(self):
        return (0,)


class _FakeConn:
    """只记录 SQL，不做任何真实通信。"""

    def __init__(self):
        self.sink: list = []
        self.autocommit = True

    def cursor(self):
        return _FakeCursor(self.sink)


@pytest.fixture
def age_store() -> AGEGraphStore:
    st = AGEGraphStore(dsn="postgresql://kb:kb@localhost:5432/kb", graph_name="kb_graph")
    st.conn = _FakeConn()
    return st


def _cols_of(sql: str) -> int:
    """解析 `... as (c0 agtype, c1 agtype);` 末尾的列定义列表列数。"""
    m = re.search(r"as \(([^)]*)\)\s*;?\s*$", sql, flags=re.IGNORECASE)
    assert m, f"未能解析列定义列表：{sql!r}"
    body = m.group(1).strip()
    return 0 if not body else len([c for c in body.split(",") if c.strip()])


def _cypher_call(store: AGEGraphStore, needle: str) -> tuple[str, object]:
    """在捕获到的语句里，找出（内联的）Cypher 文本含 needle 的那条。"""
    for sql, args in store.conn.sink:
        if "ag_catalog.cypher" in sql and needle in sql:
            return sql, args
    raise AssertionError(
        f"未捕获到含 {needle!r} 的 cypher 调用；已捕获："
        f"{[s for s, _ in store.conn.sink if 'ag_catalog.cypher' in s]}"
    )


# --------------------------------------------------------------------------- #
# 约束 2：图名与 Cypher 原文必须是常量字面量
# --------------------------------------------------------------------------- #
def test_graph_name_and_query_are_inlined_literals(age_store):
    age_store.find_entities(limit=5)
    sql, args = _cypher_call(age_store, "RETURN n.name AS name")
    # 图名是字面量，不是占位符
    assert "cypher('kb_graph'," in sql
    # Cypher 原文用美元引用内联，且原文确实在 SQL 里
    assert "$kb$" in sql and "MATCH (n" in sql
    # 绝不能出现 Postgres 位置参数 $1/$2 —— AGE 会报 a name constant is expected
    assert "$1" not in sql and "$2" not in sql
    # 无 agtype 参数映射时不得给 execute 传参数（否则 Cypher 里的 % 会被当占位符）
    assert args is None


def test_params_are_passed_as_single_bound_argument(age_store):
    age_store._cypher("MATCH (n) WHERE n.name = $nm RETURN n", params=({"nm": "x"},), nout=1)
    sql, args = _cypher_call(age_store, "$nm")
    # 只有第三个参数（agtype 映射）允许是绑定参数
    assert sql.rstrip().endswith("as (c0 agtype);")
    assert "%s" in sql
    assert isinstance(args, tuple) and len(args) == 1


def test_dollar_tag_avoids_collision_with_query(age_store):
    tag = age_store._dollar_tag("RETURN '$kb$'")
    assert tag != "$kb$"
    assert tag not in "RETURN '$kb$'"


def test_sql_literal_escapes_single_quotes(age_store):
    assert age_store._sql_literal("kb_graph") == "'kb_graph'"
    assert age_store._sql_literal("o'brien") == "'o''brien'"


# --------------------------------------------------------------------------- #
# 约束 1：列数
# --------------------------------------------------------------------------- #
def test_cypher_defaults_to_single_column(age_store):
    age_store._cypher("RETURN 1")
    assert _cols_of(_cypher_call(age_store, "RETURN 1")[0]) == 1


def test_cypher_honours_nout(age_store):
    age_store._cypher("MATCH (n) RETURN n.name, labels(n), n.props", nout=3)
    sql = _cypher_call(age_store, "labels(n)")[0]
    assert _cols_of(sql) == 3
    assert sql.lower().count("agtype") == 3  # 每列都必须声明为 agtype


def test_cypher_clamps_nout_to_at_least_one(age_store):
    """终止型语句（无 RETURN）仍需声明一列，故 nout=0 必须收敛到 1。"""
    age_store._cypher("MATCH (n) DETACH DELETE n", nout=0)
    assert _cols_of(_cypher_call(age_store, "DETACH DELETE")[0]) == 1


def test_find_entities_declares_three_columns(age_store):
    age_store.find_entities(limit=5)
    assert _cols_of(_cypher_call(age_store, "RETURN n.name AS name")[0]) == 3


def test_neighbors_declares_four_columns(age_store):
    age_store.neighbors("doc-1", node_type="Document")
    assert _cols_of(_cypher_call(age_store, "AS src")[0]) == 4


def test_paths_declares_two_columns(age_store):
    age_store.paths("a", "b")
    assert _cols_of(_cypher_call(age_store, "AS names")[0]) == 2


def test_export_graph_declares_five_columns(age_store):
    age_store.export_graph()
    assert _cols_of(_cypher_call(age_store, "AS sn")[0]) == 5


def test_upsert_entity_declares_one_column(age_store):
    age_store.upsert_entity("Document", "doc-1", {"doc_id": "doc-1"})
    assert _cols_of(_cypher_call(age_store, "MERGE (n:Document")[0]) == 1


def test_upsert_relation_declares_one_column(age_store):
    age_store.upsert_relation("Document", "doc-1", "MENTIONS", "IssueCategory", "退款退货")
    assert _cols_of(_cypher_call(age_store, "MERGE (a)-[r:MENTIONS")[0]) == 1


def test_clear_is_terminal_single_column(age_store):
    age_store.clear()
    assert _cols_of(_cypher_call(age_store, "DETACH DELETE n")[0]) == 1


def test_stats_count_is_single_column(age_store):
    age_store.stats()
    assert _cols_of(_cypher_call(age_store, "RETURN count(n)")[0]) == 1
    assert _cols_of(_cypher_call(age_store, "RETURN count(r)")[0]) == 1
