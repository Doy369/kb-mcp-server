"""评测基线校验：通过率低于阈值即退出码 1，用于阻断合并。

**本地与 CI 共用同一份实现**——.github/workflows/ci.yml 直接调用本脚本，
不内联复制逻辑，避免两处阈值/口径漂移。

本地复现 CI 的完整三步：
    python -m pytest tests/ -q     # 单测
    python eval_run.py             # 回归评测（产出 eval_report.json）
    python scripts/check_baseline.py   # 本脚本，通过则退出码 0
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 基线阈值：CI 用 dev 嵌入（不下载 1.3GB 模型、不装 torch）。
# 实测 dev 通过率：语义级断言前 82%（9/11，P2 哨兵用例受限于字面区分能力），
# 断言后 91%（10/11，仅剩同一哨兵用例 FAIL）。
# 取 0.72 留出余量——阈值贴着实测值会让 CI 变得噪音化，最终被人习惯性忽略；
# 定太低则失去拦截作用。bge 下的 100% 基线由本地/带模型的 job 单独验证。
MIN_PASS_RATE = 0.72


def main() -> int:
    report = os.path.join(_HERE, "eval_report.json")
    if not os.path.exists(report):
        print("[FAIL] 未找到 eval_report.json，请先运行 python eval_run.py", file=sys.stderr)
        return 1

    with open(report, encoding="utf-8") as f:
        rep = json.load(f)

    rate = rep["pass_rate"]
    print(f"通过率 {rate:.0%}（{rep['passed']}/{rep['total']}）")
    print(f"平均召回 {rep['avg_recall']:.3f}  平均置信度 {rep['avg_confidence']:.3f}")
    print(f"时延 p50/p95: {rep['latency_p50_ms']:.0f}ms / {rep['latency_p95_ms']:.0f}ms")

    for c in rep["failed_cases"]:
        why = []
        if c["missed"]:
            why.append("未命中 " + "、".join(c["missed"]))
        if c["forbidden"]:
            why.append("误含 " + "、".join(c["forbidden"]))
        print(f"  · {c['question']}（{'；'.join(why) or '置信度不足'}）")

    if rate < MIN_PASS_RATE:
        print(f"\n[FAIL] 通过率 {rate:.0%} 低于基线 {MIN_PASS_RATE:.0%}", file=sys.stderr)
        return 1
    print(f"\n[OK] 通过率不低于基线 {MIN_PASS_RATE:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
