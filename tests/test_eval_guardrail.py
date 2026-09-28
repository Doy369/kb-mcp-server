"""评测器与护栏单测：数字边界断言 / 证据段剔除 / 置信度护栏 / 审计日志。

**这里覆盖 ROADMAP 记录的第三个真 bug —— 假通过用例**：
`严重故障多久修复` 原先是靠 `48 小时` 里的子串蒙对的（期望「8 小时」）。
加了数字边界匹配后这条假通过才暴露出来。

教训：**测试本身的正确性也需要被验证**。这个文件就是对评测器的测试。
"""

from __future__ import annotations

import json

import pytest

from kb_mcp_server.eval import RegressionEvaluator, _has, _strip_evidence, run_eval
from kb_mcp_server.extensions import (
    ConfidenceGuardrail,
    GoldenCase,
    PassthroughGuardrail,
    install_default_guardrail,
    load_golden,
    set_guardrail,
)


# --------------------------------------------------------------------------- #
# 关键词匹配：假通过用例的根因
# --------------------------------------------------------------------------- #
class TestHasKeyword:
    def test_plain_substring(self):
        assert _has("退款需在 7 个自然日内申请", "退款")
        assert not _has("物流配送说明", "退款")

    def test_digit_boundary_rejects_substring(self):
        """**核心回归**：期望「8 小时」不能命中「48 小时」里的子串。

        修复前 `严重故障多久修复` 就是靠这个假通过蒙对的。
        """
        assert not _has("48 小时内出库发货", "8 小时"), "数字前缀边界失效（假通过回归）"
        assert not _has("48 小时内出库发货", "8 小时修复")
        assert _has("8 小时内修复", "8 小时")

    def test_digit_boundary_at_start_ok(self):
        assert _has("8 小时发货", "8 小时")

    def test_multiple_digits(self):
        assert not _has("215 分钟内响应", "15 分钟") is False or True
        # 15 前面是「2」→ 属于更长数字，不应命中
        assert not _has("215 分钟", "15 分钟")
        assert _has("15 分钟", "15 分钟")

    def test_chinese_number_prefix(self):
        """「21.9 分钟」中的「1.9 分钟」也不应误命中。"""
        assert not _has("21.9 分钟", "1.9 分钟")

    def test_non_digit_still_substring(self):
        assert _has("企业版可用性 99.95%", "可用性")


# --------------------------------------------------------------------------- #
# 证据段剔除
# --------------------------------------------------------------------------- #
class TestStripEvidence:
    def test_removes_relation_path_section(self):
        """【关系路径】天然会列出同类其他条款，算进答案会把「证据覆盖面广」误判成「答错」。"""
        ans = (
            "【实时数据】\n订单 1001：已发货\n"
            "【关系路径】\n1. 物流配送 --适用条款--> 48小时内发货\n"
            "【知识依据】\n1. P2 问题响应时限为 4 小时"
        )
        out = _strip_evidence(ans)
        assert "48小时内发货" not in out
        assert "已发货" in out, "【实时数据】必须保留"
        assert "4 小时" in out, "【知识依据】必须保留"

    def test_multiple_sections(self):
        ans = "【关系路径】\nA\n【知识依据】\nB\n【关系路径】\nC"
        out = _strip_evidence(ans)
        assert "A" not in out and "C" not in out
        assert "B" in out

    def test_no_evidence_section(self):
        assert _strip_evidence("普通答复文本") == "普通答复文本"

    def test_empty(self):
        assert _strip_evidence("") == ""


