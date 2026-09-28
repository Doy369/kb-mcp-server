"""检索层单测：RRF 融合 / 分数不越界 / MMR / 阈值 / 分词。

**这里覆盖的是 ROADMAP 记录的两个真 bug**：
1. BM25-only 合成分数越界（最高 0.8）反超真实向量余弦（0.64~0.72），
   导致标题行压过真正的答案块；
2. 向量召回 + BM25 双路融合的排序稳定性。

这两个 bug 的共同特点：**不报错、只是答案悄悄变差**，没有测试就只能靠肉眼发现。
"""

from __future__ import annotations

import pytest

from kb_mcp_server.embeddings import DevEmbedder
from kb_mcp_server.retrieval import HybridRetriever, _cosine, _tokenize
from kb_mcp_server.storage import MemoryVectorStore


@pytest.fixture
def retriever(tmp_path, monkeypatch):
    """构造一个真实的检索器：内存库 + dev 嵌入，装载一组可预期的语料。"""
    from kb_mcp_server import storage

    monkeypatch.setattr(storage, "_MEM_STORE_PATH", str(tmp_path / "s.json"))
    store = MemoryVectorStore()
    emb = DevEmbedder()

    corpus = {
        "faq": [
            "物流配送常见问题",                      # 标题行：字面高度匹配但无答案
            "下单后 48 小时内出库发货",               # 真正的答案块
            "退款申请需在签收后 7 个自然日内提交",
            "发票开具需要提供公司名称与税号",
        ],
        "sla": [
            "P0 故障响应时限为 15 分钟",
            "P2 问题响应时限为 4 小时",
        ],
    }
    for doc_id, texts in corpus.items():
        for i, t in enumerate(texts):
            store.add_chunk(doc_id, t, emb.encode_one(t), meta={"chunk_index": i})
    return HybridRetriever(store=store, embedder=emb)


class TestFuseScoreBounds:
    """_fuse 的分数边界——这是 ROADMAP 记录的真 bug，必须有回归护栏。"""

    def _mk(self, doc_id, content, score=None, norm=None, idx=0):
        h = {"doc_id": doc_id, "content": content, "meta": {"chunk_index": idx}}
        if score is not None:
            h["score"] = score
        if norm is not None:
            h["_norm"] = norm
        return h

    def test_bm25_only_score_stays_below_vector_hits(self, retriever):
        """BM25-only 命中必须**显著低于**任何向量命中。

        修复前：映射为 `0.3 + 0.5*_norm`，最高 0.8，反超真实向量余弦（0.64~0.72），
        结果标题行「物流配送常见问题」在 MMR 里压过真正的 P2 答案块。
        修复后：映射为 `0.3 * _norm`，区间 [0, 0.3]，恒低于向量命中。
        """
        vec = self._mk("faq", "向量命中块", score=0.65, idx=0)
        bm25_perfect = self._mk("faq", "仅字面完美匹配的标题行", norm=1.0, idx=1)

        fused = retriever._fuse([[vec], [bm25_perfect]])
        by_content = {h["content"]: h for h in fused}

        bm_score = by_content["仅字面完美匹配的标题行"]["score"]
        vec_score = by_content["向量命中块"]["score"]

        assert bm_score <= 0.3, f"BM25-only 分数 {bm_score} 越界（应 ≤0.3）"
        assert bm_score < vec_score, "BM25-only 分数反超向量命中（分数越界回归）"
        assert "score" in bm_score.__class__.__name__ or isinstance(bm_score, float)

    def test_bm25_only_score_is_zero_when_norm_zero(self, retriever):
        h = self._mk("faq", "零分块", norm=0.0, idx=0)
        fused = retriever._fuse([[], [h]])
        assert fused[0]["score"] == 0.0

    def test_vector_hit_keeps_its_cosine(self, retriever):
        """向量命中的最终 score 必须是余弦原值，不能被 BM25 拉偏。"""
        vec = self._mk("faq", "向量块", score=0.71, idx=0)
        fused = retriever._fuse([[vec], []])
        assert fused[0]["score"] == pytest.approx(0.71)
        assert fused[0]["vec_score"] == pytest.approx(0.71)

    def test_both_paths_hit_prefers_vector_cosine(self, retriever):
        """同一块两路都命中时，取向量余弦（语义置信度）而非 BM25 分。"""
        vec = self._mk("faq", "双路命中", score=0.68, idx=0)
        bm = self._mk("faq", "双路命中", norm=1.0, idx=0)
        fused = retriever._fuse([[vec], [bm]])
        assert len(fused) == 1, "同一 (doc_id, chunk_index) 应合并为一条"
        assert fused[0]["score"] == pytest.approx(0.68)

    def test_rrf_ranks_dual_hits_above_single_path(self, retriever):
        """RRF 融合的核心收益：两路都排前面的块应优于仅一路命中的块。"""
        dual = self._mk("faq", "两路都靠前", score=0.55, idx=0)
        dual_bm = self._mk("faq", "两路都靠前", norm=0.9, idx=0)
        single = self._mk("faq", "仅向量命中但很靠前", score=0.99, idx=1)

        fused = retriever._fuse([[single, dual], [dual_bm]])
        assert fused[0]["content"] == "两路都靠前", (
            f"RRF 未把双路命中排到首位，实际顺序：{[h['content'] for h in fused]}"
        )


