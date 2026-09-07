"""回归评测入口（ROADMAP P0-1 / P1-5）。

运行：
    python eval_run.py                    # 用 golden.jsonl 跑基线
    python eval_run.py --golden 其它.jsonl

与 run_demo.py 的关系：
- **语料相同**（内置 FAQ + samples/ + test-docs/），所以基线指标能代表线上演示的真实水平；
- 但写在**独立 store 文件**（kb_store_eval.json / kb_graph_eval.json）上，不污染真实数据；
- 每次运行重新播种，保证可复现。

零外部依赖：memory 存储 + dev 嵌入 + deterministic 编排 + mock 实时数据，离线可跑。
产出：控制台报告 + eval_report.json（供改动前后对比）。
"""

import glob
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 隔离 store + 离线默认值（必须在 import app 之前设置）----
os.environ.setdefault("KB_GRAPH_STORE", os.path.join(_HERE, "kb_graph_eval.json"))
os.environ.setdefault("KB_MEM_STORE", os.path.join(_HERE, "kb_store_eval.json"))
os.environ.setdefault("KB_GRAPH_BACKEND", "memory")
os.environ.setdefault("KB_STORAGE_BACKEND", "memory")
os.environ.setdefault("KB_EMBEDDING_BACKEND", "dev")
os.environ.setdefault("KB_LLM_ENABLED", "0")
os.environ.setdefault("KB_AGENT_MODE", "deterministic")
os.environ.setdefault("KB_API_MOCK", "1")

# 每次重跑重新播种，保证结果可复现
for _f in ("kb_store_eval.json", "kb_graph_eval.json"):
    _p = os.path.join(_HERE, _f)
    if os.path.exists(_p):
        os.remove(_p)

import app  # noqa: E402  触发模块级初始化（空 store / dev 嵌入器 / 摄取管线）
from kb_mcp_server.adapters import reload_adapters  # noqa: E402
from kb_mcp_server.config import set_cfg  # noqa: E402


def seed() -> int:
    """播种与线上演示一致的语料，返回片段数。"""
    # 1) 内置示例 FAQ
    for doc_id, text in app.SAMPLE.items():
        app._ingestor.ingest_text(doc_id, text)

    # 2) samples/ 与 test-docs/ 下的文档（md / txt / docx，零依赖解析）
    for folder in ("samples", "test-docs"):
        fdir = os.path.join(_HERE, folder)
        if not os.path.isdir(fdir):
            continue
        for fp in sorted(glob.glob(os.path.join(fdir, "**", "*"), recursive=True)):
            if not os.path.isfile(fp):
                continue
            if not fp.lower().endswith((".md", ".txt", ".docx")):
                continue
            name = os.path.basename(fp)
            doc_id = f"{folder}_{os.path.splitext(name)[0]}"
            with open(fp, "rb") as fh:
                data = fh.read()
            app._ingestor.ingest_file(doc_id, name, data)

    # 3) 实时数据走 mock：离线即可评测 LiveData 链路
    set_cfg("KB_API_MOCK", "1")
    reload_adapters()
    return app._store.count()


def main() -> None:
    golden = os.path.join(_HERE, "golden.jsonl")
    if "--golden" in sys.argv:
        golden = sys.argv[sys.argv.index("--golden") + 1]

    n_chunks = seed()
    print(f"[eval] 语料已载入：{n_chunks} 个片段")

    from kb_mcp_server.agents import get_orchestrator  # noqa: E402
    from kb_mcp_server.eval import format_report, run_eval  # noqa: E402

    o = get_orchestrator()

    def ask(q: str, order_id=None, sku=None, top_k: int = 3) -> dict:
        return o.ask(q, top_k=top_k, order_id=order_id, sku=sku)

    report = run_eval(golden, ask)
    print(format_report(report))

    out = os.path.join(_HERE, "eval_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[eval] 报告已写入：{out}")


if __name__ == "__main__":
    main()
