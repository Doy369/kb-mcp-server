"""四个职责 agent（P7）。编排器负责调度，各自只写 AgentContext 里自己的字段。

- GraphBuilder  : 图谱构建（对未入图的存量文档补建三元组）
- Retriever     : 语义侧召回（向量 + BM25 混合检索）
- GraphReasoner : 关系侧召回（图谱多跳扩展，路径即证据）
- Synthesizer   : 合成（知识 + 图谱 + 实时数据 → 结构化答复）
- LiveData      : 实时数据（订单 / 库存适配器）
"""

from __future__ import annotations

from kb_mcp_server.agents.base import AgentContext, AgentResult, BaseAgent
from kb_mcp_server.config import get_cfg
from kb_mcp_server.extensions import agent_registry


def graph_store_or_none():
    """拿共享图存储单例；不可用返回 None（agent 层统一入口）。"""
    try:
        from kb_mcp_server.graph import get_graph_store

        return get_graph_store()
    except Exception:  # noqa: BLE001
        return None


def _graph_enabled() -> bool:
    return get_cfg("KB_GRAPH_ENABLED", "1").lower() in ("1", "true", "yes")


class GraphBuilderAgent(BaseAgent):
    """图谱构建者：检查向量库里有、但图谱里没有的文档，补建三元组。

    触发时机：编排器在问答前调用，增量补图。抽取本体校验在 graph.add_triples 内置。
    """

    name = "GraphBuilder"
    role = "图谱构建"
    description = "对未入图的文档补建三元组（LLM 优先，规则兜底）"
    capabilities = ["graph", "ingest"]

    def run(self, ctx: AgentContext) -> AgentResult:
        if not _graph_enabled():
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary="图谱总开关关闭，跳过")
        g = graph_store_or_none()
        if not g:
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary="图后端不可用，跳过")
        from kb_mcp_server.graph import extract_triples
        from kb_mcp_server.storage import get_store

        store = get_store()
        # 向量库按 doc 聚合全文；图谱已打标的文档（Document 节点）跳过
        by_doc: dict[str, list[str]] = {}
        for c in store.get_chunks():
            by_doc.setdefault(c["doc_id"], []).append(c["content"])

        try:
            graphed = {e["name"] for e in g.find_entities(node_type="Document", limit=1000)}
        except Exception:  # noqa: BLE001
            graphed = set()

        todo = [d for d in by_doc if d not in graphed]
        added = invalid = 0
        for doc_id in todo:
            text = "\n".join(by_doc[doc_id])
            res = g.add_triples(extract_triples(text, doc_id))
            g.upsert_entity("Document", doc_id, {"doc_id": doc_id})
            added += res["added"]
            invalid += res["invalid"]

        if not todo:
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary=f"图谱已是最新（{len(graphed)} 篇已入图）",
                               detail={"docs_in_graph": len(graphed)})
        return AgentResult(agent=self.name, role=self.role, ok=True,
                           summary=f"补建 {len(todo)} 篇文档，新增 {added} 条关系",
                           detail={"docs": len(todo), "added": added, "invalid": invalid})


class RetrieverAgent(BaseAgent):
    """检索者：语义侧召回（向量 + BM25），结果写 ctx.hits。"""

    name = "Retriever"
    role = "语义检索"
    description = "向量 + BM25 混合召回知识片段"
    capabilities = ["retrieval", "semantic"]

    def run(self, ctx: AgentContext) -> AgentResult:
        from kb_mcp_server.retrieval import HybridRetriever

        retriever = HybridRetriever()
        ctx.hits = retriever.search(ctx.question, top_k=ctx.top_k)
        return AgentResult(agent=self.name, role=self.role, ok=True,
                           summary=f"召回 {len(ctx.hits)} 个片段",
                           detail={"hits": len(ctx.hits)})


class GraphReasonerAgent(BaseAgent):
    """图谱推理者：关系侧召回（多跳扩展），结果写 ctx.graph_facts。

    与 Retriever 互补：语义管「像什么」，这里管「和谁关联」。图后端不可用时静默降级。
    """

    name = "GraphReasoner"
    role = "图谱推理"
    description = "从问题抽实体，查图谱多跳邻居，返回可解释关系路径"
    capabilities = ["graph", "reasoning"]

    def run(self, ctx: AgentContext) -> AgentResult:
        if not _graph_enabled():
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary="图谱总开关关闭，跳过")
        g = graph_store_or_none()
        if not g:
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary="图后端不可用，跳过")
        from kb_mcp_server.graph import expand_facts

        gf = expand_facts(g, ctx.question)
        ctx.graph_facts = gf
        ents = [e["name"] for e in gf.get("entities", [])]
        facts = gf.get("facts", [])
        if not ents:
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary="问题中未识别出图谱实体")
        return AgentResult(agent=self.name, role=self.role, ok=True,
                           summary=f"命中 {len(ents)} 个实体，{len(facts)} 条关系事实",
                           detail={"entities": ents, "facts": len(facts)})


class LiveDataAgent(BaseAgent):
    """实时数据者：按意图调订单 / 库存适配器，结果写 ctx.live。"""

    name = "LiveData"
    role = "实时数据"
    description = "按问题意图调用订单/库存适配器取实时数据"
    capabilities = ["live", "api"]

    def run(self, ctx: AgentContext) -> AgentResult:
        from kb_mcp_server.adapters import fetch_live

        ctx.live = fetch_live(ctx.question, order_id=ctx.order_id, sku=ctx.sku)
        if ctx.live:
            return AgentResult(agent=self.name, role=self.role, ok=True,
                               summary=f"取到 {len(ctx.live)} 条实时数据",
                               detail={"live": len(ctx.live)})
        return AgentResult(agent=self.name, role=self.role, ok=True,
                           summary="无实时数据诉求")


class SynthesizerAgent(BaseAgent):
    """合成者：把语义侧 + 关系侧 + 实时数据装配成结构化答复，结果写 ctx.answer。"""

    name = "Synthesizer"
    role = "合成答复"
    description = "知识片段 + 图谱路径 + 实时数据 → 结构化答复（含置信度与轨迹）"
    capabilities = ["synthesis"]

    def run(self, ctx: AgentContext) -> AgentResult:
        from kb_mcp_server.synthesis import synthesize

        ctx.answer = synthesize(ctx.question, ctx.hits, ctx.live,
                                history=ctx.history, graph_facts=ctx.graph_facts)
        method = ctx.answer.get("synthesis_method", "?")
        conf = ctx.answer.get("confidence", {})
        n_paths = len(ctx.answer.get("graph_paths", []))
        extra = f"，{n_paths} 条关系路径" if n_paths else ""
        return AgentResult(agent=self.name, role=self.role, ok=True,
                           summary=f"合成完成（{method}，置信度{conf.get('label', '?')}{extra}）",
                           detail={"method": method, "confidence": conf})


# ---- 自注册进 AgentRegistry（P1-4 能力发现；未来「共同体」据此动态组队）----
def _register_all() -> None:
    reg = agent_registry()
    for _a in (GraphBuilderAgent(), RetrieverAgent(), GraphReasonerAgent(),
               LiveDataAgent(), SynthesizerAgent()):
        reg.register(_a)


_register_all()
