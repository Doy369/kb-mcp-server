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

P1-4 收尾补齐两件事（同样默认不改变旧行为）：
4. **子任务依赖编排**：`Subtask.depends_on` 真正被消费——一轮内按依赖分层，
   同层并行、层间串行；**一个依赖都没有时恰好一层**，与旧的「全并行」逐字等价。
   成环时不重排、降级成一层照跑并记 `dep_cycles`（卡死比顺序偏差严重得多）。
5. **双向消息协商**：协商不再只是「评审点名 → agent 被静默跑一遍」，而是带**请求/应答账本**
   （`AgentContext.negotiation`）：
   - 发起方两边都可以是——评审提缺口，**worker 也能主动委托 peer**（`request_help`）；
   - 被点名方要么拿出证据（`provided`），要么明确说「我也没有」（`declined`），
     沉默不算数；
   - 被拒/无法服务的能力进「别再提」名单并在轨迹里写明原因。

P2-9 起接入**动作型工具**（`KB_AGENT_ACTIONS=1`，默认关闭）：
4. `ActionAgent` 识别「退款 / 改单 / 建工单」等动作意图并真正执行；
   不可逆动作（destructive）**不会被自动确认**，只停在待确认状态，并同样并入
   `pending_human` 判定——「agent 能动手」与「动手必须有人负责」同时成立。

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
    ActionAgent,
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
    Subtask,
    agent_gate_open,
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


# --------------------------------------------------------------------------- #
# P1-4 收尾：子任务依赖分层
# --------------------------------------------------------------------------- #
def _dep_layers(todo: list, subtasks: list) -> tuple[list[list], list[str]]:
    """把本轮待跑的 agent 按 `depends_on` 分层（同层并行、层间串行）。

    返回 `(layers, blocked)`：`blocked` 是因**成环**而无法分层的节点名（空 = 正常）。

    向后兼容的前提：**一个依赖都没有时必须恰好分成一层**，且层内顺序 == 计划顺序，
    这样「全并行」的旧行为逐字不变（既有用例锁定 `rounds[0]["agents"]` 的顺序）。

    依赖指向本轮之外的名字（已执行过 / 未入选）时**视为已满足**——上游都已跑完，
    没有等待的必要；这也是 LLM 给出的依赖名稍有偏差时唯一不会卡住的解释。

    成环时不抛异常、不做部分重排：把剩余节点合成一层照跑。理由与全项目的降级约定一致——
    「顺序略有偏差」远比「编排器卡死」可接受。
    """
    names = [getattr(a, "name", "") for a in todo]
    if not names:
        return [], []

    # 只保留「本轮内、非自己」的依赖
    in_round = set(names)
    pending_deps: dict[str, set[str]] = {n: set() for n in names}
    for s in subtasks or []:
        a = getattr(s, "agent", "")
        if a not in pending_deps:
            continue
        for d in (getattr(s, "depends_on", None) or []):
            if d in in_round and d != a:
                pending_deps[a].add(d)

    if not any(pending_deps.values()):
        return [todo], []                      # 无依赖：单层，顺序不变（热门路径）

    layers: list[list] = []
    remaining = {n: set(v) for n, v in pending_deps.items()}
    while remaining:
        ready = [n for n in names if n in remaining and not remaining[n]]
        if not ready:
            break                              # 剩余节点互相等待 → 成环
        ready_set = set(ready)
        # 层内保持**计划顺序**，而不是「谁先解冻谁在前」
        layers.append([a for a in todo if getattr(a, "name", "") in ready_set])
        for n in ready_set:
            remaining.pop(n, None)
        for n in remaining:
            remaining[n] -= ready_set

    if remaining:
        return [todo], sorted(remaining)        # 成环降级：整体一层照跑，留痕
    return layers, []


# 能力 → 该能力产出落在哪条黑板通道上（用于判「这次点名到底有没有产出」）
_CAP_EVIDENCE = {
    "retrieval": "hits", "semantic": "hits", "bm25": "hits", "keyword": "hits",
    "graph": "facts", "reasoning": "facts", "kg": "facts",
    "live": "live", "api": "live", "realtime": "live",
    "action": "actions", "actions": "actions", "ticket": "actions",
}


def _evidence_snapshot(ctx: AgentContext) -> dict:
    """黑板上「证据」的规模快照（请求前后各取一次，用于判产出）。"""
    return {
        "hits": len(ctx.hits or []),
        "facts": len((ctx.graph_facts or {}).get("facts") or []),
        "live": len(ctx.live or []),
        "actions": len(ctx.actions or []),
    }


