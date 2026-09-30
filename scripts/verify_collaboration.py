"""P1-4 动态协作真实链路联调：**真实 worker**（真检索 / 真图谱 / 真适配器 / 真合成），
只有 LLM 换成可编程替身。

为什么需要这个脚本（而不是只跑单测）：
单测里 worker 全是替身，验证的是「编排逻辑对不对」；这里反过来——worker 全是真的，
验证的是「协作逻辑接进真实链路后**是否真的发生**」。两类证据缺一不可：
- 只有单测 → 「代码写了、但没人证明它在真实链路上生效」；
- 只有这个脚本 → 「跑起来了、但边界条件没人钉住」。

覆盖场景（每条都落成断言，不再是"看着对"）：
  A 单轮 + 动态分解：只叫 Retriever，而不是固定三件套
  B 多轮协商：评审发现缺口 → 补轮叫 LiveData，实时数据真的进了答复
  C 降级：LLM 输出非法 → 回退确定性规划（全跑），链路不断
  D 人工介入：护栏未通过 → pending_human
  E 依赖分层：LLM 声明 depends_on → 同一轮内分两层、层间串行
  F 双向协商：worker **主动委托** peer → 补轮 source=delegation，
    且被委托方在**没有证据时明确 declined**（而不是沉默地算作"已尽力"）
  G 无主请求：委托一个没人具备的能力 → 当场关账，不空转补轮

沿用本方其它 check 脚本的自诊断约定：阶段隔离异常、报告无论成败都落盘、
`::error::` / `::notice::` 注解（CI 日志读不到时仍可定位）。

用法：python scripts/verify_collaboration.py
退出码 0 = 全部断言通过。
"""

import glob
import json
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

# ---- 隔离运行时环境（必须在 import app 之前；config 在导入期求值 DATA_DIR 等常量）----
os.environ["KB_DATA_DIR"] = os.path.join(_ROOT, ".collab_runtime")
os.environ["KB_MEM_STORE"] = os.path.join(_ROOT, "kb_store_collab.json")
os.environ["KB_GRAPH_STORE"] = os.path.join(_ROOT, "kb_graph_collab.json")
os.environ["KB_GRAPH_BACKEND"] = "memory"
os.environ["KB_STORAGE_BACKEND"] = "memory"
os.environ["KB_EMBEDDING_BACKEND"] = "dev"
os.environ["KB_LLM_ENABLED"] = "0"      # 合成走模板，离线可复现
os.environ["KB_AGENT_MODE"] = "deterministic"
os.environ["KB_API_MOCK"] = "1"
os.environ["KB_AUDIT_LOG"] = "off"

for _f in ("kb_store_collab.json", "kb_graph_collab.json"):
    _p = os.path.join(_ROOT, _f)
    if os.path.exists(_p):
        os.remove(_p)

import app  # noqa: E402  触发模块级初始化
from kb_mcp_server.adapters import reload_adapters  # noqa: E402
from kb_mcp_server.agents.base import AgentContext  # noqa: E402
from kb_mcp_server.agents.orchestrator import Orchestrator  # noqa: E402
from kb_mcp_server.agents.workers import RetrieverAgent  # noqa: E402
from kb_mcp_server.config import set_cfg  # noqa: E402
from kb_mcp_server.extensions import (  # noqa: E402
    AgentRegistry,
    ConfidenceGuardrail,
    LLMPlanner,
    install_default_guardrail,
    set_guardrail,
)

CHECKS: list = []
FACTS: dict = {}


# --------------------------------------------------------------------------- #
# 断言 / 输出 / 注解
# --------------------------------------------------------------------------- #
def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), str(detail)))
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(ok)


def _oneline(s) -> str:
    """GitHub 注解要求单行且不宜过长，否则整条被丢弃。"""
    return str(s).replace("\r", " ").replace("\n", " ")[:400]


