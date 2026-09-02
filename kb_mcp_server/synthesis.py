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


def _confidence(top_score: float | None) -> tuple[str, float]:
    if top_score is None:
        return ("低", 0.0)
    if top_score >= 0.35:
        return ("高", top_score)
    if top_score >= 0.20:
        return ("中", top_score)
    return ("低", top_score)


def _graph_lines(graph_facts: dict | None, limit: int = 5) -> list[str]:
    """把图谱事实渲染成可读路径行：路径本身就是可解释证据。"""
    facts = (graph_facts or {}).get("facts") or []
    return [f["path"] for f in facts[:limit] if f.get("path")]


def template_synthesis(question: str, hits: list[dict], live_cards: list[dict],
                       graph_facts: dict | None = None) -> tuple[str, str]:
    """返回 (summary 摘要, detail 详情)。"""
    top = hits[0] if hits else None
    summary = _clean_content(top["content"])[:160] if top else ""
    graph_lines = _graph_lines(graph_facts)

    parts: list[str] = []
    if live_cards:
        parts.append("【实时数据】")
        for c in live_cards:
            if c["type"] == "order":
                parts.append(f"订单 {c['order_id']}：{c['status']}（{c.get('carrier', '')} 预计 {c.get('eta', '')}）")
            elif c["type"] == "inventory":
                parts.append(f"SKU {c['sku']}：库存 {c['stock']}（{c.get('warehouse', '')}）")
            elif c["type"] == "prompt":
                parts.append(f"{c['adapter']}：{c['note']}")

    if graph_lines:
        parts.append("【关系路径】")
        for i, line in enumerate(graph_lines, 1):
            parts.append(f"{i}. {line}")

    if not summary and not live_cards and not graph_lines:
        summary = "未在知识库中找到相关片段，建议补充知识或转人工客服。"

    if hits:
        parts.append("【知识依据】")
        for i, h in enumerate(hits, 1):
            parts.append(f"{i}. {_clean_content(h['content'])}")

    detail = "\n".join(parts) if parts else summary
    return summary, detail


def _llm_synthesize(question: str, hits: list[dict], live_cards: list[dict], history: list[dict] | None = None,
                    graph_facts: dict | None = None) -> str | None:
    """调用本地 LLM 合成自然语言答复；任何异常返回 None（交由模板回退）。"""
    c = _llm_cfg()
    ctx = "\n".join(f"- {_clean_content(h['content'])}" for h in hits)
    live_txt = "\n".join(
        f"- 订单 {c['order_id']}：{c['status']}" if c["type"] == "order"
        else f"- SKU {c['sku']}：库存 {c['stock']}" if c["type"] == "inventory"
        else f"- {c['adapter']}：{c['note']}"
        for c in live_cards
    )
    graph_txt = "\n".join(f"- {line}" for line in _graph_lines(graph_facts))
    prompt = (
        "你是企业 B2B 客服助手。仅依据给定的知识片段、关系事实与实时数据，用简洁中文回答用户问题，"
        "不要编造信息。关系事实来自知识图谱，其路径即为判断依据，可在答复中说明推理链路。\n\n"
        f"用户问题：{question}\n\n知识片段：\n{ctx}\n\n"
        f"关系事实：\n{graph_txt}\n\n实时数据：\n{live_txt}\n\n答复："
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
               history: list[dict] | None = None, graph_facts: dict | None = None) -> dict:
    """入口：装配最终答复。语义侧（hits）+ 关系侧（graph_facts）+ 实时数据（live）融合。"""
    c = _llm_cfg()
    live_cards = normalize_live(live)
    summary, detail = template_synthesis(question, hits, live_cards, graph_facts)
    method = "template"

    if c["enabled"]:
        llm_text = _llm_synthesize(question, hits, live_cards, history=history, graph_facts=graph_facts)
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

    return {
        "question": question,
        "answer": detail,
        "summary": summary,
        "sources": [{"doc_id": h["doc_id"], "score": round(h["score"], 4)} for h in hits],
        "live_data": live,
        "live_cards": live_cards,
        "graph_entities": (graph_facts or {}).get("entities", []),
        "graph_facts": (graph_facts or {}).get("facts", []),
        "graph_paths": _graph_lines(graph_facts),
        "confidence": {"label": conf_label, "score": round(conf_score, 4) if conf_score else 0.0},
        "synthesis_method": method,
        "trace_id": tid,
        "latency_ms": 0,
    }
    # P1-5 护栏 seam：默认直通；接入真实 Guardrail 后此处自动生效（置信度拦截/人工复核）
    return apply_guardrail(out)
