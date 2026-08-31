from mcp.server.fastmcp import FastMCP

from kb_mcp_server.adapters import fetch_live
from kb_mcp_server.config import get_settings
from kb_mcp_server.embeddings import get_embedder
from kb_mcp_server.ingestion import IngestionPipeline
from kb_mcp_server.retrieval import HybridRetriever
from kb_mcp_server.storage import PGVectorStore, get_store
from kb_mcp_server.synthesis import synthesize

settings = get_settings()
mcp = FastMCP("kb-mcp-server")

# 进程内单例：保证 ingest / search 共用同一存储（memory 后端尤其重要）
_store = None
_retriever = None
_ingestor = None


def _get_store():
    global _store
    if _store is None:
        _store = get_store()
        if isinstance(_store, PGVectorStore):
            _store.connect()
        _store.ensure_schema()
    return _store


def _pipeline():
    global _retriever, _ingestor
    if _retriever is None:
        store = _get_store()
        emb = get_embedder()
        _retriever = HybridRetriever(store, emb)
        _ingestor = IngestionPipeline(store, emb)
    return _retriever, _ingestor


@mcp.tool()
def ping() -> str:
    """健康检查：返回 ok 表示服务存活。"""
    return "ok"


@mcp.tool()
def ingest_document(doc_id: str, text: str, chunk_size: int = 300, overlap: int = 50) -> dict:
    """把一篇文档灌入知识库：切片 -> 本地嵌入 -> 写入向量库。返回切片数。"""
    _, ingestor = _pipeline()
    n = ingestor.ingest_text(doc_id, text, chunk_size=chunk_size, overlap=overlap)
    return {"doc_id": doc_id, "chunks": n, "backend": settings.storage_backend}


@mcp.tool()
def search_knowledge(query: str, top_k: int = 5, mode: str = "hybrid") -> list[dict]:
    """混合检索知识库（向量语义召回）。返回带相似度分数的知识片段。"""
    retriever, _ = _pipeline()
    return retriever.search(query, top_k=top_k, mode=mode)


@mcp.tool()
def ask_with_live_data(question: str, top_k: int = 5, order_id: str | None = None, sku: str | None = None, history: list[dict] | None = None) -> dict:
    """检索知识 + 调用外部 API + 合成最终答复。

    先向量召回 top-k 知识片段，再按显式参数或问题意图调用外部后端适配器
    （订单状态 / 库存）取实时数据，最后由合成层装配为结构化答复（含置信度与轨迹）。
    history 为可选多轮上下文（[{role, content}]），仅在启用本地 LLM 时用于增强合成。
    """
    retriever, _ = _pipeline()
    hits = retriever.search(question, top_k=top_k)
    live = fetch_live(question, order_id=order_id, sku=sku)
    return synthesize(question, hits, live, history=history)


@mcp.tool()
def list_documents() -> list[dict]:
    """列出知识库中所有文档（含片段数与首段预览）。用于知识库管理。"""
    store = _get_store()
    return store.list_docs()


@mcp.tool()
def delete_document(doc_id: str) -> dict:
    """从知识库删除某篇文档及其全部片段。返回删除的片段数。"""
    store = _get_store()
    n = store.delete_doc(doc_id)
    return {"doc_id": doc_id, "deleted_chunks": n}


if __name__ == "__main__":
    mcp.run()
