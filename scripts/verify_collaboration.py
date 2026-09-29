"""本地验证 P1-4 动态协作链路：真实检索 + 真实合成，只有 LLM 用可编程替身。

为什么需要这个脚本（而不是只跑单测）：
单测里 worker 全是替身，验证的是「编排逻辑对不对」；这里反过来——
worker 全是真的（真向量检索 / 真图谱 / 真 mock 适配器 / 真合成），
只有 LLM 被替换成确定性替身，验证的是「协作逻辑接进真实链路后是否真的发生」。

四个场景覆盖 P1-4 的三件事 + 一条降级路径：
A 单轮：LLM 动态分解（只叫 Retriever）——证明「按问题组队」生效，不是固定全跑
B 多轮：同样只叫 Retriever，但问题含实时诉求 → critic 发现缺口 → 补轮叫 LiveData
C 降级：LLM 输出非法 → 回退确定性规划（全跑），链路不断
D 人工介入：护栏未通过 → pending_human

用法：python scripts/verify_collaboration.py
"""

import glob
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

# ---- 隔离运行时环境（必须在 import app 之前）----
os.environ["KB_DATA_DIR"] = os.path.join(_ROOT, ".eval_runtime")
os.environ.setdefault("KB_MEM_STORE", os.path.join(_ROOT, "kb_store_collab.json"))
os.environ.setdefault("KB_GRAPH_STORE", os.path.join(_ROOT, "kb_graph_collab.json"))
os.environ.setdefault("KB_GRAPH_BACKEND", "memory")
os.environ.setdefault("KB_STORAGE_BACKEND", "memory")
os.environ.setdefault("KB_EMBEDDING_BACKEND", "dev")
os.environ.setdefault("KB_LLM_ENABLED", "0")      # 合成走模板，离线可复现
os.environ.setdefault("KB_AGENT_MODE", "deterministic")
os.environ.setdefault("KB_API_MOCK", "1")
os.environ.setdefault("KB_AUDIT_LOG", "off")

for _f in ("kb_store_collab.json", "kb_graph_collab.json"):
    _p = os.path.join(_ROOT, _f)
    if os.path.exists(_p):
        os.remove(_p)

import app  # noqa: E402  触发模块级初始化
from kb_mcp_server.adapters import reload_adapters  # noqa: E402
from kb_mcp_server.agents.orchestrator import Orchestrator  # noqa: E402
from kb_mcp_server.config import set_cfg  # noqa: E402
from kb_mcp_server.extensions import (  # noqa: E402
    ConfidenceGuardrail,
    LLMPlanner,
    install_default_guardrail,
    set_guardrail,
)


def seed() -> int:
    """播种与演示一致的语料（复用 eval_run 的口径）。"""
    for doc_id, text in app.SAMPLE.items():
        app._ingestor.ingest_text(doc_id, text)
    for folder in ("samples", "test-docs"):
        fdir = os.path.join(_ROOT, folder)
        if not os.path.isdir(fdir):
            continue
        for fp in sorted(glob.glob(os.path.join(fdir, "**", "*"), recursive=True)):
            if not os.path.isfile(fp) or not fp.lower().endswith((".md", ".txt", ".docx")):
                continue
            name = os.path.basename(fp)
            doc_id = f"{folder}_{os.path.splitext(name)[0]}"
            with open(fp, "rb") as fh:
                app._ingestor.ingest_file(doc_id, name, fh.read())
    set_cfg("KB_API_MOCK", "1")
    reload_adapters()
    return app._store.count()


def make_orch(llm) -> Orchestrator:
    """真实 Orchestrator，只把规划器的 LLM 换成替身。"""
    o = Orchestrator(mode="deterministic")
    o.planner = LLMPlanner(o.retriever, o.graph_reasoner, o.live_data,
                           registry=o.registry, llm=llm)
    return o


def show(tag: str, out: dict) -> None:
    coll = out.get("collaboration") or {}
    rounds = coll.get("rounds") or []
    print(f"\n=== {tag} ===")
    print(f"  问题：{out.get('question')}")
    print(f"  规划器={coll.get('planner')}  轮次={len(rounds)}  协商={coll.get('negotiated')}")
    for r in rounds:
        c = r.get("critique")
        extra = ""
        if c:
            verdict = "需补轮" if c.get("need_more") else "证据充分·收束"
            extra = f"  → 协商[{verdict}] 缺: {c.get('missing')}"
        print(f"    第{r.get('round')}轮 [{r.get('source')}] {' + '.join(r.get('agents') or [])}{extra}")
        for s in (r.get("subtasks") or []):
            print(f"        · {s.get('agent')}（{s.get('capability')}）：{s.get('reason')}")
    print(f"  来源片段={len(out.get('sources') or [])}  置信度={(out.get('confidence') or {}).get('label')}"
          f"  合成={out.get('synthesis_method')}")
    if "pending_human" in out:
        hr = out.get("human_review") or {}
        print(f"  HITL：pending_human={out.get('pending_human')}  status={hr.get('status')}  "
              f"reason={hr.get('reason')}")
    print(f"  答复：{(out.get('answer') or '')[:72]}...")


def main() -> None:
    print(f"[verify] 语料已载入：{seed()} 个片段")

    only_retrieval = lambda p: (  # noqa: E731
        '{"subtasks":[{"agent":"Retriever","reason":"纯知识问答，只需语义检索"}],'
        '"reason":"问题只涉及政策条款，无需图谱与实时数据"}')
    garbage = lambda p: "抱歉，我不太确定你指的是什么。"  # noqa: E731

    # A · 单轮 + LLM 动态分解：应当只叫 Retriever，而不是固定三件套
    set_cfg("KB_AGENT_MAX_ROUNDS", "1")
    show("A · 单轮 + 动态分解（LLM 只选 Retriever）",
         make_orch(only_retrieval).ask("如何申请退货？", top_k=3))

    # B · 多轮协商：同样只选 Retriever，但问题含实时诉求 → 补轮叫 LiveData
    set_cfg("KB_AGENT_MAX_ROUNDS", "2")
    show("B · 多轮协商（critic 补齐实时数据能力）",
         make_orch(only_retrieval).ask("我的订单现在到哪了？", order_id="SO123", top_k=3))

    # C · LLM 输出非法 → 回退确定性规划（全跑），链路不断
    set_cfg("KB_AGENT_MAX_ROUNDS", "1")
    show("C · LLM 输出非法（回退确定性规划）",
         make_orch(garbage).ask("退货运费谁承担？", top_k=3))

    # D · 人工介入：抬高护栏阈值逼出「需人工复核」→ 挂起等待放行
    set_cfg("KB_AGENT_HITL", "1")
    set_guardrail(ConfidenceGuardrail(min_confidence=0.99))
    try:
        show("D · 人工介入（护栏未过 → 挂起）",
             make_orch(only_retrieval).ask("如何申请退货？", top_k=3))
    finally:
        set_guardrail(None)
        install_default_guardrail()   # 还原默认护栏，避免影响同进程后续调用


if __name__ == "__main__":
    main()
