"""动作结果回写图谱（P2-9 收尾）：把「执行」接回「关系」与「再检索」。

为什么需要它
------------
P2-9 让 agent 能真的执行动作，但执行结果只落在两处：动作审计 JSONL 与本次答复。
知识图谱里没有留下任何痕迹 —— 于是「执行 → 关系 → 再检索」是**断的**：
客户接着问「刚才给 A 客户建的工单涉及哪个产品」，图谱答不出来。

本体其实早已就绪（`graph.NODE_TYPES` 里有 `Ticket` / `Customer` / `Product`，
`REL_SCHEMA` 里有 `SUBMITTED_BY` / `ABOUT_PRODUCT`），但**只有「LLM 从文档抽取」这一条
写入路径**。本模块补上第二条：动作的执行结果。

三条设计底线
------------
1. **默认关闭**（`KB_ACTION_GRAPH=0`）：不开时零副作用，返回值与审计里连 `graph` 字段都不出现。
   与 P1-4 / P2-9 的开关风格一致——新增能力不得改变默认行为。
2. **只有真的执行成功了才写**：`needs_confirmation`（确认门拦住）与 `rejected`（校验失败 /
   下游报错）一律不写。被拦下的动作在真实世界里什么都没发生，图谱里也不该有它。
3. **写失败不拖垮动作**：图谱不可用 / 映射写错 / 本体不合规 → 一律降级成
   `{"applied": false, "reason": ...}` 并留在审计里。动作**已经发生**，不能因为「记账失败」
   就报告失败——那会诱导调用方重试，而重试等于第二次执行；写操作被重复执行才是真事故。

刻意**不**写的动作（不是遗漏）
------------------------------
- `update_order` / `request_refund`：本体里没有 `Order` / `Refund` 节点类型，而且它们的真相
  在**实时后端**（P0-2）而不在知识图谱里。把运行态数据塞进知识图会污染检索
  （同一个订单号在两侧语义不同）。要写回来，先论证本体该不该扩——本体驱动（D3）的
  意义正是「不让图谱被随手扩张」。
- `list_tickets`：只读动作不产生新事实，天然没有映射。

写入内容（当前只有 `create_ticket` 一条映射）
--------------------------------------------
    Ticket:TKT-XXXXXXXX   props: subject / priority / sla_response / status
      ├─ SUBMITTED_BY ──→ Customer:<customer>      （客户为空则整条关系跳过）
      └─ ABOUT_PRODUCT ─→ Product:<sku>            （无 sku / 无 product 则跳过）

映射是**声明式**的（`Effect`），三元组统一过 `Triple.is_valid()` 的本体校验——
写错映射只会被丢弃并在 `dropped` 里列出来，不可能污染图。新增动作的映射就是加一条
`ACTION_EFFECTS` 条目。
"""

from __future__ import annotations

from dataclasses import dataclass

from kb_mcp_server.config import get_cfg
from kb_mcp_server.graph import NODE_TYPES, Triple, get_graph_store
# 与 `extensions.agent_gate_open` 同款**白名单**判定：只有明确写出的真值才算开启。
# 用白名单而不是「不在黑名单里就算开」，是为了让任何拼错 / 空值 / 未预期取值
# 都落到「关」这一侧——带副作用的开关，模糊即关闭。
_ON = ("1", "true", "yes", "on")


def effects_enabled() -> bool:
    """动作结果是否回写图谱。默认关（`KB_ACTION_GRAPH=0`）——新能力不改默认行为。"""
    return str(get_cfg("KB_ACTION_GRAPH", "0")).strip().lower() in _ON


@dataclass(frozen=True)
class Effect:
    """一个动作的图谱副作用声明。

    `relations` 里的取值字段会在 **result → context → params** 中按序查找；
    取到空值就**整条关系跳过**（绝不建一个空名字的节点）。
    """

    node_type: str
    name_field: str
    props: tuple[str, ...] = ()
    relations: tuple[tuple[str, str, str], ...] = ()