# --------------------------------------------------------------------------- #
# 评测器
# --------------------------------------------------------------------------- #
class TestRegressionEvaluator:
    def _actual(self, answer: str, confidence: float = 0.8):
        return {"answer": answer, "summary": answer,
                "confidence": {"label": "高", "score": confidence},
                "agents": {"total_ms": 100}}

    def test_pass_when_all_keywords_hit(self):
        ev = RegressionEvaluator(recall_threshold=0.6)
        case = GoldenCase(question="q", expect_contains=["退款", "7 个自然日"])
        r = ev.evaluate(case, self._actual("退款须在 7 个自然日内申请"))
        assert r["passed"] is True
        assert r["recall"] == 1.0

    def test_fail_when_recall_below_threshold(self):
        ev = RegressionEvaluator(recall_threshold=0.6)
        case = GoldenCase(question="q", expect_contains=["退款", "7 个自然日", "运费"])
        r = ev.evaluate(case, self._actual("退款说明"))
        assert r["passed"] is False
        assert r["recall"] == pytest.approx(1 / 3, abs=1e-3)
        assert "7 个自然日" in r["missed"]

    def test_forbidden_word_causes_failure(self):
        """反向断言：命中禁止词说明召回了错误分块（如把 P2 的 4 小时当成 P0 的 15 分钟）。"""
        ev = RegressionEvaluator()
        case = GoldenCase(question="P0 故障多久响应",
                          expect_contains=["15 分钟"],
                          expect_not_contains=["4 小时"])
        r = ev.evaluate(case, self._actual("响应时限为 15 分钟，P2 为 4 小时"))
        assert r["passed"] is False, "误含禁止词却判通过"
        assert r["forbidden"] == ["4 小时"]

    def test_evidence_section_not_scored(self):
        """答案主体正确、证据段列了其他条款，不应判为失败。"""
        ev = RegressionEvaluator()
        case = GoldenCase(question="P0 故障多久响应", expect_contains=["15 分钟"])
        ans = "【关系路径】\n1. 服务响应 --适用条款--> 4 小时\n【知识依据】\nP0 响应时限为 15 分钟"
        assert ev.evaluate(case, self._actual(ans))["passed"] is True

    def test_min_confidence_gate(self):
        ev = RegressionEvaluator()
        case = GoldenCase(question="q", expect_contains=["退款"], min_confidence=0.9)
        r = ev.evaluate(case, self._actual("退款说明", confidence=0.5))
        assert r["passed"] is False, "置信度未达 min_confidence 却判通过"

    def test_falls_back_to_summary_when_answer_empty(self):
        ev = RegressionEvaluator()
        case = GoldenCase(question="q", expect_contains=["退款"])
        actual = {"answer": "", "summary": "退款流程说明",
                  "confidence": {"score": 0.8}, "agents": {}}
        assert ev.evaluate(case, actual)["passed"] is True

    def test_no_expectations_does_not_crash(self):
        ev = RegressionEvaluator()
        assert ev.evaluate(GoldenCase(question="q"), self._actual("任意"))["recall"] == 0.0


class TestRunEval:
    def test_aggregate_report_shape(self, tmp_path):
        golden = tmp_path / "g.jsonl"
        golden.write_text(
            '\n'.join([
                json.dumps({"question": "q1", "expect_contains": ["退款"]}, ensure_ascii=False),
                json.dumps({"question": "q2", "expect_contains": ["发票"]}, ensure_ascii=False),
            ]),
            encoding="utf-8",
        )

        def ask(q, order_id=None, sku=None):
            return {"answer": "退款说明" if q == "q1" else "其他",
                    "confidence": {"score": 0.8}, "agents": {"total_ms": 50}}

        rep = run_eval(str(golden), ask)
        assert rep["total"] == 2
        assert rep["passed"] == 1
        assert rep["pass_rate"] == pytest.approx(0.5)
        assert rep["latency_p50_ms"] == 50
        assert len(rep["failed_cases"]) == 1

    def test_empty_golden(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        rep = run_eval(str(p), lambda *a, **k: {})
        assert rep["total"] == 0


class TestLoadGolden:
    def test_jsonl(self, tmp_path):
        p = tmp_path / "g.jsonl"
        p.write_text(
            '\n'.join([
                json.dumps({"question": "q1", "expect_contains": ["a"]}, ensure_ascii=False),
                json.dumps({"question": "q2", "expect_not_contains": ["b"]}, ensure_ascii=False),
            ]),
            encoding="utf-8",
        )
        cases = load_golden(str(p))
        assert len(cases) == 2
        assert cases[0].expect_contains == ["a"]
        assert cases[1].expect_not_contains == ["b"]

    def test_json_array(self, tmp_path):
        p = tmp_path / "g.json"
        p.write_text(json.dumps([{"question": "q1"}, {"question": "q2"}]), encoding="utf-8")
        assert len(load_golden(str(p))) == 2

    def test_missing_file_returns_empty(self):
        assert load_golden("不存在的文件.jsonl") == []

    def test_real_golden_is_wellformed(self):
        """项目自带的 golden.jsonl 必须始终可解析且字段完整。"""
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "golden.jsonl")
        if not os.path.exists(path):
            pytest.skip("golden.jsonl 不存在")
        cases = load_golden(path)
        assert len(cases) >= 10, f"golden 用例过少（{len(cases)}），回归覆盖不足"
        for c in cases:
            assert c.question.strip(), "存在空问题"
            assert c.expect_contains or c.expect_not_contains, (
                f"用例「{c.question}」没有任何断言，等于白测"
            )


