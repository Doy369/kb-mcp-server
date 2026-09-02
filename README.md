# KB MCP Server · 企业级 B2B 客服工单知识库 RAG 系统

> 基于 **MCP 协议 + Python 3.13** 的可调用外部 API 的知识库。给 Agent 一套「检索知识 + 调用实时数据 + 结构化合成答复」的工具，让客服工单场景的问答既**有据可依**（知识库）、又**实时准确**（订单/库存等外部系统）。

🌐 **在线演示**：<https://db4cf4dd1f9242009c50427cdd5a79.app.workbuddy.link>

> 演示为离线模式（本地私有化检索 + mock 实时数据），无需任何外部依赖，开箱即跑。

---

## ✨ 特性

- **MCP 工具化**：通过 MCP 协议把知识库能力暴露给任意 MCP 客户端（Claude Desktop、自研 Agent 等）。
- **混合检索（RAG）**：向量语义召回 + 关键词，命中片段带相似度分数与可解释来源。
- **知识图谱（P6 · GraphRAG）**：摄取时自动抽取三元组建图；问答时语义召回与图谱多跳**并行**，答复附带**可解释关系路径**（路径即证据）。
- **实时数据接入（P4）**：内置订单状态 / 库存适配器，按问题意图自动调用外部 API，合成为「知识 + 实时数据」结构化答复（含置信度与轨迹）。
- **多格式摄取**：`.md / .txt / .docx`（零依赖解析），支持单文档、文件夹批量、文件上传。
- **可切换存储 / 嵌入**：
  - 存储：`memory`（纯 Python 离线开发）⇄ `pgvector`（生产，Postgres + 向量索引）。
  - 嵌入：`dev`（离线条目哈希，零依赖）⇄ `bge`（sentence-transformers 本地模型，数据不出域）。
- **Web 控制台**：内置前端（`static/index.html`），知识摄取、检索、指标、知识图谱、Agent 协作、接口配置、对话一体。
- **生产加固（P5）**：可选 Bearer 鉴权、按 IP 限流、结构化访问日志、`/api/metrics` 指标。
- **桌面客户端**：可用 PyInstaller 打包为单文件 exe，原生窗口承载控制台（pywebview）。

---

## 🏗 架构

```
   MCP Client ──▶ MCP Server (FastMCP)
   (Agent / IDE)  ingest · search · ask_with_live
                  graph_query · graph_expand · graph_paths
                          │
                          ▼
     ┌──────────────────────────────────────────────────┐
     │               KB MCP Server 核心                 │
     │                                                  │
     │  Ingestion ──┬─▶ Embeddings ────▶ 向量库          │
     │              └─▶ TripleExtractor ▶ 知识图谱       │
     │                                                  │
     │  Retrieval(向量+BM25) ─┐                         │
     │  graph.expand_facts ───┼─▶ Synthesis 合成答复     │
     │  Adapters(实时数据) ───┘                         │
     └────┬──────────────┬───────────────┬─────────────┘
          │              │               │
          ▼              ▼               ▼
   memory / pgvector  memory / AGE    订单 · 库存 · CRM
   （向量：管语义）    （图谱：管关系）  （实时数据）
          └──────── 同一个 PostgreSQL 实例 ─────────┘
                          │
                          ▼
   Web 控制台 (app.py → static/index.html)
   知识库 / 检索·指标 / 知识图谱 / 接口配置 / 对话
```

> **GraphRAG 的分工**：向量检索回答「哪段话像这个问题」，知识图谱回答「这些实体之间什么关系、能推出什么」。
> 两者互补——图谱是向量库的**关系增强层**，不是替代品。

---

## 📁 目录结构

