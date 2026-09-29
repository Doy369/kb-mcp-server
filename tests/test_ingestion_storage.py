"""离线数据层单测：分块 / 嵌入 / 存储 / 图谱本体。

这些都是「上层链路全依赖、出错却很难定位」的基础环节，因此优先覆盖。
全部零外部依赖（dev 嵌入 + memory 后端），可离线跑、可在 CI 跑。
"""

from __future__ import annotations

import math

import pytest

from kb_mcp_server.embeddings import DevEmbedder, get_embedder
from kb_mcp_server.graph import (
    NODE_TYPES,
    REL_SCHEMA,
    MemoryGraphStore,
    Triple,
    relation_is_valid,
)
from kb_mcp_server.ingestion import chunk_structured, chunk_text, extract_text
from kb_mcp_server.storage import MemoryVectorStore


# --------------------------------------------------------------------------- #
# 分块：P2-8 的核心回归面
# --------------------------------------------------------------------------- #
class TestChunkStructured:
    """结构感知分块（P2-8）。这些断言对应 ROADMAP 里明确修过的三个问题。"""

    SLA_TEXT = (
        "# SLA 手册\n\n"
        "响应时效：P0 15 分钟，P1 1 小时，P2 4 小时\n\n"
        "Q：如何申请退货\n"
        "A：签收后 7 个自然日内可申请。\n\n"
        "可用性：企业版 99.95%。\n"
    )

    def test_heading_used_as_context_prefix(self):
        """标题必须作为上下文前缀，保证每个 chunk 自包含（否则「4 小时」不知道属于谁）。"""
        chunks = chunk_structured(self.SLA_TEXT)
        assert chunks, "不应产出空切片"
        assert all(c.startswith("SLA 手册 / ") for c in chunks), chunks

    def test_one_line_multi_tier_is_split(self):
        """「一行多档」必须拆条——这是 P2 问题误命中 P0 答案的根因。

        反例（修复前）：整篇一个 chunk，问 P0 时答案里必然夹带 P2 的「4 小时」。
        """
        chunks = chunk_structured(self.SLA_TEXT)
        p0 = [c for c in chunks if "P0" in c]
        assert len(p0) == 1, f"P0 应独立成块，实际 {p0}"
        # P0 的块里不能出现 P2 的时限
        assert "15 分钟" in p0[0]
        assert "4 小时" not in p0[0], "P0 块混入了 P2 的时限（分档粘连回归）"
        assert "1 小时" not in p0[0]

        p2 = [c for c in chunks if "P2" in c]
        assert len(p2) == 1, f"P2 应独立成块，实际 {p2}"
        assert "4 小时" in p2[0]
        assert "15 分钟" not in p2[0], "P2 块混入了 P0 的时限"

    def test_qa_pair_merged(self):
        """Q/A 必须成对合并——拆开后问句会单独成块，检索时命中「没有答案的问句行」。"""
        chunks = chunk_structured(self.SLA_TEXT)
        qa = [c for c in chunks if "如何申请退货" in c]
        assert len(qa) == 1, f"Q/A 应合并成一个块，实际 {qa}"
        assert "7 个自然日" in qa[0], "问句块里缺答案内容（Q/A 未合并的回归）"

    def test_short_doc_still_splits(self):
        """短文档也必须切——旧逻辑「文本 >300 字才切」是整篇粘连的根因。"""
        chunks = chunk_structured(self.SLA_TEXT)
        assert len(chunks) >= 4, f"短文档应切成多块，实际 {len(chunks)} 块"

    def test_empty_and_blank_input(self):
        assert chunk_structured("") == []
        assert chunk_structured("   \n\n  \t ") == []

    def test_plain_strategy_still_available(self, monkeypatch):
        """KB_CHUNK_STRATEGY=plain 应能退回旧的纯长度切片（保留回退路径）。"""
        from kb_mcp_server import config

        monkeypatch.setitem(config._runtime_cfg or {}, "KB_CHUNK_STRATEGY", "plain")
        monkeypatch.setenv("KB_CHUNK_STRATEGY", "plain")
        chunks = chunk_text(self.SLA_TEXT)
        # 纯长度切片不做拆条：应仍是整篇一块（短文档）
        assert len(chunks) == 1


