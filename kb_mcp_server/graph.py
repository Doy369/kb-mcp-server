"""知识图谱层（P6 · GraphRAG 关系增强层）。

它在 kb-mcp-server 里的定位（对应架构决策 D1-D6）：
  - D1 定位：不替代向量检索，只补上「实体关系 + 多跳推理 + 可解释路径」。
  - D2 存储：memory（纯 Python，JSON 落盘，离线零依赖）| age（Postgres + Apache AGE，与 pgvector 同库）。
  - D3 本体：8 类节点 / 7 类关系，本体驱动，抽取与查询都在这个边界内，防止图谱发散。
  - D4 构建：本地 LLM 抽取优先，规则词典兜底，保证离线也能建图。
  - D6 协议：图谱能力经 MCP 工具（graph_query / graph_expand / graph_paths / graph_stats）
            暴露给任意 agent，多 agent 共享同一张图。

数据模型：
  节点 key = "类型:名称"（全局唯一）
  边 = (src_key, 关系, dst_key, props)

后端降级策略（沿用项目既有风格）：
  age 后端连接失败 / 扩展缺失时，get_graph_store() 自动回退 memory，绝不阻断主链路。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from kb_mcp_server.config import DATA_DIR, PROJECT_ROOT, get_cfg


# --------------------------------------------------------------------------- #
# 本体（D3 最小可行本体）：8 类节点 / 7 类关系
# --------------------------------------------------------------------------- #
NODE_TYPES: dict[str, str] = {
    "Document": "文档",
    "Ticket": "工单",
    "Customer": "客户",
    "Product": "产品",
    "IssueCategory": "问题类别",
    "SLAClause": "SLA条款",
    "Solution": "解决方案",
    "RootCause": "根因",
}

RELATIONS: dict[str, str] = {
    "MENTIONS": "提及",
    "CATEGORY_OF": "归类为",
    "SUBMITTED_BY": "由…提交",
    "ABOUT_PRODUCT": "涉及产品",
    "GOVERNED_BY": "适用条款",
    "SOLVED_BY": "解决方案为",
    "CAUSED_BY": "根因为",
}

# 关系的主语/宾语类型约束。本体驱动的核心：不合规的三元组直接丢弃，不让脏关系污染图。
REL_SCHEMA: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "MENTIONS":      (("Document",), ("IssueCategory", "Product", "SLAClause", "Solution", "RootCause")),
    "CATEGORY_OF":   (("Ticket",), ("IssueCategory",)),
    "SUBMITTED_BY":  (("Ticket",), ("Customer",)),
    "ABOUT_PRODUCT": (("Ticket", "IssueCategory"), ("Product",)),
    "GOVERNED_BY":   (("IssueCategory",), ("SLAClause",)),
    "SOLVED_BY":     (("IssueCategory",), ("Solution",)),
    "CAUSED_BY":     (("IssueCategory", "Ticket"), ("RootCause",)),
}


def relation_is_valid(rel: str, subj_type: str, obj_type: str) -> bool:
    """校验三元组是否符合本体约束。"""
    schema = REL_SCHEMA.get(rel)
    if not schema:
        return False
    subjects, objects = schema
    return subj_type in subjects and obj_type in objects


@dataclass
class Triple:
    """一条知识三元组。"""

    subject: str
    subject_type: str
    relation: str
    object: str
    object_type: str
    props: dict = field(default_factory=dict)

    def is_valid(self) -> bool:
        return (
            self.subject_type in NODE_TYPES
            and self.object_type in NODE_TYPES
            and relation_is_valid(self.relation, self.subject_type, self.object_type)
            and bool(self.subject.strip())
            and bool(self.object.strip())
        )

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "subject_type": self.subject_type,
            "relation": self.relation,
            "object": self.object,
            "object_type": self.object_type,
            "relation_label": RELATIONS.get(self.relation, self.relation),
            "props": self.props,
        }


def _node_key(node_type: str, name: str) -> str:
    return f"{node_type}:{name}"


# --------------------------------------------------------------------------- #
# 存储抽象 + 两个后端
# --------------------------------------------------------------------------- #
_GRAPH_STORE_PATH = os.getenv("KB_GRAPH_STORE") or (
    os.path.join(DATA_DIR, "kb_graph.json")
    if DATA_DIR
    else os.path.join(PROJECT_ROOT, "kb_graph.json")
)


class GraphStore(ABC):
    """图存储接口。上层（MCP 工具 / 合成层）只依赖这个抽象。"""

    backend: str = "abstract"

    @abstractmethod
    def ensure_schema(self) -> None: ...

    @abstractmethod
    def upsert_entity(self, node_type: str, name: str, props: dict | None = None) -> str: ...

    @abstractmethod
    def upsert_relation(self, subj_type: str, subj: str, rel: str,
                        obj_type: str, obj: str, props: dict | None = None) -> bool: ...

    @abstractmethod
    def find_entities(self, name: str = "", node_type: str | None = None, limit: int = 20) -> list[dict]: ...

    @abstractmethod
    def neighbors(self, name: str, node_type: str | None = None, rel: str | None = None,
                  direction: str = "out", depth: int = 1, limit: int = 50) -> dict: ...

    @abstractmethod
    def paths(self, src: str, dst: str, max_depth: int = 3, limit: int = 10) -> list[dict]: ...

    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def clear(self) -> int: ...

    @abstractmethod
    def delete_by_doc(self, doc_id: str) -> dict: ...

    @abstractmethod
    def export_graph(self) -> dict:
        """全量导出节点 + 边，供前端力导向可视化使用。返回 {backend, nodes, edges}。"""

    # ---- 便捷方法：批量写入三元组（本体校验在基类统一做）----
    def add_triples(self, triples: list[Triple]) -> dict:
        """写入一批三元组，返回 {added, skipped, invalid}。不合规的按本体约束丢弃。"""
        added = skipped = invalid = 0
        for t in triples:
            if not t.is_valid():
                invalid += 1
                continue
            self.upsert_entity(t.subject_type, t.subject)
            self.upsert_entity(t.object_type, t.object, t.props)
            ok = self.upsert_relation(
                t.subject_type, t.subject, t.relation, t.object_type, t.object, t.props
            )
            if ok:
                added += 1
            else:
                skipped += 1
        return {"added": added, "skipped": skipped, "invalid": invalid}


class MemoryGraphStore(GraphStore):
    """纯 Python 内存图：邻接表 + BFS 多跳 + JSON 落盘。

    离线零依赖，与 MemoryVectorStore 同款思路：启动恢复、增删后落盘、损坏则忽略。
    """

    backend = "memory"

    def __init__(self):
        self._nodes: dict[str, dict] = {}          # key -> {type, name, props}
        self._out: dict[str, list[tuple[str, str]]] = {}   # key -> [(rel, dst_key)]
        self._in: dict[str, list[tuple[str, str]]] = {}    # key -> [(rel, src_key)]
        self._edges: list[dict] = []               # 边全量（含 props），用于删除与统计
        self._defer_persist = False                # 批量写入时暂缓落盘
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not os.path.exists(_GRAPH_STORE_PATH):
            return
        try:
            with open(_GRAPH_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._nodes = data.get("nodes", {})
            self._edges = data.get("edges", [])
            self._reindex()
        except Exception:
            self._nodes, self._edges = {}, []
            self._out, self._in = {}, {}

    def _reindex(self) -> None:
        self._out, self._in = {}, {}
        for e in self._edges:
            self._out.setdefault(e["src"], []).append((e["rel"], e["dst"]))
            self._in.setdefault(e["dst"], []).append((e["rel"], e["src"]))

    def _persist(self) -> None:
        if self._defer_persist:
            return
        try:
            payload = {"nodes": self._nodes, "edges": self._edges}
            d = os.path.dirname(_GRAPH_STORE_PATH) or "."
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, default=str)
            os.replace(tmp, _GRAPH_STORE_PATH)
        except Exception:
            pass

    def ensure_schema(self) -> None:
        pass

    # ---- 写入 ----
    def upsert_entity(self, node_type: str, name: str, props: dict | None = None) -> str:
        if node_type not in NODE_TYPES:
            raise ValueError(f"未知节点类型：{node_type}（本体允许：{list(NODE_TYPES)}）")
        key = _node_key(node_type, name)
        node = self._nodes.get(key)
        if node is None:
            p = dict(props or {})
            if p.get("doc_id"):
                p["docs"] = [p["doc_id"]]      # 溯源：该实体出自哪些文档
            self._nodes[key] = {"type": node_type, "name": name, "props": p}
            self._persist()
        elif props:
            # 已存在：不覆盖首个来源，但把新来源文档追加进 docs（同一实体常被多篇文档提及）
            node["props"].update(
                {k: v for k, v in props.items() if k not in ("doc_id", "method", "evidence")}
            )
            d = props.get("doc_id")
            if d:
                docs = node["props"].setdefault("docs", [])
                if d not in docs and len(docs) < 5:
                    docs.append(d)
                    self._persist()
        return key

    def upsert_relation(self, subj_type: str, subj: str, rel: str,
                        obj_type: str, obj: str, props: dict | None = None) -> bool:
        if not relation_is_valid(rel, subj_type, obj_type):
            return False
        src = self.upsert_entity(subj_type, subj)
        dst = self.upsert_entity(obj_type, obj)
        for e in self._edges:
            if e["src"] == src and e["dst"] == dst and e["rel"] == rel:
                e["props"].update(props or {})
                self._persist()
                return False  # 已存在，不重复计数
        self._edges.append({"src": src, "rel": rel, "dst": dst, "props": dict(props or {})})
        self._out.setdefault(src, []).append((rel, dst))
        self._in.setdefault(dst, []).append((rel, src))
        self._persist()
        return True

    # ---- 查询 ----
    def _node_out(self, key: str) -> dict | None:
        n = self._nodes.get(key)
        if not n:
            return None
        return {
            "key": key,
            "type": n["type"],
            "type_label": NODE_TYPES.get(n["type"], n["type"]),
            "name": n["name"],
            "props": n.get("props", {}),
            "degree": len(self._out.get(key, [])) + len(self._in.get(key, [])),
        }

    def _resolve(self, name: str, node_type: str | None = None) -> str | None:
        """按名称定位节点：先精确，后包含匹配。可指定类型缩小范围。"""
        name = (name or "").strip()
        if not name:
            return None
        if ":" in name and name.split(":", 1)[0] in NODE_TYPES:
            cand = name
            if cand in self._nodes:
                return cand
            name = name.split(":", 1)[1]
        exact = _resolve_in(self._nodes, name, node_type, exact=True)
        if exact:
            return exact
        return _resolve_in(self._nodes, name, node_type, exact=False)

    def find_entities(self, name: str = "", node_type: str | None = None, limit: int = 20) -> list[dict]:
        out = []
        for key, n in self._nodes.items():
            if node_type and n["type"] != node_type:
                continue
            if name and name not in n["name"]:
                continue
            out.append(self._node_out(key))
        out.sort(key=lambda x: (-x["degree"], x["name"]))
        return out[:limit]

    def _adjacency(self, key: str, direction: str) -> list[tuple[str, str, str]]:
        """返回 [(rel, other_key, 方向标记)]。"""
        res = []
        if direction in ("out", "both"):
            for rel, dst in self._out.get(key, []):
                res.append((rel, dst, "out"))
        if direction in ("in", "both"):
            for rel, src in self._in.get(key, []):
                res.append((rel, src, "in"))
        return res

    def neighbors(self, name: str, node_type: str | None = None, rel: str | None = None,
                  direction: str = "out", depth: int = 1, limit: int = 50) -> dict:
        start = self._resolve(name, node_type)
        if not start:
            return {"entity": None, "neighbors": [], "note": f"未找到实体：{name}"}
        depth = max(1, min(int(depth), 4))
        visited = {start}
        frontier: list[tuple[str, list[dict]]] = [(start, [])]
        out: list[dict] = []
        for d in range(1, depth + 1):
            nxt: list[tuple[str, list[dict]]] = []
            for key, path in frontier:
                for edge_rel, other, dirn in self._adjacency(key, direction):
                    if rel and edge_rel != rel:
                        continue
                    if other in visited:
                        continue
                    visited.add(other)
                    step = {
                        "rel": edge_rel,
                        "rel_label": RELATIONS.get(edge_rel, edge_rel),
                        "direction": dirn,
                        "node": self._node_out(other),
                    }
                    p = path + [step]
                    out.append({"depth": d, "path": p, "node": step["node"]})
                    nxt.append((other, p))
            frontier = nxt
            if not frontier or len(out) >= limit:
                break
        return {"entity": self._node_out(start), "neighbors": out[:limit], "backend": "memory"}

    def paths(self, src: str, dst: str, max_depth: int = 3, limit: int = 10) -> list[dict]:
        """找两实体间的所有简单路径（DFS，深度受限）。路径本身就是可解释证据。"""
        s = self._resolve(src)
        t = self._resolve(dst)
        if not s or not t:
            return []
        max_depth = max(1, min(int(max_depth), 5))
        found: list[dict] = []

        def _fmt(keys: list[str], rels: list[tuple[str, str]]) -> list[dict]:
            steps = []
            for i, r in enumerate(rels):
                steps.append({
                    "from": self._node_out(keys[i]),
                    "rel": r[0],
                    "rel_label": RELATIONS.get(r[0], r[0]),
                    "direction": r[1],
                    "to": self._node_out(keys[i + 1]),
                })
            return steps

        def _dfs(cur: str, visited: set[str], keys: list[str], rels: list[tuple[str, str]]) -> None:
            if len(found) >= limit:
                return
            if cur == t and len(rels) >= 1:
                found.append({"length": len(rels), "steps": _fmt(keys, rels)})
                return
            if len(rels) >= max_depth:
                return
            for edge_rel, nxt, dirn in self._adjacency(cur, "both"):
                if nxt in visited:
                    continue
                _dfs(nxt, visited | {nxt}, keys + [nxt], rels + [(edge_rel, dirn)])

        _dfs(s, {s}, [s], [])
        found.sort(key=lambda x: x["length"])
        return found

    def stats(self) -> dict:
        by_type: dict[str, int] = {}
        for n in self._nodes.values():
            by_type[n["type"]] = by_type.get(n["type"], 0) + 1
        by_rel: dict[str, int] = {}
        for e in self._edges:
            by_rel[e["rel"]] = by_rel.get(e["rel"], 0) + 1
        return {
            "backend": "memory",
            "nodes": len(self._nodes),
            "edges": len(self._edges),
            "by_type": by_type,
            "by_relation": by_rel,
            "ontology": {"node_types": NODE_TYPES, "relations": RELATIONS},
        }

    def export_graph(self) -> dict:
        nodes = [{
            "id": key,
            "name": n["name"],
            "type": n["type"],
            "type_label": NODE_TYPES.get(n["type"], n["type"]),
            "degree": len(self._out.get(key, [])) + len(self._in.get(key, [])),
        } for key, n in self._nodes.items()]
        edges = [{
            "source": e["src"],
            "target": e["dst"],
            "rel": e["rel"],
            "rel_label": RELATIONS.get(e["rel"], e["rel"]),
        } for e in self._edges]
        return {"backend": "memory", "nodes": nodes, "edges": edges}

    def clear(self) -> int:
        n = len(self._edges)
        self._nodes, self._edges = {}, []
        self._out, self._in = {}, {}
        self._persist()
        return n

    def delete_by_doc(self, doc_id: str) -> dict:
        """删除某文档抽取出的边，以及该文档节点本身。"""
        keep = [e for e in self._edges if e.get("props", {}).get("doc_id") != doc_id]
        removed_edges = len(self._edges) - len(keep)
        self._edges = keep
        self._nodes.pop(_node_key("Document", doc_id), None)
        self._reindex()
        self._persist()
        return {"doc_id": doc_id, "removed_edges": removed_edges}

    def add_triples(self, triples: list[Triple]) -> dict:
        """批量写入覆写：整批写完落一次盘。

        逐条落盘在一篇文档几十条三元组时，Windows 上每次原子替换都是全量重写，
        能慢出一个数量级（实测 4 篇文档 12.7s → 数百 ms）。
        """
        self._defer_persist = True
        try:
            return super().add_triples(triples)
        finally:
            self._defer_persist = False
            self._persist()


def _resolve_in(nodes: dict, name: str, node_type: str | None, exact: bool) -> str | None:
    for key, n in nodes.items():
        if node_type and n["type"] != node_type:
            continue
        hit = (n["name"] == name) if exact else (name in n["name"] or n["name"] in name)
        if hit:
            return key
    return None


class AGEGraphStore(GraphStore):
    """Postgres + Apache AGE 图存储（生产后端，与 pgvector 同库，对应决策 D2）。

    前置条件（见 setup_db.py --graph）：
      CREATE EXTENSION age;
      SET search_path = ag_catalog, "$user", public;
      SELECT create_graph('kb_graph');

    注意：AGE 的 cypher() 在不同版本对参数绑定的支持不一致，实现里统一做标识符转义，
    生产启用前请在目标 AGE 版本上跑一遍 demo_graph.py --backend age 验证。
    """

    backend = "age"

    def __init__(self, dsn: str | None = None, graph_name: str | None = None):
        from kb_mcp_server.config import get_settings

        s = get_settings()
        self.dsn = dsn or s.database_url
        self.graph_name = graph_name or get_cfg("KB_GRAPH_NAME", "kb_graph")
        self.conn = None

    def connect(self) -> None:
        import psycopg  # noqa: PLC0415

        self.conn = psycopg.connect(self.dsn, autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS age;")
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")

    def ensure_schema(self) -> None:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(
                "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s;", (self.graph_name,)
            )
            exists = cur.fetchone()[0]
            if not exists:
                cur.execute("SELECT ag_catalog.create_graph(%s);", (self.graph_name,))
            # 名称索引：让按 name 定位实体走索引而不是全图扫
            try:
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS kb_graph_name_idx "
                    f"ON ag_catalog.ag_label USING btree (name) WHERE name = %s;",
                    (self.graph_name,),
                )
            except Exception:
                pass

    # ---- Cypher 执行 ----
    def _cypher(self, query: str, params: tuple = ()) -> list[tuple]:
        assert self.conn is not None
        sql = (
            "SELECT * FROM ag_catalog.cypher(%s, %s"
            + (", %s" if params else "")
            + ") as (v agtype);"
        )
        args = (self.graph_name, query) + (tuple(params),) if params else (self.graph_name, query)
        with self.conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(sql, args)
            return cur.fetchall()

    @staticmethod
    def _esc(v: str) -> str:
        return (v or "").replace("\\", "\\\\").replace("'", "\\'")

    def upsert_entity(self, node_type: str, name: str, props: dict | None = None) -> str:
        if node_type not in NODE_TYPES:
            raise ValueError(f"未知节点类型：{node_type}")
        q = (
            f"MERGE (n:{node_type} {{name: '{self._esc(name)}'}}) "
            f"SET n.props = '{self._esc(json.dumps(props or {}, ensure_ascii=False))}' RETURN n"
        )
        self._cypher(q)
        return _node_key(node_type, name)

    def upsert_relation(self, subj_type: str, subj: str, rel: str,
                        obj_type: str, obj: str, props: dict | None = None) -> bool:
        if not relation_is_valid(rel, subj_type, obj_type):
            return False
        q = (
            f"MERGE (a:{subj_type} {{name: '{self._esc(subj)}'}}) "
            f"MERGE (b:{obj_type} {{name: '{self._esc(obj)}'}}) "
            f"MERGE (a)-[r:{rel} {{{''}}}]->(b) "
            f"SET r.props = '{self._esc(json.dumps(props or {}, ensure_ascii=False))}' RETURN r"
        )
        self._cypher(q)
        return True

    def find_entities(self, name: str = "", node_type: str | None = None, limit: int = 20) -> list[dict]:
        label = f":{node_type}" if node_type else ""
        where = f"WHERE n.name CONTAINS '{self._esc(name)}'" if name else ""
        q = f"MATCH (n{label}) {where} RETURN n.name AS name, labels(n) AS labels, n.props AS props LIMIT {int(limit)}"
        rows = self._cypher(q)
        out = []
        for r in rows:
            nm = _age_str(r[0])
            labels = _age_list(r[1]) or ["Unknown"]
            out.append({
                "key": _node_key(labels[0], nm),
                "type": labels[0],
                "type_label": NODE_TYPES.get(labels[0], labels[0]),
                "name": nm,
                "props": _age_dict(r[2]),
                "degree": 0,
            })
        return out

    def neighbors(self, name: str, node_type: str | None = None, rel: str | None = None,
                  direction: str = "out", depth: int = 1, limit: int = 50) -> dict:
        label = f":{node_type}" if node_type else ""
        rel_pat = f":{rel}" if rel else ""
        depth = max(1, min(int(depth), 4))
        arrow = {"out": f"-[r{rel_pat}*1..{depth}]->", "in": f"<-[r{rel_pat}*1..{depth}]-",
                 "both": f"-[r{rel_pat}*1..{depth}]-"}[direction if direction in ("out", "in", "both") else "out"]
        q = (
            f"MATCH (n{label} {{name: '{self._esc(name)}'}}){arrow}(m) "
            f"RETURN n.name AS src, [e IN r | type(e)] AS rels, m.name AS dst, labels(m) AS labels LIMIT {int(limit)}"
        )
        rows = self._cypher(q)
        start = self.find_entities(name=name, node_type=node_type, limit=1)
        nbrs = []
        for r in rows:
            rels = _age_list(r[1]) or []
            dst = _age_str(r[2])
            labels = _age_list(r[3]) or ["Unknown"]
            steps = [{
                "rel": rels[-1] if rels else "",
                "rel_label": RELATIONS.get(rels[-1], rels[-1]) if rels else "",
                "direction": direction,
                "node": {
                    "key": _node_key(labels[0], dst),
                    "type": labels[0],
                    "type_label": NODE_TYPES.get(labels[0], labels[0]),
                    "name": dst,
                    "props": {},
                    "degree": 0,
                },
            } for _ in rels] or []
            nbrs.append({"depth": len(rels), "path": steps, "node": steps[-1]["node"] if steps else None})
        return {"entity": start[0] if start else None, "neighbors": nbrs, "backend": "age"}

    def paths(self, src: str, dst: str, max_depth: int = 3, limit: int = 10) -> list[dict]:
        max_depth = max(1, min(int(max_depth), 5))
        q = (
            f"MATCH p = (a {{name: '{self._esc(src)}'}})-[*1..{max_depth}]-(b {{name: '{self._esc(dst)}'}}) "
            f"RETURN [n IN nodes(p) | n.name] AS names, [r IN relationships(p) | type(r)] AS rels LIMIT {int(limit)}"
        )
        rows = self._cypher(q)
        out = []
        for r in rows:
            names = _age_list(r[0]) or []
            rels = _age_list(r[1]) or []
            steps = []
            for i, rel in enumerate(rels):
                steps.append({
                    "from": {"name": names[i], "type": "", "type_label": ""},
                    "rel": rel,
                    "rel_label": RELATIONS.get(rel, rel),
                    "direction": "out",
                    "to": {"name": names[i + 1] if i + 1 < len(names) else "", "type": "", "type_label": ""},
                })
            out.append({"length": len(rels), "steps": steps})
        out.sort(key=lambda x: x["length"])
        return out

    def stats(self) -> dict:
        node_rows = self._cypher("MATCH (n) RETURN count(n) AS c")
        edge_rows = self._cypher("MATCH ()-[r]->() RETURN count(r) AS c")
        return {
            "backend": "age",
            "graph": self.graph_name,
            "nodes": _age_int(node_rows[0][0]) if node_rows else 0,
            "edges": _age_int(edge_rows[0][0]) if edge_rows else 0,
            "ontology": {"node_types": NODE_TYPES, "relations": RELATIONS},
        }

    def export_graph(self) -> dict:
        q = (
            "MATCH (n)-[r]->(m) "
            "RETURN n.name AS sn, labels(n) AS sl, type(r) AS rel, m.name AS mn, labels(m) AS ml"
        )
        rows = self._cypher(q)
        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        for r in rows:
            sn = _age_str(r[0]); sl = _age_list(r[1]) or ["Unknown"]
            rel = _age_str(r[2]); mn = _age_str(r[3]); ml = _age_list(r[4]) or ["Unknown"]
            sk = _node_key(sl[0], sn); dk = _node_key(ml[0], mn)
            nodes[sk] = {"id": sk, "name": sn, "type": sl[0],
                         "type_label": NODE_TYPES.get(sl[0], sl[0]), "degree": 0}
            nodes[dk] = {"id": dk, "name": mn, "type": ml[0],
                         "type_label": NODE_TYPES.get(ml[0], ml[0]), "degree": 0}
            edges.append({"source": sk, "target": dk, "rel": rel,
                          "rel_label": RELATIONS.get(rel, rel)})
        for e in edges:
            nodes[e["source"]]["degree"] += 1
            nodes[e["target"]]["degree"] += 1
        return {"backend": "age", "nodes": list(nodes.values()), "edges": edges}

    def clear(self) -> int:
        before = self.stats().get("edges", 0)
        self._cypher("MATCH (n) DETACH DELETE n")
        return int(before or 0)

    def delete_by_doc(self, doc_id: str) -> dict:
        q = (
            f"MATCH ()-[r {{doc_id: '{self._esc(doc_id)}'}}]->() DELETE r"
        )
        self._cypher(q)
        self._cypher(
            f"MATCH (n:Document {{name: '{self._esc(doc_id)}'}}) DETACH DELETE n"
        )
        return {"doc_id": doc_id, "removed_edges": -1}


def _age_str(v) -> str:
    s = str(v or "")
    return s.strip('"')


def _age_int(v) -> int:
    try:
        return int(str(v).strip('"'))
    except Exception:
        return 0


def _age_list(v) -> list:
    import re as _re

    s = str(v or "")
    return [x.strip().strip('"') for x in _re.findall(r'"([^"]*)"', s)]


def _age_dict(v) -> dict:
    import json as _json

    try:
        return _json.loads(str(v or "{}"))
    except Exception:
        return {}


def get_graph_store() -> GraphStore:
    """按配置返回图存储；age 后端不可用时自动回退 memory（不阻断主链路）。"""
    backend = get_cfg("KB_GRAPH_BACKEND", "memory").lower()
    if backend == "age":
        try:
            store: GraphStore = AGEGraphStore()
            store.connect()  # type: ignore[attr-defined]
            store.ensure_schema()
            return store
        except Exception as e:  # noqa: BLE001
            print(f"[graph] AGE 后端不可用（{e}），已回退 memory 图存储")
    s = MemoryGraphStore()
    s.ensure_schema()
    return s


# --------------------------------------------------------------------------- #
# 三元组抽取（D4）：本地 LLM 优先，规则词典兜底
# --------------------------------------------------------------------------- #
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "物流配送": ("物流", "配送", "发货", "运输", "快递", "送达", "签收", "仓储", "运费"),
    "退款退货": ("退款", "退货", "退换", "退回", "驳回"),
    "保修维修": ("保修", "维修", "维保", "检修", "质保", "换新"),
    "发票计费": ("发票", "计费", "开票", "账单", "扣费", "对账", "结算"),
    "账户权限": ("账户", "账号", "权限", "登录", "密码", "实名", "子账号"),
    "续费签约": ("续费", "签约", "合同", "续约", "年费", "到期"),
    "技术支持": ("故障", "报错", "异常", "宕机", "无法使用", "崩溃"),
    "服务响应": ("响应", "受理", "工单", "客服", "投诉", "升级"),
}

_SLA_RX = re.compile(r"(\d+)\s*(个)?\s*(分钟|小时|工作日|自然日|天)")
# 动作词：中文里常出现在时限之后（「48小时内发货」），也可能在前（「响应时效 2 小时」），两��都看
_SLA_ACTION = (
    "响应", "回复", "受理", "处理", "解决", "修复", "送达", "赔付", "退款", "上门", "发货",
    "退货", "换货", "开具", "到账", "出库", "完成", "交付", "对账", "开票", "审核",
)

_SOLUTION_KEYWORDS = (
    "全额退款", "部分退款", "赔偿", "补偿", "换货", "重新发货", "重发", "免费维修",
    "优惠券", "代金券", "延期", "上门服务", "技术支持", "重新开票", "补发",
)

_PRODUCT_RX = re.compile(r"([一-龥A-Za-z0-9]{0,8}(?:专业版|企业版|标准版|旗舰版|基础版|云服务|云平台))")
_PRODUCT_WORDS = ("SLA", "CRM", "ERP", "OA", "SaaS", "PaaS", "API", "SDK", "VPN", "WMS", "TMS")

_CAUSE_RX = re.compile(r"(?:原因(?:是|在于)?[：:]?|由于|因为|系因|根因[：:]?)\s*([^\n。；;，,]{2,24})")

# SLA 时限的上下文窗口按这些标点截断，避免跨句串味
_CLAUSE_SPLIT = re.compile(r"[，。；;：:、\n]")


def _nearest_action(prefix: str, suffix: str) -> str:
    """取离时限最近的动作词：先看时限后（取最靠前的），再看时限前（取最靠后的）。

    不能按词表顺序取第一个命中——那样「内完成。退款原路返回」会被判成「退款」。
    """
    best: tuple[int, str] | None = None
    for a in _SLA_ACTION:
        i = suffix.find(a)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, a)
    if best:
        return best[1]
    best2: tuple[int, str] | None = None
    for a in _SLA_ACTION:
        i = prefix.rfind(a)
        if i >= 0 and (best2 is None or i > best2[0]):
            best2 = (i, a)
    return best2[1] if best2 else ""
_TICKET_RX = re.compile(r"((?:工单|订单|单号|案例)[^，。；\s]{0,12}?([A-Za-z]*\d{3,}))")
_CUSTOMER_RX = re.compile(r"([一-龥A-Za-z]{2,12}(?:集团|公司|科技|有限公司|股份))")

_LLM_EXTRACT_MAX_CHARS = 3000


class TripleExtractor:
    """从文本抽取三元组。LLM 可用时用它（更准），否则规则词典兜底（离线必跑通）。"""

    def __init__(self, use_llm: bool | None = None):
        self._use_llm = use_llm

    def _llm_on(self) -> bool:
        if self._use_llm is not None:
            return self._use_llm
        return get_cfg("KB_LLM_ENABLED", "0").lower() in ("1", "true", "yes")

    def extract(self, text: str, doc_id: str) -> list[Triple]:
        text = (text or "").strip()
        if not text:
            return []
        if self._llm_on():
            got = self._extract_by_llm(text, doc_id)
            if got:
                return got
        return self.extract_by_rules(text, doc_id)

    # ---- 规则抽取：不依赖任何模型，离线可用 ----
    def extract_by_rules(self, text: str, doc_id: str) -> list[Triple]:
        triples: list[Triple] = []
        props_base = {"doc_id": doc_id, "method": "rule"}

        def _evi(kw: str) -> str:
            """取关键词附近的一句话作为证据片段（可解释性来源）。"""
            i = text.find(kw)
            if i < 0:
                return ""
            s = max(0, i - 20)
            return text[s:i + 60].replace("\n", " ").strip()

        # 1) 问题类别：按关键词命中次数排序，取最相关的前 3 类
        cats = self._match_categories(text)
        for cat, cnt in cats:
            triples.append(Triple(doc_id, "Document", "MENTIONS", cat, "IssueCategory",
                                  {**props_base, "hits": cnt, "evidence": _evi(cat[:2])}))

        primary = cats[0][0] if cats else None

        # 2) SLA 条款：时限表达 + 就近动作词
        for clause, evi in self._match_sla(text):
            if primary:
                triples.append(Triple(primary, "IssueCategory", "GOVERNED_BY", clause, "SLAClause",
                                      {**props_base, "evidence": evi}))
            else:
                triples.append(Triple(doc_id, "Document", "MENTIONS", clause, "SLAClause",
                                      {**props_base, "evidence": evi}))

        # 3) 解决方案
        for sol in self._match_solutions(text):
            if primary:
                triples.append(Triple(primary, "IssueCategory", "SOLVED_BY", sol, "Solution",
                                      {**props_base, "evidence": _evi(sol)}))
            else:
                triples.append(Triple(doc_id, "Document", "MENTIONS", sol, "Solution",
                                      {**props_base, "evidence": _evi(sol)}))

        # 4) 产品
        for prod in self._match_products(text):
            if primary:
                triples.append(Triple(primary, "IssueCategory", "ABOUT_PRODUCT", prod, "Product",
                                      {**props_base}))
            else:
                triples.append(Triple(doc_id, "Document", "MENTIONS", prod, "Product", {**props_base}))

        # 5) 根因
        for cause, evi in self._match_root_causes(text):
            if primary:
                triples.append(Triple(primary, "IssueCategory", "CAUSED_BY", cause, "RootCause",
                                      {**props_base, "evidence": evi}))

        # 6) 工单 / 客户：文本里出现具体单号或客户名时才建（避免噪音）
        for tk in self._match_tickets(text):
            triples.append(Triple(tk, "Ticket", "CATEGORY_OF", primary or "未分类", "IssueCategory",
                                  {**props_base}))
        for cust in self._match_customers(text):
            for tk in self._match_tickets(text)[:1]:
                triples.append(Triple(tk, "Ticket", "SUBMITTED_BY", cust, "Customer", {**props_base}))

        return [t for t in triples if t.is_valid()]

    # ---- 各类实体的匹配 ----
    def _match_categories(self, text: str) -> list[tuple[str, int]]:
        scored = []
        for cat, kws in _CATEGORY_KEYWORDS.items():
            cnt = sum(text.count(k) for k in kws)
            if cnt:
                scored.append((cat, cnt))
        scored.sort(key=lambda x: -x[1])
        return scored[:3]

    def _match_sla(self, text: str) -> list[tuple[str, str]]:
        out = []
        seen = set()
        for m in _SLA_RX.finditer(text):
            num, _, unit = m.group(1), m.group(2), m.group(3)
            # 上下文窗口必须在标点处截断：否则前一句的动作词会串到后一句的时限上
            prefix = _CLAUSE_SPLIT.split(text[max(0, m.start() - 12):m.start()])[-1]
            suffix = _CLAUSE_SPLIT.split(text[m.end():m.end() + 12])[0]
            action = _nearest_action(prefix, suffix)
            name = f"{num}{unit}内{action}" if action else f"{num}{unit}时限"
            if name in seen:
                continue
            seen.add(name)
            out.append((name, text[max(0, m.start() - 20):m.end() + 20].replace("\n", " ").strip()))
        return out[:5]

    def _match_solutions(self, text: str) -> list[str]:
        return [k for k in _SOLUTION_KEYWORDS if k in text][:4]

    def _match_products(self, text: str) -> list[str]:
        found = []
        for m in _PRODUCT_RX.finditer(text):
            nm = m.group(1).strip()
            if len(nm) >= 2 and nm not in found:
                found.append(nm)
        for w in _PRODUCT_WORDS:
            if re.search(rf"\b{w}\b", text) and w not in found:
                found.append(w)
        return found[:4]

    def _match_root_causes(self, text: str) -> list[tuple[str, str]]:
        out, seen = [], set()
        for m in _CAUSE_RX.finditer(text):
            c = m.group(1).strip(" 的了吗呢 ")
            if 2 <= len(c) <= 24 and c not in seen:
                seen.add(c)
                out.append((c, m.group(0).replace("\n", " ").strip()[:80]))
        return out[:3]

    def _match_tickets(self, text: str) -> list[str]:
        return [m.group(1).strip() for m in _TICKET_RX.finditer(text)][:3]

    def _match_customers(self, text: str) -> list[str]:
        return [m.group(1).strip() for m in _CUSTOMER_RX.finditer(text)][:3]

    # ---- LLM 抽取 ----
    def _extract_by_llm(self, text: str, doc_id: str) -> list[Triple]:
        from kb_mcp_server.llmclient import llm_chat

        snippet = text[:_LLM_EXTRACT_MAX_CHARS]
        node_desc = "、".join(f"{k}（{v}）" for k, v in NODE_TYPES.items())
        rel_desc = "、".join(f"{k}（{v}）" for k, v in RELATIONS.items())
        prompt = (
            "你是企业客服知识图谱抽取器。从给定文本中抽取知识三元组。\n"
            f"允许的实体类型：{node_desc}\n"
            f"允许的关系类型：{rel_desc}\n"
            "关系还必须满足以下主宾类型约束，违反的输出会被丢弃：\n"
            + "\n".join(
                f"- {r}：主语∈{list(s)}，宾语∈{list(o)}" for r, (s, o) in REL_SCHEMA.items()
            )
            + "\n\n只输出 JSON 数组，不要任何解释文字，格式：\n"
            '[{"subject":"","subject_type":"","relation":"","object":"","object_type":""}]\n\n'
            f"文本：\n{snippet}\n\nJSON："
        )
        raw = llm_chat(prompt, temperature=0.0, max_tokens=800, timeout=25)
        if not raw:
            return []
        return _parse_triples_json(raw, doc_id)


def _parse_triples_json(raw: str, doc_id: str) -> list[Triple]:
    """容错解析 LLM 输出：去 code fence、截数组、逐条校验本体。"""
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    i, j = raw.find("["), raw.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        arr = json.loads(raw[i:j + 1])
    except Exception:
        return []
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        t = Triple(
            subject=str(item.get("subject", "")).strip(),
            subject_type=str(item.get("subject_type", "")).strip(),
            relation=str(item.get("relation", "")).strip(),
            object=str(item.get("object", "")).strip(),
            object_type=str(item.get("object_type", "")).strip(),
            props={"doc_id": doc_id, "method": "llm"},
        )
        if t.is_valid():
            out.append(t)
    return out


def format_path(start_name: str, steps: list[dict]) -> str:
    """把一条 BFS 路径渲染成可读串：物流配送 --适用条款--> 48小时内响应

    MCP 工具层与合成层共用，保证「路径即证据」在答复和调试输出里长得一样。
    """
    cur = start_name
    parts = []
    for s in steps:
        node = s.get("node") or {}
        nm = node.get("name", "?")
        arrow = "--{}-->" if s.get("direction") != "in" else "<--{}--"
        parts.append(f"{cur} {arrow.format(s.get('rel_label') or s.get('rel'))} {nm}")
        cur = nm
    return " ; ".join(parts)


def extract_query_entities(question: str) -> list[str]:
    """从问题里抽实体名（强制规则抽取，避免查询路径依赖 LLM 拖慢响应）。"""
    triples = TripleExtractor(use_llm=False).extract_by_rules(question, "__query__")
    names: list[str] = []
    for t in triples:
        for nm, ty in ((t.subject, t.subject_type), (t.object, t.object_type)):
            if ty != "Document" and nm and nm not in names:
                names.append(nm)
    return names


def expand_facts(graph: "GraphStore", question: str, depth: int = 2, limit: int = 12) -> dict:
    """GraphRAG 的关系侧召回：问题 -> 实体 -> 多跳邻居 -> 可解释路径。

    与向量检索（语义侧）互补：语义管「像什么」，这里管「和谁关联」。
    MCP 工具层（server.py）与 Web 控制台（app.py）共用这一份实现。
    """
    entities = extract_query_entities(question)
    if not entities:
        return {"entities": [], "facts": []}
    facts: list[dict] = []
    seen: set[str] = set()
    hit_entities: list[dict] = []
    for nm in entities[:5]:
        try:
            res = graph.neighbors(nm, direction="both", depth=depth, limit=limit)
        except Exception:  # noqa: BLE001
            continue
        ent = res.get("entity")
        if not ent:
            continue
        hit_entities.append({
            "name": ent["name"],
            "type": ent["type"],
            "type_label": ent.get("type_label", ent["type"]),
        })
        for nb in res.get("neighbors", []):
            steps = nb.get("path") or []
            if not steps:
                continue
            last = steps[-1]
            target = last.get("node") or {}
            key = f"{ent['name']}|{last.get('rel')}|{target.get('name')}"
            if key in seen:
                continue
            seen.add(key)
            facts.append({
                "subject": ent["name"],
                "subject_type": ent["type"],
                "relation": last.get("rel"),
                "relation_label": last.get("rel_label"),
                "object": target.get("name"),
                "object_type": target.get("type"),
                "depth": nb.get("depth", 1),
                "path": format_path(ent["name"], steps),
            })
    return {"entities": hit_entities, "facts": facts[:limit]}


_extractor: TripleExtractor | None = None


def extract_triples(text: str, doc_id: str) -> list[Triple]:
    """模块级入口：抽取三元��（进程内复用抽取器实例）。"""
    global _extractor
    if _extractor is None:
        _extractor = TripleExtractor()
    return _extractor.extract(text, doc_id)


def build_graph_from_store(graph: GraphStore, store) -> dict:
    """回填：把已有向量库里的文档重新抽一遍三元组（用于存量知识建图）。"""
    by_doc: dict[str, list[str]] = {}
    for c in store.get_chunks():
        by_doc.setdefault(c["doc_id"], []).append(c["content"])
    added = skipped = 0
    for doc_id, chunks in by_doc.items():
        text = "\n".join(chunks)
        res = graph.add_triples(extract_triples(text, doc_id))
        added += res["added"]
        skipped += res["invalid"]
    return {"docs": len(by_doc), "added": added, "invalid": skipped}
