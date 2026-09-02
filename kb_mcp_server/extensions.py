"""拓展接口预留层（roadmap 各待办的「可插拔 seam」）。

设计原则：
- 全部是「接口（ABC）+ 默认实现」，接入后**当前行为不变化**。
- 将来按 ROADMAP.md 的优先级补做时，只需实现对应接口并注册，
  无需改动编排 / 合成主链路 —— 这是「预留拓展接口」的核心目的。

覆盖的待办（详见仓库根 ROADMAP.md）：
- P2-6 多租户 / 工作区隔离  → TenantProvider
- P1-4 动态协作 / 规划器     → Planner（把静态 DAG 升级为可 emergent 的共同体）
- P1-4 能力注册表           → AgentRegistry（agent 自描述、可被发现）
- P1-5 护栏 / 答案闸门      → Guardrail（置信度真正用来拦截低质量答复）
- P1-5 评估 / 回归          → Evaluator + load_golden（golden 集 + 指标）
- P2-9 动作型工具           → ActionTool（区别于检索型工具，agent 可"执行动作"）
- P0-2 适配器重试 / 熔断     → RetryPolicy
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# 注意：AgentContext / BaseAgent 仅用于类型注解（本文件顶部已 `from __future__
# import annotations`，注解不触发求值），因此**不在此处顶层导入** kb_mcp_server.agents，
# 避免 extensions → agents.base → agents.__init__ → workers → extensions 的循环导入。


# ---------------------------------------------------------------------------
# P2-6 多租户 / 工作区隔离
# ---------------------------------------------------------------------------
@dataclass
class TenantContext:
    tenant_id: str | None = None
    workspace: str | None = None
    roles: list[str] = field(default_factory=list)   # RBAC 角色占位


class TenantProvider(ABC):
    """将请求解析为租户上下文（鉴权 / SSO / 工作区隔离）。默认单租户。"""

    @abstractmethod
    def resolve(self, token: str | None = None, headers: dict | None = None) -> TenantContext: ...


class SingleTenantProvider(TenantProvider):
    def resolve(self, token=None, headers=None) -> TenantContext:
        return TenantContext(tenant_id=None, workspace="default")


# ---------------------------------------------------------------------------
# P1-4 动态协作 / 规划器（把「静态 DAG」升级为「可 emergent 的共同体」）
# ---------------------------------------------------------------------------
class Planner(ABC):
    """决定本次问答并行跑哪些 agent。

    当前只有确定性全跑；未来可换成 LLM 动态任务分解 / 多轮协商 /
    human-in-the-loop，只要实现 plan() 即可插拔，不动 Orchestrator。
    """

    @abstractmethod
    def plan(self, ctx: AgentContext) -> list[BaseAgent]: ...


class DeterministicPlanner(Planner):
    def __init__(self, retriever, graph_reasoner, live_data):
        self._r, self._g, self._l = retriever, graph_reasoner, live_data

    def plan(self, ctx: AgentContext) -> list[BaseAgent]:
        routing = (ctx.data or {}).get("routing", {})
        plan: list[BaseAgent] = [self._r]
        if routing.get("need_graph", True):
            plan.append(self._g)
        if routing.get("need_live", True):
            plan.append(self._l)
        return plan


# ---------------------------------------------------------------------------
# P1-4 agent 能力注册表（让 agent 可自描述、可被「共同体」发现）
# ---------------------------------------------------------------------------
class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> BaseAgent:
        self._agents[agent.name] = agent
        return agent

    def get(self, name: str) -> BaseAgent | None:
        return self._agents.get(name)

    def all(self) -> list[BaseAgent]:
        return list(self._agents.values())

    def by_capability(self, cap: str) -> list[BaseAgent]:
        return [a for a in self._agents.values() if cap in (getattr(a, "capabilities", None) or [])]


_AGENT_REGISTRY = AgentRegistry()


def agent_registry() -> AgentRegistry:
    return _AGENT_REGISTRY


# ---------------------------------------------------------------------------
# P1-5 护栏 / 答案闸门（置信度真正用来拦截低质量答复）
# ---------------------------------------------------------------------------
@dataclass
class GateResult:
    passed: bool
    reason: str = ""
    action: str = "pass"            # pass | warn | block | escalate
    override: dict | None = None    # 若需改写答案


class Guardrail(ABC):
    @abstractmethod
    def check(self, answer: dict, ctx: AgentContext | None = None) -> GateResult: ...


class PassthroughGuardrail(Guardrail):
    def check(self, answer, ctx=None) -> GateResult:
        return GateResult(passed=True, action="pass")


_DEFAULT_GUARDRAIL: Guardrail | None = None


def set_guardrail(g: Guardrail | None) -> None:
    global _DEFAULT_GUARDRAIL
    _DEFAULT_GUARDRAIL = g


def apply_guardrail(answer: dict, ctx: AgentContext | None = None) -> dict:
    """合成后调用；默认无护栏（直通）。接入真实 Guardrail 后自动生效。"""
    g = _DEFAULT_GUARDRAIL
    if g is None:
        return answer
    res = g.check(answer, ctx)
    if not res.passed and res.action in ("block", "escalate"):
        answer = dict(answer)
        answer["guardrail"] = {"passed": False, "reason": res.reason, "action": res.action}
        answer["answer"] = (answer.get("answer") or "") + "\n[需人工复核]"
    elif res.override:
        answer = dict(answer)
        answer.update(res.override)
    return answer


# ---------------------------------------------------------------------------
# P1-5 评估 / 回归（golden 集 + 指标）
# ---------------------------------------------------------------------------
@dataclass
class GoldenCase:
    question: str
    expect_contains: list[str] = field(default_factory=list)
    order_id: str | None = None
    sku: str | None = None
    min_confidence: float = 0.0


class Evaluator(ABC):
    @abstractmethod
    def evaluate(self, case: GoldenCase, actual: dict) -> dict: ...


def load_golden(path: str) -> list[GoldenCase]:
    """从 JSON / JSONL 载入 golden 集。每行 {question, expect_contains, ...}。"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
    except Exception:
        items = [json.loads(l) for l in text.splitlines() if l.strip()]
    out: list[GoldenCase] = []
    for it in items:
        out.append(GoldenCase(
            question=it["question"],
            expect_contains=it.get("expect_contains", []),
            order_id=it.get("order_id"),
            sku=it.get("sku"),
            min_confidence=float(it.get("min_confidence", 0.0)),
        ))
    return out


# ---------------------------------------------------------------------------
# P2-9 动作型工具（区别于检索型工具：agent 可执行「改单 / 退款 / 建工单」等动作）
# ---------------------------------------------------------------------------
class ActionTool(ABC):
    name: str = "action"
    description: str = ""

    @abstractmethod
    def execute(self, params: dict) -> dict: ...


class ActionToolRegistry:
    def __init__(self):
        self._tools: dict[str, ActionTool] = {}

    def register(self, t: ActionTool) -> ActionTool:
        self._tools[t.name] = t
        return t

    def get(self, name: str) -> ActionTool | None:
        return self._tools.get(name)

    def list(self) -> list[dict]:
        return [{"name": t.name, "description": t.description} for t in self._tools.values()]


_ACTION_REGISTRY = ActionToolRegistry()


def action_registry() -> ActionToolRegistry:
    return _ACTION_REGISTRY


# ---------------------------------------------------------------------------
# P0-2 实时适配器重试 / 熔断策略（预留给真实接入）
# ---------------------------------------------------------------------------
@dataclass
class RetryPolicy:
    max_retries: int = 2
    timeout_s: float = 5.0
    backoff_s: float = 0.5
    circuit_breaker: bool = False
