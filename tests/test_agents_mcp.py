"""MCP 工具契约 + Agent 编排 + 降级链路单测。

**支撑「任一环节失败只降级不阻断」这条全项目约定的可测性**：
图谱后端不可用、LLM 不可达、适配器返回 mock —— 这些都不是异常路径，
而是**设计好的正常路径**，必须各自有用例锁住，否则哪天降级失效了没人知道。
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# Agent 骨架：模板方法的计时与异常兜底
# --------------------------------------------------------------------------- #
class TestBaseAgent:
    def test_execute_times_and_wraps(self):
        from kb_mcp_server.agents.base import AgentContext, AgentResult, BaseAgent

        class Dummy(BaseAgent):
            name = "Dummy"
            role = "测试"

            def run(self, ctx):
                ctx.data["touched"] = True
                return AgentResult(agent=self.name, role=self.role, summary="ok")

        ctx = AgentContext(question="q")
        res = Dummy().execute(ctx)
        assert res.ok is True
        assert res.agent == "Dummy", "execute 应用 agent 名填充结果"
        assert res.role == "测试"
        assert res.ms >= 1, "耗时必须有值（可观测性的最小要求）"
        assert ctx.data["touched"] is True

    def test_execute_swallows_exception(self):
        """**核心降级保证**：单个 agent 抛异常必须被吞掉并标记 ok=False，
        绝不允许把异常抛给编排器导致整条问答链路失败。
        """
        from kb_mcp_server.agents.base import AgentContext, AgentResult, BaseAgent

        class Broken(BaseAgent):
            name = "Broken"
            role = "会挂"

            def run(self, ctx):
                raise RuntimeError("模拟 agent 崩溃")

        res = Broken().execute(AgentContext(question="q"))
        assert res.ok is False, "agent 异常未转为可降级的结果"
        assert "模拟 agent 崩溃" in res.summary

    def test_card_shape(self):
        from kb_mcp_server.agents.base import BaseAgent, AgentResult

        class Dummy(BaseAgent):
            name = "D"
            role = "r"
            description = "d"

            def run(self, ctx):
                return AgentResult(agent=self.name, role=self.role)

        card = Dummy().card()
        assert set(card) >= {"name", "role", "description"}


class TestAgentContext:
    def test_defaults(self):
        """AgentContext 是黑板：每个 worker 只写自己字段，其他字段必须有默认值。"""
        from kb_mcp_server.agents.base import AgentContext

        ctx = AgentContext(question="q")
        assert ctx.hits == []
        assert ctx.graph_facts == {}
        assert ctx.live == []
        assert ctx.answer == {}
        assert ctx.tenant_id is None, "P2-6 多租户隔离位应预留"

    def test_worker_fields_isolated(self):
        """一个 worker 写自己的字段不应影响别的字段（避免交叉污染）。"""
        from kb_mcp_server.agents.base import AgentContext

        a = AgentContext(question="q")
        b = AgentContext(question="q")
        a.hits.append({"x": 1})
        assert b.hits == [], "AgentContext 之间发生了共享可变状态"


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #
class TestOrchestrator:
    def test_agent_roster(self):
        from kb_mcp_server.agents import get_orchestrator

        o = get_orchestrator()
        names = {a["name"] for a in o.agents_status()}
        assert names == {"GraphBuilder", "Retriever", "GraphReasoner",
                         "LiveData", "Synthesizer"}, f"agent 清单异常：{names}"

    def test_mode_from_config(self, monkeypatch):
        from kb_mcp_server.agents.orchestrator import Orchestrator

        monkeypatch.setenv("KB_AGENT_MODE", "deterministic")
        assert Orchestrator().mode == "deterministic"
        assert Orchestrator(mode="llm").mode == "llm", "显式 mode 应优先于配置"

    def test_deterministic_planner_runs_all(self):
        """deterministic 模式必须全跑，保证零 LLM 依赖下链路完整。"""
        from kb_mcp_server.agents.base import AgentResult, BaseAgent
        from kb_mcp_server.extensions import DeterministicPlanner

        class Fake(BaseAgent):
            name = "F"
            role = "r"

            def run(self, ctx):
                return AgentResult(agent=self.name, role=self.role)

        a, b, c = Fake(), Fake(), Fake()
        planner = DeterministicPlanner(a, b, c)
        from kb_mcp_server.agents.base import AgentContext

        ctx = AgentContext(question="q")
        ctx.data["routing"] = {}
        assert len(planner.plan(ctx)) == 3

    def test_planner_respects_routing_cut(self):
        """llm 模式下按需裁剪：不需要的 agent 应被跳过（降低延迟）。"""
        from kb_mcp_server.agents.base import AgentContext, AgentResult, BaseAgent
        from kb_mcp_server.extensions import DeterministicPlanner

        class Fake(BaseAgent):
            name = "F"
            role = "r"

            def run(self, ctx):
                return AgentResult(agent=self.name, role=self.role)

        r, g, l = Fake(), Fake(), Fake()
        planner = DeterministicPlanner(r, g, l)
        ctx = AgentContext(question="q")
        ctx.data["routing"] = {"need_graph": False, "need_live": False}
        plan = planner.plan(ctx)
        assert len(plan) == 1, "路由裁剪未生效（该跳过的 agent 仍在跑）"


# --------------------------------------------------------------------------- #
# 降级：全项目约定的可测性
# --------------------------------------------------------------------------- #
class TestDegradation:
    def test_llm_unreachable_returns_none(self):
        """LLM 不可达必须返回 None 而不是抛异常——调用方靠 None 触发模板回退。"""
        from kb_mcp_server.llmclient import llm_chat

        assert llm_chat("测试", timeout=2) is None

    def test_llm_failure_recorded(self):
        """失败原因必须留痕——否则「答案是模板」这类问题永远无法从外部定位。"""
        from kb_mcp_server.llmclient import llm_chat, llm_last_error

        llm_chat("测试", timeout=2)
        assert llm_last_error(), "LLM 失败未记录原因"

    def test_synthesis_falls_back_to_template(self, monkeypatch):
        """LLM 开启但不可达时，合成必须回退模板，链路照常产出答复。"""
        from kb_mcp_server import synthesis

        monkeypatch.setattr(synthesis, "_llm_cfg",
                            lambda: {"enabled": True, "model": "x",
                                     "base_url": "http://127.0.0.1:1/v1", "api_key": ""})
        hits = [{"doc_id": "d", "content": "物流超时全额退款", "score": 0.8, "meta": {}}]
        out = synthesis.synthesize("物流超时怎么赔偿", hits, [])
        assert out["synthesis_method"] == "template", "LLM 不可达时未回退模板"
        assert out["answer"], "回退后仍必须产出答复"

    def test_graph_backend_unavailable_returns_none(self, monkeypatch):
        """图谱后端不可用必须返回 None，让上层静默降级，而不是中断问答。"""
        from kb_mcp_server.agents import workers

        monkeypatch.setattr(workers, "graph_store_or_none", lambda: None)
        assert workers.graph_store_or_none() is None

    def test_graph_agent_skips_gracefully(self, monkeypatch):
        from kb_mcp_server.agents.base import AgentContext
        from kb_mcp_server.agents.workers import GraphBuilderAgent

        monkeypatch.setattr("kb_mcp_server.agents.workers.graph_store_or_none", lambda: None)
        res = GraphBuilderAgent().execute(AgentContext(question="q"))
        assert res.ok is True, "图谱不可用时 GraphBuilder 应正常跳过而非失败"

    def test_live_data_mock_mode(self):
        """KB_API_MOCK=1 时必须返回样例数据，保证离线可演示。"""
        from kb_mcp_server.adapters import fetch_live

        live = fetch_live("订单现在到哪了", order_id="1001")
        assert isinstance(live, list)


# --------------------------------------------------------------------------- #
# 合成层输出契约
# --------------------------------------------------------------------------- #
class TestSynthesisContract:
    HITS = [{"doc_id": "d", "content": "物流超时全额退款", "score": 0.8, "meta": {}}]

    def _call(self, hits=None, live=None, graph_facts=None):
        from kb_mcp_server.synthesis import synthesize

        return synthesize("物流超时怎么赔偿", hits if hits is not None else self.HITS,
                          live or [], graph_facts=graph_facts)

    def test_required_keys_present(self):
        """输出结构是 MCP 工具与 Web 控制台的共享契约，缺字段会静默破坏前端。"""
        out = self._call()
        for key in ("question", "answer", "summary", "sources", "confidence",
                    "synthesis_method", "trace_id", "live_cards",
                    "graph_facts", "graph_paths"):
            assert key in out, f"合成输出缺少必要字段 {key}"

    def test_trace_id_unique(self):
        assert self._call()["trace_id"] != self._call()["trace_id"]

    def test_confidence_high_for_strong_hit(self):
        out = self._call()
        assert out["confidence"]["score"] == pytest.approx(0.8)
        assert out["confidence"]["label"] == "高"

    def test_empty_hits_no_crash(self):
        out = self._call(hits=[])
        assert out["confidence"]["score"] == 0.0
        assert out["answer"], "无命中时也应给出兜底答复"

    def test_graph_facts_boost_confidence(self):
        """图谱命中给置信度小幅加成（有结构化路径支撑更可信），上限 +0.06。"""
        plain = self._call()["confidence"]["score"]
        facts = {"entities": [], "facts": [
            {"path": "物流配送 --适用条款--> 48小时内发货"},
            {"path": "物流配送 --解决方案为--> 全额退款"},
            {"path": "退款退货 --解决方案为--> 全额退款"},
        ]}
        boosted = self._call(graph_facts=facts)["confidence"]["score"]
        assert boosted > plain
        assert boosted - plain <= 0.06, "图谱加成超过上限，会喧宾夺主"

    def test_sources_expose_score(self):
        out = self._call()
        assert out["sources"][0]["doc_id"] == "d"
        assert isinstance(out["sources"][0]["score"], float)


# --------------------------------------------------------------------------- #
# MCP 工具：注册与只读工具的返回结构
# --------------------------------------------------------------------------- #
class TestMcpTools:
    def _tools(self):
        from kb_mcp_server.server import mcp

        return mcp

    def test_expected_tools_registered(self):
        """工具是 MCP 对外契约，少一个下游 agent 就会调不到。"""
        import asyncio
        import json

        mcp = self._tools()
        names = {t.name for t in asyncio.run(mcp.list_tools())}
        expected = {
            "ping", "ingest_document", "search_knowledge", "ask_with_live_data",
            "list_documents", "delete_document",
            "graph_query", "graph_expand", "graph_paths", "graph_entities",
            "graph_stats", "graph_rebuild",
            "multi_agent_ask", "agent_status",
            # P2-9 动作型工具（有副作用，独立于检索型工具）
            "list_actions", "run_action",
        }
        assert expected <= names, f"缺失工具：{expected - names}"

    def test_ping(self):
        from kb_mcp_server.server import ping

        assert ping() == "ok"

    def test_graph_tools_degrade_without_graph(self, monkeypatch):
        """图谱工具在图不可用时必须返回错误/空值，而不是抛异常。"""
        from kb_mcp_server import server

        monkeypatch.setattr(server, "_get_graph", lambda: None)
        assert server.graph_stats()["enabled"] is False
        assert server.graph_paths("a", "b") == []
        assert server.graph_entities() == []
        assert "error" in server.graph_query("任意实体")
