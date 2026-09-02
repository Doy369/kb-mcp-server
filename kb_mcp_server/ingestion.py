"""摄取管线：文档切片 -> 本地嵌入 -> 写入向量库（+ P6 图谱三元组抽取）。

P1 实现：按长度切分（优先在句末/空行断句）-> 本地嵌入 -> store.add_chunk。
P6 增强：同一篇文档顺带抽取三元组写入知识图谱（GraphRAG 的关系层）。
          图谱失败绝不阻断摄取——向量库写入成功即算成功。
"""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

from kb_mcp_server.config import get_cfg
from kb_mcp_server.embeddings import get_embedder
from kb_mcp_server.storage import VectorStore, get_store


_BREAK_CHARS = set("。！？\n；;")


def chunk_text(text: str, chunk_size: int = 300, overlap: int = 50) -> list[str]:
    """把长文本切成带重叠的片段，优先在句末标点/换行处断句。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            # 往回找最近的断句点
            for j in range(end, max(start + chunk_size // 2, start), -1):
                if text[j - 1] in _BREAK_CHARS:
                    end = j
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


# ---- 多格式文件解析（零依赖：md/txt/docx；pdf 需 pypdf）----
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SUPPORTED = (".md", ".markdown", ".txt", ".text", ".docx", ".pdf")


def extract_text(filename: str, data: bytes) -> str:
    """按扩展名把文件字节解析为纯文本。"""
    name = (filename or "").lower()
    if name.endswith((".md", ".markdown", ".txt", ".text")):
        return _clean_markdown(data.decode("utf-8", "ignore"))
    if name.endswith(".docx"):
        return _extract_docx(data)
    if name.endswith(".pdf"):
        return _extract_pdf(data)
    return data.decode("utf-8", "ignore")  # 兜底当纯文本


def _clean_markdown(text: str) -> str:
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)  # 去 frontmatter
    text = re.sub(r"```.*?```", " ", text, flags=re.S)          # 去 fenced code
    return text.strip()


def _extract_docx(data: bytes) -> str:
    """docx 是 zip 包，从 word/document.xml 提取段落文本（标准库实现，零依赖）。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    root = ET.fromstring(xml)
    paras = []
    for p in root.iter(f"{_W_NS}p"):
        line = "".join(t.text or "" for t in p.iter(f"{_W_NS}t")).strip()
        if line:
            paras.append(line)
    return "\n".join(paras).strip()


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        raise RuntimeError("PDF 解析需要 pypdf：pip install pypdf")
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages).strip()


def graph_enabled() -> bool:
    """图谱总开关（运行时配置优先，默认开）。"""
    return get_cfg("KB_GRAPH_ENABLED", "1").lower() in ("1", "true", "yes")


class IngestionPipeline:
    """摄取管线：文档切片 -> 本地嵌入(bge-zh) -> 写入向量库（+ 知识图谱）。"""

    def __init__(self, store: VectorStore | None = None, embedder=None, graph=None):
        self.store = store or get_store()
        self.embedder = embedder or get_embedder()
        self._graph = graph          # None=未初始化，False=已判定不可用
        self._graph_tried = graph is not None
        self.last_graph_result: dict = {}   # 最近一次建图结果，供控制台展示

    def _resolve_graph(self):
        if self._graph is None and not self._graph_tried:
            self._graph_tried = True
            try:
                from kb_mcp_server.graph import get_graph_store

                self._graph = get_graph_store()
            except Exception:  # noqa: BLE001
                self._graph = False
        return self._graph or None

    def _graph_hook(self, doc_id: str, text: str, enabled: bool = True) -> dict:
        """摄入一篇文档后抽取三元组入图。任何异常都吞掉，不阻断摄取。"""
        if not enabled or not graph_enabled():
            return {}
        g = self._resolve_graph()
        if not g:
            return {}
        try:
            from kb_mcp_server.graph import extract_triples

            triples = extract_triples(text, doc_id)
            res = g.add_triples(triples)
            # 无论抽没抽到三元组都给文档打上「已入图」标记：
            # 否则一篇抽不出实体的文档会被 GraphBuilder agent 反复重试
            g.upsert_entity("Document", doc_id, {"doc_id": doc_id})
            res.update({"triples": len(triples), "backend": getattr(g, "backend", "?")})
            return res
        except Exception as e:  # noqa: BLE001
            print(f"[graph] 建图失败（已忽略，不影响摄取）：{e}")
            return {}

    def ingest_text(self, doc_id: str, text: str, chunk_size: int = 300, overlap: int = 50,
                    graph: bool = True) -> int:
        chunks = chunk_text(text, chunk_size, overlap)
        for i, ch in enumerate(chunks):
            emb = self.embedder.encode_one(ch)
            self.store.add_chunk(
                doc_id, ch, emb,
                meta={"chunk_index": i, "char_len": len(ch)},
            )
        self.last_graph_result = self._graph_hook(doc_id, text, enabled=graph)
        return len(chunks)

    def ingest_file(self, doc_id: str, filename: str, data: bytes, chunk_size: int = 300,
                    overlap: int = 50, graph: bool = True) -> int:
        """解析文件字节并按 document 粒度入库（一个文件 = 一个 doc_id）。"""
        text = extract_text(filename, data)
        return self.ingest_text(doc_id, text, chunk_size, overlap, graph=graph)
