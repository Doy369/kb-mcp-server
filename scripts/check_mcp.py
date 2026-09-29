"""真实 MCP 协议端到端自检（P2-9）。

**为什么需要这个脚本**：此前「MCP 工具」只被单测**直接 import 函数**调用——
那是函数测试，不是协议验证。于是「这个 server 能被真实 MCP 客户端连上、
握手、列出工具并调通」一直是个未验证假设。本脚本把它变成一条命令。

做法：用官方 SDK 起一个**真实的 stdio 子进程**（`python -m kb_mcp_server`），
用 `ClientSession` 走完整 JSON-RPC 链路：
    initialize → list_tools → call_tool（检索 / 动作 / 多 agent）
在一个独立进程里完成一次真实问答 + 一次真实动作调用（含确认门），
因此验证的是「MCP 协议层 + 服务层 + 动作副作用落盘」整条链路，
而不是任何 mock。

用法：
    python scripts/check_mcp.py
退出码 0 = 全部通过；非 0 = 有断言失败（供 CI 卡合入）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

# ---- 隔离运行时环境（子进程继承这些变量，绝不碰开发机真实数据）----
_TMP = tempfile.mkdtemp(prefix="kb_mcp_check_")
os.environ["KB_DATA_DIR"] = _TMP
os.environ["KB_MEM_STORE"] = os.path.join(_TMP, "kb_store.json")
os.environ["KB_GRAPH_STORE"] = os.path.join(_TMP, "kb_graph.json")
os.environ["KB_STORAGE_BACKEND"] = "memory"
os.environ["KB_EMBEDDING_BACKEND"] = "dev"      # 不下载 bge 模型
os.environ["KB_GRAPH_BACKEND"] = "memory"
os.environ["KB_GRAPH_ENABLED"] = "1"
os.environ["KB_LLM_ENABLED"] = "0"              # 合成走模板，离线可复现
os.environ["KB_AGENT_MODE"] = "deterministic"
os.environ["KB_API_MOCK"] = "1"
os.environ["KB_AUDIT_LOG"] = os.path.join(_TMP, "kb_audit.jsonl")
os.environ["KB_ACTION_LOG"] = os.path.join(_TMP, "kb_actions.jsonl")
os.environ["KB_AGENT_ACTIONS"] = "1"            # 开启动作执行者，验证动作接入编排
os.environ["KB_AGENT_HITL"] = "1"              # 验证「待确认动作」并入人工队列
os.environ["KB_CHUNK_STRATEGY"] = "fine"

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

_FAILURES: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" —— {extra}" if extra else ""))
        _FAILURES.append(label)


def payload(res) -> object:
    """把 call_tool 的返回统一取成 Python 对象（兼容 structuredContent / JSON 文本）。"""
    sc = getattr(res, "structuredContent", None)
    if sc:
        # FastMCP 对非对象返回值会包一层 {"result": ...}
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    parts = []
    for c in (getattr(res, "content", None) or []):
        t = getattr(c, "text", None)
        if t:
            parts.append(t)
    text = "\n".join(parts).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 - 非 JSON 返回（如 ping 的 "ok"）原样返回
        return text


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "kb_mcp_server"],
        env=dict(os.environ),
        cwd=_ROOT,
    )

    print("[check_mcp] 启动真实 stdio 子进程并握手…")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = init.serverInfo
            print(f"\n=== 1) 协议握手 ===")
            check(bool(getattr(info, "name", "")), f"initialize 成功：{info.name} {getattr(info, 'version', '')}")

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            print(f"\n=== 2) 工具清单（{len(names)} 个）===")
            expected = {
                "ping", "ingest_document", "search_knowledge", "ask_with_live_data",
                "list_documents", "delete_document",
                "graph_query", "graph_expand", "graph_paths", "graph_entities",
                "graph_stats", "graph_rebuild",
                "multi_agent_ask", "agent_status",
            }
            check(expected <= names, "既有 14 个工具全部在线",
                  f"缺失 {sorted(expected - names)}")
            check({"list_actions", "run_action"} <= names,
                  "P2-9 动作工具已注册（list_actions / run_action）",
                  f"缺失 {sorted({'list_actions', 'run_action'} - names)}")

            print("\n=== 3) 只读工具调用 ===")
            check(payload(await session.call_tool("ping", {})) == "ok", "ping 返回 ok")

            ing = payload(await session.call_tool("ingest_document", {
                "doc_id": "mcp_check_doc",
                "text": "物流配送超时：48 小时内未发货，客户可申请全额退款。"
                        "退款申请需人工确认后执行。工单优先级 P0 需 15 分钟内响应。",
            }))
            check(isinstance(ing, dict) and ing.get("chunks", 0) > 0,
                  f"ingest_document 写入片段：{(ing or {}).get('chunks')}", str(ing))

            hits = payload(await session.call_tool("search_knowledge",
                                                   {"query": "物流超时怎么赔偿", "top_k": 3}))
            check(isinstance(hits, list) and len(hits) > 0,
                  f"search_knowledge 召回 {len(hits) if isinstance(hits, list) else 0} 个片段")

            answered = payload(await session.call_tool("ask_with_live_data",
                                                      {"question": "物流超时怎么赔偿"}))
            check(isinstance(answered, dict) and bool(answered.get("answer")),
                  "ask_with_live_data 产出答复")
            check("trace_id" in (answered or {}), "答复含 trace_id（可观测性契约）")

            print("\n=== 4) 动作型工具：清单 + 风险分级 ===")
            acts = payload(await session.call_tool("list_actions", {}))
            tool_map = {t["name"]: t for t in ((acts or {}).get("actions") or [])}
            check(set(tool_map) == {"create_ticket", "update_order",
                                    "request_refund", "list_tickets"},
                  f"动作清单完整：{sorted(tool_map)}")
            check(tool_map.get("request_refund", {}).get("risk") == "destructive"
                  and tool_map["request_refund"]["requires_confirm"] is True,
                  "request_refund 被正确定性为 destructive + 需确认")
            check(tool_map.get("list_tickets", {}).get("risk") == "read",
                  "list_tickets 被正确定性为 read")
            check(bool((acts or {}).get("enabled")) is True,
                  "动作执行者已随 KB_AGENT_ACTIONS=1 开启")

            print("\n=== 5) 确认门：destructive 未确认绝不执行 ===")
            r1 = payload(await session.call_tool("run_action", {
                "action": "request_refund",
                "params": {"order_id": "SO123", "amount": 88, "reason": "物流超时"},
            }))
            check(isinstance(r1, dict) and r1.get("status") == "needs_confirmation",
                  "未确认的退款被拦在待确认（未执行）", str(r1))
            check(not (r1 or {}).get("result"), "待确认动作没有产生业务结果（确实没执行）")

            r2 = payload(await session.call_tool("run_action", {
                "action": "request_refund",
                "params": {"order_id": "SO123", "amount": 88, "reason": "物流超时"},
                "confirmed": True,
            }))
            check(isinstance(r2, dict) and r2.get("status") == "executed"
                  and (r2.get("result") or {}).get("refund_id"),
                  f"确认后执行成功：{(r2 or {}).get('result', {}).get('refund_id')}", str(r2))

            r3 = payload(await session.call_tool("run_action", {
                "action": "create_ticket",
                "params": {"subject": "紧急故障", "priority": "P0"},
            }))
            check(isinstance(r3, dict) and r3.get("status") == "executed"
                  and (r3.get("result") or {}).get("sla_response") == "15 分钟",
                  "write 动作自动执行且 SLA 联动正确", str(r3))

            r4 = payload(await session.call_tool("run_action", {
                "action": "create_ticket", "params": {"priority": "P0"},
            }))
            check(isinstance(r4, dict) and r4.get("status") == "rejected"
                  and "subject" in (r4.get("missing") or []),
                  "缺必填参数被拒（参数契约生效）", str(r4))

            r5 = payload(await session.call_tool("run_action",
                                                {"action": "不存在的动作"}))
            check(isinstance(r5, dict) and r5.get("status") == "rejected",
                  "未知动作被拒且回带可用清单", str(r5))

            print("\n=== 6) 动作审计：副作用真的落盘了 ===")
            listed = payload(await session.call_tool("run_action",
                                                    {"action": "list_tickets"}))
            listed_res = (listed or {}).get("result") or {}
            check(listed_res.get("count", 0) >= 1,
                  f"list_tickets 能回读出前面建的工单（count={listed_res.get('count')}）",
                  str(listed)[:300])
            acts2 = payload(await session.call_tool("list_actions", {}))
            recent = (acts2 or {}).get("recent") or []
            check(len(recent) >= 4, f"动作审计累计 {len(recent)} 条")
            check(any(r.get("status") == "needs_confirmation" for r in recent),
                  "被拦下的退款同样留痕（合规要求）")

            print("\n=== 7) 多 agent 经 MCP 触发动作（含 HITL 闭环）===")
            multi = payload(await session.call_tool("multi_agent_ask", {
                "question": "帮我退款 88 元，订单 SO123",
            }))
            trace_agents = [t.get("agent") for t in
                            ((multi or {}).get("agents") or {}).get("trace") or []]
            check("ActionAgent" in trace_agents,
                  f"ActionAgent 参与编排：{trace_agents}")
            check(any(a.get("status") == "needs_confirmation"
                      for a in ((multi or {}).get("actions") or [])),
                  "不可逆动作停在待确认（未被自动确认）")
            check((multi or {}).get("pending_human") is True,
                  "待确认动作并入人工队列（pending_human=true）")
            check("【执行动作】" in ((multi or {}).get("answer") or ""),
                  "合成答复含【执行动作】段")

            st = payload(await session.call_tool("agent_status", {}))
            check(len((st or {}).get("agents") or []) == 5,
                  "agent_status 对外契约仍是 5 个骨架/worker 成员")
            check("actions" in (st or {}), "agent_status 回带动作层概况")

    print("\n" + ("=" * 56))
    if _FAILURES:
        print(f"[FAIL] {len(_FAILURES)} 项未通过：")
        for f in _FAILURES:
            print(f"  · {f}")
        return 1
    print("[OK] MCP 协议端到端全部通过（握手 / 工具 / 动作 / 审计 / 多 agent）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
