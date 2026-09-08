"""规模化语义召回评测（P2-8 真实语料验证，numpy 加速版）。

用公开电商客服合成语料（data/ecom_cs.jsonl，992 条 QA 对）测 bge 的语义召回能力：
- 每条 QA → 一个自包含 chunk：`{scene} / Q：{user} A：{bot}`
- 抽 N 条用「客户原话 user」当 query，看能否把它的答案 chunk 召回（Recall@k）
- 用 numpy 矩阵运算算全部余弦（等价于 pgvector 后端的检索速度），
  绕开 memory 后端纯 Python 逐条点积的 O(N*dim) 慢（那是 P0-3 要解决的另一件事）

两条评测口径（语料本身有大量重复问题，严格口径会低估）：
- 严格 Recall@k：query 的「自己的 chunk 索引」进入 top-k
- 语义 Recall@k：任意一条「与 query 同义的问题」的答案 chunk 进入 top-k
  （ground-truth = 归一化后 user 文本相同的那一组 chunk）

运行：
    python bench_scale.py --n 300          # 默认 bge，编码后存 bench_emb.npz
    python bench_scale.py --n 300 --embed dev  # 对照：离线哈希嵌入
    python bench_scale.py --load            # 复用 bench_emb.npz，不重新编码
"""

import json
import os
import random
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

EMBED = "bge"
if "--embed" in sys.argv:
    EMBED = sys.argv[sys.argv.index("--embed") + 1]
os.environ["KB_EMBEDDING_BACKEND"] = EMBED

import numpy as np  # noqa: E402


def load_data(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def chunk_text(r: dict) -> str:
    return f"{r['scene']} / Q：{r['user']} A：{r['bot']}"


def norm_q(s: str) -> str:
    """归一化问题文本：去标点/空白，做同义判断的 key。"""
    return re.sub(r"[？?。!！，,~.、\s]", "", s)


def encode(texts: list[str]):
    from kb_mcp_server.embeddings import get_embedder
    emb = get_embedder()
    return np.asarray(emb.encode(texts), dtype=np.float32)


def main() -> None:
    n = 300
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])

    rows = load_data(os.path.join(_HERE, "data", "ecom_cs.jsonl"))
    corpus = [chunk_text(r) for r in rows]
    emb_path = os.path.join(_HERE, f"bench_emb_{EMBED}.npz")

    # ground-truth：归一化 user -> 该组所有 chunk 索引
    gt: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        gt.setdefault(norm_q(r["user"]), []).append(i)

    if "--load" in sys.argv and os.path.exists(emb_path):
        z = np.load(emb_path, allow_pickle=True)
        C, Q, idxs = z["C"], z["Q"], z["idxs"].tolist()
        print(f"[bench] 复用 {emb_path} 的嵌入，跳过编码", flush=True)
        t0 = time.time()
    else:
        print(f"[bench] 编码 {len(corpus)} 条语料 + {n} 条查询（{EMBED}）...", flush=True)
        t0 = time.time()
        C = encode(corpus)                      # [992, dim]
        rng = random.Random(42)
        idxs = rng.sample(range(len(rows)), min(n, len(rows)))
        Q = encode([rows[i]["user"] for i in idxs])  # [n, dim]
        print(f"[bench] 编码完成，耗时 {time.time()-t0:.1f}s", flush=True)
        np.savez(emb_path, C=C, Q=Q, idxs=np.asarray(idxs))

    # 向量已 L2 归一化：点积 = 余弦
    sim = Q @ C.T                            # [n, 992]
    order = np.argsort(-sim, axis=1)         # 每行按相似度降序

    def strict_recall(k: int) -> float:
        return sum(1 for j, i in enumerate(idxs) if i in order[j, :k]) / len(idxs)

    def semantic_recall(k: int) -> float:
        hit = 0
        for j, i in enumerate(idxs):
            correct = set(gt[norm_q(rows[i]["user"])])
            if correct & set(order[j, :k].tolist()):
                hit += 1
        return hit / len(idxs)

    print("[bench] 严格 Recall（命中自己的 chunk）:", flush=True)
    for k in (1, 3, 5):
        print(f"  Recall@{k}: {strict_recall(k):.1%}", flush=True)

    print("[bench] 语义 Recall（命中任意同义问题的答案）:", flush=True)
    for k in (1, 3, 5):
        print(f"  Recall@{k}: {semantic_recall(k):.1%}", flush=True)

    top1_sim = float(np.mean(sim[np.arange(len(idxs)), order[:, 0]]))
    print(f"  平均 top-1 相似度: {top1_sim:.4f}", flush=True)

    # 看几个「严格口径召回错」的样例（top-1 命中的不是自己）
    print("\n[bench] 严格口径召回错样例（query → 实际召回的 chunk，前 5 条）:", flush=True)
    shown = 0
    for j, i in enumerate(idxs):
        if i not in order[j, :1] and shown < 5:
            wrong = int(order[j, 0])
            same_q = norm_q(rows[i]["user"]) == norm_q(rows[wrong]["user"])
            tag = "✓同义正确" if same_q else "✗真错"
            print(f"  [{tag}] 问「{rows[i]['user'][:18]}」→ 召回「{corpus[wrong][:36]}」", flush=True)
            shown += 1

    out = {
        "corpus": len(rows),
        "sample": len(idxs),
        "embedding": EMBED,
        "unique_norm_q": len(gt),
        "dup_chunks": sum(len(v) for v in gt.values() if len(v) > 1),
        "strict_recall_at": {str(k): round(strict_recall(k), 4) for k in (1, 3, 5)},
        "semantic_recall_at": {str(k): round(semantic_recall(k), 4) for k in (1, 3, 5)},
        "avg_top1_sim": round(top1_sim, 4),
        "elapsed_s": round(time.time() - t0, 1),
    }
    report_path = os.path.join(_HERE, f"bench_report_{EMBED}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[bench] 报告已写入 {report_path}", flush=True)


if __name__ == "__main__":
    main()