# --------------------------------------------------------------------------- #
# 嵌入
# --------------------------------------------------------------------------- #
class TestEmbedder:
    def test_singleton(self):
        """嵌入器必须是进程内单例。

        这是 ROADMAP 记录的真 bug：bge 模型 1.3GB，逐次 new 会反复重载并撑爆内存，
        实测导致评测进程 Segmentation fault。单例保证模型只加载一次。
        """
        assert get_embedder() is get_embedder()

    def test_dim_and_normalized(self):
        """向量维度与 L2 归一化：归一化是「点积 == 余弦」的前提，检索层依赖它。"""
        e = DevEmbedder()
        v = e.encode_one("物流配送")
        assert len(v) == e.dim
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-9, f"向量未归一化，norm={norm}"

    def test_encode_accepts_str_and_list(self):
        e = DevEmbedder()
        assert len(e.encode("单条")) == 1
        assert len(e.encode(["第一条", "第二条"])) == 2

    def test_encode_one_matches_encode(self):
        e = DevEmbedder()
        assert e.encode_one("物流") == e.encode(["物流"])[0]

    def test_same_text_same_vector(self):
        """确定性：同一文本必须产出同一向量，否则评测不可复现。"""
        e = DevEmbedder()
        assert e.encode_one("物流配送") == e.encode_one("物流配送")


# --------------------------------------------------------------------------- #
# 向量存储
# --------------------------------------------------------------------------- #
class TestMemoryVectorStore:
    def _store(self, tmp_path, monkeypatch):
        from kb_mcp_server import storage

        monkeypatch.setattr(storage, "_MEM_STORE_PATH", str(tmp_path / "s.json"))
        return MemoryVectorStore()

    def test_add_and_count(self, tmp_path, monkeypatch):
        s = self._store(tmp_path, monkeypatch)
        e = DevEmbedder()
        s.add_chunk("doc1", "物流配送 48 小时发货", e.encode_one("物流配送 48 小时发货"),
                    meta={"chunk_index": 0})
        s.add_chunk("doc1", "退款 7 个自然日", e.encode_one("退款 7 个自然日"),
                    meta={"chunk_index": 1})
        assert s.count() == 2
        assert len(s.list_docs()) == 1
        assert s.list_docs()[0]["doc_id"] == "doc1"

    def test_search_returns_sorted_by_score(self, tmp_path, monkeypatch):
        s = self._store(tmp_path, monkeypatch)
        e = DevEmbedder()
        texts = ["物流配送时效说明", "退款退货流程", "发票开具方式"]
        for i, t in enumerate(texts):
            s.add_chunk("d", t, e.encode_one(t), meta={"chunk_index": i})
        hits = s.search(e.encode_one("物流配送时效说明"), top_k=3)
        assert hits, "应召回到结果"
        scores = [h["score"] for h in hits]
        assert scores == sorted(scores, reverse=True), "结果必须按分数降序"

    def test_search_empty_store(self, tmp_path, monkeypatch):
        s = self._store(tmp_path, monkeypatch)
        assert s.search(DevEmbedder().encode_one("任意问题")) == []

    def test_delete_doc_removes_all_chunks(self, tmp_path, monkeypatch):
        s = self._store(tmp_path, monkeypatch)
        e = DevEmbedder()
        s.add_chunk("doc1", "内容一", e.encode_one("内容一"))
        s.add_chunk("doc1", "内容二", e.encode_one("内容二"))
        s.add_chunk("doc2", "内容三", e.encode_one("内容三"))
        assert s.delete_doc("doc1") == 2
        assert s.count() == 1
        assert s.list_docs()[0]["doc_id"] == "doc2"

    def test_delete_nonexistent_returns_zero(self, tmp_path, monkeypatch):
        s = self._store(tmp_path, monkeypatch)
        assert s.delete_doc("不存在") == 0

    def test_get_chunks_exposes_embedding(self, tmp_path, monkeypatch):
        """BM25 索引与 MMR 去重都依赖 get_chunks 带出 embedding，缺了会静默失效。"""
        s = self._store(tmp_path, monkeypatch)
        e = DevEmbedder()
        s.add_chunk("d", "内容", e.encode_one("内容"))
        chunks = s.get_chunks()
        assert chunks[0]["embedding"] and len(chunks[0]["embedding"]) == e.dim

    def test_persistence_roundtrip(self, tmp_path, monkeypatch):
        """重启后应能恢复：摄取是长任务，丢一次要重跑很久。"""
        from kb_mcp_server import storage

        path = str(tmp_path / "persist.json")
        monkeypatch.setattr(storage, "_MEM_STORE_PATH", path)
        e = DevEmbedder()
        s1 = MemoryVectorStore()
        s1.add_chunk("d", "持久化内容", e.encode_one("持久化内容"))
        s2 = MemoryVectorStore()
        assert s2.count() == 1
        assert s2.get_chunks()[0]["content"] == "持久化内容"

    def test_corrupted_file_does_not_crash(self, tmp_path, monkeypatch):
        """损坏的 JSON 不应阻断启动（最坏情况重新摄取，而非服务起不来）。"""
        from kb_mcp_server import storage

        path = tmp_path / "broken.json"
        path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        monkeypatch.setattr(storage, "_MEM_STORE_PATH", str(path))
        assert MemoryVectorStore().count() == 0


