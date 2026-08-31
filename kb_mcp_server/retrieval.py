"""混合检索：向量语义召回 + 关键词(BM25)召回，RRF 融合，可选硬阈值 / MMR / 重排。

检索链路（对应质量提升技术栈）：
  query -> [向量召回 top-n 余弦] ┐
                               ├─ RRF 融合重排 ─> [硬阈值 min_score] ─> [MMR 去重] ─> [可选 cross-encoder 重排] ─> top_k
         [BM25 关键词召回 top-n] ┘

- 向量召回：store.search（余弦，由 embedding 后端决定语义能力）
- 关键词召回：BM25Okapi（中文按单字 + 英文/数字词分词，补齐向量弱的专有名词）
- 融合：RRF（倒数排名融合）合并两路，无需手动调权重
- 后处理：min_score 硬阈值（按余弦过滤低分噪声）+ MMR（最大边际相关去重）+ 可选重排
"""

import math
import re

from kb_mcp_server.embeddings import get_embedder
from kb_mcp_server.config import get_cfg
from kb_mcp_server.storage import VectorStore, get_store


def _tokenize(text: str) -> list[str]:
    """中文按单字、英文/数字按词切分，适合 BM25 字面匹配。"""
    toks = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
    for ch in text or "":
        if "一" <= ch <= "鿿":
            toks.append(ch)
    return toks


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class HybridRetriever:
    def __init__(self, store: VectorStore | None = None, embedder=None):
        self.store = store or get_store()
        self.embedder = embedder or get_embedder()
        self._bm25 = None
        self._chunks = None
        self._n_cached = -1
        self._reranker = None
        self._reranker_model = None

    # ---- BM25 索引：按 chunk 数变化判定是否重建 ----
    def _ensure_index(self):
        chunks = self.store.get_chunks()
        if len(chunks) == self._n_cached and self._bm25 is not None:
            return
        self._chunks = chunks
        self._n_cached = len(chunks)
        if not chunks:
            self._bm25 = None
            return
        corpus = [_tokenize(c["content"]) for c in chunks]
        try:
            from rank_bm25 import BM25Okapi
            self._bm25 = BM25Okapi(corpus)
        except Exception:
            self._bm25 = None

    def _bm25_search(self, query: str, top_n: int) -> list[dict]:
        self._ensure_index()
        if self._bm25 is None or not self._chunks:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_n]
        mx = max(scores) if len(scores) else 1.0
        out = []
        for i in order:
            if scores[i] <= 0:
                continue
            c = self._chunks[i]
            norm = (float(scores[i]) / mx) if mx > 0 else 0.0
            out.append({
                "doc_id": c["doc_id"], "content": c["content"], "meta": c["meta"],
                "bm25_score": float(scores[i]), "_norm": norm,
                "embedding": c.get("embedding"),
            })
        return out

    def _fuse(self, lists: list[list[dict]], k: int = 60) -> list[dict]:
        """RRF 融合多路排名：每路第 rank 名得 1/(rank+k+1)，累加后排序。"""
        agg: dict = {}
        detail: dict = {}
        vec_score: dict = {}
        for rl in lists:
            for rank, h in enumerate(rl):
                key = (h["doc_id"], h.get("meta", {}).get("chunk_index"))
                detail[key] = h
                agg[key] = agg.get(key, 0.0) + 1.0 / (rank + k + 1)
                if h.get("score") is not None:
                    vec_score[key] = max(vec_score.get(key, 0.0), float(h["score"]))
        out = []
        for key in sorted(agg, key=lambda kk: -agg[kk]):
            h = dict(detail[key])
            vs = vec_score.get(key)
            h["vec_score"] = vs
            # 最终 score：有向量余弦用余弦（置信度正确）；BM25-only 降权，避免假高置信
            h["score"] = vs if vs is not None else (0.3 + 0.5 * h.get("_norm", 0.0))
            out.append(h)
        return out

    def _mmr(self, candidates: list[dict], top_k: int, lambda_: float) -> list[dict]:
        """最大边际相关：在相关性与多样性间权衡，避免 top-k 全是同一段。"""
        embs = {i: c.get("embedding") for i, c in enumerate(candidates)}
        selected: list[int] = []
        remaining = list(range(len(candidates)))
        while len(selected) < top_k and remaining:
            best_i, best_s = None, -1e9
            for i in remaining:
                if not selected:
                    s = float(candidates[i].get("score", 0.0))
                else:
                    max_sim = 0.0
                    for j in selected:
                        sim = _cosine(embs[i], embs[j])
                        if sim and sim > max_sim:
                            max_sim = sim
                    s = lambda_ * float(candidates[i].get("score", 0.0)) - (1 - lambda_) * max_sim
                if s > best_s:
                    best_s, best_i = s, i
            selected.append(best_i)
            remaining.remove(best_i)
        return [candidates[i] for i in selected]

    def _postprocess(self, hits: list[dict], top_k: int, min_score: float, mmr: float) -> list[dict]:
        # 硬阈值：仅过滤「向量命中且低于阈值、且 BM25 也未强命中」的噪声
        if min_score and min_score > 0:
            kept = []
            for h in hits:
                vs = h.get("vec_score")
                bm = h.get("_norm", 0.0)
                if vs is not None and vs < min_score and bm < 0.3:
                    continue
                kept.append(h)
            hits = kept
        # MMR 去重
        if mmr is not None and 0 <= mmr < 1 and len(hits) > top_k:
            hits = self._mmr(hits, top_k, mmr)
        return hits[:top_k]

    def _rerank(self, query: str, hits: list[dict], top_k: int) -> list[dict]:
        """可选 cross-encoder 重排（默认关闭，需 sentence-transformers + 模型）。"""
        model = get_cfg("KB_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
        try:
            from sentence_transformers import CrossEncoder
        except Exception:
            return hits
        if self._reranker is None or self._reranker_model != model:
            self._reranker = CrossEncoder(model)
            self._reranker_model = model
        pairs = [[query, h["content"]] for h in hits]
        scores = self._reranker.predict(pairs)
        for h, s in zip(hits, scores):
            h["score"] = float(s)
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:top_k]

    def search(self, query: str, top_k: int = 5, mode: str = "hybrid",
               min_score: float | None = None, mmr: float | None = None,
               rerank: bool | None = None) -> list[dict]:
        # 配置可由参数覆盖，否则读运行时/环境变量（默认：开混合、MMR=0.6、重排关）
        if min_score is None:
            min_score = float(get_cfg("KB_RETRIEVE_MIN_SCORE", "0") or 0)
        if mmr is None:
            mmr = float(get_cfg("KB_RETRIEVE_MMR", "0.6") or 0.6)
        if rerank is None:
            rerank = get_cfg("KB_RERANKER", "0").lower() in ("1", "true", "yes")
        if mode not in ("vector", "structured", "hybrid"):
            mode = "hybrid"

        q_emb = self.embedder.encode_one(query)
        cand = max(top_k * 4, 30)

        vec_hits = self.store.search(q_emb, top_k=cand)
        for h in vec_hits:
            h["source"] = "vector"

        if mode == "vector":
            fused = vec_hits
        elif mode == "structured":
            bm_hits = self._bm25_search(query, top_n=cand)
            for h in bm_hits:
                h["source"] = "bm25"
            fused = bm_hits
        else:
            bm_hits = self._bm25_search(query, top_n=cand)
            for h in bm_hits:
                h["source"] = "bm25"
            fused = self._fuse([vec_hits, bm_hits])

        if rerank:
            fused = self._rerank(query, fused, top_k=cand)

        out = self._postprocess(fused, top_k, min_score, mmr)
        for h in out:
            h.pop("_norm", None)
        return out
