"""离线自检：不依赖 Postgres，验证摄取 -> 嵌入 -> 检索整条链路。

运行：python demo_offline.py
（venv：KB_STORAGE_BACKEND=memory, KB_EMBEDDING_BACKEND=dev 为默认）
"""

from kb_mcp_server.ingestion import IngestionPipeline
from kb_mcp_server.retrieval import HybridRetriever
from kb_mcp_server.storage import get_store


def main():
    store = get_store()
    store.ensure_schema()
    emb = __import__("kb_mcp_server.embeddings", fromlist=["get_embedder"]).get_embedder()
    ingestor = IngestionPipeline(store, emb)
    retriever = HybridRetriever(store, emb)

    faq = {
        "faq_returns": (
            "如何申请退货？在订单签收后 7 天内，于「我的订单」点击申请退货，"
            "填写退货原因并上传凭证，审核通过后寄回商品即可。退货审核一般 1-2 个工作日完成。"
            "退款将原路返回，到账时间取决于支付渠道，通常 3-5 个工作日。"
        ),
        "faq_invoice": (
            "怎么开具发票？订单完成后进入「订单详情 - 申请开票」，选择电子发票或纸质发票，"
            "填写抬头与税号后提交。电子发票实时开具并发送至预留邮箱；纸质发票随下次发货寄出。"
        ),
        "faq_shipping": (
            "物流多久发货？现货商品在付款后 48 小时内出库，偏远地区顺延。可在「我的订单」"
            "查看物流单号与实时轨迹。大客户批量订单按合同约定排期交付。"
        ),
    }

    total = 0
    for doc_id, text in faq.items():
        n = ingestor.ingest_text(doc_id, text)
        total += n
        print(f"[ingest] {doc_id}: {n} chunks")

    print(f"\n库内片段总数: {store.count()} (ingested={total})")

    queries = ["退货要多久退款", "怎么开发票", "物流什么时候发货"]
    for q in queries:
        print(f"\n=== 查询: {q} ===")
        hits = retriever.search(q, top_k=2)
        for h in hits:
            print(f"  score={h['score']:.3f} doc={h['doc_id']} | {h['content'][:40]}...")


if __name__ == "__main__":
    main()
