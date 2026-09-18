"""离线回归评测（对应 ROADMAP 的 P0-1 / P1-5）。

目的：没有度量就没有「上线」。本模块用 golden 集跑出**可回归的基线指标**——
关键词召回率、置信度、时延分布、图谱路径命中数——每次改动前后对比，
就能量化判断「改动到底变好还是变坏」，而不是靠肉眼看 demo。

设计：实现 extensions.Evaluator 预留接口，零外部依赖，纯离线可跑。

用法：
    python eval_run.py            # 用 golden.jsonl 跑基线，输出报告 + eval_report.json
"""

from __future__ import annotations

import re
from typing import Callable

from kb_mcp_server.extensions import Evaluator, GoldenCase, load_golden


def _strip_evidence(answer: str) -> str:
    """剔除答复里的【关系路径】证据段，只留【实时数据】与【知识依据】等答案主体。"""
    parts = re.split(r"(【[^】]{2,8}】)", answer)
    out: list[str] = []
    skip = False
    for p in parts:
        if p.startswith("【") and p.endswith("】"):
            skip = (p == "【关系路径】")
            continue
        if not skip:
            out.append(p)
    return "\n".join(out).strip()


def _has_raw(text: str, kw: str) -> bool:
    """字面匹配。数字开头的词要求前面不是数字，避免子串误命中。

    反例：期望「8 小时」时，文本里的「48 小时」里含「8 小时」→ 假通过。
    """
    if not kw:
        return False
    if kw[:1].isdigit():
        return re.search(r"(?<!\d)" + re.escape(kw), text) is not None
    return kw in text


# ---------------------------------------------------------------------------
# 语义级断言（2026-09-09 补，P0-1 收官）
# ---------------------------------------------------------------------------
# 背景：P0-1 LLM 回归实测 9/11，两个 FAIL 抓原文后确认**语义是对的**，
# 纯粹是字面不同被误判：
#   · golden「7 个自然日」 vs LLM「7 天内 / 七天无理由」
#   · golden「48 小时」     vs LLM「48小时内出库」
# 于是加两层，从「字面口径」升级到「语义口径」：
#
# 1) _norm()：抹平**书写差异** —— 空格 / 全角 / 中文数字 / 时间单位别名。
#    例：「48 小时」→「48小时」、「48小时内」→「48小时」→ 命中。
# 2) 同义组：golden 的 expect_contains 元素可写成数组 [["顺丰","SF Express"]]，
#    组内任一命中即算命中，用于 _norm 盖不住的**词汇差异**。
#    显式声明，不靠算法猜 —— 评测集保持可审计。
#
# 匹配策略是「原文命中 OR 归一化命中」双通道：只会放宽书写差异，
# 不会引入新的漏判（原来能过的现在照样过）。
#
# 已知限制：中文数字转换在副词场景会误伤（如「十分」→「10分」），
# 评测集当前无此类用例；真出问题就在 golden 里改用同义组显式声明。

