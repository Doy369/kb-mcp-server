"""AGE 的 `cypher()` 列定义列表必须与 `RETURN` 表达式数**逐一对齐**。

这条规则此前没有任何测试顶住，后果是 `AGEGraphStore` 的四个读接口
（`find_entities` / `neighbors` / `paths` / `export_graph`）在真实 AGE 上必然报
`return row and column definition list do not match` —— `_cypher()` 恒声明单列
`(v agtype)`，而它们都是多列 `RETURN`。CI 的 `age` job 首次真跑就因此崩掉
（`store.clear()` 之后、`find_entities` 处异常逃逸，连报告都没落盘）。

本文件用**假连接**把 `_cypher()` 生成的 SQL 抓下来断言列数，不依赖任何真实 PG：
「列定义列表」是被 PostgreSQL 严格校验的契约，值得有独立回归保护。
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
    """解析 `... as (c0 agtype, c1 agtype);` 里的列数。"""
    m = re.search(r"as \(([^)]*)\)\s*;?\s*$", sql, flags=re.IGNORECASE)
    assert m, f"未能解析列定义列表：{sql!r}"
    body = m.group(1).strip()
    return 0 if not body else len([c for c in body.split(",") if c.strip()])


def _sql_containing(store: AGEGraphStore, needle: str) -> str:
    """在捕获到的 `cypher()` 调用中，找出 Cypher 语句含 needle 的那条 SQL。"""
    for sql, args in store.conn.sink:
        if "ag_catalog.cypher" in sql and args and needle in str(args[1]):
            return sql
    raise AssertionError(f"未捕获到含 {needle!r} 的 cypher 调用；已捕获：{[a[1] for _, a in store.conn.sink if a]}")


# --------------------------------------------------------------------------- #
# 直接锁定 _cypher 的列生成规则
# --------------------------------------------------------------------------- #
def test_cypher_defaults_to_single_column(age_store):
    age_store._cypher("RETURN 1")
    assert _cols_of(_sql_containing(age_store, "RETURN 1")) == 1


def test_cypher_honours_nout(age_store):
    age_store._cypher("MATCH (n) RETURN n.name, labels(n), n.props", nout=3)
    sql = _sql_containing(age_store, "labels(n)")
    assert _cols_of(sql) == 3
    # 列名任意即可，但每个都必须是 agtype
    assert sql.lower().count("agtype") == 3


def test_cypher_clamps_nout_to_at_least_one(age_store):
    """终止型语句（无 RETURN）仍需声明一列，故 nout=0 必须收敛到 1。"""
    age_store._cypher("MATCH (n) DETACH DELETE n", nout=0)
    assert _cols_of(_sql_containing(age_store, "DETACH DELETE")) == 1


# --------------------------------------------------------------------------- #
# 逐个读接口：列定义数必须等于各自的 RETURN 表达式数
# --------------------------------------------------------------------------- #
def test_find_entities_declares_three_columns(age_store):
    age_store.find_entities(limit=5)
    assert _cols_of(_sql_containing(age_store, "RETURN n.name AS name")) == 3


def test_neighbors_declares_four_columns(age_store):
    age_store.neighbors("doc-1", node_type="Document")
    assert _cols_of(_sql_containing(age_store, "AS src")) == 4


def test_paths_declares_two_columns(age_store):
    age_store.paths("a", "b")
    assert _cols_of(_sql_containing(age_store, "AS names")) == 2


def test_export_graph_declares_five_columns(age_store):
    age_store.export_graph()
    assert _cols_of(_sql_containing(age_store, "AS sn")) == 5


# --------------------------------------------------------------------------- #
# 单列 / 终止型语句不得被误改
# --------------------------------------------------------------------------- #
def test_upsert_entity_declares_one_column(age_store):
    age_store.upsert_entity("Document", "doc-1", {"doc_id": "doc-1"})
    assert _cols_of(_sql_containing(age_store, "MERGE (n:Document")) == 1


def test_upsert_relation_declares_one_column(age_store):
    age_store.upsert_relation("Document", "doc-1", "MENTIONS", "IssueCategory", "退款退货")
    assert _cols_of(_sql_containing(age_store, "MERGE (a)-[r:MENTIONS")) == 1


def test_clear_is_terminal_single_column(age_store):
    age_store.clear()
    assert _cols_of(_sql_containing(age_store, "DETACH DELETE n")) == 1


def test_stats_count_is_single_column(age_store):
    age_store.stats()
    assert _cols_of(_sql_containing(age_store, "RETURN count(n)")) == 1
    assert _cols_of(_sql_containing(age_store, "RETURN count(r)")) == 1
