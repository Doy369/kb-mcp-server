"""动作型工具的实现层（P2-9）：让 agent 从「只会答」变成「能执行」。

与 `extensions.py` 的分工：
- `extensions.ActionTool` / `ActionToolRegistry` 是**接口**（可被别的实现替换）；
- 本模块是**默认实现**，并把三条企业级底线收口在 `ActionRunner` 里：

1. **参数校验**——缺必填参数直接拒，不把半成品请求发给下游；
2. **确认门**——destructive 动作（退款等）未确认**绝不执行**，
   只返回 `status=needs_confirmation`，让上层（人工 / HITL）决定是否重放；
3. **审计落盘**——每一次动作尝试（含被拒 / 失败）都写一条 JSONL。
   这既是合规要求，也是「动作真的发生了」唯一可被断言的证据——
   和 P0-3 一样：只看「没报错」不足以证明副作用发生过。

第 4 条（**P2-9 收尾**）也收口在这里：**执行成功后把结果回写知识图谱**
（`action_graph.apply_action_effects`，默认关 `KB_ACTION_GRAPH=0`）。
同样是「每新增一个动作都自动获得」的理由——否则「执行 → 关系 → 再检索」这条闭环
会在每个新动作上重新断一次。写回失败只降级、不改动作结论（动作已经发生，
记账失败若报成失败，会诱导调用方重试 = 第二次执行）。

失败一律不抛异常，统一返回 `{"ok": False, "status": ..., "error": ...}`（沿用全项目降级约定）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

from kb_mcp_server.action_graph import apply_action_effects, effects_enabled
from kb_mcp_server.config import DATA_DIR, get_cfg
from kb_mcp_server.extensions import (
    RISK_DESTRUCTIVE,
    RISK_READ,
    RISK_WRITE,
    ActionTool,
    ActionToolRegistry,
)

_lock = threading.Lock()
_OFF = {"off", "none", "disabled", "0", "false", "no"}

# 与「语料规范」一致的 SLA 口径（P0 15 分钟 / P1 1 小时 / P2 4 小时）——
# 动作与知识联动：建工单时直接给出承诺响应时限，而不是留给客服去翻手册。
_TICKET_SLA = {"P0": "15 分钟", "P1": "1 小时", "P2": "4 小时"}


# --------------------------------------------------------------------------- #
# 动作日志（独立文件：写操作审计与问答审计性质不同，便于合规单独导出）
# --------------------------------------------------------------------------- #
def _action_log_path() -> str | None:
    p = (get_cfg("KB_ACTION_LOG", "") or "").strip()
    if p.lower() in _OFF:
        return None
    if p:
        return p
    base = DATA_DIR or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "kb_actions.jsonl")


def _safe_json(obj):
    """保证可 JSON 序列化：不可序列化的值退化为字符串，不让审计写失败。"""
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except Exception:  # noqa: BLE001
        if isinstance(obj, dict):
            return {str(k): _safe_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_safe_json(v) for v in obj]
        return str(obj)


def _append(rec: dict) -> None:
    path = _action_log_path()
    if not path:
        return
    try:
        rec = dict(rec)
        rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        with _lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_safe_json(rec), ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 审计是旁路，绝不拖垮动作链路
        pass


def read_action_log(n: int = 50) -> list[dict]:
    """读取最近 n 条动作记录（新→旧）；未启用 / 不存在 / 损坏返回空列表。"""
    try:
        path = _action_log_path()
        if not path or not os.path.exists(path):
            return []
        with _lock, open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        out: list[dict] = []
        for line in reversed(lines[-n * 2:]):   # 多读一倍容错坏行
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
            if len(out) >= n:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# 具体动作
# --------------------------------------------------------------------------- #
class CreateTicketTool(ActionTool):
    """建工单：写操作里最常用的一类，且与 SLA 知识天然联动。"""

    name = "create_ticket"
    description = "创建客服工单，并按优先级给出承诺响应时限"
    risk = RISK_WRITE
    capabilities = ["ticket", "action"]

    def params(self) -> list[dict]:
        return [
            {"name": "subject", "type": "str", "required": True, "desc": "工单标题"},
            {"name": "detail", "type": "str", "required": False, "desc": "问题详情"},
            {"name": "priority", "type": "str", "required": False,
             "desc": "P0（紧急）/ P1（高危）/ P2（普通），默认 P2"},
            {"name": "customer", "type": "str", "required": False, "desc": "客户 / 租户标识"},
        ]

    def execute(self, params: dict) -> dict:
        pr = str(params.get("priority") or "P2").strip().upper()
        if pr not in _TICKET_SLA:
            raise ValueError(f"priority 只接受 P0/P1/P2，收到 {pr!r}")
        return {
            "ticket_id": "TKT-" + uuid.uuid4().hex[:8].upper(),
            "subject": str(params.get("subject", ""))[:120],
            "priority": pr,
            "sla_response": _TICKET_SLA[pr],
            "detail": str(params.get("detail", ""))[:500],
            "customer": str(params.get("customer", "")),
            "status": "open",
        }


class UpdateOrderTool(ActionTool):
    """改单：修改收货地址 / 期望送达日期。"""

    name = "update_order"
    description = "修改订单的收货地址或期望送达日期"
    risk = RISK_WRITE
    capabilities = ["order", "action"]

    _FIELDS = {"address": "收货地址", "delivery_date": "期望送达日期"}

    def params(self) -> list[dict]:
        return [
            {"name": "order_id", "type": "str", "required": True, "desc": "订单号"},
            {"name": "field", "type": "str", "required": True,
             "desc": "address（收货地址）| delivery_date（期望送达日期）"},
            {"name": "value", "type": "str", "required": True, "desc": "新的值"},
        ]

    def execute(self, params: dict) -> dict:
        f = str(params.get("field", "")).strip()
        if f not in self._FIELDS:
            raise ValueError(f"field 只接受 address / delivery_date，收到 {f!r}")
        return {
            "order_id": str(params.get("order_id", "")),
            "field": f,
            "field_label": self._FIELDS[f],
            "new_value": str(params.get("value", ""))[:200],
            "status": "updated",
        }


class RequestRefundTool(ActionTool):
    """退款：涉及资金、不可逆 → destructive，必须显式确认。"""

    name = "request_refund"
    description = "发起退款申请（涉及资金，不可逆）"
    risk = RISK_DESTRUCTIVE
    capabilities = ["order", "refund", "action"]

    def params(self) -> list[dict]:
        return [
            {"name": "order_id", "type": "str", "required": True, "desc": "订单号"},
            {"name": "amount", "type": "float", "required": True, "desc": "退款金额（元）"},
            {"name": "reason", "type": "str", "required": False, "desc": "退款原因"},
        ]

    def execute(self, params: dict) -> dict:
        try:
            amount = float(params.get("amount"))
        except (TypeError, ValueError):
            raise ValueError(f"amount 必须是数字，收到 {params.get('amount')!r}")
        if amount <= 0:
            raise ValueError(f"退款金额必须大于 0，收到 {amount}")
        return {
            "refund_id": "RF-" + uuid.uuid4().hex[:8].upper(),
            "order_id": str(params.get("order_id", "")),
            "amount": round(amount, 2),
            "reason": str(params.get("reason", ""))[:200],
            "status": "submitted",
        }


class ListTicketsTool(ActionTool):
    """查工单：只读动作，用于证明「前面建的工单真的落盘了」。"""

    name = "list_tickets"
    description = "列出最近创建的服务工单（只读）"
    risk = RISK_READ
    capabilities = ["ticket", "action"]

    def params(self) -> list[dict]:
        return [{"name": "limit", "type": "int", "required": False, "desc": "条数，默认 10"}]

    def execute(self, params: dict) -> dict:
        try:
            limit = max(1, min(100, int(params.get("limit") or 10)))
        except (TypeError, ValueError):
            limit = 10
        rows = [r for r in read_action_log(500)
                if r.get("action") == "create_ticket" and r.get("ok")]
        return {"count": len(rows[:limit]),
                "tickets": [(r.get("result") or {}) for r in rows[:limit]]}


# --------------------------------------------------------------------------- #
# 执行器：所有动作用的唯一入口
# --------------------------------------------------------------------------- #
class ActionRunner:
    """校验 → 确认门 → 执行 → 审计。"""

    def __init__(self, registry: ActionToolRegistry | None = None, actor: str = ""):
        self._registry = registry
        self.actor = actor

    def _reg(self) -> ActionToolRegistry:
        if self._registry is None:
            self._registry = action_tools()
        return self._registry

    def run(self, action: str, params: dict | None = None, confirmed: bool = False,
            actor: str = "", context: dict | None = None) -> dict:
        given = dict(params or {})
        who = actor or self.actor or "anonymous"
        t = self._reg().get(action)

        if t is None:
            return self._reject(action, given, who, f"未知动作 {action!r}",
                                available=self._reg().names())

        missing = [k for k in t.required_params() if given.get(k) in (None, "")]
        if missing:
            return self._reject(action, given, who, "缺少必填参数：" + "、".join(missing),
                                risk=t.risk, missing=missing)

        # 确认门：destructive 未确认 → 不执行、只回待确认（这条是整个 P2-9 的安全底线）
        if t.requires_confirm and not confirmed:
            rec = self._record(action, given, who, risk=t.risk, confirmed=False, ok=False,
                               status="needs_confirmation",
                               error="destructive 动作需显式确认（confirmed=true）")
            return {
                "ok": False, "status": "needs_confirmation", "action": action,
                "risk": t.risk, "action_id": rec["action_id"], "confirmed": False,
                "preview": {"params": t.params(), "given": given},
                "message": f"{t.description}——该动作不可逆，需人工确认后以 confirmed=true 重放",
            }

        try:
            result = t.execute(given)
        except Exception as e:  # noqa: BLE001 - 动作失败不得抛穿调用方
            return self._reject(action, given, who, f"{type(e).__name__}: {e}", risk=t.risk)

        # P2-9 收尾：把执行结果回写图谱（默认关）。
        # 放在这里而不是动作工具内部，理由与确认门/审计一样——**每新增一个动作都自动获得**，
        # 不会漏。写失败只降级、不改结论：动作已经发生，记账失败不该让调用方重试。
        gfx = apply_action_effects(action, given, result, context) if effects_enabled() else None
        rec = self._record(action, given, who, risk=t.risk, confirmed=confirmed, ok=True,
                           status="executed", error="", result=result, graph_effects=gfx)
        out = {"ok": True, "status": "executed", "action": action, "risk": t.risk,
               "action_id": rec["action_id"], "confirmed": confirmed, "result": result}
        if gfx is not None:
            out["graph"] = gfx
        return out

    # ---- 内部 ----
    def _reject(self, action, params, who, error, risk="", **extra) -> dict:
        rec = self._record(action, params, who, risk=risk, confirmed=False, ok=False,
                           status="rejected", error=error)
        out = {"ok": False, "status": "rejected", "action": action,
               "error": error, "action_id": rec["action_id"]}
        out.update(extra)
        return out

    def _record(self, action, params, who, *, risk, confirmed, ok, status, error,
                result=None, graph_effects=None) -> dict:
        rec = {
            "kind": "action",
            "action_id": "ACT-" + uuid.uuid4().hex[:10],
            "action": action,
            "risk": risk,
            "params": _safe_json(params),
            "actor": who,
            "confirmed": confirmed,
            "ok": ok,
            "status": status,
            "error": error,
        }
        if result is not None:
            rec["result"] = _safe_json(result)
        if graph_effects is not None:
            # 只在实际开启回写时出现——默认关的运行时里，审计格式与旧版完全一致
            rec["graph"] = _safe_json(graph_effects)
        _append(rec)
        return rec


# --------------------------------------------------------------------------- #
# 默认动作集与便捷入口
# --------------------------------------------------------------------------- #
_REGISTRY: ActionToolRegistry | None = None


def build_registry() -> ActionToolRegistry:
    """默认动作集。新增动作只需在此注册——审计与确认门由 Runner 自动获得。"""
    return ActionToolRegistry([
        CreateTicketTool(),
        UpdateOrderTool(),
        RequestRefundTool(),
        ListTicketsTool(),
    ])


def action_tools() -> ActionToolRegistry:
    """进程内单例（Web / MCP / agent 共用同一张动作表）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return _REGISTRY


