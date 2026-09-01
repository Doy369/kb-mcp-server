# KB MCP Server · 企业级 B2B 客服工单知识库 RAG 系统

> 基于 **MCP 协议 + Python 3.13** 的可调用外部 API 的知识库。给 Agent 一套「检索知识 + 调用实时数据 + 结构化合成答复」的工具，让客服工单场景的问答既**有据可依**（知识库）、又**实时准确**（订单/库存等外部系统）。

🌐 **在线演示**：<https://db4cf4dd1f9242009c50427cdd5d7a79.app.workbuddy.link>

> 演示为离线模式（本地私有化检索 + mock 实时数据），无需任何外部依赖，开箱即跑。

---

## ✨ 特性

- **MCP 工具化**：通过 MCP 协议把知识库能力暴露给任意 MCP 客户端（Claude Desktop、自研 Agent 等）。
- **混合检索（RAG）**：向量语义召回 + 关键词，命中片段带相似度分数与可解释来源。
- **实时数据接入（P4）**：内置订单状态 / 库存适配器，按问题意图自动调用外部 API，合成为「知识 + 实时数据」结构化答复（含置信度与轨迹）。
- **多格式摄取**：`.md / .txt / .docx`（零依赖解析），支持单文档、文件夹批量、文件上传。
- **可切换存储 / 嵌入**：
  - 存储：`memory`（纯 Python 离线开发）⇄ `pgvector`（生产，Postgres + 向量索引）。
  - 嵌入：`dev`（离线条目哈希，零依赖）⇄ `bge`（sentence-transformers 本地模型，数据不出域）。
- **Web 控制台**：内置前端（`static/index.html`），知识摄取、检索、指标、接口配置、对话一体。
- **生产加固（P5）**：可选 Bearer 鉴权、按 IP 限流、结构化访问日志、`/api/metrics` 指标。
- **桌面客户端**：可用 PyInstaller 打包为单文件 exe，原生窗口承载控制台（pywebview）。

---

## 🏗 架构

```
                ┌──────────────────────────────────────────┐
   MCP Client ──▶│  MCP Server (FastMCP)                    │
   (Agent/IDE)   │  tools: ingest / search / ask_with_live │
                └───────────────┬──────────────────────────┘
                                │ 调用
                                ▼
                ┌──────────────────────────────────────────┐
                │            KB MCP Server 核心            │
                │  Ingestion ─▶ Embeddings ─▶ Storage      │
                │  Retrieval(混合) ─▶ Synthesis(合成)       │
                │  Adapters(订单/库存 … 实时数据)           │
                └───────┬───────────────────────┬──────────┘
         知识库(向量)   │                       │ 外部 API
         memory/pgvector│                       │ 订单/库存/CRM
                        ▼                       ▼
                ┌──────────────────────────────────────────┐
                │   Web 控制台 (app.py)  →  static/index.html│
                │   知识库 / 检索·指标 / 接口配置 / 对话      │
                └──────────────────────────────────────────┘
```

---

## 📁 目录结构

```
kb-mcp-server/
├── kb_mcp_server/          # 核心包
│   ├── server.py           # MCP Server（FastMCP 工具定义）
│   ├── config.py           # 配置（环境变量 + 运行时配置持久化）
│   ├── embeddings.py       # 嵌入层（DevEmbedder / BGEEmbedder）
│   ├── storage.py          # 存储（MemoryVectorStore / PGVectorStore）
│   ├── ingestion.py        # 摄取管线（切片 + 嵌入 + 写入）
│   ├── retrieval.py        # 混合检索
│   ├── synthesis.py        # 合成层（知识 + 实时数据 → 结构化答复）
│   ├── adapters.py         # 外部 API 适配器（订单/库存）
│   └── __main__.py
├── app.py                  # Web 控制台（HTTP 服务 + 前端）
├── run_demo.py             # 演示入口（离线播种，用于云端/演示部署）
├── client.py               # 桌面客户端（pywebview 原生窗口）
├── static/index.html       # 前端控制台页面
├── demo_offline.py         # 离线自检脚本（摄取→检索整条链路）
├── setup_db.py             # pgvector 建表初始化
├── make_samples.py         # 生成多格式测试样本
├── fake_order_api.py       # 本地假订单后端（验证字段映射用）
├── requirements.txt
├── .env.example            # 全部配置项示例
└── samples/ test-docs/     # 示例知识库文档
```

---

## 🚀 快速开始（离线模式）

### 1. 环境

- Python 3.13+
- 离线默认模式**零额外依赖**（仅 `pydantic`、`python-dotenv`）

```bash
pip install pydantic python-dotenv
# 生产模式按需：pip install "psycopg[binary]" pgvector
```

### 2. 运行 Web 控制台（最直观）