# --------------------------------------------------------------------------- #
# PG 存储的懒连接契约
# --------------------------------------------------------------------------- #
class TestPGVectorStoreLazyConnect:
    """PGVectorStore 必须在首次使用时自动连接 + 建表。

    回归背景：get_store() 只按配置构造实例，**不负责连接**。早先只有
    app.py / server.py 显式 connect，独立使用路径（HybridRetriever() 默认参数、
    workers 里的 get_store()、demo_*.py）拿到的是 conn=None 的实例，一进方法就
    断言失败——设了 KB_STORAGE_BACKEND=pgvector 反而直接崩。本组用例锁住
    「任何入口都能安全用 PG 后端」这一契约（不依赖真实 PG，纯单元）。
    """

    DSN = "postgresql://kb:kb@localhost:5432/kb"

    def _store(self, monkeypatch):
        from kb_mcp_server import storage

        s = storage.PGVectorStore(dsn=self.DSN)
        calls = {"connect": 0}

        def fake_connect():
            calls["connect"] += 1
            s.conn = object()          # 占位连接对象，本用例不做真实查询

        monkeypatch.setattr(s, "connect", fake_connect)
        monkeypatch.setattr(s, "ensure_schema", lambda: setattr(s, "_schema_ready", True))
        return s, calls

    def test_first_use_connects_and_builds_schema(self, monkeypatch):
        s, calls = self._store(monkeypatch)
        assert s.conn is None, "构造时不应建立连接（连接是惰性的）"
        s._ready()
        assert calls["connect"] == 1
        assert s._schema_ready is True

    def test_ready_is_idempotent(self, monkeypatch):
        """重复调用不得重复开连接——否则每次查询泄漏一个连接。"""
        s, calls = self._store(monkeypatch)
        for _ in range(3):
            s._ready()
        assert calls["connect"] == 1

    def test_explicit_connect_skips_when_already_connected(self):
        """conn 已存在时 connect() 立即返回。

        验证方式：不安装 psycopg 也不该抛 ImportError——一旦 guard 失效，
        connect() 会走进 import 逻辑并在此环境报错，用例即失败。
        """
        from kb_mcp_server import storage

        s = storage.PGVectorStore(dsn=self.DSN)
        s.conn = object()
        s.connect()                    # 有 guard 则直接 return
        assert s.conn is not None


