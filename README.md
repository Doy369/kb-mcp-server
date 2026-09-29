# KB MCP Server · 企业级 B2B 客服工单知识库 RAG 系统

> 基于 **MCP 协议 + Python 3.13** 的可调用外部 API 的知识库。给 Agent 一套「检索知识 + 调用实时数据 + 结构化合成答复」的工具，让客服工单场景的问答既**有据可依**（知识库）、又**实时准确**（订单/库存等外部系统）。

🌐 **在线演示**：<https://Doy369.github.io/kb-mcp-server/>

> 演示为离线模式（本地私有化检索 + mock 实时数据），无需任何外部依赖，开箱即跑。
> （页面挂在 GitHub Pages；后端 Render 可选——见「演示部署」章节。静态部署模式下侧边栏会显示"📦 静态演示"。）

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
- **实时数据接入韧性（P0-2）**：订单 / 库存适配器统一支持**重试（指数退避）+ 熔断
  （按后端独立，开路期不发调用）+ 降级**——后端不可用时返回 `degraded` 卡片并在答复中
  写明「实时数据暂不可用」，问答主链路不中断；`/api/status` 暴露熔断状态与降级计数。
- **多 agent 协作（P7 / P1-4）**：编排器调度多个职责 agent，按问题**动态组队**（LLM 任务分解）、
  证据不足时**多轮协商补人**、需复核时**转人工队列**；全过程留可观测轨迹。
- **动作型工具 + 真实 MCP（P2-9）**：agent 不止会答，还能**真正执行**「建工单 / 改单 / 退款」，
  按风险分级（read / write / destructive）+ **确认门**（不可逆动作未确认绝不执行）+ 全量审计；
  MCP 侧已由真实 stdio 子进程 + 官方客户端完成协议端到端验证（CI `mcp` job）。
- **质量保障（P2-10）**：**282 例 pytest 单测**（离线约 12s 跑完）+ **GitHub Actions CI 七 job**
  —— ① 单测 → 回归评测 → 基线校验（通过率低于阈值即阻断合并）；
  ② 容器镜像构建 → 启动 → 健康检查 → 容器内端到端冒烟；
  ③ 真实 Postgres + pgvector：建表 → 完整回归跑在 PG → 校验 schema 与落库数据；
  ④ 真实 MCP 协议：stdio 子进程 + 客户端握手 → 工具调用 → 动作确认门与审计；
  ⑤ **负载测试**：真实 HTTP 并发，断言零错误 / 并发写入不丢数据 / 限流精确 / 指标自洽；
  ⑥ **真实 Apache AGE**：扩展 → 建图 → 不静默降级 → 真实摄取写入 → 原始 `ag_catalog` 表对账；
  ⑦ **真实 bge 嵌入**（夜间 + 手动，阈值 0.95）——保「质量上限」，不拖慢每次推送。