def gha(kind: str, title: str, msg: str) -> None:
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    print(f"::{kind} title={_oneline(title)}::{_oneline(msg)}")


def phase(title: str, fn) -> None:
    """阶段隔离：一个阶段炸了不能带走后面的阶段、更不能带走报告。"""
    print(f"\n=== {title} ===")
    try:
        fn()
    except BaseException as e:  # noqa: BLE001
        check(f"{title} 执行", False, f"{type(e).__name__}: {e}")
        print("".join(traceback.format_exc().splitlines(keepends=True)[-12:]), file=sys.stderr)


# --------------------------------------------------------------------------- #
# 语料 / 编排器
# --------------------------------------------------------------------------- #
class _DelegatingRetriever(RetrieverAgent):
    """真实检索 + 一个**委托钩子**。

    用来演示「worker 主动向共同体求助」——检索本身是真的（真向量 + 真 BM25），
    只是在跑完之后挂一条委托。为什么不改真实 Retriever 的默认行为：
    默认行为一变，所有既有链路的轮次都会跟着变，那是另一个开关该管的事。
    """

    def __init__(self, capability: str = ""):
        self.delegates_to = capability

    def run(self, ctx: AgentContext):
        res = super().run(ctx)
        if self.delegates_to:
            ctx.request_help(self.name, self.delegates_to,
                             ask="检索到的订单实体需要实时状态")
        return res


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


def make_orch(llm, retriever=None) -> Orchestrator:
    """真实 Orchestrator，只把规划器的 LLM 换成替身（可选替换检索者的委托钩子）。"""
    o = Orchestrator(mode="deterministic")
    if retriever is not None:
        o.retriever = retriever
        # 换了成员就必须重建成员表，否则规划器看到的还是旧实例
        o.registry = AgentRegistry([o.graph_builder, o.retriever, o.graph_reasoner,
                                    o.live_data, o.synthesizer])
    o.planner = LLMPlanner(o.retriever, o.graph_reasoner, o.live_data,
                           registry=o.registry, llm=llm)
    return o


def show(tag: str, out: dict) -> None:
    """人读的分工叙事（断言之外的可解释性，演示时直接用）。"""
    coll = out.get("collaboration") or {}
    rounds = coll.get("rounds") or []
    print(f"\n--- {tag} ---")
    print(f"  问题：{out.get('question')}")
    print(f"  规划器={coll.get('planner')}  轮次={len(rounds)}  协商={coll.get('negotiated')}")
    for r in rounds:
        extra = ""
        c = r.get("critique")
        if c:
            verdict = "需补轮" if c.get("need_more") else "证据充分·收束"
            extra += f"  → 协商[{verdict}] 缺: {c.get('missing')}"
        if r.get("layers"):
            extra += f"  分层: {r['layers']}"
        if r.get("dep_cycles"):
            extra += f"  **依赖成环降级**: {r['dep_cycles']}"
        print(f"    第{r.get('round')}轮 [{r.get('source')}] {' + '.join(r.get('agents') or [])}{extra}")
        for s in (r.get("subtasks") or []):
            dep = f" ← 依赖 {s['depends_on']}" if s.get("depends_on") else ""
            print(f"        · {s.get('agent')}（{s.get('capability')}）：{s.get('reason')}{dep}")
        for e in (r.get("replies") or []):
            print(f"        ↩ {e.get('by')} 应答 {e.get('id')}：{e.get('status')}（{e.get('note')}）")
    neg = coll.get("negotiation")
    if neg:
        print(f"  协商账本：请求 {neg['requests']} / 应答 {neg['replies']}"
              f"  委托={neg['delegated']}  「别再提」={neg['declined']}")
    print(f"  来源片段={len(out.get('sources') or [])}  实时={len(out.get('live_data') or [])}"
          f"  置信度={(out.get('confidence') or {}).get('label')}  合成={out.get('synthesis_method')}")
    if "pending_human" in out:
        hr = out.get("human_review") or {}
        print(f"  HITL：pending_human={out.get('pending_human')}  status={hr.get('status')}  "
              f"reason={hr.get('reason')}")
    print(f"  答复：{(out.get('answer') or '')[:72]}...")


