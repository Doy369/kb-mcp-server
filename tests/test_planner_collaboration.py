"""P1-4 动态协作层单测：任务分解 / 证据协商 / 多轮编排 / 人工介入。

全部离线且零等待：
- LLM 用 stub 注入（`llm=lambda p: ...`），绝不真发网络；
- worker 全是替身 agent，不碰真实检索 / 图谱 / 适配器；
- 运行时配置用 monkeypatch 注入内存字典，不写用户 runtime_config.json。

核心不变量（这些比"跑通了"重要）：
1. 降级优先——LLM 任何异常路径都必须回退确定性规划，且**不能抛异常**；
2. 收敛——协商补轮只提「未执行过」的能力，同一 agent 绝不重复执行；
3. 默认等价——不开任何开关时，行为与旧版固定流水线一致。
"""

from __future__ import annotations

import pytest

from kb_mcp_server.agents.base import AgentContext, AgentResult, BaseAgent
from kb_mcp_server.extensions import (
    AgentRegistry,
    Critique,
    DeterministicPlanner,
    EvidenceCritic,
    LLMPlanner,
    PlanResult,
    Subtask,
)


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #
class _StubAgent(BaseAgent):
    """可观测行为的替身 agent：记录调用次数，按 sink 往黑板上写指定产出。"""

    def __init__(self, name, caps, sink=None, fail=False):
        self.name = name
        self.role = name
        self.description = f"{name}（替身）"
        self.capabilities = list(caps)
        self.calls = 0
        self._sink = sink
        self._fail = fail

    def run(self, ctx):
        self.calls += 1
        if self._fail:
            raise RuntimeError("替身故意失败")
        if self._sink:
            self._sink(ctx)
        return AgentResult(agent=self.name, role=self.role, summary=f"{self.name} done")


def _retriever(hits=None):
    payload = hits if hits is not None else [
        {"doc_id": "d1", "content": "退货政策：签收后 7 天内可申请。", "score": 0.9}]
    return _StubAgent("Retriever", ["retrieval", "semantic"],
                      sink=(lambda c: c.hits.extend(payload)) if payload else None)


def _graph_reasoner(facts=None):
    payload = facts if facts is not None else {
        "entities": [{"name": "退货政策"}], "facts": [{"path": "退货政策 → 适用 → 标准版"}]}
    def _sink(c):
        c.graph_facts = payload
    return _StubAgent("GraphReasoner", ["graph", "reasoning"],
                      sink=_sink if payload else None)


def _live(fail=False, cards=None):
    payload = cards if cards is not None else [
        {"type": "order", "order_id": "SO1", "status": "已签收"}]
    return _StubAgent("LiveData", ["live", "api"],
                      sink=(lambda c: c.live.extend(payload)) if payload else None,
                      fail=fail)


class _SynthStub(BaseAgent):
    """替身合成者：直接写 ctx.answer，可注入护栏判定以驱动 HITL 分支。"""

    name = "Synthesizer"
    role = "合成答复"
    capabilities = ["synthesis"]

    def __init__(self, guardrail=None, answer="这是合成后的答复（替身）"):
        self._g = guardrail
        self._answer = answer

    def run(self, ctx):
        ctx.answer = {
            "answer": self._answer,
            "summary": self._answer,
            "sources": [{"doc_id": "d1", "score": 0.9}],
            "live_cards": [], "live_data": [],
            "graph_facts": [], "graph_paths": [],
            "confidence": {"label": "高", "score": 0.8123},
            "synthesis_method": "template",
            "trace_id": "trace-fixed-1",
        }
        if self._g is not None:
            ctx.answer["guardrail"] = self._g
        return AgentResult(agent=self.name, role=self.role, summary="合成完成")


class _ListPlanner:
    """返回固定 agent 列表的规划器（鸭子类型，只需 plan/strategize）。"""

    def __init__(self, agents, source="deterministic", reason="test-plan", subtasks=None):
        self._agents = list(agents)
        self._source = source
        self._reason = reason
        self._subs = subtasks or []

    def plan(self, ctx):
        return list(self._agents)

    def strategize(self, ctx):
        return PlanResult(agents=list(self._agents), subtasks=list(self._subs),
                          source=self._source, reason=self._reason)


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg(monkeypatch):
    """隔离运行时配置：注入内存字典，测试结束自动还原，不写磁盘。"""
    from kb_mcp_server import config as cfgmod

    monkeypatch.setattr(cfgmod, "_runtime_cfg", {}, raising=False)

    def _set(**kv):
        for k, v in kv.items():
            cfgmod._runtime_cfg[k] = str(v)
        return cfgmod

    return _set