- **容器化（P0-3）**：`Dockerfile` + `docker-compose.yml`，镜像按 build arg 分档
  （轻量 ~150MB / 含 bge / 含 pgvector），内置 `HEALTHCHECK`；构建与启动由 CI 每次提交验证。

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
│   ├── synthesis.py        # 合成层（知识 + 图谱 + 实时数据 + 动作结果 → 结构化答复）
│   ├── adapters.py         # 外部 API 适配器（订单/库存）
│   ├── actions.py          # 动作型工具实现层（P2-9：ActionRunner 校验/确认门/审计 + 4 个动作）
│   ├── agents/             # 多 agent 协作层（P7）
│   │   ├── base.py         #   AgentContext 黑板 + BaseAgent 模板（计时/异常兜底/参与闸门）
│   │   ├── workers.py      #   GraphBuilder / Retriever / GraphReasoner / LiveData / ActionAgent / Synthesizer
│   │   └── orchestrator.py #   编排器（deterministic / llm 路由，多路并行 + 协商补轮 + HITL）
│   └── __main__.py
├── app.py                  # Web 控制台（HTTP 服务 + 前端）
├── run_demo.py             # 演示入口（离线播种，用于云端/演示部署）
├── static/index.html       # 前端控制台页面（含知识图谱面板）
├── demo_offline.py         # 离线自检脚本（摄取→检索整条链路）
├── demo_graph.py           # 图谱自检脚本（建图→多跳→推理路径→融合答复）
├── demo_agents.py          # 多 agent 自检脚本（补图→并行召回→合成→轨迹）
├── setup_db.py             # pgvector 建表初始化（--graph 额外初始化 AGE 图）
├── make_samples.py         # 生成多格式测试样本
├── fake_order_api.py       # 本地假订单后端（验证字段映射用）
├── eval_run.py             # 回归评测入口（golden 集 → 基线指标 + eval_report.json）
├── bench_scale.py          # 规模化召回评测（992 条公开语料，Recall/MRR/NDCG/Precision）
├── pytest.ini
├── tests/                  # 自动化测试（282 例，离线零依赖，见 tests/README.md）
├── scripts/
│   ├── check_baseline.py   # CI 基线校验：通过率低于阈值则退出码 1（阈值/报告路径可覆盖）
│   ├── check_pg.py         # CI 存储校验：schema 落库 / 数据非空（堵静默退回 memory）
│   ├── check_mcp.py        # P2-9 真实 MCP 协议端到端（stdio 子进程 + 官方客户端）
│   ├── check_load.py       # P2-10 补：真实 HTTP 并发压测（零错误 / 不丢数据 / 限流精确）
│   ├── check_age.py        # P2-10 补：真实 Apache AGE 图侧（扩展/建图/不降级/原始表对账）
│   └── verify_collaboration.py  # P1-4 动态协作真实链路联调
├── .github/workflows/ci.yml  # CI 七 job：单测+评测+基线 / 容器 / PG / MCP / 负载 / AGE / bge(夜间)
├── Dockerfile              # 容器镜像（默认轻量档，ARG 可选 bge / pgvector）
├── docker-compose.yml      # 编排：web + 数据卷 + 可选 pgvector 服务
├── .dockerignore
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
| `multi_agent_ask(question, top_k, order_id, sku, history)` | **多 agent 协作问答**：编排器调度 5 个职责 agent，答复附带每个 agent 的耗时/成败/摘要轨迹 |
| `agent_status()` | agent 清单、当前编排模式、规划器与动作层概况 |

#### 动作型工具（P2-9）—— 与检索型工具的本质区别是**有副作用**

| 工具 | 说明 |
|------|------|
| `list_actions()` | 列出可用动作（风险分级 / 是否需要确认 / 参数契约）+ 最近动作审计 |
| `run_action(action, params, confirmed, actor)` | 执行动作（建工单 / 改单 / 退款 / 查工单）。**destructive 动作未 `confirmed=true` 一律不执行**，只回 `status=needs_confirmation` |

风险分级：`read`（只读，如查工单）/ `write`（改业务数据，如建工单、改单）/ `destructive`（不可逆或涉及资金，如退款）。
每次尝试（含被拒 / 待确认）都写动作审计（`KB_ACTION_LOG`，默认 `kb_actions.jsonl`）。

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
     ┌───────────┼───────────┬───────────┐  （无依赖 worker 并行执行）
     ▼           ▼           ▼           ▼
  Retriever  GraphReasoner  LiveData   ActionAgent  ← 各自只写黑板（AgentContext）里自己的字段
  语义召回     图谱多跳       订单/库存    动作执行（P2-9，默认关闭）
     └───────────┼───────────┴───────────┘
                 ▼
              Synthesizer ──▶ 结构化答复 + agent 轨迹 + 动作结果
