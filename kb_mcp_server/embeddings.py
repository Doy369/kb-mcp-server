"""嵌入层：把文本转成向量。

- DevEmbedder：纯 Python、零依赖、离线可用（开发验证用，基于条目哈希的 bag-of-words 嵌入，
  语义召回能力有限，但足以跑通整条管线并验证 MCP 工具）。
- BGEEmbedder：sentence-transformers 本地模型（BAAI/bge-large-zh-v1.5），数据不出域，生产推荐。

两者接口一致：encode(texts) -> list[list[float]]，向量已 L2 归一化。
"""

import hashlib
import math
import re

from kb_mcp_server.config import get_settings


class DevEmbedder:
    """开发用离线嵌入器：条目哈希 + 随机投影，纯 Python 实现。"""

    def __init__(self, dim: int | None = None):
        self.dim = dim or get_settings().embedding_dim

    def _tokenize(self, text: str) -> list[str]:
        toks = [t.lower() for t in re.findall(r"[a-zA-Z0-9]+", text)]
        for ch in text:
            if "\u4e00" <= ch <= "\u9fff":
                toks.append(ch)
        return toks

    def encode(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for tok in self._tokenize(text):
                h = int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[:8], "big")
                idx = h % self.dim
                sign = 1.0 if (h >> 7) & 1 else -1.0
                v[idx] += sign
            norm = math.sqrt(sum(x * x for x in v))
            if norm > 0:
                v = [x / norm for x in v]
            out.append(v)
        return out

    def encode_one(self, text: str) -> list[float]:
        return self.encode(text)[0]


class BGEEmbedder:
    """生产用本地嵌入器：sentence-transformers 本地模型，数据不出域。"""

    def __init__(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise RuntimeError(
                "sentence-transformers 未安装，请 `pip install sentence-transformers` "
                "或把 KB_EMBEDDING_BACKEND 设为 dev 使用离线嵌入器"
            )
        s = get_settings()
        self.model = SentenceTransformer(s.embedding_model)
        self.dim = s.embedding_dim

    def encode(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        vecs = self.model.encode(texts, normalize_embeddings=True)
        return [[float(x) for x in row] for row in vecs]

    def encode_one(self, text: str) -> list[float]:
        return self.encode(text)[0]


def get_embedder():
    """按配置返回嵌入器实例。"""
    backend = get_settings().embedding_backend
    if backend == "bge":
        return BGEEmbedder()
    return DevEmbedder()
