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
    mode: str = "deterministic"          # deterministic | llm
    intent: dict = field(default_factory=dict)
    hits: list[dict] = field(default_factory=list)        # Retriever 写
    graph_facts: dict = field(default_factory=dict)       # GraphReasoner 写
    live: list[dict] = field(default_factory=list)        # LiveData 写
    graph_info: dict = field(default_factory=dict)        # GraphBuilder 写
    answer: dict = field(default_factory=dict)            # Synthesizer 写
    data: dict = field(default_factory=dict)              # 编排器用（plan / trace）


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