```

- **按职责切，不按知识域切**（初期知识域太小，按域切会切出一堆空 agent）。
- 协作靠共享黑板（`AgentContext`）传递中间结果，**不引入消息总线**——协议仍然是 MCP（决策 D6）。
- 每个 agent 只写自己负责的字段；单个 agent 失败只降级、不阻断链路（沿用全项目的降级约定）。

### 两种编排模式（`KB_AGENT_MODE`）

| 模式 | 行为 | 适用 |
|------|------|------|
| `deterministic`（默认） | 固定流水线，零 LLM 依赖 | 离线演示 / 生产兜底 |
| `llm` | 本地 LLM 判断要不要查图 / 查实时数据，裁剪流水线 | LLM 可用时降低延迟；失败自动回退 deterministic |

### 动态协作（P1-4）：按问题临时组队，而不是固定全跑

三个可选开关，**默认全关时行为与旧版固定流水线完全一致**：

| 配置 | 默认 | 作用 |
|---|---|---|
| `KB_AGENT_PLANNER` | 跟随 `KB_AGENT_MODE`（llm 模式 → `llm`） | `llm` 时由 LLM 把问题**动态分解**为子任务、从能力清单里选人；LLM 不可达 / 输出非法一律回退确定性规划 |
| `KB_AGENT_MAX_ROUNDS` | `1` | `>1` 启用**证据协商**：worker 产出后由 `EvidenceCritic` 评估证据缺口，点名补查缺失能力的 agent（只提未执行过的能力，必然收敛） |
| `KB_AGENT_HITL` | `0` | `1` 时护栏判定「需人工复核」的答复带 `pending_human=true`，供上层挂起等待人工放行 |

协作过程落在返回的 `collaboration` 段（每轮分工 / 子任务理由 / 协商结论），例如：

```
规划器=llm  轮次=2  协商=True
  第1轮 [llm] Retriever          → 协商[需补轮] 缺: ['live']
      · Retriever（retrieval）：纯知识问答，只需语义检索
  第2轮 [negotiation] LiveData
```

设计取舍：**LLM 只用在「任务分解」上**（这一步真的需要语义理解），
「证据够不够」这类判定用确定性规则——可解释、零额外 token、离线可测。
两条降级路径都保证「开启协作不会比不开更脆弱」。

### 轨迹即可观测性

`multi_agent_ask` 的返回里带 `agents.trace`——每个 agent 的名称、耗时、成败、一句话摘要：

```
[OK] GraphBuilder    10ms  图谱已是最新（4 篇已入图）
[OK] Retriever        18ms  召回 1 个片段
[OK] GraphReasoner    18ms  命中 1 个实体，10 条关系事实
[OK] LiveData          1ms  取到 1 条实时数据
[OK] Synthesizer       1ms  合成完成（template，置信度高，5 条关系路径）
```

### 动作型工具（P2-9）：从「只会答」到「能执行」

编排层默认只检索不动作。开 `KB_AGENT_ACTIONS=1` 后多一个 `ActionAgent`，
它识别「退款 / 改单 / 建工单」等动作意图并**真正执行**；有副作用，因此设三道闸：

| 闸门 | 机制 |
|---|---|
| **参与闸门** | `ActionAgent.gated_by = KB_AGENT_ACTIONS`，默认关；`extensions.agent_gate_open` 保证未获准的 agent 连 LLM 动态组队的候选池都进不去——「谁有权动手」是治理问题，不交给模型决定 |
| **意图闸门** | 只认规则能明确识别的动作意图，识别不出就什么都不做（宁可不做，不可乱做：猜错一次就是一次错误的退款） |
| **确认闸门** | `ActionAgent` 的 `confirmed` **恒为 False**。destructive 动作只会停在 `needs_confirmation`，是否放行永远由人工 / HITL 决定 |

**与 P1-4 闭环**：存在待确认动作时，若 `KB_AGENT_HITL=1`，答复一并标 `pending_human=true`
（否则「agent 发起了退款」会静默通过，确认门形同虚设）。合成层新增【执行动作】段，
三种终态——已执行 / 待确认 / 被拒——在答复与前端里都能一眼区分。

三条安全底线收口在 `ActionRunner`（校验 → 确认门 → 执行 → 审计），新动作注册即自动获得：

```
$ python scripts/check_mcp.py     # 真实 stdio 子进程 + 官方 MCP 客户端
  [OK] 未确认的退款被拦在待确认（未执行）
  [OK] 待确认动作没有产生业务结果（确实没执行）
  [OK] 确认后执行成功：RF-6552F732
  [OK] list_tickets 能回读出前面建的工单（count=1）   ← 副作用真的落盘了
