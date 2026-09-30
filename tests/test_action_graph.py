"""动作结果回写图谱（P2-9 收尾）。

断言的重点不是「函数返回了 True」，而是**图里真的多了 / 没多东西**：
节点存在且带属性、关系按本体约束建立、被拦下的动作零写入、图谱坏掉时动作仍然成功。
这正是本项目对「副作用是否真的发生」的一贯要求。
"""

from __future__ import annotations

import pytest

from kb_mcp_server.action_graph import (
    ACTION_EFFECTS,
    Effect,
    apply_action_effects,
    build_effects,
    effects_enabled,
)
from kb_mcp_server.actions import ActionRunner, action_tools, read_action_log
from kb_mcp_server import graph as graph_mod


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _gs():
    """读回时必须拿**新**的 store：回写方自己 new 了一个 store，磁盘才是唯一真相。"""
    return graph_mod.get_graph_store()


def _names(node_type: str | None = None) -> list[str]:
    return sorted(e["name"] for e in _gs().find_entities(node_type=node_type, limit=200))


def _node(name: str, node_type: str) -> dict | None:
    for e in _gs().find_entities(name=name, node_type=node_type, limit=50):
        if e.get("name") == name:
            return e
    return None


def _edges(name: str, node_type: str, direction: str = "out") -> list[tuple[str, str]]:
    """(关系, 对端名)。注意 `neighbors()` 的元素形如
    `{"depth", "path": [step, ...], "node": {...}}`——关系名在 path 的最后一步，
    节点名在 `node.name`；直接读 `n["name"]` 会永远拿到 None（断言会「因为错误的原因通过」）。
    `direction` 默认 `out`（与 API 一致）：回写建的是 `Ticket → Customer`，
    所以「按客户查工单」是**入边**，必须显式 `direction="both"` / `"in"`。
    """
    nb = _gs().neighbors(name, node_type=node_type, direction=direction) or {}
    out: list[tuple[str, str]] = []
    for n in nb.get("neighbors", []):
        path = n.get("path") or []
        rel = path[-1].get("rel", "") if path else ""
        out.append((rel, (n.get("node") or {}).get("name", "")))
    return out


def _run(action: str, params: dict | None = None, **kw) -> dict:
    return ActionRunner(action_tools(), actor="tester").run(action, params, **kw)


@pytest.fixture
def graph_on(clean_graph, monkeypatch):
    """开启回写 + 一张干净的内存图（clean_graph 已把图路径指到 tmp）。"""
    monkeypatch.setenv("KB_ACTION_GRAPH", "1")
    return clean_graph


@pytest.fixture
def action_log(tmp_path, monkeypatch):
    p = str(tmp_path / "acts.jsonl")
    monkeypatch.setenv("KB_ACTION_LOG", p)
    return p


# --------------------------------------------------------------------------- #
# 开关：默认关，且关了以后旧行为逐字不变
# --------------------------------------------------------------------------- #
class TestSwitchOff:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("KB_ACTION_GRAPH", raising=False)
        assert effects_enabled() is False

    def test_off_values(self, monkeypatch):
        for v in ("0", "off", "false", "no", "disabled", ""):
            monkeypatch.setenv("KB_ACTION_GRAPH", v)
            assert effects_enabled() is False, v
        monkeypatch.setenv("KB_ACTION_GRAPH", "1")
        assert effects_enabled() is True

    def test_no_graph_field_and_zero_writes(self, clean_graph, monkeypatch):
        monkeypatch.setenv("KB_ACTION_GRAPH", "0")
        out = _run("create_ticket", {"subject": "退货流程咨询", "customer": "ACME"})
        assert out["ok"] is True
        assert "graph" not in out                 # 默认关：连字段都不出现
        assert _names() == []                     # 图里一个节点都没有

    def test_direct_call_reports_disabled(self, clean_graph, monkeypatch):
        monkeypatch.setenv("KB_ACTION_GRAPH", "0")
        res = apply_action_effects("create_ticket", {"customer": "ACME"},
                                   {"ticket_id": "TKT-1", "subject": "x"})
        assert res == {"applied": False, "reason": "disabled"}

    def test_audit_has_no_graph_key_when_off(self, clean_graph, action_log, monkeypatch):
        monkeypatch.setenv("KB_ACTION_GRAPH", "0")
        _run("create_ticket", {"subject": "x"})
        rec = read_action_log(5)[0]
        assert rec["status"] == "executed"
        assert "graph" not in rec                 # 审计格式与旧版完全一致


