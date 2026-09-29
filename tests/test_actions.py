"""P2-9 动作型工具层单测：参数契约 / 确认门 / 审计 / 意图识别 / 接入编排。

动作型工具与检索型工具的本质区别是**有副作用**，所以这里锁的不是「功能对不对」，
而是三条安全底线——它们是这类能力能否上生产的分界线：

1. **参数契约**：缺必填参数必须被拒，绝不把半成品请求发给下游；
2. **确认门**：destructive 动作（退款等）未确认**绝不执行**，只回待确认。
   agent 自己确认自己 = 没有确认门，因此 ActionAgent 的 `confirmed` 恒为 False；
3. **审计落盘**：每次尝试（含被拒 / 待确认）都留痕——这是「动作真的发生过」
   唯一可被断言的证据（与 P0-3「没报错 ≠ 副作用发生」同一条原则）。

另有一条与 P1-4 衔接的不变量：**待确认的不可逆动作必须并入人工队列**，
否则「agent 发起了退款」会静默通过。

全部离线：不碰网络、不写真实数据目录（审计路径逐用例隔离）。
"""

from __future__ import annotations

import json
import os

import pytest

from kb_mcp_server.actions import (
    ActionRunner,
    CreateTicketTool,
    ListTicketsTool,
    RequestRefundTool,
    UpdateOrderTool,
    action_tools,
    build_registry,
    detect_intent,
    extract_params,
    read_action_log,
)
from kb_mcp_server.extensions import (
    RISK_DESTRUCTIVE,
    RISK_READ,
    RISK_WRITE,
    ActionTool,
    ActionToolRegistry,
)


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    """独立的动作审计文件：避免用例间互相污染，也不写项目根。"""
    p = str(tmp_path / "kb_actions.jsonl")
    monkeypatch.setenv("KB_ACTION_LOG", p)
    return p


@pytest.fixture
def cfg(monkeypatch):
    """隔离运行时配置：注入内存字典，结束自动还原，不写磁盘。"""
    from kb_mcp_server import config as cfgmod

    monkeypatch.setattr(cfgmod, "_runtime_cfg", {}, raising=False)

    def _set(**kv):
        for k, v in kv.items():
            cfgmod._runtime_cfg[k] = str(v)
        return cfgmod

    return _set


# --------------------------------------------------------------------------- #
# 1) 接口层：风险分级 / 参数契约 / 注册表
# --------------------------------------------------------------------------- #
class TestActionToolContract:
    def test_default_risks(self):
        assert CreateTicketTool().risk == RISK_WRITE
        assert UpdateOrderTool().risk == RISK_WRITE
        assert RequestRefundTool().risk == RISK_DESTRUCTIVE
        assert ListTicketsTool().risk == RISK_READ

    def test_requires_confirm_derives_from_risk(self):
        """默认推导：只有 destructive 需要确认——这正是确认门的入口条件。"""
        assert RequestRefundTool().requires_confirm is True
        assert RequestRefundTool().destructive is True
        assert CreateTicketTool().requires_confirm is False
        assert ListTicketsTool().requires_confirm is False

    def test_requires_confirm_can_be_overridden(self):
        """显式覆盖优先于风险推导（新动作可能「危险但可自动执行」）。"""

        class Custom(ActionTool):
            name = "custom"
            risk = RISK_DESTRUCTIVE
            _requires_confirm = False

            def execute(self, params):
                return {}

        assert Custom().requires_confirm is False
        assert Custom().destructive is True

    def test_required_params_declared(self):
        assert RequestRefundTool().required_params() == ["order_id", "amount"]
        assert CreateTicketTool().required_params() == ["subject"]
        assert ListTicketsTool().required_params() == []
        assert UpdateOrderTool().required_params() == ["order_id", "field", "value"]

    def test_schema_shape(self):
        s = RequestRefundTool().schema()
        assert {"name", "description", "risk", "requires_confirm", "capabilities",
                "params"} <= set(s)
        assert s["risk"] == RISK_DESTRUCTIVE and s["requires_confirm"] is True

    def test_default_registry_has_four_actions(self):
        assert set(action_tools().names()) == {"create_ticket", "update_order",
                                               "request_refund", "list_tickets"}
        assert len(build_registry().list()) == 4

    def test_registry_by_capability(self):
        reg = build_registry()
        assert {t.name for t in reg.by_capability("order")} == {"update_order",
                                                               "request_refund"}
        assert reg.by_capability("不存在的能力") == []

    def test_registry_prebuilt_and_lookup(self):
        t = ListTicketsTool()
        reg = ActionToolRegistry([t])
        assert reg.get("list_tickets") is t and reg.all() == [t]
        assert reg.get("nope") is None