ACTION_EFFECTS: dict[str, Effect] = {
    "create_ticket": Effect(
        node_type="Ticket",
        name_field="ticket_id",
        props=("subject", "priority", "sla_response", "status"),
        relations=(
            ("SUBMITTED_BY", "Customer", "customer"),
            ("ABOUT_PRODUCT", "Product", "sku"),
        ),
    ),
}


def _pick(field: str, *sources: dict | None) -> str:
    """按序取值：先看结果，再看上下文，最后看入参。"""
    for src in sources:
        v = (src or {}).get(field)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def build_effects(action: str, params: dict | None, result: dict | None,
                  context: dict | None = None,
                  effects: dict[str, Effect] | None = None) -> tuple[dict, str]:
    """把动作结果翻译成「主体节点 + 三元组」。返回 `(plan, reason)`。

    `plan` 为空 dict 时 `reason` 说明原因（`no_effect_mapping` / `missing_subject_name`）。
    `effects` 可注入（供测试构造非法映射），默认用 `ACTION_EFFECTS`。
    """
    table = ACTION_EFFECTS if effects is None else effects
    eff = table.get(action)
    if eff is None:
        return {}, "no_effect_mapping"

    if eff.node_type not in NODE_TYPES:
        return {}, f"unknown_node_type:{eff.node_type}"

    name = _pick(eff.name_field, result, context, params)
    if not name:
        return {}, "missing_subject_name"

    src = {**(params or {}), **(context or {}), **(result or {})}
    props = {k: src[k] for k in eff.props if src.get(k) not in (None, "")}

    triples: list[Triple] = []
    for rel, obj_type, field in eff.relations:
        obj = _pick(field, result, context, params)
        if not obj:
            continue
        triples.append(Triple(subject=name, subject_type=eff.node_type, relation=rel,
                              object=obj, object_type=obj_type, props={"source": "action"}))
    return ({"node_type": eff.node_type, "name": name, "props": props, "triples": triples}, "")


def apply_action_effects(action: str, params: dict | None, result: dict | None,
                         context: dict | None = None, *,
                         store=None, effects: dict[str, Effect] | None = None) -> dict:
    """把动作结果写回图谱。**绝不抛异常**——任何失败都转成结构化降级结果。

    返回字段：
      `applied`   本次是否真的写了图
      `node`      主体节点 key（`Ticket:TKT-XXXX`），便于调用方核对
      `triples`   实际写入的关系条数
      `dropped`   被本体校验丢掉的三元组（映射写错的证据）
      `reason`    `disabled` / `no_effect_mapping` / `missing_subject_name` /
                  `graph_error:<异常类名>` / `ontology_dropped` / 空串
      `backend`   实际落到的图后端（memory / age）——「没静默降级」的证据
    """
    if not effects_enabled():
        return {"applied": False, "reason": "disabled"}

    plan, reason = build_effects(action, params, result, context, effects)
    if not plan:
        return {"applied": False, "reason": reason, "action": action}

    triples = plan["triples"]
    valid = [t for t in triples if t.is_valid()]
    dropped = [{"relation": t.relation, "subject_type": t.subject_type,
                "object_type": t.object_type} for t in triples if not t.is_valid()]

    out = {"applied": False, "action": action, "node_type": plan["node_type"],
           "node": f"{plan['node_type']}:{plan['name']}", "triples": len(valid),
           "dropped": dropped, "reason": "ontology_dropped" if dropped else "",
           "backend": ""}
    try:
        gs = store if store is not None else get_graph_store()
        gs.ensure_schema()
        gs.upsert_entity(plan["node_type"], plan["name"], plan["props"] or None)
        if valid:
            gs.add_triples(valid)
        out["applied"] = True
        out["backend"] = getattr(gs, "backend", "")
    except Exception as e:  # noqa: BLE001 - 记账失败绝不能拖垮已经发生的动作
        out.update(applied=False, reason=f"graph_error:{type(e).__name__}",
                   error=str(e)[:200])
    return out