# --------------------------------------------------------------------------- #
# 护栏
# --------------------------------------------------------------------------- #
class TestConfidenceGuardrail:
    def _ans(self, score):
        return {"answer": "答复内容", "confidence": {"label": "低", "score": score}}

    def test_low_confidence_escalated(self):
        g = ConfidenceGuardrail(min_confidence=0.5)
        r = g.check(self._ans(0.3))
        assert r.passed is False
        assert r.action == "escalate"
        assert "0.3" in r.reason or "0.30" in r.reason

    def test_high_confidence_passes(self):
        assert ConfidenceGuardrail(min_confidence=0.5).check(self._ans(0.8)).passed is True

    def test_zero_threshold_disables(self):
        assert ConfidenceGuardrail(min_confidence=0.0).check(self._ans(0.01)).passed is True

    def test_missing_confidence_treated_as_zero(self):
        r = ConfidenceGuardrail(min_confidence=0.5).check({"answer": "x"})
        assert r.passed is False

    def test_passthrough_always_passes(self):
        assert PassthroughGuardrail().check(self._ans(0.0)).passed is True


class TestApplyGuardrail:
    def test_escalate_appends_review_marker(self):
        """低置信答复必须显式标记「需人工复核」，不能静默放行。"""
        from kb_mcp_server.extensions import apply_guardrail

        set_guardrail(ConfidenceGuardrail(min_confidence=0.5))
        try:
            out = apply_guardrail({"answer": "低质量答复", "confidence": {"score": 0.2}})
            assert "[需人工复核]" in out["answer"]
            assert out["guardrail"]["passed"] is False
        finally:
            set_guardrail(None)
            install_default_guardrail()

    def test_pass_returns_unchanged(self):
        from kb_mcp_server.extensions import apply_guardrail

        set_guardrail(ConfidenceGuardrail(min_confidence=0.5))
        try:
            out = apply_guardrail({"answer": "好答复", "confidence": {"score": 0.9}})
            assert out["answer"] == "好答复"
            assert "guardrail" not in out
        finally:
            set_guardrail(None)
            install_default_guardrail()

    def test_no_guardrail_is_passthrough(self):
        from kb_mcp_server.extensions import apply_guardrail

        set_guardrail(PassthroughGuardrail())
        try:
            assert apply_guardrail({"answer": "x"})["answer"] == "x"
        finally:
            set_guardrail(None)
            install_default_guardrail()


# --------------------------------------------------------------------------- #
# 审计日志
# --------------------------------------------------------------------------- #
class TestAudit:
    def test_write_and_read(self, tmp_path, monkeypatch):
        from kb_mcp_server import audit as audit_mod

        monkeypatch.setenv("KB_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
        audit_mod.audit({"trace_id": "t1", "question": "q", "confidence": 0.8})
        audit_mod.audit({"trace_id": "t2", "question": "q2", "confidence": 0.3})

        rows = audit_mod.read_recent(10)
        assert len(rows) == 2
        assert rows[0]["trace_id"] == "t2", "read_recent 应返回新→旧"
        assert rows[0]["ts"], "应自动补 ts 字段"

    def test_off_disables(self, tmp_path, monkeypatch):
        from kb_mcp_server import audit as audit_mod

        monkeypatch.setenv("KB_AUDIT_LOG", "off")
        audit_mod.audit({"trace_id": "t1"})
        assert audit_mod.read_recent(10) == []

    def test_corrupted_lines_skipped(self, tmp_path, monkeypatch):
        """审计是旁路，坏行不能影响正常记录读取。"""
        from kb_mcp_server import audit as audit_mod

        p = tmp_path / "audit.jsonl"
        p.write_text('{"a": 1}\n坏行不是JSON\n{"b": 2}\n', encoding="utf-8")
        monkeypatch.setenv("KB_AUDIT_LOG", str(p))
        rows = audit_mod.read_recent(10)
        assert len(rows) == 2

    def test_never_raises(self, tmp_path, monkeypatch):
        """审计失败必须静默——绝不允许拖垮问答主链路。"""
        from kb_mcp_server import audit as audit_mod

        monkeypatch.setenv("KB_AUDIT_LOG", str(tmp_path / "no" / "such" / "dir" / "a.jsonl"))
        audit_mod.audit({"x": 1})  # 不应抛异常

    def test_read_missing_file(self, tmp_path, monkeypatch):
        from kb_mcp_server import audit as audit_mod

        monkeypatch.setenv("KB_AUDIT_LOG", str(tmp_path / "missing.jsonl"))
        assert audit_mod.read_recent(10) == []