class TestFuseOrdering:
    def test_empty_lists(self, retriever):
        assert retriever._fuse([[], []]) == []

    def test_single_list_preserves_order(self, retriever):
        a = {"doc_id": "d", "content": "第一", "meta": {"chunk_index": 0}, "score": 0.9}
        b = {"doc_id": "d", "content": "第二", "meta": {"chunk_index": 1}, "score": 0.5}
        fused = retriever._fuse([[a, b]])
        assert [h["content"] for h in fused] == ["第一", "第二"]


class TestMMR:
    def test_mmr_avoids_duplicates(self):
        """MMR 存在的意义：避免 top-k 全是同一段内容的近似重复。"""
        emb = DevEmbedder()
        store = MemoryVectorStore()
        store._chunks = []
        store._embs = []
        r = HybridRetriever(store=store, embedder=emb)

        texts = ["物流配送时效说明", "物流配送时效说明", "退款退货流程"]
        cands = []
        for i, t in enumerate(texts):
            cands.append({
                "doc_id": "d", "content": t, "meta": {"chunk_index": i},
                "score": 0.9 - i * 0.01, "embedding": emb.encode_one(t),
            })
        picked = r._mmr(cands, top_k=2, lambda_=0.5)
        contents = [p["content"] for p in picked]
        assert "退款退货流程" in contents, (
            f"MMR 未挑出多样性内容，实际：{contents}（重复块未被去重）"
        )

    def test_mmr_respects_top_k(self):
        emb = DevEmbedder()
        store = MemoryVectorStore()
        r = HybridRetriever(store=store, embedder=emb)
        cands = [
            {"doc_id": "d", "content": f"内容{i}", "meta": {"chunk_index": i},
             "score": 1.0 - i * 0.1, "embedding": emb.encode_one(f"内容{i}")}
            for i in range(5)
        ]
        assert len(r._mmr(cands, top_k=3, lambda_=0.6)) == 3