# --------------------------------------------------------------------------- #
# 开：真的写进去了
# --------------------------------------------------------------------------- #
class TestWriteBack:
    def test_ticket_node_written_with_props(self, graph_on):
        out = _run("create_ticket", {"subject": "发票开错", "priority": "P1",
                                     "customer": "ACME"})
        tid = out["result"]["ticket_id"]
        assert out["graph"]["applied"] is True
        assert out["graph"]["node"] == f"Ticket:{tid}"
        assert out["graph"]["backend"] == "memory"

        node = _node(tid, "Ticket")
        assert node is not None, "工单节点没落图"
        assert node["props"]["subject"] == "发票开错"
        assert node["props"]["priority"] == "P1"
        assert node["props"]["sla_response"] == "1 小时"
        assert node["props"]["status"] == "open"

    def test_relation_to_customer(self, graph_on):
        out = _run("create_ticket", {"subject": "x", "customer": "ACME"})
        tid = out["result"]["ticket_id"]
        assert ("SUBMITTED_BY", "ACME") in _edges(tid, "Ticket")
        assert "ACME" in _names("Customer")

    def test_blank_customer_skips_relation(self, graph_on):
        out = _run("create_ticket", {"subject": "x"})
        tid = out["result"]["ticket_id"]
        assert out["graph"]["applied"] is True
        assert out["graph"]["triples"] == 0
        assert _edges(tid, "Ticket") == []
        assert _names("Customer") == []           # 不建空名字的节点

    def test_context_sku_links_product(self, graph_on):
        out = _run("create_ticket", {"subject": "x"}, context={"sku": "LM3-ARM"})
        tid = out["result"]["ticket_id"]
        assert ("ABOUT_PRODUCT", "LM3-ARM") in _edges(tid, "Ticket")
        assert "LM3-ARM" in _names("Product")

    def test_sku_can_also_come_from_params(self, graph_on):
        out = _run("create_ticket", {"subject": "x", "sku": "LM3-PRO"})
        tid = out["result"]["ticket_id"]
        assert ("ABOUT_PRODUCT", "LM3-PRO") in _edges(tid, "Ticket")

    def test_relations_are_walkable_both_ways(self, graph_on):
        out = _run("create_ticket", {"subject": "x", "customer": "ACME"},
                   context={"sku": "LM3-ARM"})
        tid = out["result"]["ticket_id"]
        rels = dict(_edges(tid, "Ticket"))
        assert rels.get("SUBMITTED_BY") == "ACME"
        assert rels.get("ABOUT_PRODUCT") == "LM3-ARM"
        # 反向可达：从客户能走回工单（「这个客户有哪些工单」是真实问法）。
        # 边是 Ticket → Customer，所以从客户出发是**入边**，必须 direction="both"；
        # 用默认的 "out" 会静默返回空——这是本仓最要警惕的「不报错、答案悄悄变错」。
        assert _edges("ACME", "Customer", direction="out") == []
        back = _edges("ACME", "Customer", direction="both")
        assert any(name == tid for _, name in back), back

    def test_write_back_is_idempotent(self, graph_on):
        payload = {"subject": "x", "customer": "ACME"}
        r1 = _run("create_ticket", payload)
        tid = r1["result"]["ticket_id"]
        # 同一个动作 id 再写一次（模拟重放 / 重试）：节点与边都不该翻倍
        before = _gs().stats()
        apply_action_effects("create_ticket", payload,
                             {"ticket_id": tid, "subject": "x", "customer": "ACME"})
        after = _gs().stats()
        assert after["nodes"] == before["nodes"]
        assert after["edges"] == before["edges"]


# --------------------------------------------------------------------------- #
# 只有「真的执行成功」才写
# --------------------------------------------------------------------------- #
class TestOnlyExecutedWrites:
    def test_needs_confirmation_writes_nothing(self, graph_on):
        out = _run("request_refund", {"order_id": "SO123", "amount": 88})
        assert out["status"] == "needs_confirmation"
        assert out["ok"] is False
        assert "graph" not in out                 # 确认门拦住 = 什么都没发生
        assert _names() == []

    def test_confirmed_destructive_without_mapping_writes_nothing(self, graph_on):
        out = _run("request_refund", {"order_id": "SO123", "amount": 88}, confirmed=True)
        assert out["status"] == "executed"
        assert out["graph"]["applied"] is False
        assert out["graph"]["reason"] == "no_effect_mapping"
        assert _names() == []                     # 本体没有 Refund，就不该硬塞

    def test_rejected_writes_nothing(self, graph_on):
        out = _run("create_ticket", {})           # 缺必填 subject
        assert out["status"] == "rejected"
        assert "graph" not in out
        assert _names() == []

    def test_unknown_action_writes_nothing(self, graph_on):
        out = _run("no_such_action", {})
        assert out["status"] == "rejected"
        assert "graph" not in out
        assert _names() == []

    def test_no_effect_mapping_action_writes_nothing(self, graph_on):
        out = _run("update_order", {"order_id": "SO123", "field": "address", "value": "x"})
        assert out["status"] == "executed"
        assert out["graph"]["applied"] is False
        assert out["graph"]["reason"] == "no_effect_mapping"
        assert _names() == []

    def test_read_only_action_writes_nothing(self, graph_on):
        out = _run("list_tickets", {})
        assert out["status"] == "executed"
        assert out["graph"]["applied"] is False
        assert _names() == []