def run_action(action: str, params: dict | None = None, confirmed: bool = False,
               actor: str = "", context: dict | None = None) -> dict:
    """动作唯一入口。`context` 提供图谱回写所需的旁路信息（如 `sku` / `order_id`），
    不是动作参数，因此不进参数契约、也不参与校验（P2-9 收尾）。"""
    return ActionRunner(action_tools(), actor=actor).run(
        action, params, confirmed=confirmed, actor=actor, context=context)


# --------------------------------------------------------------------------- #
# 意图识别（规则优先：可解释、离线可测、不会误触发写操作）
# --------------------------------------------------------------------------- #
INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 顺序即优先级：退款先匹配（否则「退款」会被「建工单」之外的词吞掉）
    ("request_refund", ("退款", "退钱", "申请退", "返还", "退给我")),
    ("update_order", ("改地址", "修改地址", "改收货", "改单", "改期", "改送达", "延迟送达")),
    ("create_ticket", ("建工单", "创建工单", "开工单", "提工单", "报障", "投诉", "上报")),
)

_ORDER_ID_RE = re.compile(r"\b([A-Z]{2,4}[-_]?\d{3,12})\b")


def detect_intent(question: str) -> str | None:
    """从问题里识别动作意图；识别不出返回 None（宁可不做，不可乱做）。"""
    q = question or ""
    for name, kws in INTENT_RULES:
        if any(k in q for k in kws):
            return name
    return None