```

### 自检

```bash
python demo_agents.py                  # 固定流水线协作（只入向量库 → 自动补图 → 多路召回 → 合成）
python scripts/verify_collaboration.py # P1-4 动态协作：任务分解 / 协商补轮 / 降级 / 人工介入
python scripts/check_mcp.py            # P2-9 真实 MCP 协议端到端（握手 / 工具 / 动作确认门 / 审计 / 多 agent）
python scripts/check_load.py           # P2-10 补：真实 HTTP 并发压测（会临时起 app 子进程）
python scripts/check_age.py            # P2-10 补：真实 Apache AGE 图侧（需目标 PG 带 age 扩展）
```

`check_load.py` 的四个场景与实际断言：

```
[1] 只读并发        并发 8 发 160 次 /api/ask：~150 req/s，p50 29ms / p95 38–58ms / p99 ~515ms
[2] 混合读写        并发 40 写 + 80 读：写入成功 40，store 实际 +40（丢失 0）
[3] 限流门          限流 5/min：并发发 24 次 → 放行 5、限流 19；/healthz 不受限流误伤
[4] 指标自洽        /api/metrics requests_total 不少于本次发送数
```

> 压测脚本会启动真实 app 子进程并**强制关闭浏览器自动打开**（`KB_NO_BROWSER=1`）：
> 浏览器会真的加载控制台页面、打出一串 API 请求，从而**吃掉限流配额并污染指标计数**——
> 这是实测踩到的坑（限流场景「应放行 5」被观测成「放行 1」），也是 `KB_NO_BROWSER` 存在的理由。

完整演示「知识只入向量库 → GraphBuilder 自动补图 → 多路并行召回 → 合成 → 第二轮秒回」。
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
| 协作 | `KB_AGENT_MODE` / `KB_AGENT_PLANNER` / `KB_AGENT_MAX_ROUNDS` / `KB_AGENT_HITL` | 编排模式 / 动态任务分解 / 证据协商轮次 / 人工介入 |
| 动作 | `KB_AGENT_ACTIONS` / `KB_ACTION_LOG` | 动作执行者是否参与编排（默认 `0`） / 动作审计日志路径（`off`=关闭） |

> 页面「接口配置」提交的配置会持久化到 `runtime_config.json`，优先级高于 `.env`。

---

## 🏭 生产部署

### 方式一 · 容器（推荐）

```bash
docker compose up -d          # 起 web 服务，数据落 named volume
# 打开 http://localhost:8000
```

镜像默认只装**离线档**依赖（memory 存储 + dev 嵌入），约 150MB；需要更强能力用 build arg 分档：

```bash
docker build --build-arg ENABLE_BGE=true      -t kb-mcp-server:bge .   # 本地 bge 语义嵌入（拉 torch，镜像 2GB+）
docker build --build-arg ENABLE_PGVECTOR=true -t kb-mcp-server:pg  .   # pgvector 客户端
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `KB_DATA_DIR` | `/app/data` | 数据目录（compose 已挂 named volume 持久化） |
| `PORT` | `8000` | 监听端口 |
| `KB_STORAGE_BACKEND` | `memory` | `memory` ⇄ `pgvector` |
| `KB_EMBEDDING_BACKEND` | `dev` | `dev` ⇄ `bge` |
| `KB_API_MOCK` | `1` | 实时数据走 mock |
| `KB_API_MAX_RETRIES` | `2` | 实时后端重试次数（总尝试 = 该值 + 1） |
| `KB_API_RETRY_BACKOFF` | `0.5` | 指数退避基数（秒），单次退避上限 8s |
| `KB_API_CIRCUIT_BREAKER` | `1` | 熔断开关；开路期不再发起调用 |
| `KB_API_CIRCUIT_THRESHOLD` | `5` | 连续失败多少次后开路 |
| `KB_API_CIRCUIT_COOLDOWN` | `30` | 开路冷却秒数，之后放半开探测 |
| `KB_AGENT_ACTIONS` | `0` | `1` 时 ActionAgent 参与编排（不可逆动作仍只停在待确认） |
| `KB_ACTION_LOG` | `kb_actions.jsonl` | 动作审计日志路径（`off`=不落盘） |

