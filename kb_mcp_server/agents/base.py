"""多 agent 协作骨架（P7 · 架构决策 D5）。

三个设计原则：
1. **按职责切分，不按知识域切**——初期知识域太小，按域切会切出一堆空 agent。
2. **共享同一张图**：所有 agent 读写同一个 GraphStore 与向量库，协作靠 AgentContext（黑板）
   传递中间结果，不引入消息总线（D6：协议仍然是 MCP）。
3. **确定性优先**：默认 KB_AGENT_MODE=deterministic，全链路不依赖 LLM，离线必跑通；
   设为 llm 时 Orchestrator 才用本地 LLM 做路由决策。任一 agent 失败只降级，不阻断链路。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AgentContext:
    """agent 之间共享的黑板。每个 worker 只写自己负责的字段，别的不碰。"""

    question: str
    top_k: int = 5
    order_id: str | None = None
    sku: str | None = None
    history: list[dict] | None = None
    tenant_id: str | None = None          # P2-6 多租户隔离占位（预留）
    mode: str = "deterministic"          # deterministic | llm
    intent: dict = field(default_factory=dict)
    hits: list[dict] = field(default_factory=list)        # Retriever 写
    graph_facts: dict = field(default_factory=dict)       # GraphReasoner 写
    live: list[dict] = field(default_factory=list)        # LiveData 写
    graph_info: dict = field(default_factory=dict)        # GraphBuilder 写
    actions: list[dict] = field(default_factory=list)     # P2-9 ActionAgent 写（含被拒/待确认）
    answer: dict = field(default_factory=dict)            # Synthesizer 写
    data: dict = field(default_factory=dict)              # 编排器用（plan / trace）
    # P1-4 收尾：协商账本（append-only）。请求与应答都落这里，谁提的、谁答的、
    # 答的是「我有」还是「我也没有」都可追溯——单向点名变成双向协商。
    negotiation: list[dict] = field(default_factory=list)

    # ---- P1-4 收尾：双向消息协商（请求 / 应答）----
    def request_help(self, by: str, capability: str, ask: str = "",
                     reason: str = "") -> str:
        """登记一条「需要某项能力」的请求，返回请求 id。

        发起方可以是评审者（critic），也可以是**任何一个 worker**——
        worker 在执行中发现「我需要实时数据/图谱」时主动委托，比编排器猜更准。
        只是登记，不动手：调度仍由编排器统一做（否则 agent 之间会互相递归调用）。
        """
        rid = f"req-{sum(1 for e in self.negotiation if e.get('kind') == 'request') + 1}"
        self.negotiation.append({"kind": "request", "id": rid, "by": by,
                                 "capability": str(capability), "ask": ask,
                                 "reason": reason})
        return rid

    def reply(self, rid: str, by: str, status: str = "provided",
              note: str = "") -> None:
        """对一条请求作出应答。

        `status`：`provided`（已尽力，产出见黑板）/ `declined`（这项我也做不了）
        / `unavailable`（共同体里没有谁能做）。**declined 是有效信息**——
        编排器记住它，就不会再拿同一项能力反复空转。
        """
        self.negotiation.append({"kind": "reply", "id": rid, "by": by,
                                 "status": status, "note": note})

    def replies(self) -> dict[str, dict]:
        """已应答请求：id -> reply 条目。"""
        return {e["id"]: e for e in self.negotiation if e.get("kind") == "reply"}

    def open_requests(self) -> list[dict]:
        """尚未被应答的请求（协商的下一步输入）。"""
        answered = set(self.replies())
        return [e for e in self.negotiation
                if e.get("kind") == "request" and e.get("id") not in answered]



@dataclass
class AgentResult:
    """单个 agent 的执行结果，直接作为可观测轨迹的一行。"""

    agent: str
    role: str
    ok: bool = True
    ms: int = 0
    summary: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"agent": self.agent, "role": self.role, "ok": self.ok,
               "ms": self.ms, "summary": self.summary}
        if self.detail:
            out["detail"] = self.detail
        return out


class BaseAgent(ABC):
    name: str = "Agent"
    role: str = ""
    description: str = ""
    capabilities: list[str] = []          # P1-4 能力标签，供 AgentRegistry 发现
    # P2-9 参与闸门：留空=始终可被提名；非空=该配置键为真时才参与动态组队。
    # 有副作用的 agent（如 ActionAgent）必须挂闸门，宁可默认不动手。
    gated_by: str = ""

    @abstractmethod
    def run(self, ctx: AgentContext) -> AgentResult:
        """执行本 agent 的职责，把结果写进 ctx。"""

    def execute(self, ctx: AgentContext) -> AgentResult:
        """模板方法：计时 + 异常兜底——单个 agent 挂了不能拖垮整条链路。"""
        t0 = time.perf_counter()
        try:
            res = self.run(ctx)
        except Exception as e:  # noqa: BLE001
            res = AgentResult(agent=self.name, role=self.role, ok=False,
                              summary=f"失败：{e}")
        res.ms = max(1, round((time.perf_counter() - t0) * 1000))
        res.agent = res.agent or self.name
        res.role = res.role or self.role
        return res

    def card(self) -> dict:
        """agent 能力卡片，供 agent_status 与前端展示。"""
        return {"name": self.name, "role": self.role, "description": self.description}

    # ---- P1-4 收尾：worker 侧的协商便利方法（薄封装，账本仍在黑板上）----
    def request_help(self, ctx: AgentContext, capability: str, ask: str = "",
                     reason: str = "") -> str:
        """执行中主动委托：请求共同体里具备该能力的 agent 参与本轮。

        与「等编排器发现缺口」的区别在于发起方——worker 自己最清楚缺什么
        （例如检索到的实体需要按关系再查一层）。**只登记不直接调用**，
        由编排器统一调度，避免 agent 之间递归互调。
        """
        return ctx.request_help(self.name, capability, ask=ask, reason=reason)

    def reply_to(self, ctx: AgentContext, rid: str, status: str = "provided",
                 note: str = "") -> None:
        """应答一条点名请求。做不了就明确 `declined`——沉默会被当成「已尽力」。"""
        ctx.reply(rid, self.name, status=status, note=note)