def extract_params(action: str, question: str, order_id: str | None = None,
                   sku: str | None = None) -> dict:
    """从自然语言里抽取动作参数（规则抽取，不做 LLM 猜测）。

    抽不全也没关系：ActionRunner 的必填校验会拒掉，并明确告诉你缺哪个参数——
    这比「猜一个数字去退款」安全得多。
    """
    q = question or ""

    def _oid() -> str:
        if order_id:
            return order_id
        m = _ORDER_ID_RE.search(q.upper())
        return m.group(1) if m else ""

    if action == "create_ticket":
        pr = "P2"
        up = q.upper()
        for cand in ("P0", "P1", "P2"):
            if cand in up:
                pr = cand
                break
        if any(k in q for k in ("紧急", "严重", "业务中断", "全挂")):
            pr = "P0"
        elif any(k in q for k in ("高危", "降级", "很慢")):
            pr = "P1"
        return {"subject": q[:60], "detail": q[:500], "priority": pr}

    if action == "update_order":
        field = "delivery_date" if any(
            k in q for k in ("改期", "送达", "延迟", "日期", "时间")) else "address"
        return {"order_id": _oid(), "field": field, "value": q[:200]}

    if action == "request_refund":
        # 先剥掉订单号再找金额：否则「订单 SO123 退款 88 元」会命中 SO123 里的 123，
        # 把退款金额抽成 123 元——金额抽错比抽不到危险得多（抽不到会被必填校验拦下）。
        # 优先取带币种标记的数字，其次才退化为「第一个数字」。
        stripped = _ORDER_ID_RE.sub(" ", q.upper())
        m = (re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块|RMB)", stripped, re.I)
             or re.search(r"(\d+(?:\.\d+)?)", stripped))
        amount = float(m.group(1)) if m else ""
        return {"order_id": _oid(), "amount": amount, "reason": q[:200]}

    if action == "list_tickets":
        return {}

    return {}