```bash
python app.py
# 自动打开浏览器：http://localhost:8000
```

控制台包含：
- **📚 知识库**：粘贴文本入库 / 载入示例 FAQ / 选择文件夹或输入路径批量导入。
- **🔍 检索 · 指标**：测试查询、查看命中片段与相似度、查看 `/api/metrics` 运行指标。
- **⚙ 接口配置**：在页面上填写外部 API 地址、鉴权、字段路径等，立即生效并持久化。
- **💬 对话**：多轮问答，自动召回知识 + 拉取实时数据（订单/库存）合成答复。

### 3. 运行 MCP Server

```bash
python -m kb_mcp_server.server
# 也可 mcp.run() 默认 stdio 传输，供 MCP 客户端连接
```

### 4. 演示 / 云端部署入口

```bash
python run_demo.py
# 强制离线默认 + 重新播种示例知识库 + mock 实时数据
# 端口优先读 $PORT，回退 $KB_WEB_PORT，再回退 8000
```

---

## 🧰 MCP 工具一览

| 工具 | 说明 |
|------|------|
| `ping` | 健康检查 |
| `ingest_document(doc_id, text, chunk_size, overlap)` | 把文档灌入知识库，返回切片数 |
| `search_knowledge(query, top_k, mode)` | 混合检索，返回带相似度分数的知识片段 |
| `ask_with_live_data(question, top_k, order_id, sku, history)` | 检索 + 调外部 API + 合成结构化答复（含置信度/轨迹），支持多轮 `history` |
| `list_documents()` | 列出知识库所有文档（含片段数与预览） |
| `delete_document(doc_id)` | 删除某篇文档及其全部片段 |

---

## ⚙️ 配置

复制 `.env.example` 为 `.env` 后按需修改，关键项：

| 分组 | 变量 | 说明 |
|------|------|------|
| 存储 | `KB_STORAGE_BACKEND` | `memory`（离线） / `pgvector`（生产） |
| 嵌入 | `KB_EMBEDDING_BACKEND` | `dev`（零依赖） / `bge`（本地模型） |
| 合成 | `KB_LLM_ENABLED` / `KB_LLM_BASE_URL` / `KB_LLM_MODEL` | 本地 LLM（OpenAI 兼容，如 Ollama），失败自动回退模板 |
| 实时 API | `KB_API_MOCK` / `KB_ORDER_API_URL` / `KB_INVENTORY_API_URL` / 字段路径 | `KB_API_MOCK=1` 走样例；配 URL 且 `=0` 走真实 HTTP |
| 加固 | `KB_API_TOKEN` / `KB_RATE_LIMIT` / `KB_LOG_FILE` | Bearer 鉴权 / 每 IP 限流 / 结构化日志 |

> 页面「接口配置」提交的配置会持久化到 `runtime_config.json`，优先级高于 `.env`。

---

## 🏭 生产部署

1. **存储切 pgvector**
   ```bash
   pip install "psycopg[binary]" pgvector
   # 目标 PG 安装 vector 扩展后：
   KB_STORAGE_BACKEND=pgvector KB_DATABASE_URL=postgresql://user:pass@host:5432/kb python setup_db.py
   ```
2. **嵌入切 bge（数据不出域）**
   ```bash
   pip install sentence-transformers
   KB_EMBEDDING_BACKEND=bge   # 默认 BAAI/bge-large-zh-v1.5
   ```
3. **启用本地 LLM 合成**：`KB_LLM_ENABLED=1` + 可达的 `KB_LLM_BASE_URL`（如 Ollama `http://localhost:11434/v1`）。
4. **接真实外部 API**：在 `.env` 配 `KB_ORDER_API_URL` / `KB_INVENTORY_API_URL` + 字段路径，并设 `KB_API_MOCK=0`。
5. **加固**：设置 `KB_API_TOKEN` 启用 Bearer 鉴权，`KB_RATE_LIMIT` 限流，`KB_LOG_FILE` 落结构化日志。

---

## 🖥 桌面客户端（可选）

```bash
pip install pywebview
python client.py            # 原生窗口承载控制台
# 打包为单文件 exe：
pyinstaller kb-mcp-client-v2.spec
```

---

## 📌 备注

- `runtime_config.json`、`kb_store.json`、`.env` 均已 gitignore，不进仓库（含本地私有数据）。
- 离线 `dev` 嵌入为条目哈希实现，语义召回能力有限，仅用于跑通管线与演示；生产请用 `bge`。
- 外部 API 适配器为可扩展框架：继承 `APIAdapter` 实现 `call()` + `_mock()`，在 `build_registry` 注册即可新增后端（CRM / 工单系统等）。

---

## 📄 License

内部 B2B 项目，使用请遵循团队内部约定。