def _cap_gained(ctx: AgentContext, capability: str, before: dict) -> bool:
    """这项能力在本轮有没有真的产出证据（按能力映射到对应黑板通道）。"""
    after = _evidence_snapshot(ctx)
    channel = _CAP_EVIDENCE.get(capability)
    if channel:
        return after.get(channel, 0) > before.get(channel, 0)
    return sum(after.values()) > sum(before.values())




class Orchestrator:
    def __init__(self, mode: str | None = None, planner=None):
        self._mode = mode
        self.graph_builder = GraphBuilderAgent()
        self.retriever = RetrieverAgent()
        self.graph_reasoner = GraphReasonerAgent()
        self.live_data = LiveDataAgent()
        self.synthesizer = SynthesizerAgent()
        # P2-9 动作执行者：**单独持有，不进 self.registry**。
        # 理由：agents_status() / roster() 是被既有用例锁定的对外契约（5 个成员），
        # 而动作执行者是否参与由 KB_AGENT_ACTIONS 决定，二者生命周期不同，
        # 混进同一张表会让「共同体成员」随开关漂移，反而说不清。
        self.action_agent = ActionAgent()
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

    # ---- P2-9 动作层 ----
    def actions_enabled(self) -> bool:
        """动作型工具是否已开启（由 ActionAgent 自己声明的闸门键决定）。"""
        return agent_gate_open(self.action_agent)

    def actions_roster(self) -> dict:
        """动作层能力清单：执行者 + 可用动作（含风险分级与参数契约）。

        与 roster() 分开返回，避免把「有副作用的执行者」混进只读的 agent 清单里。
        """
        from kb_mcp_server.actions import read_action_log

        card = self.action_agent.card()
        card["capabilities"] = list(self.action_agent.capabilities)
        card["gated_by"] = self.action_agent.gated_by
        tools = []
        try:
            from kb_mcp_server.actions import action_tools

            tools = action_tools().list()
        except Exception:  # noqa: BLE001 - 动作层不可用不应影响状态端点
            tools = []
        return {"enabled": self.actions_enabled(), "agent": card, "tools": tools,
                "recent": read_action_log(10)}

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

    def _ensure_request(self, ctx: AgentContext, by: str, capability: str,
                        ask: str = "", reason: str = "") -> str:
        """登记缺口请求（同一能力已有未应答请求时不重复登记）。"""
        cap = str(capability or "")
        for e in ctx.open_requests():
            if e.get("capability") == cap:
                return e["id"]
        return ctx.request_help(by, cap, ask=ask, reason=reason)

    def _resolve_requests(self, ctx: AgentContext, executed: set[str],
                          declined: set[str]) -> tuple[list, list[dict], list[dict]]:
        """把**所有未应答请求**解析成下一轮要跑的 agent。

        返回 `(agents, served, closed)`：
        - `agents`：下一轮要跑的 worker（按请求顺序去重）；
        - `served`：这些 agent 本轮要应答的请求条目；
        - `closed`：无法服务的条目 `{"id", "capability", "status"}`——能力已被明确拒绝
          （`declined`）、共同体里没有具备该能力的成员（`unavailable`）、
          或该成员已经跑过（`unavailable`）。

        「无法服务」必须当场记账：留着一条永远没人应答的请求，下一轮还会被重新解析，
        补轮就变成了空转。
        """
        agents: list = []
        served: list[dict] = []
        closed: list[dict] = []
        seen: set[str] = set()

        for req in ctx.open_requests():
            cap = str(req.get("capability") or "")

            def _shut(status: str) -> None:
                closed.append({"id": req["id"], "capability": cap, "status": status})

            if cap in declined:
                _shut("declined")
                continue
            cands = [a for a in self.registry.by_capability(cap)
                     if getattr(a, "name", "") in _WORKER_NAMES]
            if not cands:
                _shut("unavailable")
                continue
            a = cands[0]
            if getattr(a, "name", "") in executed:
                # 已经跑过：再叫一次只会重复消耗，且违反「同一 agent 只执行一次」
                _shut("unavailable")
                continue
            served.append(req)
            if a.name not in seen:
                seen.add(a.name)
                agents.append(a)
        return agents, served, closed


    def _auto_reply(self, ctx: AgentContext, served: list[dict],
                    ran: list[str], before: dict) -> tuple[list[dict], set[str]]:
        """给本轮被点名的请求补应答——**除非被点名方已经自己应答过**。

        规则很直白：跑完之后要么黑板上的对应证据变多了（`provided`），
        要么明确说「我也没有」（`declined`）。沉默不算数——
        否则「补了一轮还是没证据」会被当成「已尽力」，下一轮继续空转。

        `before` 是本轮执行前的证据快照（由调用方传入，**不放在 self 上**：
        编排器是进程级单例，服务端是多线程，实例状态会被并发请求互相污染）。

        返回 `(本轮新增的应答条目, 应记入「别再提」的能力集合)`。
        """
        replied = ctx.replies()
        out: list[dict] = []
        closed_caps: set[str] = set()

        for req in served:
            rid = req["id"]
            if rid in replied:
                entry = dict(replied[rid])
                out.append(entry)
                if entry.get("status") in ("declined", "unavailable"):
                    closed_caps.add(str(req.get("capability") or ""))
                continue

            cap = str(req.get("capability") or "")
            who = next((a.name for a in self.registry.by_capability(cap) if a.name in ran), None)
            if who is None:
                continue                     # 本轮没跑对应成员（不该发生，保守跳过）
            status = "provided" if _cap_gained(ctx, cap, before) else "declined"
            note = ("已产出该项证据" if status == "provided"
                    else "本轮跑完仍无该项证据，明确拒绝")
            ctx.reply(rid, who, status=status, note=note)
            out.append({"id": rid, "by": who, "status": status, "note": note})
            if status == "declined":
                closed_caps.add(cap)
        return out, closed_caps



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

        # 3) 规划 + 执行（P1-4）：Planner 决定分工；按子任务依赖分层；多轮双向协商补缺口。
        #    同层无依赖的 worker 用线程池并行（纯 IO/CPU 轻，GIL 无碍），跨层串行。
        max_rounds = _safe_int(get_cfg("KB_AGENT_MAX_ROUNDS", "1"), 1)
        executed: set[str] = set()
        rounds: list[dict] = []
        declined: set[str] = set()          # 「别再提」名单：被拒 / 无人可服务的能力
        plan_res = self.planner.strategize(ctx)
        # P2-9：动作执行者不在任何规划器的候选池里——它由开关控制，不由模型提名。
        # 「谁有权动手」是治理问题，不能交给 LLM 决定；开启后在此显式补进本轮计划。
        if self.actions_enabled() and self.action_agent not in plan_res.agents:
            plan_res.agents.append(self.action_agent)
            plan_res.subtasks.append(Subtask(
                agent=self.action_agent.name, capability="action",
                reason="已开启动作工具：识别到动作意图则执行（不可逆动作仅发起到待确认）"))
        pending = list(plan_res.agents)
        meta = {"source": plan_res.source, "reason": plan_res.reason,
                "subtasks": plan_res.subtasks}
        served_reqs: list[dict] = []        # 本轮「要应答谁」——首轮无人点名，为空

        for rnd in range(1, max_rounds + 1):
            todo = [a for a in pending if getattr(a, "name", "") not in executed]
            if not todo:
                break
            layers, blocked = _dep_layers(todo, meta.get("subtasks") or [])
            before = _evidence_snapshot(ctx)      # 执行前快照（判「本轮有没有产出」）
            results = []
            for layer in layers:
                if not layer:
                    continue
                with ThreadPoolExecutor(max_workers=len(layer)) as ex:
                    results.extend(ex.map(lambda a: a.execute(ctx), layer))
            trace.extend(r.to_dict() for r in results)
            ran = [getattr(a, "name", "") for a in todo]
            executed.update(ran)

            # 被点名方应答：自己答过就用它的，没答按「有没有产出证据」补一条。
            replies, just_closed = self._auto_reply(ctx, served_reqs, ran, before)
            declined |= just_closed

            rec = {"round": rnd, "agents": ran,
                   "source": meta["source"], "reason": meta["reason"]}
            if len(layers) > 1:
                # 只在真的分层时才出现——单层（默认）不改变既有轨迹形状
                rec["layers"] = [[getattr(a, "name", "") for a in l] for l in layers]
            if blocked:
                rec["dep_cycles"] = blocked
            subs = meta.get("subtasks") or []
            if subs:
                rec["subtasks"] = [s.to_dict() for s in subs]
            if served_reqs:
                rec["requests"] = [dict(r) for r in served_reqs]
            if replies:
                rec["replies"] = replies

            if rnd >= max_rounds:
                rounds.append(rec)
                break
            crit = self.critic.review(ctx, self._done_caps(executed), declined)
            rec["critique"] = crit.to_dict()
            rounds.append(rec)

            # 把评审缺口登记成**可应答请求**（发起方=EvidenceCritic），
            # 与 worker 自己挂的委托请求走同一本账、同一条调度路径。
            for r in crit.requests:
                self._ensure_request(ctx, r.get("by", "EvidenceCritic"),
                                     r.get("capability", ""),
                                     ask=r.get("ask", ""), reason=crit.reason)

            # worker 主动委托（delegation）也在这里被看到：即使评审认为证据已够，
            # 只要有 agent 提出了请求，就还得再走一轮去应答它。
            if not crit.need_more and not ctx.open_requests():
                break
            nxt, served_reqs, closed = self._resolve_requests(ctx, executed, declined)
            for c in closed:
                # 无法服务必须当场关账，否则下一轮还会被重新解析成「待办」
                ctx.reply(c["id"], "Orchestrator", status=c["status"],
                          note="本轮内无人可服务该请求（已执行过 / 无此能力 / 已被拒绝）")
                # 「别再提」名单：同一能力不再重复索要，也让「为什么没补轮」可解释。
                # 注：当前每个能力只有一个 owner，「已执行过」本身已经拦住了绝大多数
                # 重复索要；这份名单的价值在于**把原因写明**（declined vs unavailable），
                # 并在未来一个能力有多个 owner 时保持正确。
                if c["capability"]:
                    declined.add(c["capability"])
            if not nxt:
                break
            by_workers = {str(r.get("by") or "") for r in served_reqs}
            source = "negotiation" if ("EvidenceCritic" in by_workers or crit.need_more) \
                else "delegation"
            pending = nxt
            meta = {"source": source,
                    "reason": (crit.reason if crit.need_more
                               else "worker 主动委托：" + "、".join(sorted(by_workers))),
                    "subtasks": [Subtask(agent=a.name, capability="",
                                         reason="应请求补查")
                                 for a in nxt]}

        # 循环结束仍没人应答的请求一律关账：账本里不留悬空请求
        for req in ctx.open_requests():
            ctx.reply(req["id"], "Orchestrator", status="unavailable",
                      note="本轮次内未安排到服务者，已关账")
            if req.get("capability"):
                declined.add(str(req["capability"]))

        # 4) 合成
        trace.append(self.synthesizer.execute(ctx).to_dict())

        ms = round((time.perf_counter() - t0) * 1000)
        out = dict(ctx.answer)

        # P1-4 协作轨迹（新增字段；routing / trace 结构保持向后兼容）
        coll = {
            "planner": plan_res.source,
            "rounds": rounds,
            "negotiated": len(rounds) > 1,
            "executed": sorted(executed),
        }
        # P1-4 收尾：协商账本（谁提出、谁应答、答的是"有"还是"没有"）。
        # 只在真的发生过协商时出现——没协商就不给轨迹添噪音。
        if ctx.negotiation:
            coll["negotiation"] = {
                "requests": sum(1 for e in ctx.negotiation if e.get("kind") == "request"),
                "replies": sum(1 for e in ctx.negotiation if e.get("kind") == "reply"),
                "declined": sorted(declined),
                "delegated": any(
                    e.get("kind") == "request" and e.get("by") != "EvidenceCritic"
                    for e in ctx.negotiation),
                "ledger": list(ctx.negotiation),
            }
        out["collaboration"] = coll

        # P2-9 动作结果（新增字段；未开启动作层时恒为空列表，保持向后兼容）
        actions = list(ctx.actions or [])
        pending_actions = [a for a in actions if a.get("status") == "needs_confirmation"]
        out["actions"] = actions
        out["pending_actions"] = pending_actions

        # P1-4 人工介入：护栏判定「需人工复核」时把答复挂起等待放行。
        # 默认关闭；开启后才写入 human_review / pending_human。
        # P2-9：**存在待确认的不可逆动作**时同样必须进人工队列——
        # 否则「agent 发起了退款」会静默通过，确认门就形同虚设。
        if get_cfg("KB_AGENT_HITL", "0").lower() in ("1", "true", "yes"):
            g = out.get("guardrail") or {}
            need_review = g.get("passed") is False or bool(pending_actions)
            reasons: list[str] = []
            if g.get("passed") is False:
                reasons.append(g.get("reason", ""))
            if pending_actions:
                reasons.append("存在待人工确认的不可逆动作：" + "、".join(
                    str(a.get("action", "")) for a in pending_actions))
            out["human_review"] = {
                "required": need_review,
                "status": "pending" if need_review else "auto_approved",
                "reason": "；".join(r for r in reasons if r),
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
            # P1-4 收尾：协商可观测——「补了几轮」不够，还要知道「谁提的、有没有被拒」
            "declined_caps": sorted(declined),
            "delegated": bool((out["collaboration"].get("negotiation") or {})
                              .get("delegated")),
            "actions": len(actions),
            "pending_actions": len(pending_actions),
        })
        return out


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
