from mcp.server.fastmcp import FastMCP

from kb_mcp_server.adapters import fetch_live
from kb_mcp_server.config import get_settings
from kb_mcp_server.embeddings import get_embedder
from kb_mcp_server.extensions import install_default_guardrail
from kb_mcp_server.graph import expand_facts, format_path
from kb_mcp_server.ingestion import IngestionPipeline, graph_enabled
from kb_mcp_server.retrieval import HybridRetriever
from kb_mcp_server.storage import PGVectorStore, get_store
from kb_mcp_server.synthesis import synthesize

settings = get_settings()
mcp = FastMCP("kb-mcp-server")
# P1-5 护栏：低置信度答复标记「需人工复核」（阈值见 KB_GUARDRAIL_MIN_CONFIDENCE）
install_default_guardrail()

# 进程内单例：保证 ingest / search 共用同一存储（memory 后端尤其重要）
_store = None
_retriever = None
_ingestor = None
_graph = None


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


def _get_graph():
    """图存储单例；不可用时返回 None（所有图谱能力静默降级，不影响主链路）。"""
    global _graph
    if _graph is None:
        if not graph_enabled():
            _graph = False
            return None
        try:
            from kb_mcp_server.graph import get_graph_store

            _graph = get_graph_store()
        except Exception as e:  # noqa: BLE001
            print(f"[graph] 图存储不可用（{e}），图谱能力已关闭")
            _graph = False
    return _graph or None


def _graph_facts(question: str, depth: int = 2, limit: int = 12) -> dict:
    """GraphRAG 关系侧召回（实现在 graph.expand_facts，这里只做单例装配与降级）。"""
    g = _get_graph()
    if not g:
        return {}
    return expand_facts(g, question, depth=depth, limit=limit)


@mcp.tool()
def ping() -> str:
    """健康检查：返回 ok 表示服务存活。"""
    return "ok"


@mcp.tool()
def ingest_document(doc_id: str, text: str, chunk_size: int = 300, overlap: int = 50) -> dict:
    """把一篇文档灌入知识库：切片 -> 本地嵌入 -> 写入向量库，并顺带抽取三元组入知识图谱。"""
    _, ingestor = _pipeline()
    n = ingestor.ingest_text(doc_id, text, chunk_size=chunk_size, overlap=overlap)
    out = {"doc_id": doc_id, "chunks": n, "backend": settings.storage_backend}
    if ingestor.last_graph_result:
        out["graph"] = ingestor.last_graph_result
    return out


@mcp.tool()
def search_knowledge(query: str, top_k: int = 5, mode: str = "hybrid") -> list[dict]:
    """混合检索知识库（向量语义召回）。返回带相似度分数的知识片段。"""
    retriever, _ = _pipeline()
    return retriever.search(query, top_k=top_k, mode=mode)


@mcp.tool()
def ask_with_live_data(question: str, top_k: int = 5, order_id: str | None = None, sku: str | None = None, history: list[dict] | None = None) -> dict:
    """检索知识 + 调用外部 API + 查知识图谱 + 合成最终答复。

    先向量召回 top-k 知识片段，再按显式参数或问题意图调用外部后端适配器
    （订单状态 / 库存）取实时数据，同时从知识图谱取多跳关系事实，
    最后由合成层装配为结构化答复（含置信度、关系路径与轨迹）。
    history 为可选多轮上下文（[{role, content}]），仅在启用本地 LLM 时用于增强合成。
    """
    retriever, _ = _pipeline()
    hits = retriever.search(question, top_k=top_k)
    live = fetch_live(question, order_id=order_id, sku=sku)
    gf = _graph_facts(question)
    return synthesize(question, hits, live, history=history, graph_facts=gf)


@mcp.tool()
def list_documents() -> list[dict]:
    """列出知识库中所有文档（含片段数与首段预览）。用于知识库管理。"""
    store = _get_store()
    return store.list_docs()


@mcp.tool()
def delete_document(doc_id: str) -> dict:
    """从知识库删除某篇文档及其全部片段，并清理它在图谱中抽出的关系。"""
    store = _get_store()
    n = store.delete_doc(doc_id)
    out = {"doc_id": doc_id, "deleted_chunks": n}
    g = _get_graph()
    if g:
        try:
            out["graph"] = g.delete_by_doc(doc_id)
        except Exception as e:  # noqa: BLE001
            out["graph_error"] = str(e)
    return out