# 只叫 Retriever（纯知识问答）；LLM 替身，避免真发网络
_ONLY_RETRIEVAL = lambda p: (  # noqa: E731
    '{"subtasks":[{"agent":"Retriever","reason":"纯知识问答，只需语义检索"}],'
    '"reason":"问题只涉及政策条款，无需图谱与实时数据"}')
_GARBAGE = lambda p: "抱歉，我不太确定你指的是什么。"  # noqa: E731
_DEP_CHAIN = lambda p: (  # noqa: E731
    '{"subtasks":[{"agent":"Retriever","reason":"先检索定位实体"},'
    '{"agent":"LiveData","reason":"再按实体查实时状态","depends_on":["Retriever"]}],'
    '"reason":"先检索、后实时（有依赖）"}')


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
def _stage_single_round() -> None:
    set_cfg("KB_AGENT_MAX_ROUNDS", "1")
    out = make_orch(_ONLY_RETRIEVAL).ask("如何申请退货？", top_k=3)
    show("A · 单轮 + 动态分解", out)
    coll = out["collaboration"]
    check("A 动态分解：只叫 Retriever（不是固定三件套）",
          coll["executed"] == ["Retriever"], str(coll["executed"]))
    check("A 恰好 1 轮", len(coll["rounds"]) == 1, str(len(coll["rounds"])))
    check("A 未发生协商（无协商字段）",
          coll["negotiated"] is False and "negotiation" not in coll, str(list(coll)))
    check("A 答复非空", bool(out.get("answer")), (out.get("answer") or "")[:40])


def _stage_negotiation() -> None:
    set_cfg("KB_AGENT_MAX_ROUNDS", "2")
    out = make_orch(_ONLY_RETRIEVAL).ask("我的订单现在到哪了？", order_id="SO123", top_k=3)
    show("B · 多轮协商（补齐实时数据能力）", out)
    coll = out["collaboration"]
    check("B 评审发现缺口 → 补轮", coll["negotiated"] is True and len(coll["rounds"]) == 2,
          f"rounds={len(coll['rounds'])}")
    check("B 补轮来源 = negotiation",
          coll["rounds"][-1]["source"] == "negotiation", coll["rounds"][-1]["source"])
    check("B 补轮叫来 LiveData", coll["rounds"][-1]["agents"] == ["LiveData"],
          str(coll["rounds"][-1]["agents"]))
    check("B 实时数据真的进了答复", len(out.get("live_data") or []) > 0,
          f"live={len(out.get('live_data') or [])}")
    neg = coll.get("negotiation") or {}
    led = neg.get("ledger") or []
    reps = [e for e in led if e.get("kind") == "reply"]
    check("B 账本记下请求与应答", neg.get("requests", 0) >= 1 and neg.get("replies", 0) >= 1,
          f"requests={neg.get('requests')} replies={neg.get('replies')}")
    check("B 被点名方应答 provided（真的产出了证据）",
          bool(reps) and reps[0].get("status") == "provided",
          json.dumps(reps[:1], ensure_ascii=False))
    FACTS["b_rounds"] = len(coll["rounds"])


def _stage_fallback() -> None:
    set_cfg("KB_AGENT_MAX_ROUNDS", "1")
    out = make_orch(_GARBAGE).ask("退货运费谁承担？", top_k=3)
    show("C · LLM 输出非法（回退确定性规划）", out)
    coll = out["collaboration"]
    check("C LLM 非法输出 → 回退确定性规划", coll["planner"] == "fallback", coll["planner"])
    check("C 回退后仍跑满三个 worker",
          set(coll["executed"]) == {"Retriever", "GraphReasoner", "LiveData"},
          str(sorted(coll["executed"])))
    check("C 链路未断（答复非空）", bool(out.get("answer")), "")


