"""回归评测入口（ROADMAP P0-1 / P1-5）。

运行：
    python eval_run.py                    # 模板合成基线（dev 嵌入，离线）
    python eval_run.py --embedding bge    # bge 真实嵌入（需已装 sentence-transformers）
    python eval_run.py --llm --embedding bge   # LLM 合成回归（P0-1，读项目 runtime_config 里的 key）
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

# ---- 命令行开关（必须在设 env 之前解析，app 会读这些 env）----
#   --llm          启用 LLM 合成（KB_LLM_ENABLED=1），跑 P0-1 真实 LLM 回归
#   --embedding X  覆盖嵌入后端（dev | bge）
_USE_LLM = "--llm" in sys.argv
_EMBED = None
if "--embedding" in sys.argv:
    _i = sys.argv.index("--embedding")
    _EMBED = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else None

# ---- 隔离 runtime_config（KB_DATA_DIR 必须在 import app 之前设）----
# eval 必须可复现，不能依赖「用户在页面配了什么」：用独立数据目录让
# runtime_config 落到隔离位置（不存在→空），get_cfg 全部回退到本脚本设的 env。
# 否则 get_cfg 优先读项目根 runtime_config.json（含真 key / KB_LLM_ENABLED=1），
# 会导致「模板基线」其实被 LLM 覆盖，评测失真。
os.environ["KB_DATA_DIR"] = os.path.join(_HERE, ".eval_runtime")

# ---- --llm 模式：从项目 runtime_config 读 LLM 凭证到 env（key 只进内存、不落盘）----
if _USE_LLM:
    _rc_path = os.path.join(_HERE, "runtime_config.json")
    _rc: dict = {}
    if os.path.exists(_rc_path):
        try:
            _rc = json.load(open(_rc_path, encoding="utf-8"))
        except Exception:
            _rc = {}
    for _k in ("KB_LLM_API_KEY", "KB_LLM_BASE_URL", "KB_LLM_MODEL"):
        if _rc.get(_k):
            os.environ[_k] = str(_rc[_k])

# ---- 隔离 store + 离线默认值（必须在 import app 之前设置）----
os.environ.setdefault("KB_GRAPH_STORE", os.path.join(_HERE, "kb_graph_eval.json"))
os.environ.setdefault("KB_MEM_STORE", os.path.join(_HERE, "kb_store_eval.json"))
os.environ.setdefault("KB_GRAPH_BACKEND", "memory")
os.environ.setdefault("KB_STORAGE_BACKEND", "memory")
if _EMBED:
    os.environ["KB_EMBEDDING_BACKEND"] = _EMBED
else:
    os.environ.setdefault("KB_EMBEDDING_BACKEND", "dev")
if _USE_LLM:
    os.environ["KB_LLM_ENABLED"] = "1"
else:
    os.environ.setdefault("KB_LLM_ENABLED", "0")
os.environ.setdefault("KB_AGENT_MODE", "deterministic")
os.environ.setdefault("KB_API_MOCK", "1")
os.environ.setdefault("KB_AUDIT_LOG", "off")  # 评测不污染真实审计日志

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
