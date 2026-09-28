"""本地模拟 CI 的评测基线校验步骤，确保该段脚本在 Python 3.9+ 语法下可跑。

这段逻辑与 .github/workflows/ci.yml 中的内联脚本保持一致：
CI 里用 heredoc 内联执行，这里保留一份可本地复现的等价实现，
避免「CI 上才发现的语法/路径问题」这类只能在远端暴露的故障。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 基线阈值：CI 用 dev 嵌入（不下载 1.3GB 模型、不装 torch），实测通过率 82%。
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
