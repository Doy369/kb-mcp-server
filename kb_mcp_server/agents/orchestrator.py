"""编排器（Orchestrator）：多 agent 协作的调度中枢（P7 · 架构决策 D5/D6）。

两种模式（KB_AGENT_MODE）：
- deterministic（默认）：固定流水线，零 LLM 依赖，离线必跑通。
      GraphBuilder（增量补图）→ [Retriever ∥ GraphReasoner ∥ LiveData] → Synthesizer
      中间三个无依赖的 worker 用线程池并行，降低问答延迟。
- llm：本地 LLM 参与路由（判断要不要查图 / 查实时数据），失败自动回退 deterministic。

轨迹（trace）：每次执行返回每个 agent 的耗时、成败、摘要——
多 agent 的可观测性是硬要求，不然演示时说不清「谁干了什么」。
"""

from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from kb_mcp_server.agents.base import AgentContext, AgentResult
from kb_mcp_server.agents.workers import (
    GraphBuilderAgent,
    GraphReasonerAgent,
    LiveDataAgent,
    RetrieverAgent,
    SynthesizerAgent,
)
from kb_mcp_server.config import get_cfg
from kb_mcp_server.extensions import DeterministicPlanner


class Orchestrator:
    def __init__(self, mode: str | None = None, planner=None):
        self._mode = mode
        self.graph_builder = GraphBuilderAgent()
        self.retriever = RetrieverAgent()
        self.graph_reasoner = GraphReasonerAgent()
        self.live_data = LiveDataAgent()
        self.synthesizer = SynthesizerAgent()
        # P1-4 规划器 seam：默认确定性全跑；未来换成 LLM 动态分解只需传入别的 Planner
        self.planner = planner or DeterministicPlanner(
            self.retriever, self.graph_reasoner, self.live_data)

    @property
    def mode(self) -> str:
        if self._mode is not None:
            return self._mode
        return get_cfg("KB_AGENT_MODE", "deterministic")

    def agents_status(self) -> list[dict]:
        return [a.card() for a in (
            self.graph_builder, self.retriever, self.graph_reasoner,
            self.live_data, self.synthesizer,
        )]

    # ---- llm 路由（可选）----
    def _llm_route(self, ctx: AgentContext) -> dict | None:
        """让本地 LLM 决定要不要跑图谱推理 / 实时数据。失败返回 None（回退全跑）。"""
        from kb_mcp_server.llmclient import llm_chat

        prompt = (
            "你是客服问答的路由器。判断下面这个问题需要哪些能力，只输出 JSON：\n"
            '{"need_graph": true/false, "need_live": true/false, "reason": "一句话"}\n'
            "need_graph：问题涉及实体关系/多跳推理（如「A 适用于哪条条款」「同一根因吗」）时为 true；\n"
            "need_live：问题需要订单/库存等实时数据时为 true。\n\n"
            f"问题：{ctx.question}\nJSON："
        )
        raw = llm_chat(prompt, temperature=0.0, max_tokens=120, timeout=10)
        if not raw:
            return None
        try:
            i, j = raw.find("{"), raw.rfind("}")
            if i < 0 or j <= i:
                return None
            return json.loads(raw[i:j + 1])
        except Exception:
            return None

    # ---- 主入口 ----
    def ask(self, question: str, top_k: int = 5, order_id: str | None = None,
            sku: str | None = None, history: list[dict] | None = None) -> dict:
        ctx = AgentContext(question=question, top_k=top_k, order_id=order_id,
                           sku=sku, history=history, mode=self.mode)
        t0 = time.perf_counter()
        trace: list[dict] = []

        # 1) 建图（增量补图，幂等；图谱关掉时 agent 自己会跳过）
        if get_cfg("KB_AGENT_BUILD_GRAPH", "1").lower() in ("1", "true", "yes"):
            trace.append(self.graph_builder.execute(ctx).to_dict())

        # 2) 路由：deterministic 全跑；llm 模式下按需裁剪（失败回退全跑）
        need_graph = need_live = True
        routing = {"mode": self.mode, "decision": "run_all"}
        if self.mode == "llm":
            r = self._llm_route(ctx)
            if r:
                need_graph = bool(r.get("need_graph", True))
                need_live = bool(r.get("need_live", True))
                routing = {"mode": "llm", "decision": r.get("reason", ""),
                           "need_graph": need_graph, "need_live": need_live}
        ctx.data["routing"] = routing

        # 3) 并行执行三个无依赖的 worker（线程池；纯 IO/CPU 轻，GIL 无碍）
        #    由 Planner 决定本次跑哪些 agent —— 默认确定性全跑，预留动态协作入口
        plan = self.planner.plan(ctx)
        with ThreadPoolExecutor(max_workers=len(plan)) as ex:
            results = list(ex.map(lambda a: a.execute(ctx), plan))
        trace.extend(r.to_dict() for r in results)

        # 4) 合成
        trace.append(self.synthesizer.execute(ctx).to_dict())

        ms = round((time.perf_counter() - t0) * 1000)
        out = dict(ctx.answer)
        out["agents"] = {
            "mode": self.mode,
            "routing": routing,
            "trace": trace,
            "total_ms": ms,
            "failed": [t["agent"] for t in trace if not t.get("ok", False)],
        }
        return out


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
