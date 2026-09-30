"""答案合成层（P5）。

把「检索到的知识片段 + 外部 API 实时数据」装配成结构化、可读的答复。

- template_synthesis：确定性模板合成（离线可用，零依赖）。从最相关片段抽取答案主体，
  按类型把实时数据排到最前，给出来源与置信度。
- 可选本地 LLM 合成：KB_LLM_ENABLED=1 时调用 OpenAI 兼容接口（Ollama/vLLM）做自然语言合成；
  任何失败（无模型 / 网络不通）自动回退模板，保证链路不崩。

输出结构（供 MCP 工具与 Web 控制台共用）：
  { question, answer, summary, sources, live_data, live_cards,
    confidence{label,score}, synthesis_method, trace_id }
"""

import json
import time
import uuid

from kb_mcp_server.adapters import normalize_live
from kb_mcp_server.config import get_cfg
from kb_mcp_server.llmclient import llm_chat
from kb_mcp_server.extensions import apply_guardrail


def _new_trace() -> str:
    return uuid.uuid4().hex[:12]


def _llm_cfg() -> dict:
    """读取 LLM 配置：运行时配置优先于环境变量。"""
    return {
        "enabled": get_cfg("KB_LLM_ENABLED", "0").lower() in ("1", "true", "yes"),
        "model": get_cfg("KB_LLM_MODEL", "qwen2.5:7b"),
        "base_url": get_cfg("KB_LLM_BASE_URL", "http://localhost:11434/v1"),
        "api_key": get_cfg("KB_LLM_API_KEY", ""),
    }


def _clean_content(content: str) -> str:
    """清理片段文本：若以「问题？」开头，去掉问题只留答案。"""
    c = (content or "").strip()
    if "？" in c[:40]:
        idx = c.index("？")
        ans = c[idx + 1:].strip()
        if ans:
            return ans
    return c


def _conf_thresholds() -> tuple[float, float]:
    """置信度分档阈值（高 / 中 边界），按嵌入后端校准。

    dev 哈希嵌入分数带低（相关命中约 0.3-0.4）；bge 分数带整体抬高
    （无关问题也能到 ~0.47，真实问题 0.6-0.85）。固定阈值在 bge 下会把
    垃圾答复也标成「高」，因此分档必须随后端走。可用 KB_CONFIDENCE_HIGH/LOW 覆盖。
    """
    from kb_mcp_server.config import get_settings

    backend = get_settings().embedding_backend
    if backend == "bge":
        hi = float(get_cfg("KB_CONFIDENCE_HIGH", "0.6") or 0.6)
        lo = float(get_cfg("KB_CONFIDENCE_LOW", "0.5") or 0.5)
    else:
        hi = float(get_cfg("KB_CONFIDENCE_HIGH", "0.35") or 0.35)
        lo = float(get_cfg("KB_CONFIDENCE_LOW", "0.2") or 0.2)
    return hi, lo


def _confidence(top_score: float | None) -> tuple[str, float]:
    if top_score is None:
        return ("低", 0.0)
    hi, lo = _conf_thresholds()
    if top_score >= hi:
        return ("高", top_score)
    if top_score >= lo:
        return ("中", top_score)
    return ("低", top_score)


def _graph_lines(graph_facts: dict | None, limit: int = 5) -> list[str]:
    """把图谱事实渲染成可读路径行：路径本身就是可解释证据。"""
    facts = (graph_facts or {}).get("facts") or []
    return [f["path"] for f in facts[:limit] if f.get("path")]