```
kb-mcp-server/
├── kb_mcp_server/          # 核心包
│   ├── server.py           # MCP Server（FastMCP 工具定义，含图谱与多 agent 工具）
│   ├── config.py           # 配置（环境变量 + 运行时配置持久化）
│   ├── embeddings.py       # 嵌入层（DevEmbedder / BGEEmbedder）
│   ├── storage.py          # 向量存储（MemoryVectorStore / PGVectorStore）
│   ├── graph.py            # 知识图谱（本体 / 双后端 / 三元组抽取 / 多跳查询）
│   ├── llmclient.py        # 本地 LLM 客户端（代理绕过 + 60s 熔断，三处调用共用）
│   ├── ingestion.py        # 摄取管线（切片 + 嵌入 + 写入，顺带抽取三元组入图）
│   ├── retrieval.py        # 混合检索
│   ├── synthesis.py        # 合成层（知识 + 图谱 + 实时数据 → 结构化答复）
│   ├── adapters.py         # 外部 API 适配器（订单/库存）
│   ├── agents/             # 多 agent 协作层（P7）
│   │   ├── base.py         #   AgentContext 黑板 + BaseAgent 模板（计时/异常兜底）
│   │   ├── workers.py      #   GraphBuilder / Retriever / GraphReasoner / LiveData / Synthesizer
│   │   └── orchestrator.py #   编排器（deterministic / llm 路由，三路并行）
│   └── __main__.py
├── app.py                  # Web 控制台（HTTP 服务 + 前端）
├── run_demo.py             # 演示入口（离线播种，用于云端/演示部署）
├── client.py               # 桌面客户端（pywebview 原生窗口）
├── static/index.html       # 前端控制台页面（含知识图谱面板）
├── demo_offline.py         # 离线自检脚本（摄取→检索整条链路）
├── demo_graph.py           # 图谱自检脚本（建图→多跳→推理路径→融合答复）
├── demo_agents.py          # 多 agent 自检脚本（补图→并行召回→合成→轨迹）
├── setup_db.py             # pgvector 建表初始化（--graph 额外初始化 AGE 图）
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
- **📚 知识库**：粘贴文本入库 / 载入示例 FAQ / 选择文件夹或输入路径批量导入（自动建图）。
- **🔍 检索 · 指标**：测试查询、查看命中片段与相似度、查看 `/api/metrics` 运行指标。
- **🕸 知识图谱**：图谱统计、实体列表、点实体查多跳路径、用问题试关系侧召回、一键重建。
- **🤝 Agent 协作**：agent 团队清单、协作问答——答复附带每个 agent 的耗时/成败/摘要轨迹。
- **⚙ 接口配置**：在页面上填写外部 API 地址、鉴权、字段路径等，立即生效并持久化。
- **💬 对话**：多轮问答，自动召回知识 + 图谱路径 + 拉取实时数据（订单/库存）合成答复。

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
| `ask_with_live_data(question, top_k, order_id, sku, history)` | 检索 + 调外部 API + 查图谱 + 合成结构化答复（含置信度/关系路径/轨迹），支持多轮 `history` |
| `list_documents()` | 列出知识库所有文档（含片段数与预览） |
| `delete_document(doc_id)` | 删除某篇文档及其全部片段，并清理它在图谱中抽出的关系 |

#### 知识图谱工具（P6）

| 工具 | 说明 |
|------|------|
| `graph_expand(query, top_k, depth)` | **GraphRAG 关系侧召回**：从问题抽实体 → 查图谱多跳邻居 → 返回可解释路径 |
| `graph_query(entity, relation, direction, depth)` | 查某实体的关联（1-4 跳），返回每个邻居的**完整路径** |
| `graph_paths(src, dst, max_depth)` | 查两实体间的推理路径，例如「物流配送」到「全额退款」走了几跳 |
| `graph_entities(name, node_type, limit)` | 列出图谱实体（模糊匹配 / 按类型过滤），按关联度排序 |
| `graph_stats()` | 图谱概况：节点/边数量、类型分布、本体定义 |
| `graph_rebuild()` | 用向量库已有文档重新抽取建图（存量知识补建 / 换本体后重建） |

#### 多 agent 协作工具（P7）

| 工具 | 说明 |
|------|------|
| `multi_agent_ask(question, top_k, order_id, sku, history)` | **多 agent 协作问答**：编排器调度 4 个职责 agent，答复附带每个 agent 的耗时/成败/摘要轨迹 |
| `agent_status()` | agent 清单与当前编排模式 |

---

## 🕸 知识图谱（GraphRAG 关系层）

### 它解决什么问题

纯向量检索答不准**关系型多跳问题**——「这个客户的问题是不是和前几单同一根因」
「这条产品线适用哪条 SLA 例外条款」。这类问题的答案不在任何一段文本里，而在**实体之间的关系**里。
知识图谱就是把这层关系显式建出来，给向量检索补上「推理 + 可解释」。

### 本体（最小可行：8 类节点 / 7 类关系）

本体驱动的意义：抽取与查询都在这个边界内，不合规的三元组**直接丢弃**，图谱不会发散。

节点类型：

| 类型 | 含义 | 类型 | 含义 |
|------|------|------|------|
| `IssueCategory` | 问题类别 | `Product` | 产品 |
| `SLAClause` | SLA 条款 | `RootCause` | 根因 |
| `Solution` | 解决方案 | `Ticket` | 工单 |
| `Document` | 文档（知识来源） | `Customer` | 客户 |

关系（带主宾类型约束）：

| 关系 | 含义 | 主语 → 宾语 |
|------|------|-------------|
| `GOVERNED_BY` | 适用条款 | 问题类别 → SLA 条款 |
| `SOLVED_BY` | 解决方案为 | 问题类别 → 解决方案 |
| `CAUSED_BY` | 根因为 | 问题类别/工单 → 根因 |
| `ABOUT_PRODUCT` | 涉及产品 | 问题类别/工单 → 产品 |
| `CATEGORY_OF` | 归类为 | 工单 → 问题类别 |
| `SUBMITTED_BY` | 由…提交 | 工单 → 客户 |
| `MENTIONS` | 提及 | 文档 → 各类实体 |

### 三元组怎么来的

- **本地 LLM 抽取**（`KB_LLM_ENABLED=1`）：按本体约束输出 JSON 三元组，数据不出域。
- **规则词典兜底**（默认，离线零依赖）：关键词/正则匹配问题类别、SLA 时限、解决方案、产品、根因。
  例：「付款后 48 小时内发货」→ `(物流配送) --适用条款--> (48小时内发货)`。
- 抽取挂在摄取管线上：**每摄入一篇文档顺带把三元组写进图**，不需要额外步骤。
  图谱失败绝不阻断摄取——向量库写成功即算成功。

### 三种用法

1. **MCP 工具**（给任意 agent）：`graph_expand` / `graph_query` / `graph_paths`。
2. **Web 控制台**：侧栏「🕸 知识图谱」——看统计、翻实体、点实体查多跳、用问题试关系侧召回。
3. **代码**：

   ```python
   from kb_mcp_server.graph import get_graph_store, expand_facts

   g = get_graph_store()
   g.add_triples(triples)                       # 建图（本体校验内置）
   facts = expand_facts(g, "物流超时怎么赔偿")    # 关系侧召回，返回带路径的事实
   ```

### 自检

```bash
python demo_graph.py
```

跑完整链路：4 篇示例文档摄取建图 → 图谱统计 → 多跳查询 → 推理路径 → GraphRAG 融合答复。
实测输出（memory 后端、离线零依赖）：

```
节点 34 / 边 34
按类型: Document 4, IssueCategory 6, SLAClause 12, Solution 6, Product 4, RootCause 2
─
物流配送 --适用条款--> 48小时内发货
退款退货 --解决方案为--> 全额退款
退款退货 --提及--> sla_policy ; sla_policy --提及--> 服务响应 ; 服务响应 --解决方案为--> 全额退款
```

最后一行正是图谱的价值所在：两段知识在向量空间里毫无相似之处，图谱却把它们串成了一条
**跨文档、可解释的推理链**——这就是「路径即证据」。

---

## 🤝 多 agent 协作（P7）

### 架构：按职责切分，共享同一张图

```
                Orchestrator（编排 · 路由）
                 │ 增量补图
                 ▼
                GraphBuilder ──写入──▶ 知识图谱
                 │
     ┌───────────┼───────────┐  （三个无依赖 worker 并行执行）
     ▼           ▼           ▼
  Retriever  GraphReasoner  LiveData      ← 各自只写黑板（AgentContext）里自己的字段
  语义召回     图谱多跳       订单/库存
     └───────────┼───────────┘
                 ▼
              Synthesizer ──▶ 结构化答复 + agent 轨迹
