"""重建向量索引：用当前配置的嵌入后端重新编码所有已入库文本。

为什么需要它：
  切换嵌入模型（如 dev -> bge）后，旧向量空间与新查询向量空间不一致，
  不重建则检索全乱。本脚本就地重编码 kb_store.json（或 pgvector）中已有
  chunk 的文本，不依赖原始文件，所以源文件丢了我们也能重建。

前置条件（在本机/有网络的部署服务器上执行）：
  1) 已安装依赖：  pip install sentence-transformers
  2) 已设后端环境变量（或写进 .env）：
       KB_EMBEDDING_BACKEND=bge
     首次运行会联网下载 BAAI/bge-large-zh-v1.5 权重（约 1.2GB，需外网）。
  3) 若用 pgvector，还需配置 KB_DATABASE_URL 并先 ensure_schema。

用法：
  KB_EMBEDDING_BACKEND=bge python rebuild_index.py
"""
import os
import sys

# 若未通过环境变量/.env 传入，给个默认后端（bge 为生产语义嵌入）
os.environ.setdefault("KB_EMBEDDING_BACKEND", "bge")

from kb_mcp_server.embeddings import get_embedder
from kb_mcp_server.storage import get_store, MemoryVectorStore, PGVectorStore
from kb_mcp_server.config import get_settings


def rebuild_memory(store: MemoryVectorStore) -> int:
    chunks = store.get_chunks()
    if not chunks:
        print("知识库为空，无需重建。")
        return 0
    texts = [c["content"] for c in chunks]
    print(f"重新编码 {len(texts)} 个片段（后端={get_settings().embedding_backend}）...")
    emb = get_embedder()
    vecs = emb.encode(texts)
    # 就地重建内部结构并落盘（旧空间向量整体替换为新空间）
    store._chunks = [
        {"doc_id": c["doc_id"], "content": c["content"], "meta": c.get("meta") or {}}
        for c in chunks
    ]
    store._embs = [list(v) for v in vecs]
    store._persist()
    return len(texts)


def rebuild_pg(store: PGVectorStore) -> int:
    chunks = store.get_chunks()
    if not chunks:
        print("知识库为空，无需重建。")
        return 0
    texts = [c["content"] for c in chunks]
    docs = [c["doc_id"] for c in chunks]
    metas = [c.get("meta") or {} for c in chunks]
    print(f"重新编码 {len(texts)} 个片段（后端={get_settings().embedding_backend}）...")
    emb = get_embedder()
    vecs = emb.encode(texts)
    store.connect()
    store.ensure_schema()
    with store.conn.cursor() as cur:
        cur.execute("DELETE FROM kb_chunks")  # 旧空间向量清空
    for d, t, m, v in zip(docs, texts, metas, vecs):
        store.add_chunk(d, t, list(v), m)
    return len(texts)


def self_check(store, backend: str) -> None:
    q = "退货退款政策是怎样的"
    try:
        qv = get_embedder().encode_one(q)
        hits = store.search(qv, top_k=3)
    except Exception as e:  # noqa: BLE001
        print(f"自检跳过（{type(e).__name__}: {e}）")
        return
    print(f"自检查询「{q}」 top-3：")
    for h in hits:
        print(f"  {h['doc_id']}  score={h['score']:.3f}")


def main() -> None:
    backend = get_settings().embedding_backend
    if backend not in ("bge", "dev"):
        print(f"未知嵌入后端: {backend}（应为 bge 或 dev）")
        sys.exit(1)
    if backend == "bge":
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            print("错误：后端=bge 但需要 sentence-transformers。请先 `pip install sentence-transformers`")
            sys.exit(1)
    store = get_store()
    n = rebuild_pg(store) if isinstance(store, PGVectorStore) else rebuild_memory(store)
    print(f"完成：已用 {backend} 重新编码 {n} 个片段并落盘。")
    if n:
        self_check(store, backend)


if __name__ == "__main__":
    main()