def _registry(*agents):
    return AgentRegistry(list(agents))


def _planner_with(reg, llm, **kw):
    r = reg.get("Retriever")
    g = reg.get("GraphReasoner")
    l = reg.get("LiveData")
    return LLMPlanner(r, g, l, registry=reg, llm=llm, **kw)


def _orch(planner, retriever=None, graph_reasoner=None, live_data=None,
          synthesizer=None, critic=None, mode="deterministic"):
    """构造 worker 全为替身的 Orchestrator（不碰真实后端）。"""
    from kb_mcp_server.agents.orchestrator import Orchestrator

    r = retriever or _retriever()
    g = graph_reasoner or _graph_reasoner()
    l = live_data or _live()
    o = Orchestrator(mode=mode, planner=planner)
    o.graph_builder = _StubAgent("GraphBuilder", ["graph", "ingest"])
    o.retriever, o.graph_reasoner, o.live_data = r, g, l
    o.synthesizer = synthesizer or _SynthStub()
    o.registry = _registry(o.graph_builder, r, g, l, o.synthesizer)
    if critic is not None:
        o.critic = critic
    return o


# --------------------------------------------------------------------------- #
# PlanResult
# --------------------------------------------------------------------------- #
class TestPlanResult:
    def test_to_dict_shape(self):
        a = _StubAgent("Retriever", ["retrieval"])
        res = PlanResult(agents=[a], subtasks=[Subtask("Retriever", "retrieval", "因为要检索")],
                         source="llm", reason="整体思路")
        d = res.to_dict()
        assert d["source"] == "llm"
        assert d["agents"] == ["Retriever"]
        assert d["subtasks"][0]["capability"] == "retrieval"
        assert d["subtasks"][0]["reason"] == "因为要检索"

    def test_subtask_depends_on_omitted_when_empty(self):
        assert "depends_on" not in Subtask("A", "x").to_dict()
        assert Subtask("A", "x", depends_on=["B"]).to_dict()["depends_on"] == ["B"]

    def test_defaults(self):
        res = PlanResult()
        assert res.agents == [] and res.source == "deterministic"
        assert res.to_dict()["agents"] == []


# --------------------------------------------------------------------------- #
# 确定性规划器的 strategize 默认实现
# --------------------------------------------------------------------------- #
class TestDeterministicStrategize:
    def test_wraps_plan_with_capabilities(self):
        r, g, l = _retriever(), _graph_reasoner(), _live()
        p = DeterministicPlanner(r, g, l)
        ctx = AgentContext(question="退货政策", mode="deterministic")
        ctx.data["routing"] = {}
        res = p.strategize(ctx)
        assert res.source == "deterministic"
        assert [a.name for a in res.agents] == ["Retriever", "GraphReasoner", "LiveData"]
        assert len(res.subtasks) == 3
        assert res.subtasks[0].capability == "retrieval", "子任务能力取自 agent 自描述"

    def test_respects_routing_cut(self):
        """向后兼容：routing 裁剪仍然生效（既有契约不能破）。"""
        r, g, l = _retriever(), _graph_reasoner(), _live()
        ctx = AgentContext(question="退货政策")
        ctx.data["routing"] = {"need_graph": False, "need_live": False}
        res = DeterministicPlanner(r, g, l).strategize(ctx)
        assert [a.name for a in res.agents] == ["Retriever"]