_CN_NUM = {"零": "0", "一": "1", "两": "2", "二": "2", "三": "3",
           "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}

# 时间单位别名：长规则必须在前，否则短规则会先吃掉长规则
_UNIT_ALIAS = (
    ("个自然日", "日"),
    ("个工作日内", "工作日"),
    ("自然日", "日"),
    ("天内", "日"),
    ("个小时", "小时"),
    ("小时内", "小时"),
    ("天", "日"),
)


def _cn2digit(s: str) -> str:
    """中文数字 → 阿拉伯数字（覆盖 0~99，够评测集用）。

    支持：七→7、十五→15、四十八→48、十→10。
    """
    def _rep(m: "re.Match[str]") -> str:
        hi, lo = m.group(1), m.group(2)
        hi_v = int(_CN_NUM.get(hi, 0)) if hi else 1     # 「十X」的十缺省为 1
        lo_v = int(_CN_NUM[lo]) if lo else 0
        return str(hi_v * 10 + lo_v)

    s = re.sub(r"([一二三四五六七八九])?十([一二三四五六七八九])?", _rep, s)
    for cn, d in _CN_NUM.items():
        s = s.replace(cn, d)
    return s


def _norm(s: str) -> str:
    """归一化到「可比较形式」：全角转半角 + 中文数字转阿拉伯 + 去空白 + 单位别名。"""
    if not s:
        return ""
    t = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    t = t.replace("\u3000", " ")
    t = _cn2digit(t)
    t = re.sub(r"\s+", "", t)
    for a, b in _UNIT_ALIAS:
        t = t.replace(a, b)
    return t.lower()


def _has(text: str, kw: str) -> bool:
    """语义级匹配：字面命中 OR 归一化后命中。"""
    return _has_raw(text, kw) or _has_raw(_norm(text), _norm(kw))


def _as_groups(expect) -> list[list[str]]:
    """把 expect_contains 规整成同义组列表。

    "顺丰"                -> [["顺丰"]]
    ["顺丰", "SF Express"] -> [["顺丰", "SF Express"]]   # 组内任一命中即算命中
    """
    groups: list[list[str]] = []
    for item in expect or []:
        if isinstance(item, (list, tuple)):
            g = [str(x) for x in item if str(x).strip()]
        else:
            g = [str(item)]
        if g:
            groups.append(g)
    return groups


class RegressionEvaluator(Evaluator):
    """关键词召回 + 置信度 + 时延的综合评测。

    recall = 命中的期望关键词数 / 期望关键词总数（在最终答复文本中匹配）。
    passed = recall >= recall_threshold 且 confidence >= case.min_confidence。
    """

    def __init__(self, recall_threshold: float = 0.6):
        self.recall_threshold = recall_threshold

    def evaluate(self, case: GoldenCase, actual: dict) -> dict:
        ans = actual.get("answer") or ""
        # 只对**答案主体**做断言：【关系路径】是图谱证据段，天然会列出同类的其他条款，
        # 算进答案会把「证据覆盖面广」误判成「答错」。注意要保留【实时数据】与【知识依据】。
        # （证据段自身的质量问题见 ROADMAP 已知问题：图谱抽取未保留 P0/P1/P2 档位绑定）
        text = _strip_evidence(ans) or (ans + "\n" + (actual.get("summary") or ""))
        # 语义级断言：先按同义组展开，组内任一命中即算命中（见 _norm 上方注释）
        expect = case.expect_contains or []
        groups = _as_groups(expect)
        hit: list[str] = []
        missed: list[str] = []
        for g in groups:
            h = next((k for k in g if _has(text, k)), None)
            if h is not None:
                hit.append(h)
            else:
                missed.append("／".join(g))
        total = len(groups) or 1
        recall = len(hit) / total
        conf = (actual.get("confidence") or {}).get("score", 0.0) or 0.0
        agents = actual.get("agents") or {}
        # 反向断言：命中的禁止词说明召回了错误分块（如把 P2 的 4 小时当成 P0 的 15 分钟）
        forbidden = [k for k in (case.expect_not_contains or []) if _has(text, k)]
        passed = (recall >= self.recall_threshold
                  and float(conf) >= (case.min_confidence or 0.0)
                  and not forbidden)
        return {
            "question": case.question,
            "expect": ["／".join(g) for g in groups],   # 同义组展示为「A／B」
            "hit": hit,
            "missed": missed,
            "forbidden": forbidden,
            "recall": round(recall, 3),
            "confidence": round(float(conf or 0), 3),
            "latency_ms": agents.get("total_ms", 0),
            "graph_paths": len(actual.get("graph_paths") or []),
            "live": len(actual.get("live_cards") or actual.get("live_data") or []),
            "method": actual.get("synthesis_method", ""),
            "match_mode": "raw+normalized",   # 字面 + 归一化(空格/全角/中文数字/单位别名)
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
        f"匹配模式    : 字面 + 归一化（空格／全角／中文数字／时间单位别名）＋ 同义组",
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
