"""编排器（Orchestrator）：多 agent 协作的调度中枢（P7 · 架构决策 D5/D6）。

两种模式（KB_AGENT_MODE）：
- deterministic（默认）：固定流水线，零 LLM 依赖，离线必跑通。
      GraphBuilder（增量补图）→ [Retriever ∥ GraphReasoner ∥ LiveData] → Synthesizer
      中间三个无依赖的 worker 用线程池并行，降低问答延迟。
- llm：本地 LLM 参与路由（判断要不要查图 / 查实时数据），失败自动回退 deterministic。

P1-4 起支持三层「动态协作」（均为可选，默认关闭时行为与旧版完全一致）：
1. **动态任务分解**（`KB_AGENT_PLANNER=llm`）：由 `LLMPlanner` 按问题临时决定叫哪些 worker，
   而不是固定全跑；LLM 不可用时回退确定性规划。
2. **多轮协商**（`KB_AGENT_MAX_ROUNDS>1`）：worker 产出后由 `EvidenceCritic` 评估证据缺口，
   点名补查缺失能力的 agent 再跑一轮（受轮次上限约束，必然收敛）。
3. **人工介入**（`KB_AGENT_HITL=1`）：护栏判定「需人工复核」时把答复标为 `pending_human`。

轨迹（trace）：每次执行返回每个 agent 的耗时、成败、摘要，外加 `collaboration` 段
（每轮分工 / 协商结论）——多 agent 的可观测性是硬要求，不然演示时说不清「谁干了什么」。
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
from kb_mcp_server.audit import audit
from kb_mcp_server.config import get_cfg
from kb_mcp_server.extensions import (
    AgentRegistry,
    DeterministicPlanner,
    EvidenceCritic,
    LLMPlanner,
)

# 可参与动态协作的 worker（骨架 agent 由编排器固定调度，不参与选人）
_WORKER_NAMES = frozenset({"Retriever", "GraphReasoner", "LiveData"})


def _safe_int(raw, default: int, lo: int = 1) -> int:
    """容错解析整数配置：非法值回落默认。

    配置一旦被写坏（页面/环境变量），问答不能因此崩掉——这是全项目的降级约定。
    """
    try:
        n = int(str(raw).strip())
    except Exception:  # noqa: BLE001
        return default
    return max(lo, n)


class Orchestrator:
    def __init__(self, mode: str | None = None, planner=None):
        self._mode = mode
        self.graph_builder = GraphBuilderAgent()
        self.retriever = RetrieverAgent()
        self.graph_reasoner = GraphReasonerAgent()
        self.live_data = LiveDataAgent()
        self.synthesizer = SynthesizerAgent()
        # P1-4：本实例的「共同体成员表」。用私有 registry 而非全局单例——
        # 全局注册表是模块级的，多个 Orchestrator（含测试实例）并存会互相覆盖。
        self.registry = AgentRegistry([
            self.graph_builder, self.retriever, self.graph_reasoner,
            self.live_data, self.synthesizer,
        ])
        # 规划器 seam：deterministic（默认）| llm（动态任务分解，失败回退确定性）
        self.planner = planner or self._build_planner()
        # 证据协商者：草稿后评估缺口 → 补轮（仅 KB_AGENT_MAX_ROUNDS>1 时真正生效）
        self.critic = EvidenceCritic()

    def _build_planner(self):
        """按配置选规划器：未显式配置时，llm 协作模式默认启用动态分解。"""
        want = (get_cfg("KB_AGENT_PLANNER", "") or "").strip().lower()
        if not want:
            want = "llm" if self.mode == "llm" else "deterministic"
        if want == "llm":
            return LLMPlanner(self.retriever, self.graph_reasoner, self.live_data,
                              registry=self.registry)
        return DeterministicPlanner(self.retriever, self.graph_reasoner, self.live_data)

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

    def roster(self) -> list[dict]:
        """能力清单：agent 自描述 + capabilities，供状态端点与「谁被叫来」的对照展示。"""
        out = []
        for a in self.registry.all():
            card = a.card()
            card["capabilities"] = list(getattr(a, "capabilities", None) or [])
            out.append(card)
        return out

    # ---- llm 路由（可选；启用 LLM 动态分解时不再重复调用）----
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

    # ---- P1-4 协商辅助 ----
    def _done_caps(self, executed: set[str]) -> set[str]:
        """已执行 agent 覆盖到的能力集合（协商据此判断「这项能力是否已试过」）。"""
        caps: set[str] = set()
        for name in executed:
            a = self.registry.get(name)
            caps.update(getattr(a, "capabilities", None) or [])
        return caps

    def _agents_for_caps(self, caps: list[str]) -> list:
        """按能力从共同体成员表里点名补人（只认 worker，不碰骨架 agent）。"""
        picked: list = []
        seen: set[str] = set()
        for c in caps:
            for a in self.registry.by_capability(c):
                if a.name not in _WORKER_NAMES or a.name in seen:
                    continue
                seen.add(a.name)
                picked.append(a)
                break
        return picked

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

        # 2) 路由：deterministic 全跑；llm 模式下按需裁剪（失败回退全跑）。
        #    启用 LLM 动态分解时，规划器自己会调 LLM 做分解，此处不再重复问一次。
        llm_decompose = isinstance(self.planner, LLMPlanner)
        need_graph = need_live = True
        routing = {"mode": self.mode, "decision": "run_all"}
        if self.mode == "llm" and not llm_decompose:
            r = self._llm_route(ctx)
            if r:
                need_graph = bool(r.get("need_graph", True))
                need_live = bool(r.get("need_live", True))
                routing = {"mode": "llm", "decision": r.get("reason", ""),
                           "need_graph": need_graph, "need_live": need_live}
        ctx.data["routing"] = routing

        # 3) 规划 + 执行（P1-4）：Planner 决定分工；多轮协商补齐证据缺口。
        #    worker 之间无依赖，用线程池并行（纯 IO/CPU 轻，GIL 无碍）。
        max_rounds = _safe_int(get_cfg("KB_AGENT_MAX_ROUNDS", "1"), 1)
        executed: set[str] = set()
        rounds: list[dict] = []
        plan_res = self.planner.strategize(ctx)
        pending = list(plan_res.agents)
        meta = {"source": plan_res.source, "reason": plan_res.reason,
                "subtasks": plan_res.subtasks}

        for rnd in range(1, max_rounds + 1):
            todo = [a for a in pending if getattr(a, "name", "") not in executed]
            if not todo:
                break
            with ThreadPoolExecutor(max_workers=len(todo)) as ex:
                results = list(ex.map(lambda a: a.execute(ctx), todo))
            trace.extend(r.to_dict() for r in results)
            executed.update(getattr(a, "name", "") for a in todo)

            rec = {"round": rnd,
                   "agents": [getattr(a, "name", "") for a in todo],
                   "source": meta["source"],
                   "reason": meta["reason"]}
            subs = meta.get("subtasks") or []
            if subs:
                rec["subtasks"] = [s.to_dict() for s in subs]

            if rnd >= max_rounds:
                rounds.append(rec)
                break
            crit = self.critic.review(ctx, self._done_caps(executed))
            rec["critique"] = crit.to_dict()
            rounds.append(rec)
            if not crit.need_more:
                break
            extra = [a for a in self._agents_for_caps(crit.missing)
                     if getattr(a, "name", "") not in executed]
            if not extra:
                break
            pending, meta = extra, {"source": "negotiation",
                                    "reason": crit.reason, "subtasks": []}

        # 4) 合成
        trace.append(self.synthesizer.execute(ctx).to_dict())

        ms = round((time.perf_counter() - t0) * 1000)
        out = dict(ctx.answer)

        # P1-4 协作轨迹（新增字段；routing / trace 结构保持向后兼容）
        out["collaboration"] = {
            "planner": plan_res.source,
            "rounds": rounds,
            "negotiated": len(rounds) > 1,
            "executed": sorted(executed),
        }

        # P1-4 人工介入：护栏判定「需人工复核」时把答复挂起等待放行。
        # 默认关闭；开启后才写入 human_review / pending_human。
        if get_cfg("KB_AGENT_HITL", "0").lower() in ("1", "true", "yes"):
            g = out.get("guardrail") or {}
            need_review = g.get("passed") is False
            out["human_review"] = {
                "required": need_review,
                "status": "pending" if need_review else "auto_approved",
                "reason": (g.get("reason", "") if need_review else ""),
            }
            out["pending_human"] = need_review

        out["agents"] = {
            "mode": self.mode,
            "routing": routing,
            "trace": trace,
            "total_ms": ms,
            "failed": [t["agent"] for t in trace if not t.get("ok", False)],
        }
        # P1-5 审计日志：每次问答落一条 JSONL（问题/置信度/护栏判定/耗时/失败 agent）
        audit({
            "trace_id": out.get("trace_id"),
            "question": question,
            "confidence": out.get("confidence"),
            "synthesis_method": out.get("synthesis_method"),
            "guardrail": out.get("guardrail"),
            "latency_ms": ms,
            "agent_mode": self.mode,
            "failed_agents": out["agents"]["failed"],
            "planner": out["collaboration"]["planner"],
            "rounds": len(rounds),
        })
        return out


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