镜像内置 `HEALTHCHECK` 探 `/healthz`（`docker compose ps` 可直接看健康状态）。
需要 pgvector 时，`docker-compose.yml` 里已备好注释掉的 `pgvector/pgvector:pg16` 服务，取消注释即可。

> **这一步已被 CI 持续验证**：每次提交都会构建镜像、启动容器、轮询健康检查，
> 并在容器内真实调用 `/api/ask` 做端到端冒烟（`.github/workflows/ci.yml` 的 `docker` job）。
> 因此「镜像能构建、容器能跑」不是一次性检查，而是提交门禁的一部分。

> **pgvector 生产存储同样被 CI 真跑**：`pg` job 用 `pgvector/pgvector:pg16` service container
> 起真实 Postgres → `python setup_db.py --graph` 建表 → **完整回归评测读写全部落在 PG** →
> `scripts/check_pg.py` 校验「vector 扩展 / `kb_chunks` 表 / HNSW 索引 / `vector(1024)` 维度」与
> 「表内非空」。最后两项专门堵住「存储连不上 → 悄悄退回 memory → 评测照样过」的静默降级。
> 注：该镜像不含 Apache AGE，`--graph` 的建图部分会按设计降级为 memory 图（不阻断）。

### 方式二 · 直接跑（无容器）

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

### 演示部署 · GitHub Pages（仅前端，零成本）

> 适合作品集/汇报/界面预览。零成本、零后端，但功能受限——页面上按钮可点，但
> 摄取/检索/对话等 API 调用会返回 404（页面顶部会出现"📦 静态演示模式"蓝色横幅提示）。

**Step 1 · 启用 GitHub Pages**

仓库 → **Settings** → **Pages**：
- **Source**: Deploy from a branch
- **Branch**: `main` · **Folder**: `/docs`
- Save

几分钟后站点出现在 `https://<user>.github.io/kb-mcp-server/`（默认 Doy369 用户）。

无需修改任何代码——`docs/index.html` 里 `window.KB_API_BASE` 保持空字符串即可。

---

## 🧪 质量保障（P2-10）

### 自动化测试

```bash
pip install pytest
python -m pytest tests/ -v      # 282 例，离线约 12s
```

覆盖范围（全部零外部依赖，dev 嵌入 + memory 后端）：

| 文件 | 例数 | 覆盖重点 |
|---|---|---|
| `test_ingestion_storage.py` | 43 | 分块三要素（标题前缀 / 一行多档拆条 / Q-A 成对）、嵌入单例、存储持久化与容错、图谱本体约束、**PG 懒连接 / 注册时序 / 写库参数适配** |
| `test_retrieval.py` | 27 | **BM25 分数越界回归护栏**、RRF 融合、MMR 去重、硬阈值、分词、余弦边界 |
| `test_eval_guardrail.py` | 36 | **数字边界断言**（防假通过）、证据段剔除、禁止词反向断言、护栏分级、审计容错 |
| `test_agents_mcp.py` | 24 | Agent 异常兜底、黑板隔离、路由裁剪、**降级链路**、合成契约、16 工具注册完整性 |
| `test_adapters_resilience.py` | 29 | 重试次数语义与指数退避封顶、熔断状态机、降级不抛异常、失败不写缓存 |
| `test_planner_collaboration.py` | 48 | 动态任务分解的四条降级回退、协商只提未执行能力、多轮补轮不重复执行、HITL 开关 |
| `test_actions.py` | 53 | 动作参数契约、**确认门**、审计三终态、意图识别与参数抽取、参与闸门、HITL 闭环 |
| `test_baseline_gate.py` | 11 | **基线校验脚本本身**：阈值覆盖、报告路径三来源、恰好等于阈值放行、低于必红、缺失必失败 |
| `test_graph_age_cypher.py` | 11 | **AGE `cypher()` 列定义契约**：列数必须等于 `RETURN` 表达式数（多了少了一律报错）。假连接抓 SQL 断言各读接口列数，**不需要真实 PG** |

> `test_baseline_gate.py` 的理由：该脚本**同时把守 `test` 与 `bge` 两个 job**。
> 参数解析写错时表现不是报错，而是**静默失效**——CI 照样绿，只是不再拦任何东西。