# --------------------------------------------------------------------------- #
# P6 · 知识图谱工具（GraphRAG 关系层，对任意 MCP agent 开放）
# --------------------------------------------------------------------------- #
@mcp.tool()
def graph_query(entity: str, relation: str | None = None, direction: str = "out",
                depth: int = 2, limit: int = 30) -> dict:
    """查知识图谱里某实体的关联（支持多跳）。

    entity：实体名，如「物流配送」「48小时内响应」（也可写「IssueCategory:物流配送」）
    relation：可选，限定关系类型（MENTIONS/CATEGORY_OF/SUBMITTED_BY/ABOUT_PRODUCT/
              GOVERNED_BY/SOLVED_BY/CAUSED_BY）
    direction：out（出边）/ in（入边）/ both
    depth：跳数，1-4
    返回每个邻居的完整路径，路径本身就是可解释证据。
    """
    g = _get_graph()
    if not g:
        return {"error": "图谱未启用或后端不可用（检查 KB_GRAPH_ENABLED / KB_GRAPH_BACKEND）"}
    return g.neighbors(entity, rel=relation, direction=direction, depth=depth, limit=limit)


@mcp.tool()
def graph_expand(query: str, top_k: int = 5, depth: int = 2) -> dict:
    """GraphRAG 关系侧召回：从问题抽实体，再查图谱多跳邻居，返回可解释的关系事实。

    与 search_knowledge（语义侧）互补：语义管「像什么」，这里管「和谁关联」。
    返回 {entities:[命中实体], facts:[{subject, relation, object, path}]}。
    """
    limit = max(1, int(top_k) * 2) if top_k else 10
    return _graph_facts(query, depth=depth, limit=limit)


@mcp.tool()
def graph_paths(src: str, dst: str, max_depth: int = 3, limit: int = 10) -> list[dict]:
    """查两个实体之间的推理路径，例如「物流配送」到「全额退款」走了几跳。

    路径即证据：可直接作为答复「为什么这么判」的依据。
    """
    g = _get_graph()
    if not g:
        return []
    return g.paths(src, dst, max_depth=max_depth, limit=limit)


@mcp.tool()
def graph_entities(name: str = "", node_type: str | None = None, limit: int = 20) -> list[dict]:
    """列出图谱中的实体（可按名称模糊匹配 / 按类型过滤），按关联度排序。

    node_type：Document / Ticket / Customer / Product / IssueCategory /
               SLAClause / Solution / RootCause
    """
    g = _get_graph()
    if not g:
        return []
    return g.find_entities(name=name, node_type=node_type, limit=limit)


@mcp.tool()
def graph_stats() -> dict:
    """图谱概况：节点/边数量、按类型分布、本体定义（节点类型与关系约束）。"""
    g = _get_graph()
    if not g:
        return {"enabled": False, "reason": "图谱未启用或后端不可用"}
    s = g.stats()
    s["enabled"] = True
    return s


@mcp.tool()
def graph_rebuild() -> dict:
    """用向量库里已有的全部文档重新抽取建图（存量知识补建图谱 / 换本体后重建）。"""
    g = _get_graph()
    store = _get_store()
    if not g:
        return {"error": "图谱未启用或后端不可用"}
    from kb_mcp_server.graph import build_graph_from_store

    return build_graph_from_store(g, store)