# --------------------------------------------------------------------------- #
# LLMPlanner：动态任务分解
# --------------------------------------------------------------------------- #
class TestLLMPlannerParsing:
    def _ctx(self, q="退货政策是什么"):
        return AgentContext(question=q, mode="llm")

    def test_plain_json(self):
        reg = _registry(_retriever(), _graph_reasoner(), _live())
        llm = lambda p: '{"subtasks":[{"agent":"Retriever","reason":"需要语义检索"}],"reason":"只需检索"}'
        res = _planner_with(reg, llm).strategize(self._ctx())
        assert res.source == "llm"
        assert [a.name for a in res.agents] == ["Retriever"]
        assert res.subtasks[0].reason == "需要语义检索"
        assert res.reason == "只需检索"

    def test_fenced_json(self):
        reg = _registry(_retriever(), _graph_reasoner(), _live())
        llm = lambda p: '```json\n{"subtasks":[{"agent":"LiveData","reason":"查物流"}]}\n```'
        res = _planner_with(reg, llm).strategize(self._ctx("物流到哪了"))
        assert res.source == "llm"
        assert [a.name for a in res.agents] == ["LiveData"]

    def test_json_with_surrounding_noise(self):
        """模型常加前后解释——必须能抠出 JSON，否则动态分解形同虚设。"""
        reg = _registry(_retriever(), _graph_reasoner(), _live())
        llm = lambda p: '好的，我的分析如下：\n{"subtasks":[{"agent":"Retriever"}]}\n以上。'
        res = _planner_with(reg, llm).strategize(self._ctx())
        assert res.source == "llm"
        assert [a.name for a in res.agents] == ["Retriever"]

    def test_multiple_agents_preserve_order_and_dedup(self):
        reg = _registry(_retriever(), _graph_reasoner(), _live())
        llm = lambda p: ('{"subtasks":[{"agent":"Retriever"},{"agent":"LiveData"},'
                         '{"agent":"Retriever"}]}')
        res = _planner_with(reg, llm).strategize(self._ctx())
        assert [a.name for a in res.agents] == ["Retriever", "LiveData"], "重复项应去重且保序"

    def test_unknown_agent_dropped(self):
        """幻觉 agent 绝不能进执行链。"""
        reg = _registry(_retriever(), _graph_reasoner(), _live())
        llm = lambda p: '{"subtasks":[{"agent":"HackerAgent"},{"agent":"Retriever"}]}'
        res = _planner_with(reg, llm).strategize(self._ctx())
        assert [a.name for a in res.agents] == ["Retriever"]

    def test_capability_alias_resolves(self):
        """允许用 capability 指代 agent（模型未必记得住名字）。"""
        reg = _registry(_retriever(), _graph_reasoner(), _live())
        llm = lambda p: '{"subtasks":[{"capability":"reasoning","reason":"需要多跳"}]}'
        res = _planner_with(reg, llm).strategize(self._ctx())
        assert res.source == "llm"
        assert [a.name for a in res.agents] == ["GraphReasoner"]

    def test_skeleton_agents_not_candidates(self):
        """骨架 agent（建图/合成）不参与动态选人，否则可能被重复或漏掉。"""
        reg = _registry(_StubAgent("GraphBuilder", ["graph", "ingest"]),
                        _retriever(), _graph_reasoner(), _live(), _SynthStub())
        llm = lambda p: '{"subtasks":[{"agent":"Synthesizer"},{"agent":"GraphBuilder"},{"agent":"Retriever"}]}'
        res = _planner_with(reg, llm).strategize(self._ctx())
        assert [a.name for a in res.agents] == ["Retriever"]


class TestLLMPlannerFallback:
    """降级是硬要求：LLM 的每种失败都必须平滑回退，且不抛异常。"""

    def _ctx(self):
        return AgentContext(question="退货政策", mode="llm")

    def _expect_fallback(self, llm, reg=None):
        reg = reg or _registry(_retriever(), _graph_reasoner(), _live())
        res = _planner_with(reg, llm).strategize(self._ctx())
        assert res.source == "fallback", f"未回退：source={res.source}"
        assert res.agents, "回退后必须有可执行的 agent"
        assert res.reason, "回退必须带原因，否则线上无从定位"
        return res

    def test_llm_unreachable(self):
        res = self._expect_fallback(lambda p: None)
        assert [a.name for a in res.agents] == ["Retriever", "GraphReasoner", "LiveData"]

    def test_llm_raises(self):
        """LLM 抛异常也不能拖垮问答链路——规划器必须吞掉并回退。"""
        def _boom(p):
            raise RuntimeError("网络炸了")
        res = self._expect_fallback(_boom)
        assert "异常" in res.reason, "回退原因应说明是 LLM 异常"

    def test_garbage_output(self):
        self._expect_fallback(lambda p: "我不太确定，你再说一遍？")

    def test_json_but_no_valid_agent(self):
        self._expect_fallback(lambda p: '{"subtasks":[{"agent":"不存在"}]}')

    def test_empty_subtasks(self):
        self._expect_fallback(lambda p: '{"subtasks":[],"reason":"无需任何能力"}')

    def test_no_candidates(self):
        """注册表为空（或只有骨架）时也必须回退，而不是返回空计划。"""
        reg = _registry(_SynthStub(), _StubAgent("GraphBuilder", ["graph", "ingest"]))
        res = _planner_with(reg, lambda p: '{"subtasks":[{"agent":"Retriever"}]}').strategize(self._ctx())
        assert res.source == "fallback"

    def test_raw_preserved_for_audit(self):
        bad = "完全不是 JSON"
        res = _planner_with(_registry(_retriever()), lambda p: bad).strategize(self._ctx())
        assert res.raw == bad, "非法输出要留痕，否则线上无法解释为何没分解"