def _live_line(c: dict) -> str:
    """把一张实时卡片转成一行文本。**对未知类型也安全**（全部走 .get）。

    P0-2 加 `degraded` 卡后，原先「else 分支直接取 c['note']」的写法会 KeyError，
    把一次后端不可用升级成 /api/ask 500。这里改为显式分支 + 安全取值。
    """
    t = c.get("type")
    if t == "order":
        return (f"订单 {c.get('order_id')}：{c.get('status')}"
                f"（{c.get('carrier', '')} 预计 {c.get('eta', '')}）")
    if t == "inventory":
        return f"SKU {c.get('sku')}：库存 {c.get('stock')}（{c.get('warehouse', '')}）"
    if t == "not_found":
        # 「查不到」≠「后端挂了」。分开表述，客服才知道该去找单号还是找运维。
        target = c.get("order_id") or c.get("sku") or ""
        return f"{c.get('adapter', '')}：未查询到 {target}（后端明确返回不存在，非故障）"
    if t == "degraded":
        if c.get("short_circuited"):
            why = "熔断开路，未发起调用"
        elif c.get("non_retryable"):
            why = f"请求被后端拒绝，未重试（{c.get('error', '')}）"
        else:
            why = f"尝试 {c.get('attempts', 0)} 次仍失败"
        return f"{c.get('adapter', '')}：实时数据暂不可用（{why}）"
    return f"{c.get('adapter', '')}：{c.get('note', '')}"


def _action_line(a: dict) -> str:
    """把一条动作结果渲染成一行可读文本（P2-9）。**对未知动作类型也安全**。

    三种终态必须能区分清楚，否则用户/客服无法判断「到底做没做」：
    已执行（executed）/ 待确认（needs_confirmation）/ 被拒（rejected）。
    """
    name = str(a.get("action", "") or "")
    res = a.get("result") or {}
    status = a.get("status")
    if status == "executed":
        if name == "create_ticket":
            return (f"已创建工单 {res.get('ticket_id')}（{res.get('priority')}，"
                    f"承诺 {res.get('sla_response')} 响应）：{res.get('subject', '')}")
        if name == "update_order":
            return (f"订单 {res.get('order_id')} 的"
                    f"{res.get('field_label') or res.get('field')}已改为「{res.get('new_value')}」")
        if name == "request_refund":
            return (f"退款申请已提交：{res.get('refund_id')}，订单 {res.get('order_id')}，"
                    f"金额 {res.get('amount')} 元")
        if name == "list_tickets":
            return f"当前工单 {res.get('count', 0)} 条"
        return f"已执行动作 {name}"
    if status == "needs_confirmation":
        given = (a.get("preview") or {}).get("given") or {}
        amt = f" {given.get('amount')} 元" if given.get("amount") not in (None, "") else ""
        oid = f"（订单 {given.get('order_id')}）" if given.get("order_id") else ""
        return f"待确认：{name or '该动作'}{amt}{oid}——不可逆，需人工确认后才会执行"
    return f"未执行 {name or '动作'}：{a.get('error') or '未知原因'}"


def _action_lines(actions: list[dict] | None) -> list[str]:
    return [_action_line(a) for a in (actions or [])]


def template_synthesis(question: str, hits: list[dict], live_cards: list[dict],
                       graph_facts: dict | None = None,
                       actions: list[dict] | None = None) -> tuple[str, str]:
    """返回 (summary 摘要, detail 详情)。"""
    top = hits[0] if hits else None
    summary = _clean_content(top["content"])[:160] if top else ""
    graph_lines = _graph_lines(graph_facts)
    action_lines = _action_lines(actions)

    parts: list[str] = []
    # 动作结果排最前：用户要求「执行某动作」时，做没做才是他关心的第一件事。
    if action_lines:
        parts.append("【执行动作】")
        for line in action_lines:
            parts.append(f"- {line}")

    if live_cards:
        parts.append("【实时数据】")
        for c in live_cards:
            parts.append(_live_line(c))

    if graph_lines:
        parts.append("【关系路径】")
        for i, line in enumerate(graph_lines, 1):
            parts.append(f"{i}. {line}")

    if not summary and not live_cards and not graph_lines and not action_lines:
        summary = "未在知识库中找到相关片段，建议补充知识或转人工客服。"
    elif action_lines and not summary:
        # 没有知识命中时，摘要直接给动作结果，避免「答非所问」的空摘要
        summary = action_lines[0][:160]

    if hits:
        parts.append("【知识依据】")
        for i, h in enumerate(hits, 1):
            parts.append(f"{i}. {_clean_content(h['content'])}")

    detail = "\n".join(parts) if parts else summary
    return summary, detail