```

- **按职责切，不按知识域切**（初期知识域太小，按域切会切出一堆空 agent）。
- 协作靠共享黑板（`AgentContext`）传递中间结果，**不引入消息总线**——协议仍然是 MCP（决策 D6）。
- 每个 agent 只写自己负责的字段；单个 agent 失败只降级、不阻断链路（沿用全项目的降级约定）。

### 两种编排模式（`KB_AGENT_MODE`）

| 模式 | 行为 | 适用 |
|------|------|------|
| `deterministic`（默认） | 固定流水线，零 LLM 依赖 | 离线演示 / 生产兜底 |
| `llm` | 本地 LLM 判断要不要查图 / 查实时数据，裁剪流水线 | LLM 可用时降低延迟；失败自动回退 deterministic |

### 轨迹即可观测性

`multi_agent_ask` 的返回里带 `agents.trace`——每个 agent 的名称、耗时、成败、一句话摘要：

```
[OK] GraphBuilder    10ms  图谱已是最新（4 篇已入图）
[OK] Retriever        18ms  召回 1 个片段
[OK] GraphReasoner    18ms  命中 1 个实体，10 条关系事实
[OK] LiveData          1ms  取到 1 条实时数据
[OK] Synthesizer       1ms  合成完成（template，置信度高，5 条关系路径）
```

### 自检

```bash
python demo_agents.py
```

完整演示「知识只入向量库 → GraphBuilder 自动补图 → 三路并行召回 → 合成 → 第二轮秒回」。
LLM 不可达时自动熔断（60s 内不再重试）并回退规则/模板，链路照常跑通。

---

## ⚙️ 配置

复制 `.env.example` 为 `.env` 后按需修改，关键项：

| 分组 | 变量 | 说明 |
|------|------|------|
| 存储 | `KB_STORAGE_BACKEND` | `memory`（离线） / `pgvector`（生产） |
| 图谱 | `KB_GRAPH_BACKEND` | `memory`（离线纯 Python 图） / `age`（Postgres + Apache AGE，与 pgvector 同库） |
| 图谱 | `KB_GRAPH_ENABLED` | 总开关，摄取时是否自动抽取三元组入图（默认 `1`） |
| 图谱 | `KB_GRAPH_NAME` | AGE 模式下的图名（默认 `kb_graph`） |
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
1. **图谱切 AGE（可选，与 pgvector 同库，不额外引入数据库）**
   ```bash
   # 前提：目标 PG 已安装 age 扩展（CREATE EXTENSION age 可用）
   KB_STORAGE_BACKEND=pgvector KB_GRAPH_BACKEND=age \
     KB_DATABASE_URL=postgresql://user:pass@host:5432/kb python setup_db.py --graph
   ```
   > AGE 不可用时会自动回退 `memory` 图并在日志里说明，主链路不受影响。
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