class TestLLMPlannerContract:
    def test_plan_matches_strategize_agents(self):
        reg = _registry(_retriever(), _graph_reasoner(), _live())
        p = _planner_with(reg, lambda x: '{"subtasks":[{"agent":"LiveData"}]}')
        ctx = AgentContext(question="订单在哪")
        assert [a.name for a in p.plan(ctx)] == ["LiveData"]

    def test_prompt_carries_roster_and_question(self):
        reg = _registry(_retriever(), _graph_reasoner(), _live())
        seen = {}

        def _llm(prompt):
            seen["p"] = prompt
            return '{"subtasks":[{"agent":"Retriever"}]}'

        _planner_with(reg, _llm).strategize(AgentContext(question="我的发票怎么开"))
        assert "我的发票怎么开" in seen["p"]
        assert "Retriever" in seen["p"] and "semantic" in seen["p"], "必须把能力清单交给模型"


# --------------------------------------------------------------------------- #
# EvidenceCritic：协商判定
# --------------------------------------------------------------------------- #
class TestEvidenceCritic:
    def _ctx(self, q="退货政策是什么", order_id=None, sku=None):
        return AgentContext(question=q, order_id=order_id, sku=sku)

    def test_missing_retrieval(self):
        c = EvidenceCritic().review(self._ctx(), done_caps=set())
        assert c.need_more and c.missing == ["retrieval"]

    def test_no_repeat_when_already_done(self):
        """核心收敛保证：已执行过的能力不再提，否则补轮永不结束。"""
        c = EvidenceCritic().review(self._ctx(), done_caps={"retrieval", "semantic"})
        assert not c.need_more, f"重复索要已执行能力：{c.to_dict()}"

    def test_live_needed_for_order_hint(self):
        c = EvidenceCritic().review(self._ctx("我的订单到哪了"), done_caps={"retrieval"})
        assert "live" in c.missing

    def test_live_needed_for_order_id(self):
        c = EvidenceCritic().review(self._ctx("查一下", order_id="SO123"),
                                    done_caps={"retrieval"})
        assert "live" in c.missing

    def test_no_live_for_plain_kb_question(self):
        c = EvidenceCritic().review(self._ctx("退货政策是什么"), done_caps={"retrieval"})
        assert "live" not in c.missing

    def test_graph_needed_for_relation_hint(self):
        c = EvidenceCritic().review(self._ctx("这条条款适用于哪个版本"),
                                    done_caps={"retrieval"})
        assert "graph" in c.missing

    def test_no_graph_when_facts_present(self):
        ctx = self._ctx("这条条款适用于哪个版本")
        ctx.graph_facts = {"facts": [{"path": "A → B"}]}
        c = EvidenceCritic().review(ctx, done_caps={"retrieval", "graph"})
        assert not c.need_more

    def test_all_covered(self):
        ctx = self._ctx("退货政策是什么")
        ctx.hits = [{"doc_id": "d", "content": "x", "score": 0.9}]
        c = EvidenceCritic().review(ctx, done_caps={"retrieval", "semantic"})
        assert not c.need_more and c.missing == [] and c.reason

    def test_to_dict_shape(self):
        d = Critique(need_more=True, missing=["live"], reason="缺实时").to_dict()
        assert d == {"need_more": True, "missing": ["live"], "reason": "缺实时",
                     "source": "deterministic"}