def _stage_hitl() -> None:
    set_cfg("KB_AGENT_MAX_ROUNDS", "1")
    set_cfg("KB_AGENT_HITL", "1")
    set_guardrail(ConfidenceGuardrail(min_confidence=0.99))   # 抬高阈值逼出「需复核」
    try:
        out = make_orch(_ONLY_RETRIEVAL).ask("如何申请退货？", top_k=3)
        show("D · 人工介入（护栏未过 → 挂起）", out)
        check("D 护栏未过 → pending_human",
              out.get("pending_human") is True
              and (out.get("human_review") or {}).get("status") == "pending",
              str(out.get("human_review")))
    finally:
        set_cfg("KB_AGENT_HITL", "0")
        set_guardrail(None)
        install_default_guardrail()   # 还原默认护栏，避免影响同进程后续调用


def _stage_dependency_layers() -> None:
    """E · 依赖分层：真实 worker，仅由 LLM 替身声明依赖。"""
    set_cfg("KB_AGENT_MAX_ROUNDS", "1")
    out = make_orch(_DEP_CHAIN).ask("订单到哪了", order_id="SO123", top_k=3)
    show("E · 子任务依赖分层（Retriever → LiveData）", out)
    rec = out["collaboration"]["rounds"][0]
    check("E 依赖被解析成 subtask.depends_on",
          any(s.get("depends_on") == ["Retriever"] for s in (rec.get("subtasks") or [])),
          str(rec.get("subtasks")))
    check("E 分两层：Retriever → LiveData",
          rec.get("layers") == [["Retriever"], ["LiveData"]], str(rec.get("layers")))
    check("E 扁平化顺序 = 计划顺序", rec["agents"] == ["Retriever", "LiveData"],
          str(rec["agents"]))
    check("E 无依赖成环", "dep_cycles" not in rec, str(rec.get("dep_cycles")))


def _stage_delegation() -> None:
    """F · 双向协商：worker 主动委托（不是评审点名）。

    注意这里刻意选了一个「委托了、对方也确实跑了、但确实没证据」的场景：
    问题里没有实时诉求、也没有 order_id，所以 LiveData 真的取不到东西。
    它必须**明确拒绝**（declined），而不是沉默着让人以为「已尽力」——
    这正是本轮新增的那条规则在真实链路上的证据。真要演示「委托拿到证据」，
    见场景 B（那里的 provided 是评审点名后真的产出）。
    """
    set_cfg("KB_AGENT_MAX_ROUNDS", "2")
    out = make_orch(_ONLY_RETRIEVAL, retriever=_DelegatingRetriever("live")).ask(
        "如何申请退货？", top_k=3)          # 问题不含实时诉求 → 评审不会点名 live
    show("F · worker 主动委托 peer（delegation）", out)
    coll = out["collaboration"]
    check("F 委托触发补轮且来源 = delegation",
          len(coll["rounds"]) == 2 and coll["rounds"][-1]["source"] == "delegation",
          f"rounds={[(r['round'], r['source']) for r in coll['rounds']]}")
    check("F 被委托方 = LiveData", coll["rounds"][-1]["agents"] == ["LiveData"],
          str(coll["rounds"][-1]["agents"]))
    neg = coll.get("negotiation") or {}
    led = neg.get("ledger") or []
    reqs = [e for e in led if e.get("kind") == "request"]
    reps = [e for e in led if e.get("kind") == "reply"]
    check("F 请求由 worker 发起（不是评审）",
          bool(reqs) and reqs[0].get("by") == "Retriever"
          and reqs[0].get("capability") == "live",
          json.dumps(reqs[:1], ensure_ascii=False))
    check("F 被委托方诚实应答：没证据就 declined（沉默不算数）",
          bool(reps) and reps[0].get("by") == "LiveData"
          and reps[0].get("status") == "declined",
          json.dumps(reps[:1], ensure_ascii=False))
    check("F 被拒能力进「别再提」名单", "live" in (neg.get("declined") or []),
          str(neg.get("declined")))
    check("F delegated 标记为真", neg.get("delegated") is True, str(neg.get("delegated")))
    check("F 账本不留悬空请求",
          {e["id"] for e in led if e.get("kind") == "reply"} >=
          {e["id"] for e in led if e.get("kind") == "request"},
          json.dumps(led, ensure_ascii=False)[:160])