# --------------------------------------------------------------------------- #
# 图谱本体
# --------------------------------------------------------------------------- #
class TestGraphOntology:
    def test_relation_schema_covers_all_relations(self):
        """本体定义完整性：每个关系都必须有主宾类型约束，否则抽取无边界、图谱会发散。"""
        for rel in REL_SCHEMA:
            subjects, objects = REL_SCHEMA[rel]
            assert subjects, f"{rel} 缺少主语类型约束"
            assert objects, f"{rel} 缺少宾语类型约束"
            assert all(s in NODE_TYPES for s in subjects), f"{rel} 主语类型未在本体定义"
            assert all(o in NODE_TYPES for o in objects), f"{rel} 宾语类型未在本体定义"

    def test_valid_relation_accepted(self):
        assert relation_is_valid("GOVERNED_BY", "IssueCategory", "SLAClause")
        assert relation_is_valid("SOLVED_BY", "IssueCategory", "Solution")
        assert relation_is_valid("MENTIONS", "Document", "Product")

    def test_wrong_direction_rejected(self):
        """方向写反必须被拒——否则图谱里会出现「条款适用问题类别」这类颠倒关系。"""
        assert not relation_is_valid("GOVERNED_BY", "SLAClause", "IssueCategory")

    def test_unknown_relation_rejected(self):
        assert not relation_is_valid("NOT_A_RELATION", "IssueCategory", "Solution")

    def test_unknown_node_type_rejected(self):
        assert not relation_is_valid("GOVERNED_BY", "UnknownType", "SLAClause")

    def test_triple_validation(self):
        ok = Triple("物流配送", "IssueCategory", "GOVERNED_BY", "48小时内发货", "SLAClause")
        assert ok.is_valid()

        bad_dir = Triple("48小时内发货", "SLAClause", "GOVERNED_BY", "物流配送", "IssueCategory")
        assert not bad_dir.is_valid()

        assert not Triple("", "IssueCategory", "GOVERNED_BY", "x", "SLAClause").is_valid()
        assert not Triple("x", "IssueCategory", "GOVERNED_BY", "   ", "SLAClause").is_valid()


class TestMemoryGraphStore:
    def test_add_triples_reports_invalid(self, tmp_path, monkeypatch):
        """不合规三元组必须计入 invalid 并丢弃，不能静默入库。"""
        from kb_mcp_server import graph

        monkeypatch.setattr(graph, "_GRAPH_STORE_PATH", str(tmp_path / "g.json"))
        g = MemoryGraphStore()
        valid = Triple("物流配送", "IssueCategory", "GOVERNED_BY", "48小时内发货", "SLAClause")
        invalid = Triple("48小时内发货", "SLAClause", "GOVERNED_BY", "物流配送", "IssueCategory")
        res = g.add_triples([valid, invalid])
        assert res["added"] == 1
        assert res["invalid"] == 1

    def test_neighbors_and_path(self, tmp_path, monkeypatch):
        """多跳查询：路径本身即可解释证据，这是 GraphRAG 的核心产出。"""
        from kb_mcp_server import graph

        monkeypatch.setattr(graph, "_GRAPH_STORE_PATH", str(tmp_path / "g.json"))
        g = MemoryGraphStore()
        g.add_triples([
            Triple("物流配送", "IssueCategory", "SOLVED_BY", "全额退款", "Solution"),
        ])
        res = g.neighbors("物流配送", direction="out", depth=1)
        assert res["entity"]["name"] == "物流配送"
        assert any(nb["node"]["name"] == "全额退款" for nb in res["neighbors"])

    def test_neighbors_missing_entity(self, tmp_path, monkeypatch):
        from kb_mcp_server import graph

        monkeypatch.setattr(graph, "_GRAPH_STORE_PATH", str(tmp_path / "g.json"))
        assert MemoryGraphStore().neighbors("不存在的实体")["entity"] is None

    def test_delete_by_doc_cleans_relations(self, tmp_path, monkeypatch):
        """删除文档必须连带清理它抽出的关系，否则会留下指向已删文档的悬空边。"""
        from kb_mcp_server import graph

        monkeypatch.setattr(graph, "_GRAPH_STORE_PATH", str(tmp_path / "g.json"))
        g = MemoryGraphStore()
        g.add_triples([
            Triple("d1", "Document", "MENTIONS", "物流配送", "IssueCategory"),
        ])
        g.delete_by_doc("d1")
        assert g.find_entities(node_type="Document") == []


# --------------------------------------------------------------------------- #
# 文件解析
# --------------------------------------------------------------------------- #
class TestExtractText:
    def test_markdown_and_txt(self):
        assert "物流配送" in extract_text("a.md", "物流配送说明".encode())
        assert "物流配送" in extract_text("a.txt", "物流配送说明".encode())

    def test_markdown_strips_frontmatter_and_code_fence(self):
        raw = "---\ntitle: x\n---\n正文内容\n```py\nprint(1)\n```\n结尾\n"
        out = extract_text("a.md", raw.encode())
        assert "正文内容" in out
        assert "title: x" not in out, "frontmatter 未剥离"
        assert "print(1)" not in out, "代码块未剥离"

    def test_unknown_extension_falls_back_to_text(self):
        assert "内容" in extract_text("a.unknown", "内容".encode())