# --------------------------------------------------------------------------- #
# P7 · 多 agent 协作（编排器 + 5 个职责 agent，共享同一张图与向量库）
# --------------------------------------------------------------------------- #
@mcp.tool()
def multi_agent_ask(question: str, top_k: int = 5, order_id: str | None = None,
                    sku: str | None = None, history: list[dict] | None = None) -> dict:
    """多 agent 协作问答。

    编排器调度多个职责 agent：GraphBuilder（增量补图）→
    [Retriever（语义召回）∥ GraphReasoner（图谱多跳）∥ LiveData（实时数据）
     ∥ ActionAgent（动作执行，默认关闭）] →
    Synthesizer（合成）。答复附带每个 agent 的耗时 / 成败 / 摘要轨迹（agents.trace）。

    模式由 KB_AGENT_MODE 决定：deterministic（默认，零 LLM，离线可跑）| llm（本地 LLM 路由，
    失败自动回退 deterministic）。

    P1-4 动态协作（可选，默认关闭）：KB_AGENT_PLANNER=llm 时由 LLM 动态分解分工
    （失败回退确定性）；KB_AGENT_MAX_ROUNDS>1 时 worker 产出后由证据协商补查缺口；
    KB_AGENT_HITL=1 时护栏判定「需人工复核」的答复标记 pending_human。
    协作轨迹见返回的 collaboration 段（每轮分工 / 协商结论）。

    P2-9 动作型工具（可选，默认关闭）：KB_AGENT_ACTIONS=1 时 ActionAgent 参与编排，
    识别「退款 / 改单 / 建工单」等动作意图并真正执行；不可逆动作只会停在待确认
    （见返回的 actions / pending_actions），并同样并入 pending_human 判定。
    """
    from kb_mcp_server.agents import get_orchestrator

    return get_orchestrator().ask(question, top_k=top_k, order_id=order_id,
                                  sku=sku, history=history)


@mcp.tool()
def agent_status() -> dict:
    """列出多 agent 协作层的 agent 清单（含能力标签）、当前编排模式与规划器。"""
    from kb_mcp_server.agents import get_orchestrator

    o = get_orchestrator()
    return {"mode": o.mode, "agents": o.agents_status(), "roster": o.roster(),
            "planner": type(o.planner).__name__, "actions": o.actions_roster()}


# --------------------------------------------------------------------------- #
# P2-9 · 动作型工具（与检索型工具的本质区别：**有副作用**）
#
# 因此对外暴露时多了三样东西：风险分级（read/write/destructive）、
# 是否需要确认（requires_confirm）、以及每次尝试都落审计（KB_ACTION_LOG）。
# 「能被真实 MCP 客户端调起并真的产生副作用」才是 MCP 从标题变成实质的证据。
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_actions() -> dict:
    """列出可用动作及其风险分级、是否需要确认、参数契约，并回带最近的动作审计。

    risk 含义：read（只读）| write（改业务数据）| destructive（不可逆/涉及资金）。
    需要确认的动作（requires_confirm=true）必须以 confirmed=true 重放才会执行。
    `graph_writeback` 表示执行成功后是否把结果回写知识图谱（`KB_ACTION_GRAPH`，
    默认关）：开启后每次执行会多出 `graph` 字段，审计里同样多一段。
    """
    from kb_mcp_server.action_graph import effects_enabled
    from kb_mcp_server.actions import action_tools, read_action_log
    from kb_mcp_server.agents.workers import ActionAgent
    from kb_mcp_server.extensions import agent_gate_open

    return {"enabled": agent_gate_open(ActionAgent()),
            "graph_writeback": effects_enabled(),
            "actions": action_tools().list(),
            "recent": read_action_log(10)}


@mcp.tool()
def run_action(action: str, params: dict | None = None, confirmed: bool = False,
               actor: str = "mcp-client") -> dict:
    """执行一个动作（create_ticket / update_order / request_refund / list_tickets）。

    **安全底线**：risk=destructive 的动作（退款）在 confirmed 不为 true 时
    **一律不执行**，只返回 status=needs_confirmation 并回显待执行的参数，
    必须由人工确认后以 confirmed=true 重放。
    返回 status ∈ {executed, needs_confirmation, rejected}；失败不抛异常。
    每次尝试（含被拒 / 待确认）都会写入动作审计日志。

    开启 `KB_ACTION_GRAPH=1` 时，**执行成功**的动作会多返回一个 `graph` 段
    （结果已回写知识图谱：节点 key / 关系条数 / 实际后端）；图谱不可用时该段
    变成 `applied=false` 的降级说明，**不影响动作本身的结论**。
    需要挂到客户 / 产品上的动作，可在 params 里附带 `customer` / `sku`。
    """
    from kb_mcp_server.actions import run_action as _run

    return _run(action, params, confirmed=confirmed, actor=actor or "mcp-client")


if __name__ == "__main__":
    mcp.run()
