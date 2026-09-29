# syntax=docker/dockerfile:1
# =============================================================
# kb-mcp-server 容器化
# 默认「轻量离线」：memory 存储 + dev 嵌入，纯 Python，镜像约 150MB
# 需要本地 bge 语义嵌入：docker build --build-arg ENABLE_BGE=true .
# 需要 pgvector 生产后端：--build-arg ENABLE_PGVECTOR=true（配合 compose 的 postgres）
# =============================================================

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- 可选重依赖开关（默认关，保持镜像小）----
ARG ENABLE_BGE=false
ARG ENABLE_PGVECTOR=false

# 轻量核心依赖（不直接 -r requirements.txt，避免把 torch/pg 全装上）
#
# rank_bm25 + numpy：**核心检索链路**（BM25 混合召回）。
#   注意二者在 retrieval.py 里是 `try: from rank_bm25 import ... except: self._bm25 = None`
#   的延迟导入——**缺失不会报错，只会静默降级为纯向量检索**，使容器行为与
#   CI/本地已测试的行为不一致（test_retrieval.py 正是测 BM25 融合的那 27 例）。
#   体积代价小（约 20MB），必须装。
# pypdf：PDF 摄取（ingestion.py 同样延迟导入，缺失时仅 PDF 格式不可用）。
RUN pip install --no-cache-dir "mcp<2" pydantic python-dotenv rank-bm25 numpy pypdf

# 可选：pgvector 客户端（配合外部 Postgres + pgvector）
RUN if [ "$ENABLE_PGVECTOR" = "true" ]; then \
        pip install --no-cache-dir "psycopg[binary]" pgvector; \
    fi

# 可选：bge 本地语义嵌入（会拉 torch，镜像暴涨到 2GB+，谨慎）
RUN if [ "$ENABLE_BGE" = "true" ]; then \
        pip install --no-cache-dir sentence-transformers; \
    fi

# 代码（只带运行 Web 控制台必需的部分）
COPY kb_mcp_server ./kb_mcp_server
COPY app.py ./
COPY static ./static

# 默认运行时环境（docker run -e / compose 可覆盖）
ENV KB_STORAGE_BACKEND=memory \
    KB_EMBEDDING_BACKEND=dev \
    KB_GRAPH_BACKEND=memory \
    KB_API_MOCK=1 \
    KB_LLM_ENABLED=0 \
    KB_DATA_DIR=/app/data \
    KB_NO_BROWSER=1 \
    PORT=8000

RUN mkdir -p /app/data /app/logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

CMD ["python", "app.py"]
