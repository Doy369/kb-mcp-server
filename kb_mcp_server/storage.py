"""存储层：向量库抽象 + 两个后端。

- MemoryVectorStore：纯 Python 内存向量库（开发/离线，零依赖）。
- PGVectorStore：Postgres + pgvector（生产，混合存储的向量侧）。

两者实现同一套接口：ensure_schema / add_chunk / search / count，上层无需感知后端。
"""

import json
import math
import os
import tempfile
from abc import ABC, abstractmethod

from kb_mcp_server.config import get_settings


# 内存存储的持久化文件（纯本地、离线、私有）；设 KB_MEM_STORE 可改路径
_MEM_STORE_PATH = os.getenv("KB_MEM_STORE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "kb_store.json",
)


class VectorStore(ABC):
    @abstractmethod
    def ensure_schema(self) -> None: ...

    @abstractmethod
    def add_chunk(self, doc_id: str, content: str, embedding: list[float], meta: dict | None = None) -> None: ...

    @abstractmethod
    def search(self, embedding: list[float], top_k: int = 5) -> list[dict]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def list_docs(self) -> list[dict]: ...

    @abstractmethod
    def get_chunks(self) -> list[dict]: ...

    @abstractmethod
    def delete_doc(self, doc_id: str) -> int: ...


class MemoryVectorStore(VectorStore):
    """纯 Python 内存向量库：余弦相似度检索，离线开发用。

    额外做本地 JSON 持久化：启动时从 kb_store.json 恢复，每次增删后落盘，
    避免进程重启导致文档管理清空（兜底方案，生产用 PGVectorStore）。
    """

    def __init__(self):
        self._chunks: list[dict] = []
        self._embs: list[list[float]] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(_MEM_STORE_PATH):
            return
        try:
            with open(_MEM_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._chunks = data.get("chunks", [])
            self._embs = [list(e) for e in data.get("embs", [])]
        except Exception:
            # 损坏则忽略，不影响启动（最坏情况重新摄取）
            self._chunks, self._embs = [], []

    def _persist(self) -> None:
        try:
            payload = {"chunks": self._chunks, "embs": [list(e) for e in self._embs]}
            d = os.path.dirname(_MEM_STORE_PATH) or "."
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, default=str)
            os.replace(tmp, _MEM_STORE_PATH)
        except Exception:
            pass

    def ensure_schema(self) -> None:
        pass

    def add_chunk(self, doc_id: str, content: str, embedding: list[float], meta: dict | None = None) -> None:
        self._chunks.append({"doc_id": doc_id, "content": content, "meta": meta or {}})
        self._embs.append(list(embedding))
        self._persist()

    def search(self, embedding: list[float], top_k: int = 5) -> list[dict]:
        if not self._embs:
            return []
        out = []
        for ch, emb in zip(self._chunks, self._embs):
            dot = sum(a * b for a, b in zip(embedding, emb))
            nq = math.sqrt(sum(a * a for a in embedding))
            ne = math.sqrt(sum(b * b for b in emb))
            score = dot / (nq * ne) if nq and ne else 0.0
            out.append({"doc_id": ch["doc_id"], "content": ch["content"], "meta": ch["meta"], "score": score})
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:top_k]

    def get_chunks(self) -> list[dict]:
        return [
            {"doc_id": c["doc_id"], "content": c["content"], "meta": c["meta"], "embedding": list(e)}
            for c, e in zip(self._chunks, self._embs)
        ]

    def count(self) -> int:
        return len(self._chunks)

    def list_docs(self) -> list[dict]:
        agg: dict[str, dict] = {}
        for ch in self._chunks:
            d = agg.setdefault(ch["doc_id"], {"doc_id": ch["doc_id"], "chunks": 0, "preview": ""})
            d["chunks"] += 1
            if not d["preview"]:
                d["preview"] = ch["content"][:60]
        return list(agg.values())

    def delete_doc(self, doc_id: str) -> int:
        keep = [i for i, ch in enumerate(self._chunks) if ch["doc_id"] != doc_id]
        removed = len(self._chunks) - len(keep)
        self._chunks = [self._chunks[i] for i in keep]
        self._embs = [self._embs[i] for i in keep]
        self._persist()
        return removed


class PGVectorStore(VectorStore):
    """Postgres + pgvector 存储（混合存储的向量侧）。

    P0 仅搭好连接与建表逻辑，P1 接入本地嵌入后写入，P2 提供向量检索。
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or get_settings().database_url
        self.conn = None

    def connect(self) -> None:
        import psycopg
        from pgvector.psycopg import register_vector

        self.conn = psycopg.connect(self.dsn, autocommit=True)
        register_vector(self.conn)

    def ensure_schema(self) -> None:
        assert self.conn is not None
        dim = get_settings().embedding_dim
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id BIGSERIAL PRIMARY KEY,
                    doc_id TEXT,
                    content TEXT,
                    embedding vector({dim}),
                    meta JSONB DEFAULT '{{}}'::jsonb
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx "
                "ON kb_chunks USING hnsw (embedding vector_cosine_ops);"
            )

    def add_chunk(self, doc_id: str, content: str, embedding: list[float], meta: dict | None = None) -> None:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kb_chunks (doc_id, content, embedding, meta) VALUES (%s, %s, %s, %s)",
                (doc_id, content, embedding, meta or {}),
            )

    def search(self, embedding: list[float], top_k: int = 5) -> list[dict]:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT doc_id, content, meta, 1 - (embedding <=> %s) AS score "
                "FROM kb_chunks ORDER BY embedding <=> %s LIMIT %s",
                (embedding, embedding, top_k),
            )
            rows = cur.fetchall()
        return [
            {"doc_id": r[0], "content": r[1], "meta": r[2], "score": float(r[3])}
            for r in rows
        ]

    def get_chunks(self) -> list[dict]:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute("SELECT doc_id, content, meta, embedding FROM kb_chunks")
            rows = cur.fetchall()
        out = []
        for r in rows:
            emb = r[3]
            if hasattr(emb, "tolist"):
                emb = emb.tolist()
            elif isinstance(emb, str):
                import json as _json
                emb = _json.loads(emb)
            out.append({
                "doc_id": r[0], "content": r[1],
                "meta": r[2] or {}, "embedding": list(emb) if emb else [],
            })
        return out

    def count(self) -> int:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM kb_chunks")
            return cur.fetchone()[0]

    def list_docs(self) -> list[dict]:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT doc_id, count(*), min(content) FROM kb_chunks "
                "GROUP BY doc_id ORDER BY doc_id"
            )
            return [
                {"doc_id": r[0], "chunks": r[1], "preview": (r[2] or "")[:60]}
                for r in cur.fetchall()
            ]

    def delete_doc(self, doc_id: str) -> int:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM kb_chunks WHERE doc_id = %s", (doc_id,))
            return cur.rowcount


def get_store() -> VectorStore:
    """按配置返回存储后端。"""
    if get_settings().storage_backend == "pgvector":
        return PGVectorStore()
    return MemoryVectorStore()