class TestPostprocess:
    def _mk(self, content, score, vec_score=None, norm=0.0, idx=0):
        return {
            "doc_id": "d", "content": content, "meta": {"chunk_index": idx},
            "score": score, "vec_score": vec_score if vec_score is not None else score,
            "_norm": norm,
        }

    def test_hard_threshold_filters_low_vector_hits(self, retriever):
        """硬阈值必须滤掉「向量分低、BM25 也没强命中」的噪声。"""
        good = self._mk("好块", 0.8, idx=0)
        noise = self._mk("噪声块", 0.05, norm=0.1, idx=1)
        out = retriever._postprocess([good, noise], top_k=5, min_score=0.3, mmr=None)
        assert [h["content"] for h in out] == ["好块"]

    def test_threshold_keeps_strong_bm25_hit(self, retriever):
        """BM25 强命中（如专有名词）即使向量分低也应保留——双路召回的意义所在。"""
        bm_strong = self._mk("顺丰快递专有名词块", 0.25, vec_score=0.1, norm=0.9, idx=0)
        out = retriever._postprocess([bm_strong], top_k=5, min_score=0.3, mmr=None)
        assert len(out) == 1, "BM25 强命中被硬阈值误杀（专有名词召回能力会丢失）"

    def test_threshold_zero_disables_filter(self, retriever):
        low = self._mk("低分块", 0.01, norm=0.0, idx=0)
        out = retriever._postprocess([low], top_k=5, min_score=0.0, mmr=None)
        assert len(out) == 1

    def test_respects_top_k(self, retriever):
        hits = [self._mk(f"块{i}", 0.9 - i * 0.01, idx=i) for i in range(10)]
        out = retriever._postprocess(hits, top_k=3, min_score=0.0, mmr=None)
        assert len(out) == 3


class TestTokenizer:
    def test_chinese_split_by_char(self):
        toks = _tokenize("物流配送")
        assert "物" in toks and "流" in toks and "配" in toks and "送" in toks

    def test_english_and_digits_kept_as_words(self):
        toks = _tokenize("P0 15 分钟内响应 SLA")
        assert "p0" in toks
        assert "15" in toks
        assert "sla" in toks

    def test_empty(self):
        assert _tokenize("") == []
        assert _tokenize(None) == []


class TestCosine:
    def test_identical_vectors(self):
        assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_length_mismatch_returns_zero(self):
        """维度不一致必须安全返回 0，而不是抛异常中断检索。"""
        assert _cosine([1.0, 0.0], [1.0]) == 0.0

    def test_zero_vector_returns_zero(self):
        assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestHybridSearchEndToEnd:
    """端到端检索：三层（向量 + BM25 + 后处理）串起来是否还认得对答案。"""

    def test_finds_answer_chunk_for_sla_question(self, retriever):
        hits = retriever.search("P2 问题多久响应", top_k=3)
        assert hits, "应召回到结果"
        joined = " ".join(h["content"] for h in hits)
        assert "4 小时" in joined, f"未召回 P2 时限，实际命中：{joined}"

    def test_search_returns_score_and_source(self, retriever):
        hits = retriever.search("退款申请", top_k=3)
        assert hits
        for h in hits:
            assert isinstance(h["score"], float)
            assert "source" in h
            assert h["source"] in ("vector", "bm25")

    def test_internal_norm_field_stripped(self, retriever):
        """`_norm` 是内部中间量，不能泄漏到对外结果里。"""
        for h in retriever.search("物流", top_k=5):
            assert "_norm" not in h

    def test_invalid_mode_falls_back_to_hybrid(self, retriever):
        hits = retriever.search("物流配送", top_k=3, mode="不存在的模式")
        assert hits, "非法 mode 应回退 hybrid 而不是返回空"

    def test_vector_only_mode(self, retriever):
        hits = retriever.search("退款", top_k=3, mode="vector")
        assert all(h["source"] == "vector" for h in hits)

    def test_structured_mode(self, retriever):
        hits = retriever.search("物流", top_k=3, mode="structured")
        assert all(h["source"] == "bm25" for h in hits)

    def test_empty_store_returns_empty(self, tmp_path, monkeypatch):
        from kb_mcp_server import storage

        monkeypatch.setattr(storage, "_MEM_STORE_PATH", str(tmp_path / "empty.json"))
        r = HybridRetriever(store=MemoryVectorStore(), embedder=DevEmbedder())
        assert r.search("任意问题") == []