# --------------------------------------------------------------------------- #
# 本体校验：映射写错也不能污染图
# --------------------------------------------------------------------------- #
class TestOntologySafety:
    def test_invalid_relation_is_dropped(self, clean_graph, monkeypatch):
        monkeypatch.setenv("KB_ACTION_GRAPH", "1")
        bad = {"create_ticket": Effect(
            node_type="Ticket", name_field="ticket_id",
            relations=(("MENTIONS", "Document", "doc"),))}   # MENTIONS 只允许 Document→*
        res = apply_action_effects("create_ticket", {"doc": "D1"},
                                   {"ticket_id": "TKT-BAD"}, effects=bad)
        assert res["applied"] is True             # 节点本身合规，仍然写
        assert res["triples"] == 0
        assert res["dropped"] and res["dropped"][0]["relation"] == "MENTIONS"
        assert res["reason"] == "ontology_dropped"
        assert _edges("TKT-BAD", "Ticket") == []

    def test_unknown_node_type_is_rejected_before_any_write(self, clean_graph, monkeypatch):
        monkeypatch.setenv("KB_ACTION_GRAPH", "1")
        bad = {"create_ticket": Effect(node_type="Order", name_field="ticket_id")}
        res = apply_action_effects("create_ticket", {}, {"ticket_id": "TKT-X"},
                                   effects=bad)
        assert res["applied"] is False
        assert res["reason"] == "unknown_node_type:Order"
        assert _names() == []

    def test_missing_subject_name_is_reported(self, clean_graph, monkeypatch):
        monkeypatch.setenv("KB_ACTION_GRAPH", "1")
        bad = {"create_ticket": Effect(node_type="Ticket", name_field="nope")}
        res = apply_action_effects("create_ticket", {}, {"ticket_id": "TKT-X"},
                                   effects=bad)
        assert res["applied"] is False
        assert res["reason"] == "missing_subject_name"
        assert _names() == []

    def test_build_effects_plan_for_default_mapping(self, monkeypatch):
        monkeypatch.setenv("KB_ACTION_GRAPH", "1")
        plan, reason = build_effects(
            "create_ticket", {"customer": "ACME"},
            {"ticket_id": "TKT-1", "subject": "s", "priority": "P2",
             "sla_response": "4 小时", "status": "open"})
        assert reason == ""
        assert plan["name"] == "TKT-1" and plan["node_type"] == "Ticket"
        assert len(plan["triples"]) == 1          # 只有 customer，没有 sku
        assert plan["triples"][0].is_valid()

    def test_default_mapping_only_claims_ontology_types(self, monkeypatch):
        """默认映射本身必须是本体子集——否则上线第一天就在丢数据。"""
        from kb_mcp_server.graph import NODE_TYPES, relation_is_valid
        monkeypatch.setenv("KB_ACTION_GRAPH", "1")
        for action, eff in ACTION_EFFECTS.items():
            assert eff.node_type in NODE_TYPES, f"{action}: {eff.node_type}"
            for rel, obj_type, _field in eff.relations:
                assert obj_type in NODE_TYPES, f"{action}: {obj_type}"
                assert relation_is_valid(rel, eff.node_type, obj_type), f"{action}: {rel}"


# --------------------------------------------------------------------------- #
# 降级：记账失败绝不影响动作结论
# --------------------------------------------------------------------------- #
class TestDegradation:
    @pytest.fixture
    def broken_graph(self, monkeypatch):
        import kb_mcp_server.action_graph as ag

        def _boom():
            raise RuntimeError("AGE down")

        monkeypatch.setattr(ag, "get_graph_store", _boom)

    def test_graph_error_degrades_but_action_succeeds(self, broken_graph, monkeypatch):
        monkeypatch.setenv("KB_ACTION_GRAPH", "1")
        out = _run("create_ticket", {"subject": "x", "customer": "ACME"})
        assert out["ok"] is True                  # 动作确实执行了
        assert out["status"] == "executed"
        assert out["graph"]["applied"] is False
        assert out["graph"]["reason"] == "graph_error:RuntimeError"
        assert "AGE down" in out["graph"]["error"]

    def test_audit_records_the_degradation(self, broken_graph, action_log, monkeypatch):
        monkeypatch.setenv("KB_ACTION_GRAPH", "1")
        _run("create_ticket", {"subject": "x"})
        rec = read_action_log(5)[0]
        assert rec["ok"] is True
        assert rec["graph"]["applied"] is False
        assert rec["graph"]["reason"] == "graph_error:RuntimeError"


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #
class TestAudit:
    def test_audit_contains_graph_when_on(self, graph_on, action_log):
        out = _run("create_ticket", {"subject": "x", "customer": "ACME"},
                   context={"sku": "LM3-ARM"})
        rec = read_action_log(5)[0]
        assert rec["graph"]["applied"] is True
        assert rec["graph"]["node"] == out["graph"]["node"]
        assert rec["graph"]["triples"] == 2
