"""多 agent 协作自检（P7）：验证编排器调度的完整协作链路。

运行：python demo_agents.py

零外部依赖（确定性编排 + memory 图 + dev 嵌入），离线可跑。
写在独立 demo 文件上，不污染真实数据。

链路：GraphBuilder（增量补图）→ [Retriever ∥ GraphReasoner ∥ LiveData] → Synthesizer
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("KB_GRAPH_STORE", os.path.join(_HERE, "kb_graph_demo.json"))
os.environ.setdefault("KB_MEM_STORE", os.path.join(_HERE, "kb_store_demo.json"))
os.environ.setdefault("KB_GRAPH_BACKEND", "memory")
os.environ.setdefault("KB_STORAGE_BACKEND", "memory")
os.environ.setdefault("KB_EMBEDDING_BACKEND", "dev")
os.environ.setdefault("KB_AGENT_MODE", "deterministic")

from kb_mcp_server.agents import get_orchestrator
from kb_mcp_server.embeddings import get_embedder
from kb_mcp_server.graph import get_graph_store
from kb_mcp_server.ingestion import IngestionPipeline
from kb_mcp_server.storage import get_store

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
        "退款原路返回，3-5 个工作日到账。解决方案为全额退款或换货。"
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
    graph.clear()

    print("── 0. 预置知识（只入向量库，不入图——留给 GraphBuilder 补）──")
    emb = get_embedder()
    ing = IngestionPipeline(store, emb, graph=False)  # graph=False：手动绕过摄取钩子
    for doc_id, text in DOCS.items():
        n = ing.ingest_text(doc_id, text, graph=False)
        print(f"  {doc_id}: {n} 片段（未入图）")

    o = get_orchestrator()
    print("\n── agent 清单 ──")
    print("  编排模式:", o.mode)
    for a in o.agents_status():
        print(f"  · {a['name']}（{a['role']}）：{a['description']}")

    for q in QUERIES:
        print(f"\n{'=' * 56}\nQ: {q}")
        res = o.ask(q, top_k=2)

        print("── agent 轨迹 ──")
        for t in res["agents"]["trace"]:
            flag = "OK " if t.get("ok") else "ERR"
            print(f"  [{flag}] {t['agent']:<14} {t['ms']:>5}ms  {t.get('summary', '')}")

        print(f"── 路由: {res['agents']['routing']['decision']} | 总耗时 {res['agents']['total_ms']}ms ──")
        conf = res["confidence"]
        print(f"置信度: {conf['label']} ({conf['score']}) | 方法: {res['synthesis_method']}")
        for p in res.get("graph_paths", []):
            print(f"  路径: {p}")
        print("答复:")
        for line in res["answer"].splitlines():
            print("   " + line)

    print(f"\n{'=' * 56}")
    print("── 第二轮（图谱已建好，GraphBuilder 应报「已是最新」）──")
    res = o.ask(QUERIES[0], top_k=2)
    for t in res["agents"]["trace"]:
        print(f"  [{'OK ' if t.get('ok') else 'ERR'}] {t['agent']:<14} {t.get('summary', '')}")


if __name__ == "__main__":
    main()