> `test_graph_age_cypher.py` 锁的是一条**只在真机上才暴露**的契约：AGE 的 `cypher()`
> 返回 `SETOF record`，PostgreSQL 会逐一比对列定义列表与 `RETURN` 表达式数，不符即报
> `return row and column definition list do not match`。`_cypher()` 曾恒声明单列
> `(v agtype)`，而 `find_entities` / `neighbors` / `paths` / `export_graph` 都是多列
> `RETURN`——本地单测（memory 后端）永远走不到，于是「一直没错」；直到 `age` job 在
> 真实 AGE 上首跑即崩。把契约钉进单测后，任何新接口忘了传 `nout` 都会在本地就红。

> 其中多条用例直接锁住 ROADMAP 记录过的真 bug——**不报错、只是答案悄悄变差**的那类问题，
> 没有测试就只能靠肉眼发现。

### CI 流水线

`.github/workflows/ci.yml` 七个 job：

| job | 触发 | 内容 | 验证什么 |
|---|---|---|---|
| `test` | 推送 / PR | 单测 → 回归评测 → 基线校验 | 逻辑正确性；通过率低于基线（72%）则**阻断合并** |
| `docker` | 推送 / PR | 构建镜像 → 启动 → 健康检查 → 容器内端到端冒烟 | 「镜像能构建」且「容器能跑」（build 成功 ≠ 能跑） |
| `pg` | 推送 / PR | 起真实 PG + pgvector → 建表 → 回归跑在 PG → 校验 schema 与落库数据 | 生产存储路径真通，且**没有静默退回 memory** |
| `mcp` | 推送 / PR | stdio 子进程 + 官方客户端走完整 JSON-RPC | MCP 是**协议实质**而非标题；动作确认门与审计真落盘 |
| `load` | 推送 / PR | 真实 HTTP 并发压测（只读 / 混合读写 / 限流门 / 指标自洽） | 并发下**不丢数据、限流精确、零错误** |
| `age` | 推送 / PR | 官方 `apache/age` 容器 → 扩展 → 建图 → **AGE 语法能力探针**（含多列 `RETURN` 列数契约）→ 真实摄取写入 → 原始表对账 | 图侧真跑，且 `get_graph_store()` **没有静默降级**成 memory 图 |
| `bge` | **夜间 03:00 + 手动** | 真实 bge 嵌入回归（阈值 **0.95**） | 质量**上限**；缓存 1.3GB 模型，不拖慢每次推送 |

评测报告作为 artifact 归档 30 天；`load` / `age` 的报告同样归档。

**为何 bge 不跟每次推送**：dev 嵌入下通过率 91%（语义级断言前 82%；唯一 FAIL 是 P2 哨兵用例，
受限于 dev 对 `P0/P1/P2` 字面相似片段的区分能力），bge 下 **100%**。
装 torch + 下 1.3GB 模型会让单次 CI 从 ~40s 涨到数分钟——高频 job 一旦变慢就会被习惯性忽略。
所以**职责分离**：推送跑快口径（保「不许变差」），夜间跑重口径（保「质量上限」），
两者**共用同一份 `check_baseline.py`**，只是阈值与报告路径不同。

### 本地复现 CI 的基线校验

```bash
python eval_run.py                 # 产出 eval_report.json（dev 口径）
python scripts/check_baseline.py   # 与 CI 同一份逻辑，通过则退出码 0

# bge 口径（报告写独立文件，别覆盖 dev 基线；阈值单独指定）
python eval_run.py --embedding bge --out eval_report_bge.json
KB_BASELINE_MIN=0.95 python scripts/check_baseline.py eval_report_bge.json
```

---

## 📌 备注

- `runtime_config.json`、`kb_store.json`、`.env` 均已 gitignore，不进仓库（含本地私有数据）。
- 离线 `dev` 嵌入为条目哈希实现，语义召回能力有限，仅用于跑通管线与演示；生产请用 `bge`。
- 外部 API 适配器为可扩展框架：继承 `APIAdapter` 实现 `call()` + `_mock()`，在 `build_registry` 注册即可新增后端（CRM / 工单系统等）。

---

## 📄 License

内部 B2B 项目，使用请遵循团队内部约定。