# --------------------------------------------------------------------------- #
# Orchestrator：单轮默认 / 多轮协商 / HITL
# --------------------------------------------------------------------------- #
class TestOrchestratorSingleRound:
    def test_default_is_one_round(self):
        r, g, l = _retriever(), _graph_reasoner(), _live()
        o = _orch(_ListPlanner([r, g, l]), r, g, l)
        out = o.ask("退货政策是什么")
        coll = out["collaboration"]
        assert len(coll["rounds"]) == 1
        assert coll["negotiated"] is False
        assert coll["rounds"][0]["agents"] == ["Retriever", "GraphReasoner", "LiveData"]
        assert "critique" not in coll["rounds"][0], "单轮不应留下协商痕迹"

    def test_agent_trace_contract_preserved(self):
        """旧契约不能破：agents.trace / routing / total_ms / failed 仍在。"""
        r, g, l = _retriever(), _graph_reasoner(), _live()
        o = _orch(_ListPlanner([r, g, l]), r, g, l)
        out = o.ask("退货政策")
        ag = out["agents"]
        assert set(["mode", "routing", "trace", "total_ms", "failed"]) <= set(ag)
        names = [t["agent"] for t in ag["trace"]]
        assert names[0] == "GraphBuilder" and names[-1] == "Synthesizer"
        assert ag["failed"] == []

    def test_planner_source_recorded(self):
        r = _retriever()
        o = _orch(_ListPlanner([r], source="llm", reason="只查知识"), r)
        out = o.ask("退货政策")
        assert out["collaboration"]["planner"] == "llm"
        assert out["collaboration"]["rounds"][0]["reason"] == "只查知识"
        assert out["collaboration"]["executed"] == ["Retriever"]


class TestOrchestratorNegotiation:
    def test_negotiation_pulls_missing_agent(self, cfg):
        """规划漏了检索 → critic 发现 hits 为空 → 补轮把 Retriever 叫回来。"""
        cfg(KB_AGENT_MAX_ROUNDS=2)
        r, g = _retriever(), _graph_reasoner(facts=None)
        o = _orch(_ListPlanner([g]), r, g, _live(), critic=EvidenceCritic())
        out = o.ask("退货政策是什么")
        coll = out["collaboration"]
        assert len(coll["rounds"]) == 2, f"未发生补轮：{coll}"
        assert coll["rounds"][0]["critique"]["missing"] == ["retrieval"]
        assert coll["rounds"][1]["agents"] == ["Retriever"]
        assert coll["rounds"][1]["source"] == "negotiation"
        assert coll["negotiated"] is True
        assert r.calls == 1, "补轮应恰好执行一次，不多不少"

    def test_converges_without_duplicate_execution(self, cfg):
        """所有能力都试过仍无证据 → 必须停，不能无限补轮。"""
        cfg(KB_AGENT_MAX_ROUNDS=5)
        r, g = _retriever(hits=[]), _graph_reasoner(facts=None)
        o = _orch(_ListPlanner([g]), r, g, _live(), critic=EvidenceCritic())
        out = o.ask("退货政策是什么")
        coll = out["collaboration"]
        executed = [t["agent"] for t in out["agents"]["trace"]
                    if t["agent"] not in ("GraphBuilder", "Synthesizer")]
        assert len(executed) == len(set(executed)), f"同一 agent 被执行多次：{executed}"
        assert len(coll["rounds"]) <= 2, "证据无望时应尽早收束"
        assert r.calls <= 1 and g.calls <= 1

    def test_rounds_capped_by_config(self, cfg):
        cfg(KB_AGENT_MAX_ROUNDS=2)
        r, g = _retriever(hits=[]), _graph_reasoner(facts=None)
        o = _orch(_ListPlanner([g]), r, g, _live(), critic=EvidenceCritic())
        out = o.ask("订单物流到哪了？")
        assert len(out["collaboration"]["rounds"]) <= 2

    def test_failed_agent_does_not_break_chain(self):
        """worker 崩了也要出结果（降级不阻断）——failed 记录而非抛异常。"""
        r, g = _retriever(), _StubAgent("GraphReasoner", ["graph", "reasoning"], fail=True)
        o = _orch(_ListPlanner([r, g]), r, g, _live())
        out = o.ask("退货政策")
        assert out["agents"]["failed"] == ["GraphReasoner"]
        assert out["answer"]