# --------------------------------------------------------------------------- #
# 2) 执行器：参数契约 + 确认门 + 审计
# --------------------------------------------------------------------------- #
class TestActionRunner:
    def test_unknown_action_rejected(self, log_path):
        out = ActionRunner().run("nope", {})
        assert out["status"] == "rejected"
        assert out["ok"] is False
        assert "create_ticket" in out["available"], "被拒时应回带可用清单"

    def test_missing_required_params_rejected(self, log_path):
        out = ActionRunner().run("create_ticket", {"priority": "P0"})
        assert out["status"] == "rejected"
        assert out["missing"] == ["subject"]
        assert "subject" in out["error"]

    def test_destructive_without_confirm_not_executed(self, log_path):
        """**P2-9 的安全底线**：未确认的退款绝不能产生业务结果。"""
        out = ActionRunner().run("request_refund",
                                 {"order_id": "SO1", "amount": 10})
        assert out["status"] == "needs_confirmation"
        assert out["ok"] is False
        assert out["confirmed"] is False
        assert "result" not in out, "待确认的动作竟然产生了业务结果"
        assert out["preview"]["given"]["order_id"] == "SO1"
        assert "需人工确认" in out["message"]

    def test_destructive_after_confirm_executed(self, log_path):
        out = ActionRunner().run("request_refund",
                                 {"order_id": "SO1", "amount": 10},
                                 confirmed=True)
        assert out["status"] == "executed" and out["ok"] is True
        assert out["result"]["refund_id"].startswith("RF-")
        assert out["result"]["amount"] == 10.0

    def test_write_action_auto_executes(self, log_path):
        out = ActionRunner().run("create_ticket", {"subject": "故障", "priority": "P0"})
        assert out["status"] == "executed"
        assert out["result"]["sla_response"] == "15 分钟", "动作与 SLA 知识应联动"

    def test_read_action_auto_executes(self, log_path):
        out = ActionRunner().run("list_tickets", {})
        assert out["status"] == "executed" and out["risk"] == RISK_READ

    def test_invalid_enum_value_rejected(self, log_path):
        """业务校验失败必须转成结构化拒绝，而不是抛穿调用方。"""
        out = ActionRunner().run("create_ticket", {"subject": "x", "priority": "P9"})
        assert out["status"] == "rejected"
        assert "P0/P1/P2" in out["error"]

    def test_invalid_amount_rejected(self, log_path):
        out = ActionRunner().run("request_refund",
                                 {"order_id": "SO1", "amount": 0}, confirmed=True)
        assert out["status"] == "rejected"
        assert "大于 0" in out["error"]

    def test_audit_records_every_attempt(self, log_path):
        """审计必须覆盖「被拒 / 待确认 / 已执行」三种终态——缺一种就查不清事故。"""
        r = ActionRunner()
        r.run("nope", {})
        r.run("request_refund", {"order_id": "SO1", "amount": 5})
        r.run("request_refund", {"order_id": "SO1", "amount": 5}, confirmed=True)
        r.run("create_ticket", {"subject": "s"})
        logs = read_action_log(20)
        assert [x["status"] for x in reversed(logs)] == [
            "rejected", "needs_confirmation", "executed", "executed"]
        assert all(x.get("action_id", "").startswith("ACT-") for x in logs)

    def test_audit_written_to_configured_path(self, log_path):
        ActionRunner().run("list_tickets", {})
        assert os.path.exists(log_path), "审计未落到配置的路径"
        with open(log_path, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        assert rec["kind"] == "action" and rec["action"] == "list_tickets"

    def test_audit_can_be_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KB_ACTION_LOG", "off")
        out = ActionRunner().run("create_ticket", {"subject": "s"})
        assert out["status"] == "executed", "关掉审计不能影响动作本身"
        assert read_action_log(10) == []

    def test_actions_are_readable_after_run(self, log_path):
        """list_tickets 能回读到刚建的工单——证明副作用真的落盘了，而不是只返回了个 id。"""
        ActionRunner().run("create_ticket", {"subject": "故障", "priority": "P1"})
        out = ActionRunner().run("list_tickets", {})
        assert out["result"]["count"] == 1
        assert out["result"]["tickets"][0]["priority"] == "P1"

    def test_actor_recorded(self, log_path):
        ActionRunner(actor="unit-test").run("list_tickets", {})
        assert read_action_log(1)[0]["actor"] == "unit-test"

    def test_read_action_log_handles_missing_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KB_ACTION_LOG", str(tmp_path / "nope.jsonl"))
        assert read_action_log(5) == []


# --------------------------------------------------------------------------- #
# 3) 意图识别与参数抽取（规则优先：可解释、离线可测、不误触发写操作）
# --------------------------------------------------------------------------- #
class TestIntent:
    @pytest.mark.parametrize("q,expect", [
        ("帮我退款 88 元", "request_refund"),
        ("订单要退款", "request_refund"),
        ("帮我改地址", "update_order"),
        ("这个订单要改期送达", "update_order"),
        ("帮我建工单", "create_ticket"),
        ("我要投诉", "create_ticket"),
        ("物流超时怎么赔偿", None),
        ("", None),
    ])
    def test_detect_intent(self, q, expect):
        assert detect_intent(q) == expect

    def test_refund_wins_over_ticket(self):
        """优先级：一句话里既有「投诉」又有「退款」时必须判成退款（金额相关更危险）。"""
        assert detect_intent("我要投诉并申请退款") == "request_refund"

    def test_extract_params_refund(self):
        p = extract_params("request_refund", "订单 SO123 退款 88 元")
        assert p["order_id"] == "SO123" and p["amount"] == 88.0

    def test_extract_params_prefers_explicit_order_id(self):
        p = extract_params("request_refund", "帮我退款 50 元", order_id="SO999")
        assert p["order_id"] == "SO999"

    def test_extract_params_missing_order_id_yields_empty(self):
        """抽不全就留空——由 Runner 的必填校验拒绝，比「猜一个订单号去退款」安全得多。"""
        p = extract_params("request_refund", "帮我退款 50 元")
        assert p["order_id"] == ""
        out = ActionRunner().run("request_refund", p)
        assert out["status"] == "rejected" and out["missing"] == ["order_id"]

    def test_extract_params_ticket_priority(self):
        assert extract_params("create_ticket", "全站不可用，紧急")["priority"] == "P0"
        assert extract_params("create_ticket", "系统很慢")["priority"] == "P1"
        assert extract_params("create_ticket", "有个小问题")["priority"] == "P2"

    def test_extract_params_update_order_field(self):
        assert extract_params("update_order", "帮我改地址")["field"] == "address"
        assert extract_params("update_order", "帮我改送达日期")["field"] == "delivery_date"


# --------------------------------------------------------------------------- #
# 4) ActionAgent：接入黑板的执行者（三道闸）
# --------------------------------------------------------------------------- #
class TestActionAgent:
    def _agent(self):
        from kb_mcp_server.agents.workers import ActionAgent

        return ActionAgent()

    def test_gate_declared(self):
        a = self._agent()
        assert a.gated_by == "KB_AGENT_ACTIONS", "有副作用的 agent 必须挂参与闸门"
        assert "action" in a.capabilities

    def test_no_intent_touches_nothing(self, log_path):
        from kb_mcp_server.agents.base import AgentContext

        ctx = AgentContext(question="物流超时怎么赔偿")
        res = self._agent().execute(ctx)
        assert res.ok is True and ctx.actions == []
        assert "无动作意图" in res.summary

    def test_destructive_intent_never_self_confirms(self, log_path):
        from kb_mcp_server.agents.base import AgentContext

        ctx = AgentContext(question="帮我退款 88 元，订单 SO123")
        self._agent().execute(ctx)
        assert len(ctx.actions) == 1
        a = ctx.actions[0]
        assert a["status"] == "needs_confirmation"
        assert a["confirmed"] is False, "agent 不能自己确认自己的不可逆动作"

    def test_write_intent_executes(self, log_path):
        from kb_mcp_server.agents.base import AgentContext

        ctx = AgentContext(question="帮我建工单：紧急故障")
        res = self._agent().execute(ctx)
        assert ctx.actions[0]["status"] == "executed"
        assert "已执行动作" in res.summary

    def test_rejected_action_is_not_agent_failure(self, log_path):
        """被拒 / 待确认不算 agent 失败——agent 的职责（识别+发起）已正确完成。"""
        from kb_mcp_server.agents.base import AgentContext

        ctx = AgentContext(question="帮我退款 88 元")   # 无订单号 → 必填缺失
        res = self._agent().execute(ctx)
        assert res.ok is True
        assert ctx.actions[0]["status"] == "rejected"


# --------------------------------------------------------------------------- #
# 5) 编排接入：开关 / 轨迹 / 与 HITL 闭环
# --------------------------------------------------------------------------- #
class TestOrchestratorActions:
    def _ask(self, monkeypatch, question, **env):
        from kb_mcp_server.agents.orchestrator import Orchestrator

        # 显式钉住全部相关开关，避免受宿主环境 / 用例顺序影响
        base = {
            "KB_AGENT_BUILD_GRAPH": "0",
            "KB_GRAPH_ENABLED": "0",          # 无需图谱，保持离线轻量
            "KB_AGENT_MODE": "deterministic",
            "KB_AGENT_PLANNER": "deterministic",
            "KB_AGENT_MAX_ROUNDS": "1",
            "KB_AGENT_ACTIONS": "0",
            "KB_AGENT_HITL": "0",
            "KB_ACTION_LOG": "off",
        }
        base.update({k: str(v) for k, v in env.items()})
        for k, v in base.items():
            monkeypatch.setenv(k, v)
        return Orchestrator(mode="deterministic").ask(question)

    def test_gate_off_by_default(self, monkeypatch):
        """默认关闭：不新增 agent、不产生任何动作，行为与旧版完全一致。"""
        out = self._ask(monkeypatch, "帮我退款 88 元，订单 SO123")
        names = [t["agent"] for t in out["agents"]["trace"]]
        assert "ActionAgent" not in names
        assert out["actions"] == [] and out["pending_actions"] == []
        assert "pending_human" not in out

    def test_gate_on_adds_action_agent(self, monkeypatch, tmp_path):
        out = self._ask(monkeypatch, "帮我退款 88 元，订单 SO123",
                        KB_AGENT_ACTIONS="1",
                        KB_ACTION_LOG=str(tmp_path / "a.jsonl"))
        names = [t["agent"] for t in out["agents"]["trace"]]
        assert "ActionAgent" in names
        assert "ActionAgent" in out["collaboration"]["executed"]
        assert [a["status"] for a in out["actions"]] == ["needs_confirmation"]
        assert len(out["pending_actions"]) == 1

    def test_gate_on_without_intent_is_noop(self, monkeypatch, tmp_path):
        out = self._ask(monkeypatch, "物流超时怎么赔偿",
                        KB_AGENT_ACTIONS="1",
                        KB_ACTION_LOG=str(tmp_path / "a.jsonl"))
        assert out["actions"] == []

    def test_pending_action_joins_human_queue(self, monkeypatch, tmp_path):
        """P1-4 × P2-9 的闭环：待确认的不可逆动作必须进人工队列。"""
        out = self._ask(monkeypatch, "帮我退款 88 元，订单 SO123",
                        KB_AGENT_ACTIONS="1", KB_AGENT_HITL="1",
                        KB_ACTION_LOG=str(tmp_path / "a.jsonl"))
        assert out["pending_human"] is True
        assert out["human_review"]["required"] is True
        assert "request_refund" in out["human_review"]["reason"]

    def test_actions_roster_exposes_tools_and_gate(self, monkeypatch):
        from kb_mcp_server.agents.orchestrator import Orchestrator

        monkeypatch.setenv("KB_AGENT_ACTIONS", "0")
        o = Orchestrator(mode="deterministic")
        r = o.actions_roster()
        assert r["enabled"] is False
        assert r["agent"]["gated_by"] == "KB_AGENT_ACTIONS"
        assert {t["name"] for t in r["tools"]} == {"create_ticket", "update_order",
                                                  "request_refund", "list_tickets"}
        assert isinstance(r["recent"], list)

    def test_roster_and_agents_status_unchanged(self, monkeypatch, tmp_path):
        """对外契约不变：动作执行者不混进 agent 清单（它由开关控制、生命周期不同）。"""
        from kb_mcp_server.agents.orchestrator import Orchestrator

        monkeypatch.setenv("KB_AGENT_ACTIONS", "1")
        o = Orchestrator(mode="deterministic")
        assert len(o.agents_status()) == 5
        assert {a["name"] for a in o.roster()} == {
            "GraphBuilder", "Retriever", "GraphReasoner", "LiveData", "Synthesizer"}


# --------------------------------------------------------------------------- #
# 6) 合成渲染：三种终态必须能一眼区分（对未知动作类型也安全）
# --------------------------------------------------------------------------- #
class TestSynthesisActions:
    def _render(self, actions):
        from kb_mcp_server.synthesis import synthesize

        return synthesize("问题", [], [], actions=actions)

    def test_actions_key_always_present(self):
        assert self._render([])["actions"] == []

    def test_executed_action_rendered(self):
        out = self._render([{
            "action": "create_ticket", "status": "executed", "risk": RISK_WRITE,
            "result": {"ticket_id": "TKT-1", "priority": "P0",
                       "sla_response": "15 分钟", "subject": "故障"}}])
        assert "【执行动作】" in out["answer"]
        assert "TKT-1" in out["answer"] and "15 分钟" in out["answer"]

    def test_needs_confirmation_rendered_with_hint(self):
        out = self._render([{
            "action": "request_refund", "status": "needs_confirmation",
            "risk": RISK_DESTRUCTIVE,
            "preview": {"given": {"order_id": "SO1", "amount": 88}}}])
        assert "待确认" in out["answer"]
        assert "SO1" in out["answer"] and "88" in out["answer"]
        assert "需人工确认" in out["answer"]
        assert out["summary"], "无知识命中时摘要应回落到动作结果，而不是空摘要"

    def test_rejected_action_rendered(self):
        out = self._render([{"action": "create_ticket", "status": "rejected",
                             "risk": RISK_WRITE, "error": "缺少必填参数：subject"}])
        assert "未执行" in out["answer"] and "subject" in out["answer"]

    def test_unknown_action_type_is_safe(self):
        """未知 action 名不能 KeyError——一次新动作不该让 /api/ask 500。"""
        out = self._render([{"action": "future_action", "status": "executed",
                             "risk": "write", "result": {"x": 1}},
                            {"action": "another", "status": "weird"}])
        assert out["answer"] and out["summary"]

    def test_empty_actions_keeps_old_behavior(self):
        """不开动作层时，答复内容与旧版一致（不出现空的动作段）。"""
        out = self._render([])
        assert "【执行动作】" not in out["answer"]