def _llm_synthesize(question: str, hits: list[dict], live_cards: list[dict], history: list[dict] | None = None,
                    graph_facts: dict | None = None,
                    actions: list[dict] | None = None) -> str | None:
    """调用本地 LLM 合成自然语言答复；任何异常返回 None（交由模板回退）。"""
    c = _llm_cfg()
    ctx = "\n".join(f"- {_clean_content(h['content'])}" for h in hits)
    live_txt = "\n".join(_live_line(c) for c in live_cards)
    graph_txt = "\n".join(f"- {line}" for line in _graph_lines(graph_facts))
    action_txt = "\n".join(f"- {line}" for line in _action_lines(actions))
    prompt = (
        "你是企业 B2B 客服助手。仅依据给定的知识片段、关系事实与实时数据，用简洁中文回答用户问题，"
        "不要编造信息。关系事实来自知识图谱，其路径即为判断依据，可在答复中说明推理链路。\n\n"
        f"用户问题：{question}\n\n知识片段：\n{ctx}\n\n"
        f"关系事实：\n{graph_txt}\n\n实时数据：\n{live_txt}\n\n"
        "系统动作记录（**只能照实转述，不得承诺未列出的操作**；标注「待确认」的必须提示用户确认后才会执行）：\n"
        f"{action_txt or '（无）'}\n\n答复："
    )
    if history:
        hist_txt = "\n".join(
            f"{'用户' if h.get('role') == 'user' else '助手'}：{h.get('content', '')}"
            for h in history[-6:]
        )
        prompt = (
            "你是企业 B2B 客服助手。\n\n"
            f"对话历史（仅作上下文参考）：\n{hist_txt}\n\n" + prompt
        )
    return llm_chat(prompt, temperature=0.2, max_tokens=400, timeout=15)


def synthesize(question: str, hits: list[dict], live: list[dict], trace_id: str | None = None,
               history: list[dict] | None = None, graph_facts: dict | None = None,
               actions: list[dict] | None = None) -> dict:
    """入口：装配最终答复。语义侧（hits）+ 关系侧（graph_facts）+ 实时数据（live）+ 动作（actions）融合。"""
    c = _llm_cfg()
    live_cards = normalize_live(live)
    summary, detail = template_synthesis(question, hits, live_cards, graph_facts, actions)
    method = "template"

    if c["enabled"]:
        llm_text = _llm_synthesize(question, hits, live_cards, history=history,
                                   graph_facts=graph_facts, actions=actions)
        if llm_text:
            summary = llm_text[:200]
            detail = llm_text
            method = "llm"

    top_score = hits[0]["score"] if hits else None
    conf_label, conf_score = _confidence(top_score)
    # 图谱命中给置信度小幅加成：有结构化路径支撑，比纯向量命中更可信（上限 +0.06，避免喧宾夺主）
    n_facts = len((graph_facts or {}).get("facts") or [])
    if n_facts and conf_score:
        conf_score = min(1.0, conf_score + 0.03 * min(n_facts, 2))
        conf_label, _ = _confidence(conf_score)
    tid = trace_id or _new_trace()

    out = {
        "question": question,
        "answer": detail,
        "summary": summary,
        "sources": [{"doc_id": h["doc_id"], "score": round(h["score"], 4)} for h in hits],
        "live_data": live,
        "live_cards": live_cards,
        "actions": list(actions or []),
        "graph_entities": (graph_facts or {}).get("entities", []),
        "graph_facts": (graph_facts or {}).get("facts", []),
        "graph_paths": _graph_lines(graph_facts),
        "confidence": {"label": conf_label, "score": round(conf_score, 4) if conf_score else 0.0},
        "synthesis_method": method,
        "trace_id": tid,
        "latency_ms": 0,
    }
    # P1-5 护栏：低置信度答复标记「需人工复核」（见 extensions.ConfidenceGuardrail）
    return apply_guardrail(out)
