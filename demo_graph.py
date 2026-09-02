"""图谱离线自检（P6 · GraphRAG）：验证「摄取自动建图 -> 多跳查询 -> 推理路径 -> 融合答复」全链路。

运行：python demo_graph.py

零外部依赖（默认 memory 图 + dev 嵌入），离线即可跑。
图谱写入独立的 kb_graph_demo.json，不污染真实 kb_graph.json。
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("KB_GRAPH_STORE", os.path.join(_HERE, "kb_graph_demo.json"))
os.environ.setdefault("KB_MEM_STORE", os.path.join(_HERE, "kb_store_demo.json"))
os.environ.setdefault("KB_GRAPH_BACKEND", "memory")
os.environ.setdefault("KB_STORAGE_BACKEND", "memory")
os.environ.setdefault("KB_EMBEDDING_BACKEND", "dev")

from kb_mcp_server.embeddings import get_embedder
from kb_mcp_server.graph import format_path, get_graph_store
from kb_mcp_server.ingestion import IngestionPipeline
from kb_mcp_server.retrieval import HybridRetriever
from kb_mcp_server.storage import get_store
from kb_mcp_server.synthesis import synthesize

DOCS = {
    "sla_policy": (
        "服务级别协议（SLA）。技术支持响应时效：企业版客户提交工单后 2 小时内响应，"
        "标准版客户 4 小时内响应。故障修复时限：严重故障 8 小时内解决，一般故障 24 小时内解决。"
        "原因是系统负载过高导致服务降级。解决方案为免费延长服务期或全额退款。"
    ),
    "logistics_faq": (
        "物流配送常见问题：现货商品付款后 48 小时内发货，偏远地区顺延 3 个工作日。"
        "由于仓库拣货延误导致配送超时，解决方案为补发或优惠券补偿。涉及产品：供应链云服务。"
    ),
    "refund_policy": (
        "退款退货政策：商品签收后 7 天内可申请退货，审核 1 个工作日内完成。"
        "退款原路返回，3-5 个工作日到账。若出现商品质量争议，运费由平台承担。"
        "解决方案为全额退款或换货。"
    ),
    "billing_faq": (
        "账户与计费说明：账单每月 1 日出账，需在 15 个工作日内完成对账，逾期将暂停服务。"
        "企业版客户支持子账号权限管理。开票问题请在 3 个工作日内提交申请，"
        "发票由财务在 5 个工作日内开具。"
    ),
}

QUERIES = [
    "企业版客户提交工单后多久响应",
    "物流配送超时怎么赔偿",
    "退货退款要几个工作日到账",
]


def main() -> None:
    store = get_store()
    store.ensure_schema()
    graph = get_graph_store()
    graph.ensure_schema()
    graph.clear()  # demo 每次重建，保证可重复

    print("图后端:", graph.backend, "| 图文件:", os.environ["KB_GRAPH_STORE"])

    emb = get_embedder()
    ing = IngestionPipeline(store, emb, graph=graph)
    retriever = HybridRetriever(store, emb)

    print("\n── 1. 摄取并自动建图 ──")
    for doc_id, text in DOCS.items():
        n = ing.ingest_text(doc_id, text)
        g = ing.last_graph_result or {}
        print(f"  {doc_id}: {n} 片段 -> 抽三元组 {g.get('triples', 0)} 条，"
              f"入图 {g.get('added', 0)}，本体不合规丢弃 {g.get('invalid', 0)}")

    print("\n── 2. 图谱统计 ──")
    st = graph.stats()
    print(f"  节点 {st['nodes']} / 边 {st['edges']}")
    print("  按类型:", st["by_type"])
    print("  按关系:", st["by_relation"])

    print("\n── 3. 多跳查询（depth=2, both）──")
    for ent in ("服务响应", "物流配送", "退款退货"):
        res = graph.neighbors(ent, direction="both", depth=2, limit=6)
        if not res.get("entity"):
            print(f"  {ent}: 未命中")
            continue
        print(f"  {ent}（{res['entity']['type_label']}）：")
        for nb in res["neighbors"]:
            print(f"    [{nb['depth']}跳] {format_path(res['entity']['name'], nb['path'])}")

    print("\n── 4. 推理路径（路径即证据）──")
    for s, d in (("物流配送", "优惠券补偿"), ("退款退货", "全额退款"), ("服务响应", "2小时内响应")):
        ps = graph.paths(s, d, max_depth=3, limit=3)
        if not ps:
            print(f"  {s} -> {d}: 无路径")
            continue
        for p in ps[:2]:
            steps = " ; ".join(
                f"{st_['from']['name']} --{st_.get('rel_label') or st_.get('rel')}--> {st_['to']['name']}"
                for st_ in p["steps"]
            )
            print(f"  {s} -> {d}（{p['length']}跳）: {steps}")

    print("\n── 5. GraphRAG 融合答复（向量召回 + 图谱路径）──")
    gf_fn = None
    try:
        from kb_mcp_server import server as kb

        kb._graph = graph  # 复用同一图实例，避免双实例互相覆盖持久化文件
        gf_fn = kb._graph_facts
    except Exception as e:  # noqa: BLE001
        print("  （跳过关系侧：MCP 层不可用，", e, "）")

    for q in QUERIES:
        print(f"\n  Q: {q}")
        hits = retriever.search(q, top_k=2)
        gf = gf_fn(q) if gf_fn else {}
        if gf.get("entities"):
            ents = "、".join(f"{e['name']}({e['type_label']})" for e in gf["entities"])
            print(f"  命中实体: {ents}")
        for i, p in enumerate(gf.get("facts", [])[:4], 1):
            print(f"  关系路径 {i}: {p['path']}")
        out = synthesize(q, hits, [], graph_facts=gf)
        conf = out["confidence"]
        print(f"  置信度: {conf['label']} ({conf['score']}) | 方法: {out['synthesis_method']}")
        print("  答复:")
        for line in out["answer"].splitlines():
            print("    " + line)


if __name__ == "__main__":
    main()