class TestOrchestratorConfigTolerance:
    def test_bad_max_rounds_falls_back_to_one(self, cfg):
        """配置写坏不能让问答挂掉（全项目降级约定）。"""
        cfg(KB_AGENT_MAX_ROUNDS="abc")
        r, g, l = _retriever(), _graph_reasoner(), _live()
        o = _orch(_ListPlanner([r, g, l]), r, g, l)
        out = o.ask("退货政策")
        assert len(out["collaboration"]["rounds"]) == 1

    def test_zero_or_negative_rounds_clamped(self, cfg):
        cfg(KB_AGENT_MAX_ROUNDS="0")
        r = _retriever()
        o = _orch(_ListPlanner([r]), r)
        assert len(o.ask("退货政策")["collaboration"]["rounds"]) == 1


class TestHumanInTheLoop:
    def test_disabled_by_default(self):
        r = _retriever()
        o = _orch(_ListPlanner([r]), r,
                  synthesizer=_SynthStub(guardrail={"passed": False, "reason": "低置信"}))
        out = o.ask("退货政策")
        assert "pending_human" not in out, "默认关闭时不得引入新字段"
        assert "human_review" not in out

    def test_enabled_marks_pending(self, cfg):
        cfg(KB_AGENT_HITL=1)
        r = _retriever()
        o = _orch(_ListPlanner([r]), r, synthesizer=_SynthStub(
            guardrail={"passed": False, "reason": "置信度 0.31 低于阈值"}))
        out = o.ask("退货政策")
        assert out["pending_human"] is True
        assert out["human_review"]["status"] == "pending"
        assert "0.31" in out["human_review"]["reason"]

    def test_enabled_auto_approves_clean_answer(self, cfg):
        cfg(KB_AGENT_HITL=1)
        r = _retriever()
        o = _orch(_ListPlanner([r]), r, synthesizer=_SynthStub(guardrail=None))
        out = o.ask("退货政策")
        assert out["pending_human"] is False
        assert out["human_review"] == {"required": False, "status": "auto_approved",
                                      "reason": ""}


# --------------------------------------------------------------------------- #
# 规划器选择 / 能力清单
# --------------------------------------------------------------------------- #
class TestPlannerSelection:
    def test_explicit_deterministic(self, cfg):
        from kb_mcp_server.agents.orchestrator import Orchestrator
        cfg(KB_AGENT_PLANNER="deterministic", KB_AGENT_MODE="llm")
        assert isinstance(Orchestrator(mode="llm").planner, DeterministicPlanner)

    def test_explicit_llm(self, cfg):
        from kb_mcp_server.agents.orchestrator import Orchestrator
        cfg(KB_AGENT_PLANNER="llm", KB_AGENT_MODE="deterministic")
        assert isinstance(Orchestrator(mode="deterministic").planner, LLMPlanner)

    def test_follows_llm_mode_when_unset(self, cfg):
        from kb_mcp_server.agents.orchestrator import Orchestrator
        cfg(KB_AGENT_PLANNER="", KB_AGENT_MODE="llm")
        assert isinstance(Orchestrator(mode="llm").planner, LLMPlanner)
        cfg(KB_AGENT_PLANNER="", KB_AGENT_MODE="deterministic")
        assert isinstance(Orchestrator(mode="deterministic").planner, DeterministicPlanner)

    def test_roster_exposes_capabilities(self):
        from kb_mcp_server.agents.orchestrator import Orchestrator
        roster = Orchestrator(mode="deterministic").roster()
        by = {r["name"]: r for r in roster}
        assert set(by) == {"GraphBuilder", "Retriever", "GraphReasoner",
                           "LiveData", "Synthesizer"}
        assert by["Retriever"]["capabilities"] == ["retrieval", "semantic"]
        assert by["LiveData"]["capabilities"] == ["live", "api"]

    def test_registry_by_capability(self):
        reg = _registry(_retriever(), _graph_reasoner(), _live())
        assert [a.name for a in reg.by_capability("live")] == ["LiveData"]
        assert reg.by_capability("不存在的能力") == []

    def test_registry_prebuilt(self):
        r = _retriever()
        reg = AgentRegistry([r])
        assert reg.get("Retriever") is r and reg.all() == [r]