def _stage_unowned_request() -> None:
    """G · 请求一个没人具备的能力 → 当场关账，不空转补轮。"""
    set_cfg("KB_AGENT_MAX_ROUNDS", "3")
    out = make_orch(_ONLY_RETRIEVAL,
                    retriever=_DelegatingRetriever("nonexistent-capability")).ask(
        "如何申请退货？", top_k=3)
    show("G · 无主请求（当场关账）", out)
    coll = out["collaboration"]
    check("G 不产生空转补轮", len(coll["rounds"]) == 1, str(len(coll["rounds"])))
    led = (coll.get("negotiation") or {}).get("ledger") or []
    reps = [e for e in led if e.get("kind") == "reply"]
    check("G 请求被标为 unavailable",
          bool(reps) and reps[0].get("status") == "unavailable",
          json.dumps(reps[:1], ensure_ascii=False))
    check("G 该能力进入「别再提」名单",
          "nonexistent-capability" in ((coll.get("negotiation") or {}).get("declined") or []),
          str((coll.get("negotiation") or {}).get("declined")))


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def _write_report(failed: list) -> None:
    """报告无论成败都要落盘——否则 CI 失败时连「失败在哪一步」都拿不到。"""
    report = {
        "passed": len(CHECKS) - len(failed),
        "total": len(CHECKS),
        "failures": failed,
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS],
        "facts": FACTS,
    }
    rp = os.path.join(_ROOT, "collaboration_report.json")
    try:
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n[collab-verify] 报告已写入：{rp}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 报告写入失败：{e}", file=sys.stderr)


def main() -> int:
    print("=" * 68)
    print("P1-4 动态协作真实链路联调（真实 worker / 替身 LLM）")
    print("=" * 68)
    try:
        n = seed()
        print(f"[collab-verify] 语料已载入：{n} 个片段")
        FACTS["chunks"] = n
    except BaseException as e:  # noqa: BLE001
        check("语料播种", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        _write_report([n for n, ok, _ in CHECKS if not ok])
        return 1

    for title, fn in (
        ("A 单轮 + 动态分解", _stage_single_round),
        ("B 多轮协商", _stage_negotiation),
        ("C LLM 非法回退", _stage_fallback),
        ("D 人工介入", _stage_hitl),
        ("E 依赖分层", _stage_dependency_layers),
        ("F worker 主动委托", _stage_delegation),
        ("G 无主请求关账", _stage_unowned_request),
    ):
        phase(title, fn)

    failed = [n for n, ok, _ in CHECKS if not ok]
    _write_report(failed)

    for name, ok, detail in CHECKS:
        if not ok:
            gha("error", f"协作联调断言失败：{name}", detail)
    gha("notice", "协作联调结论（真跑凭据）",
        f"共 {len(CHECKS)} 项，失败 {len(failed)} 项；语料 {FACTS.get('chunks')} 片段；"
        f"多轮场景轮次 {FACTS.get('b_rounds')}")

    print("-" * 68)
    if failed:
        print(f"[FAIL] 协作联调未通过（{len(failed)}/{len(CHECKS)} 项失败）：{failed}",
              file=sys.stderr)
    else:
        print(f"[OK] 协作联调全部通过（{len(CHECKS)} 项）：单轮分解 / 多轮协商 / "
              "LLM 回退 / 人工介入 / 依赖分层 / worker 主动委托 / 无主请求关账")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
