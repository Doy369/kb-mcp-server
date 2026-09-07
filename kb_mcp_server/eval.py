"""离线回归评测（对应 ROADMAP 的 P0-1 / P1-5）。

目的：没有度量就没有「上线」。本模块用 golden 集跑出**可回归的基线指标**——
关键词召回率、置信度、时延分布、图谱路径命中数——每次改动前后对比，
就能量化判断「改动到底变好还是变坏」，而不是靠肉眼看 demo。

设计：实现 extensions.Evaluator 预留接口，零外部依赖，纯离线可跑。

用法：
    python eval_run.py            # 用 golden.jsonl 跑基线，输出报告 + eval_report.json
"""

from __future__ import annotations

from typing import Callable

from kb_mcp_server.extensions import Evaluator, GoldenCase, load_golden


class RegressionEvaluator(Evaluator):
    """关键词召回 + 置信度 + 时延的综合评测。

    recall = 命中的期望关键词数 / 期望关键词总数（在最终答复文本中匹配）。
    passed = recall >= recall_threshold 且 confidence >= case.min_confidence。
    """

    def __init__(self, recall_threshold: float = 0.6):
        self.recall_threshold = recall_threshold

    def evaluate(self, case: GoldenCase, actual: dict) -> dict:
        text = (actual.get("answer") or "") + "\n" + (actual.get("summary") or "")
        expect = case.expect_contains or []
        hit = [k for k in expect if k in text]
        missed = [k for k in expect if k not in hit]
        total = len(expect) or 1
        recall = len(hit) / total
        conf = (actual.get("confidence") or {}).get("score", 0.0) or 0.0
        agents = actual.get("agents") or {}
        # 反向断言：命中的禁止词说明召回了错误分块（如把 P2 的 4 小时当成 P0 的 15 分钟）
        forbidden = [k for k in (case.expect_not_contains or []) if k in text]
        passed = (recall >= self.recall_threshold
                  and float(conf) >= (case.min_confidence or 0.0)
                  and not forbidden)
        return {
            "question": case.question,
            "expect": expect,
            "hit": hit,
            "missed": missed,
            "forbidden": forbidden,
            "recall": round(recall, 3),
            "confidence": round(float(conf or 0), 3),
            "latency_ms": agents.get("total_ms", 0),
            "graph_paths": len(actual.get("graph_paths") or []),
            "live": len(actual.get("live_cards") or actual.get("live_data") or []),
            "method": actual.get("synthesis_method", ""),
            "passed": passed,
        }


def _quantile(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = max(0, min(len(s) - 1, int(round((len(s) - 1) * q))))
    return float(s[i])


def run_eval(golden_path: str, ask_fn: Callable[..., dict],
             recall_threshold: float = 0.6) -> dict:
    """跑完整 golden 集，返回聚合报告 + 逐条明细。"""
    cases = load_golden(golden_path)
    ev = RegressionEvaluator(recall_threshold)
    rows: list[dict] = []
    for c in cases:
        actual = ask_fn(c.question, order_id=c.order_id, sku=c.sku)
        rows.append(ev.evaluate(c, actual))

    n = len(rows) or 1
    passed = sum(1 for r in rows if r["passed"])
    lat = [r["latency_ms"] for r in rows]
    return {
        "total": len(rows),
        "passed": passed,
        "pass_rate": round(passed / n, 3),
        "avg_recall": round(sum(r["recall"] for r in rows) / n, 3),
        "avg_confidence": round(sum(r["confidence"] for r in rows) / n, 3),
        "latency_p50_ms": _quantile(lat, 0.5),
        "latency_p95_ms": _quantile(lat, 0.95),
        "avg_graph_paths": round(sum(r["graph_paths"] for r in rows) / n, 2),
        "recall_threshold": recall_threshold,
        "failed_cases": [
            {"question": r["question"], "missed": r["missed"],
             "forbidden": r["forbidden"], "recall": r["recall"]}
            for r in rows if not r["passed"]
        ],
        "rows": rows,
    }


def format_report(report: dict) -> str:
    """把报告渲染成可读文本，便于贴进 commit message / PR / 组会汇报。"""
    lines = [
        "=" * 60,
        "回归评测报告",
        "=" * 60,
        f"用例总数    : {report['total']}",
        f"通过        : {report['passed']}  (通过率 {report['pass_rate']:.0%})",
        f"平均召回率  : {report['avg_recall']:.3f}  (阈值 {report['recall_threshold']})",
        f"平均置信度  : {report['avg_confidence']:.3f}",
        f"时延 p50/p95: {report['latency_p50_ms']:.0f}ms / {report['latency_p95_ms']:.0f}ms",
        f"平均图谱路径: {report['avg_graph_paths']}",
        "-" * 60,
    ]
    for i, r in enumerate(report["rows"], 1):
        flag = "PASS" if r["passed"] else "FAIL"
        lines.append(f"{i:>2}. [{flag}] {r['question']}")
        lines.append(
            f"     召回 {r['recall']:.2f} 置信度 {r['confidence']:.3f} "
            f"耗时 {r['latency_ms']}ms 路径 {r['graph_paths']} 实时 {r['live']} 方法 {r['method']}"
        )
        if r["missed"]:
            lines.append(f"     未命中: {'、'.join(r['missed'])}")
        if r["forbidden"]:
            lines.append(f"     误命中禁止词（召回错误分块）: {'、'.join(r['forbidden'])}")
    if report["failed_cases"]:
        lines.append("-" * 60)
        lines.append("未通过用例：")
        for f in report["failed_cases"]:
            why = []
            if f["missed"]:
                why.append("未命中 " + "、".join(f["missed"]))
            if f["forbidden"]:
                why.append("误含 " + "、".join(f["forbidden"]))
            lines.append(f"  · {f['question']}（{'；'.join(why) or '置信度不足'}）")
    lines.append("=" * 60)
    return "\n".join(lines)
