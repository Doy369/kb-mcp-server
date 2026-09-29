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
- P0-2 适配器重试 / 熔断     → RetryPolicy + CircuitBreaker（**已落地**，由 adapters 接入）
"""

from __future__ import annotations

import json
import os
import time
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


class ConfidenceGuardrail(Guardrail):
    """P1-5 置信度护栏：低于阈值的答复标记「需人工复核」，不静默放行低质量答案。

    阈值读运行时配置 KB_GUARDRAIL_MIN_CONFIDENCE；未配置时按嵌入后端给校准默认
    （bge 分数带整体高于 dev：无关问题 bge 也能到 ~0.47，dev 约 0.1）。
    设 0 = 关闭护栏（直通）。
    """

    def __init__(self, min_confidence: float | None = None):
        self._min = min_confidence

    def _threshold(self) -> float:
        if self._min is not None:
            return self._min
        try:
            from kb_mcp_server.config import get_cfg, get_settings

            v = (get_cfg("KB_GUARDRAIL_MIN_CONFIDENCE", "") or "").strip()
            if v:
                return float(v)
            return 0.5 if get_settings().embedding_backend == "bge" else 0.2
        except Exception:  # noqa: BLE001
            return 0.2

    def check(self, answer: dict, ctx: AgentContext | None = None) -> GateResult:
        th = self._threshold()
        if th <= 0:
            return GateResult(passed=True, action="pass")
        conf = answer.get("confidence") or {}
        score = float(conf.get("score") or 0.0)
        if score < th:
            return GateResult(
                passed=False,
                reason=f"置信度 {score:.3f} 低于阈值 {th:.2f}（档位：{conf.get('label', '?')}）",
                action="escalate",
            )
        return GateResult(passed=True, action="pass")


_DEFAULT_GUARDRAIL: Guardrail | None = None


def set_guardrail(g: Guardrail | None) -> None:
    global _DEFAULT_GUARDRAIL
    _DEFAULT_GUARDRAIL = g


def install_default_guardrail(g: Guardrail | None = None) -> Guardrail:
    """安装默认护栏（已安装则不动）。P1-5 落地后由 app / server 启动时调用。"""
    global _DEFAULT_GUARDRAIL
    if _DEFAULT_GUARDRAIL is None:
        _DEFAULT_GUARDRAIL = g or ConfidenceGuardrail()
    return _DEFAULT_GUARDRAIL


def apply_guardrail(answer: dict, ctx: AgentContext | None = None) -> dict:
    """合成后调用；默认无护栏（直通）。接入真实 Guardrail 后自动生效。"""
    g = _DEFAULT_GUARDRAIL
    if g is None:
        return answer
    res = g.check(answer, ctx)
    if res.passed:
        return answer
    answer = dict(answer)
    # 无论何种处置都记录判定结果，供 UI / 审计展示
    answer["guardrail"] = {"passed": False, "reason": res.reason, "action": res.action}
    if res.action in ("block", "escalate"):
        answer["answer"] = (answer.get("answer") or "") + "\n[需人工复核]"
    elif res.override:
        answer.update(res.override)
    return answer


# ---------------------------------------------------------------------------
# P1-5 评估 / 回归（golden 集 + 指标）
# ---------------------------------------------------------------------------
@dataclass
class GoldenCase:
    question: str
    expect_contains: list[str] = field(default_factory=list)
    expect_not_contains: list[str] = field(default_factory=list)   # 反向断言：防止召回错误分块
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
            expect_not_contains=it.get("expect_not_contains", []),
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
# P0-2 实时适配器韧性：重试 / 指数退避 / 熔断 / 降级
#
# 为什么需要：外部后端不可用时，早先的行为是**异常直接抛穿**整条问答链路；
# 或者被下游 try/except 吞掉，变成「实时数据静默消失」。两者都不可接受——
# 前者让一次网络抖动毁掉整次答复，后者让用户以为「本来就没有实时数据」。
# 现在的语义：有限重试 → 仍失败则**降级**返回带 degraded 标记的结构化结果，
# 调用方可展示「实时查询暂不可用」，主链路不崩。
# ---------------------------------------------------------------------------
CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CIRCUIT_HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """熔断开路：本次**未发起**下游调用（快速失败）。"""


@dataclass
class CircuitBreaker:
    """连续失败达阈值即开路；冷却后放行一次半开探测，成功则闭合。

    开路期间**不再发起网络调用**，避免下游已挂时把上游线程也拖死
    （实时 API 超时 5s，无熔断时每次问答都要白等重试）。
    计时用 monotonic，避免系统时钟回拨导致冷却永久失效。
    """

    threshold: int = 5
    cooldown_s: float = 30.0
    failures: int = 0
    opened_at: float | None = None

    def state(self, now: float | None = None) -> str:
        if self.opened_at is None:
            return CIRCUIT_CLOSED
        now = time.monotonic() if now is None else now
        if now - self.opened_at >= self.cooldown_s:
            return CIRCUIT_HALF_OPEN
        return CIRCUIT_OPEN

    def allow(self, now: float | None = None) -> bool:
        """是否允许发起调用（仅开路期拒绝）。"""
        return self.state(now) != CIRCUIT_OPEN

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.failures += 1
        if self.opened_at is not None:
            self.opened_at = now            # 半开探测又失败 → 重新计时冷却
        elif self.failures >= self.threshold:
            self.opened_at = now


@dataclass
class Attempt:
    """一次（含重试的）调用结果。ok=False 时 error 为最后一次异常。"""

    ok: bool
    value: Any = None
    error: BaseException | None = None
    attempts: int = 0
    short_circuited: bool = False          # True：因熔断开路而根本没发起调用

    @property
    def error_text(self) -> str:
        if self.error is None:
            return ""
        return f"{type(self.error).__name__}: {self.error}"


@dataclass
class RetryPolicy:
    """重试 + 指数退避 + 可选熔断；由 `adapters.APIAdapter` 持有。

    语义：`max_retries` 是**首次之外**的额外尝试次数，故最多调用 `max_retries + 1` 次。
    退避 `delay_for(n) = min(backoff_s * 2^(n-1), max_backoff_s)`（n 从 1 起），
    上限防止指数爆炸把一次问答拖成分钟级。
    `timeout_s` 是**单次尝试**的超时，由 HTTP 层实际执行（本类不自行计时）。
    """

    max_retries: int = 2
    timeout_s: float = 5.0
    backoff_s: float = 0.5
    circuit_breaker: bool = False
    cb_threshold: int = 5
    cb_cooldown_s: float = 30.0
    max_backoff_s: float = 8.0

    def __post_init__(self) -> None:
        self.breaker: CircuitBreaker | None = (
            CircuitBreaker(threshold=self.cb_threshold, cooldown_s=self.cb_cooldown_s)
            if self.circuit_breaker else None
        )

    def breaker_state(self) -> str:
        return self.breaker.state() if self.breaker is not None else CIRCUIT_CLOSED

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次重试前的等待秒数（attempt 从 1 开始）。"""
        if self.backoff_s <= 0 or attempt <= 0:
            return 0.0
        return min(self.backoff_s * (2 ** (attempt - 1)), self.max_backoff_s)

    def call(self, fn, *, sleep=None) -> Attempt:
        """执行 fn，按策略重试。**不抛异常**——失败信息放在 Attempt.error 里。

        sleep 可注入（测试用），默认 time.sleep。
        """
        sleep = sleep or time.sleep
        if self.breaker is not None and not self.breaker.allow():
            return Attempt(ok=False, error=CircuitOpenError("熔断开路，跳过调用"),
                           attempts=0, short_circuited=True)

        last: BaseException | None = None
        attempts = 0
        for i in range(self.max_retries + 1):
            attempts += 1
            try:
                value = fn()
            except Exception as e:  # noqa: BLE001 —— 任何异常都可重试，降级由调用方决定
                last = e
                if self.breaker is not None:
                    self.breaker.record_failure()
                if i >= self.max_retries:
                    break
                # 重试途中若已开路，立即停止（不再等待退避，也不再打下游）
                if self.breaker is not None and not self.breaker.allow():
                    return Attempt(ok=False, error=last, attempts=attempts, short_circuited=True)
                sleep(self.delay_for(i + 1))
            else:
                if self.breaker is not None:
                    self.breaker.record_success()
                return Attempt(ok=True, value=value, attempts=attempts)
        return Attempt(ok=False, error=last, attempts=attempts)

