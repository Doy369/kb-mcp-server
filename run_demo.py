"""演示运行入口（云端 / 离线演示专用，非生产）。

与 `python app.py` 的区别：
- 强制离线默认（memory 存储 + dev 嵌入器），不依赖 Postgres / 真实嵌入模型；
- 启动时重新播种示例知识库（内置 FAQ + samples/ + test-docs/），保证线上演示内容稳定；
- 实时数据走 mock 样例（KB_API_MOCK=1），无需真实后端即可展示「知识 + 实时数据」卡片；
- 端口优先读 $PORT（发布平台约定），回退 $KB_WEB_PORT，再回退 8000。

生产部署请用 `python app.py`（配合 .env 切 pgvector + bge 嵌入 + 真实 LLM/API）。
"""

import glob
import os

# ---- 离线默认：避免依赖 Postgres / 真实嵌入模型 / 外部 API ----
os.environ.setdefault("KB_STORAGE_BACKEND", "memory")
os.environ.setdefault("KB_EMBEDDING_BACKEND", "dev")
os.environ.setdefault("KB_LLM_ENABLED", "0")

ROOT = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(ROOT, "kb_store.json")

# 演示用：每次重新播种示例知识库，保证线上演示内容稳定
if os.path.exists(STORE_PATH):
    os.remove(STORE_PATH)

import app  # 触发 app 模块级初始化（空 store / dev 嵌入器 / 摄取管线）
from kb_mcp_server.config import set_cfg
from kb_mcp_server.ingestion import IngestionPipeline

# 1) 内置示例 FAQ（与控制台「载入示例 FAQ」一致）
for doc_id, text in app.SAMPLE.items():
    app._ingestor.ingest_text(doc_id, text)

# 2) 摄取 samples/ 与 test-docs/ 下的文档（md / txt / docx，零依赖解析）
for folder in ("samples", "test-docs"):
    fdir = os.path.join(ROOT, folder)
    if not os.path.isdir(fdir):
        continue
    for fp in sorted(glob.glob(os.path.join(fdir, "**", "*"), recursive=True)):
        if not os.path.isfile(fp):
            continue
        if not fp.lower().endswith((".md", ".txt", ".docx")):
            continue
        name = os.path.basename(fp)
        doc_id = f"{folder}_{os.path.splitext(name)[0]}"
        with open(fp, "rb") as fh:
            data = fh.read()
        app._ingestor.ingest_file(doc_id, name, data)

# 3) 实时数据走 mock 样例：离线即可演示「知识 + 实时数据」合成卡片
set_cfg("KB_API_MOCK", "1")
# 重新加载适配器，使其按新的 mock 配置生效（适配器在导入时已缓存 mock 标志）
from kb_mcp_server.adapters import reload_adapters
reload_adapters()

# 端口：发布平台通常用 PORT，本地回退 KB_WEB_PORT，再回退 8000
port = int(os.environ.get("PORT") or os.environ.get("KB_WEB_PORT") or "8000")
print(f"[demo] 知识库已载入片段数={app._store.count()}  ->  http://0.0.0.0:{port}")

from http.server import ThreadingHTTPServer

ThreadingHTTPServer(("0.0.0.0", port), app.Handler).serve_forever()
