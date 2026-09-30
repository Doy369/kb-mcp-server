# 待解决优先级 · 上线路线图（ROADMAP）

> 当前状态：**可演示的企业级原型，核心链路已闭环**，完成度约 **92–95%**（2026-09-30 复核）。
> 已完成：真实 LLM 接入 + 语义级评测闭环、bge 真实嵌入、置信度护栏 + 审计、自动化测试 358 例 + CI 九 job 真实跑通
> （**九 job：`CI #23` / run `36677436460` 全绿**；八 job 基线：**`CI #21` / run `36673194795` 全绿**；七 job 基线：`CI #19` / run `36663136008`；
> 四 job 基线：`CI #13` / run `36541763060`，40s 全绿）、
> 语料对齐、规模化召回评测、**多 agent 动态协作（任务分解 / 依赖分层 / 双向协商 / 人工介入）**、
> **动作型工具 + 真实 MCP 协议端到端（协议握手 / 工具调用 / 确认门 / 动作审计）**、
> **负载测试 + 真实 Apache AGE 图侧 + bge 夜跑口径（P2-10 补）**、
> **实时适配器真实 HTTP 往返 + 可重试性分级（P0-2 收尾）**、
> **子任务依赖编排 + agent 间双向协商账本（P1-4 收尾）**、
> **动作执行结果回写图谱：执行 → 关系 → 再检索（P2-9 收尾）**。
> 本文件列出通往「真正上线的 B2B 多 agent 协作共同体」的待办，按优先级排序；
> 每项标注了**已预留的拓展接口**（位于 `kb_mcp_server/extensions.py`），补做时直接实现接口并注册即可，无需改动编排/合成主链路。
>
> ⚠️ **接口空壳清单（已定义但全仓库零引用）**：`TenantProvider` —— 对应 P2-6，
> 是「接口就位但功能未做」的准确标志。
> （`Planner` / `AgentRegistry` 已于 2026-09-29 由 P1-4 真实接入；`ActionTool` /
> `ActionToolRegistry` 已于 2026-09-29 由 P2-9 真实接入（实现层 `kb_mcp_server/actions.py`）；
> `Evaluator`、`ConfidenceGuardrail`、`RetryPolicy` 亦已真实接入。）

---

## P0 — 不补就不能算「能用」

### P0-1 真实 LLM 接入与效果评估
- 现状：demo 强制关 LLM；`llm` 路由模式仅在本地跑过一次超时熔断，答案质量/时延/成本零评估。
- **已推进（2026-09-09）**：`eval_run.py` 加 `--llm` / `--embedding` 开关 + 隔离 runtime_config（此前会误读页面配置）。
  用 DeepSeek 真 key 跑通 LLM 回归：模板 100%（11/11）vs LLM 82%（9/11）；
  2 个 FAIL 全是「LLM 改写丢关键词」的评测器误判（「7 天内」vs golden「7 个自然日」、「48小时内」vs「48 小时」），
  语义正确；P2 哨兵用例 LLM 下 PASS，证明 LLM 未引入检索错误。时延 p50 877ms → 1985ms（LLM 调用成本）。
- **✅ 语义级断言已落地（2026-09-18，P0-1 收官）**：`kb_mcp_server/eval.py` 从「字面口径」升级到「语义口径」，两层机制：
  1. `_norm()` 抹平**书写差异**：全角→半角、中文数字→阿拉伯（七/十五/四十八）、去空白、时间单位别名
     （`个自然日`/`天内`/`天` → `日`，`小时内`/`个小时` → `小时`）；
  2. **同义组**：golden 的 `expect_contains` 元素可写数组 `[["顺丰","SF Express"]]`，组内任一命中即算命中，
     用于 `_norm` 盖不住的**词汇差异**（显式声明，不靠算法猜，评测集保持可审计）。
  匹配策略 = 「原文命中 OR 归一化命中」双通道，只会放宽书写差异、不引入新漏判。
  **反向断言已专项验证未被放宽**：`48小时` 不会被 `4 小时`/`8 小时` 误命中（自测 14/14 通过）。
- **最终回归（语义口径，bge-large-zh-v1.5，n=11）**：模板 **11/11 = 100%**（置信度 0.791，p50 575ms）
  vs LLM **11/11 = 100%**（置信度 0.791，p50 1591ms）——**两种合成方式均满分，LLM 未引入检索或语义错误**。
  置信度两版完全一致，说明置信度由检索段决定、与合成方式无关（设计正确）。
  报告存档：`eval_report_bge_template.json`（模板）/ `eval_report_bge_llm.json`（LLM）。
- 待补：答案**自然度/完整性的人工评分**（语义级断言只能证明「没答错」，不能证明「答得好」）；
  以及 LLM 成本/稳定性长跑（当前仅 11 条样本，不足以评估限流与超时分布）。
- 接口：**`Evaluator` / `load_golden()`**（已预留，P1-5）。

### P0-2 实时数据真实接入（订单/库存/CRM）
- 现状（**韧性三件套已落地**，2026-09-29）：
  - ✅ 已有：真实 HTTP 路径、**超时**（`KB_API_TIMEOUT`）、**TTL 缓存**（`KB_API_TTL`）、
    **出站鉴权**（`KB_API_AUTH_SCHEME/HEADER/QUERY`，支持 bearer/header/query 三种）、
    字段映射可配（`KB_ORDER_STATUS_PATH` 等）、非法数值容错解析。
  - ✅ **本轮补齐：重试（指数退避）+ 熔断 + 降级**，由 `extensions.RetryPolicy` / `CircuitBreaker`
    实现、`adapters.APIAdapter` 接入（不再是「零引用空壳」）：
    - **重试**：`KB_API_MAX_RETRIES`（总尝试 = 该值 + 1）、`KB_API_RETRY_BACKOFF`（指数退避基数，
      `delay(n) = min(backoff * 2^(n-1), 8s)`，封顶防止一次问答被拖成分钟级）。
    - **熔断**：连续失败达 `KB_API_CIRCUIT_THRESHOLD` 即开路，开路期**不再发起下游调用**
      （实测从 `TimeoutError` 的 ~1s 降到 0.0001s）；冷却 `KB_API_CIRCUIT_COOLDOWN` 后放行一次
      半开探测，成功闭合、失败重新计时。**每个适配器独立熔断**——订单 API 挂掉不连累库存查询。
    - **降级**：重试耗尽**不抛异常**，返回 `degraded=True` 的结构化结果 → 归一化为 `degraded`
      卡片 → 合成层写明「实时数据暂不可用（重试 N 次仍失败）」。实测 `/api/ask` 在订单与库存
      后端全挂时仍正常返回知识依据，不 500。
    - **失败不写缓存**：否则一次网络抖动会被 TTL 放大成持续 30s 的错误，并掩盖熔断的快速失败。
    - **可观测**：`/api/status` 暴露每个适配器的 `circuit` 状态与 `degraded_calls` / `retries`
      计数——熔断是「看不见的故障」，没有指标只能靠「实时数据一直不可用」反推。
    此前 docstring 声称「统一重试」但 `_fetch` 一次失败即抛穿，属**文档过度承诺**，本轮一并纠正。
- ✅ **收尾已落地（2026-09-30）：真实 HTTP 往返联调 + 可重试性分级**。
  原状态是「只实测了 mock 和『后端连不上』两种」——单测把 HTTP 层整个 monkeypatch 掉
  （是**函数替身**，一行 socket 都没走），所以「真能跟一个真实 HTTP 服务完成一次往返」
  一直是未验证假设。本轮补上 `scripts/check_adapters_live.py`：起一个**真实 socket +
  真实 HTTP 协议**的本地替身后端（`ThreadingHTTPServer`），让真实 `urllib` 适配器打过去，
  **26 项断言**全部基于「服务端实际收到什么、被打多少次」——真实 200 字段映射 / 鉴权三模式 /
  TTL 缓存不产生新请求 / 5xx 重试计数 / 超时受 `timeout` 约束 / 熔断开路后请求数**冻结**且
  毫秒级快速失败 / 404 与 401 不重试 / 429 仍重试 / 瞬时故障自愈 / 端到端经 `/api/ask`
  带出**真实**后端数据（mock 模式不会有 `SHIPPED`）。CI 新增 `adapters` job 每次推送跑
  （报告 `adapters_live_report.json` 归档为 artifact）。
  **验收：`CI #21` / run `36673194795` 八 job 全绿**；`adapters` job 的 `::notice::` 注解凭据：
  **共 26 项断言，失败 0 项，真实后端（e2e）被调用 1 次**。
- ✅ **本轮抓出的真缺陷：可重试性一刀切**。旧 `RetryPolicy.call` 对**所有**异常一视同仁可重试：
  - 「单号不存在」的 404 重试 3 次还是 404——只把一次确定结果拖成三倍延迟、给下游平白加压；
    对外还被渲染成「实时数据暂不可用（重试 3 次仍失败）」，把「这个单号查不到」误导成「后端挂了」。
  - 更糟：401 鉴权失败也被计入熔断，几次调用后**熔断被打开、此后所有请求被短路**，
    真正的原因（配置写错）被彻底掩盖。
  修复（三条）：
  1. `RetryPolicy.call(fn, retryable=)` 新增可选判定；判定为不可重试时**立即返回、不重试、
     不计熔断**。默认 `None` 表示「任何异常都可重试」——**既有调用点语义不变**。
  2. `adapters.is_retryable`：**4xx（除 408/429）一律不可重试**；5xx / 连接被拒 / 超时 / DNS 照旧重试。
     为此把 `urllib` 的 `HTTPError` 收敛成显式类型 `HTTPStatusError(status, url, detail)`，
     让判定只看状态码、不必 import urllib。
  3. **`404` 独立成第三种终态 `not_found`**（`not_found=True` 且 `degraded=False`），
     在归一化 / 合成层与 `degraded` 严格区分：前者渲染成
     「未查询到 SO999（后端明确返回不存在，非故障）」，后者才是「暂不可用」。
     `probe` 也据此把 404 判为 `ok=True`——它恰好**证明** URL + 鉴权是通的，只是测试 ID 不存在。
  顺带修掉两个只在真实往返才暴露的问题：`urllib` 会把头名 capitalize
  （`X-Api-Key` → `X-api-key`，断言必须大小写不敏感）、HTTP/1.1 必须带 `Content-Length`
  （否则客户端会读成半包/挂住）。
- 接口：`RetryPolicy` / `CircuitBreaker` / `Attempt`（`extensions.py`）；
  `is_retryable` / `HTTPStatusError` / `not_found_result` / `degraded_result`（`adapters.py`）。
- 测试：`tests/test_adapters_resilience.py` **51 例**（离线、零等待：sleep 注入为 no-op、
  HTTP 层被替身接管）；真实往返由 `scripts/check_adapters_live.py`（26 项断言，CI `adapters` job）覆盖。

### P0-3 生产级存储与向量真跑通
- 现状（**Path A 已完成；Path B 向量侧已由 CI 实测通过，AGE 图侧待验**）：
  - ✅ **bge 真实嵌入已跑通**（`BAAI/bge-large-zh-v1.5`，1024 维，本地出域不泄露），
    回归通过率 91% → **100%**；**规模化召回评测已完成**（见下方「规模化召回评测」小节，992 条公开语料）。
  - ✅ **容器化已就绪且经 CI 实测**（`Dockerfile` / `docker-compose.yml` / `.dockerignore` + `KB_DATA_DIR` 数据目录支持），
    compose 含可选 `pgvector/pgvector:pg16` 服务。
    **2026-09-29 由 CI 的 `docker` job 实测通过**（run 36528068644，8 步全绿）：
    构建镜像 → 启动容器 → 轮询 `/healthz` → 探 `/api/status` → 容器内依赖自检 → 容器内真实调 `/api/ask`。
    「镜像能构建、容器能跑」不再是假设，而是**每次提交自动验证的事实**。
    顺带修掉一个静默降级缺陷：容器原本缺 `rank-bm25` / `pypdf`，会让 BM25 混合召回**无声退化**为纯向量检索。
  - ✅ **pgvector 生产存储已在真实 Postgres 上跑通**（2026-09-29，CI 新增 `pg` job，
    run `36530049484` 三 job 全绿）：
    用 service container 拉起 `pgvector/pgvector:pg16` → `setup_db.py --graph` 建表 →
    **完整回归评测（`eval_run.py`）读写全部落在 PG** → 独立脚本校验
    「vector 扩展 / `kb_chunks` 表 / HNSW 索引 / `vector(1024)` 维度」与「`kb_chunks` 非空」。
    最后两步是关键：存储连不上时上层会走降级分支、**评测照样能过**（悄悄跑在 memory 上），
    故专门把「数据确实写进 PG」做成硬断言，堵掉静默退回。
    **首跑即抓出三个真缺陷**（这正是「在真实 PG 上跑一遍」的价值——此前全是未验证假设）：
    ① **鸡生蛋**：`connect()` 里先 `register_vector()`，而 `CREATE EXTENSION vector`
       在 `ensure_schema()` 才执行；pgvector 的 `register_vector` 在类型不存在时直接抛
       `ProgrammingError('vector type not found in the database')`，故**全新库上连接阶段
       必然失败**，生产库永远 bootstrap 不起来。修复：注册拆成 `_register()`，**晚于建扩展**。
    ② **忘了连接**：`get_store()` 只构造实例不连接，而只有 `app.py` / `server.py` 记得
       显式 `connect()`；独立路径（`HybridRetriever()` 默认参数、`workers` 的
       `get_store()`、`demo_*.py`）拿到 `conn=None` 的实例，一进方法就断言失败。
       修复：`PGVectorStore` 内部**懒连接 + 懒建表 + 懒注册**（`_ready()`，幂等）。
    ③ **参数适配**：pgvector 只给 `Vector` / `numpy.ndarray` 注册 dumper，**裸 `list` 没有** ——
       psycopg 退化成「PostgreSQL 数组」适配（`[1.0,2.0]` → 文本 `{1.0,2.0}`），
       塞进 `vector(1024)` 列被服务端拒绝；`dict` 更是直接 `cannot adapt type 'dict'`，
       `meta` 必须包 `Jsonb`；读回的 `Vector` 只有 `to_list()`（无 `tolist` / 无 `__iter__`），
       `get_chunks` 按 ndarray 处理会直接打断 BM25 索引路径。
       这三条在 **memory 后端完全不可见**（纯 Python 不过适配层）。
       修复：`_to_vector()` 统一转换 + `Jsonb(meta)` + `get_chunks` 兼容 `to_list()`。
    三个契约共 11 例单测锁定（`TestPGVectorStoreLazyConnect` / `TestPGVectorStoreParamAdaptation`，
    均不依赖真实 PG）。
  - ❌ **AGE 图侧仍未在真实 PG 验证**：CI 用的 pgvector 镜像不含 Apache AGE，
    `setup_db.py --graph` 的建图部分按设计降级为 memory 图（不阻断）。AGE 真跑需
    自建含 AGE 扩展的镜像或独立服务，列为后续小项。
- 待补：① AGE 真跑（自定义镜像）；② 接 bge 嵌入验证生产存储路径（语义召回指标已在 numpy 矩阵下预演）；
  ③ bge 改为**批量编码 + 向量持久化缓存**（当前纯 CPU 逐条编码 992 条需 826s，见性能代价一节）。
- 接口：**`VectorStore` / `GraphStore` / `Embedder` 抽象已存在**，无需新增，直接接实现。

---

## P1 — 让「多 agent」名副其实

### P1-4 从静态 DAG 升级为可 emergent 的「共同体」——**已完成（2026-09-29），收尾已完成（2026-09-30）**
- ~~现状：`Orchestrator` 是固定流水线 `GraphBuilder → [Retriever ∥ GraphReasoner ∥ LiveData] → Synthesizer`，
  llm 模式只是裁剪 agent；无 agent 间消息协商、动态分解、human-in-the-loop。~~
- ✅ **已落地三件事**（均为可选开关，**默认全关时行为与旧版固定流水线完全一致**）：
  1. **LLM 动态任务分解**（`KB_AGENT_PLANNER=llm`）：`LLMPlanner` 把问题分解为子任务，
     从 `AgentRegistry` 的能力清单里选人（骨架 agent 不入候选），产出带理由的 `PlanResult`。
     LLM 不可达 / 超时 / 抛异常 / 输出非法（无候选、全是幻觉 agent、JSON 解析失败）
     **一律回退确定性规划**（`source=fallback`，并留 `reason` 与 `raw` 供排障）。
  2. **多轮协商**（`KB_AGENT_MAX_ROUNDS>1`）：worker 产出后由 `EvidenceCritic` 按黑板现状
     评估证据缺口（检索为空 / 缺图谱事实 / 有实时诉求却没实时数据），点名补查缺失能力的 agent。
     **只提「尚未执行过」的能力** → 补轮必然收敛，同一 agent 绝不重复执行。
     判定用确定性规则而非 LLM：可解释、零额外 token、离线可测。
  3. **人工介入**（`KB_AGENT_HITL=1`）：护栏判定「需人工复核」时返回 `pending_human=true`
     与 `human_review` 段，供上层挂起等待放行。
- ✅ **可观测**：答复新增 `collaboration` 段（每轮分工 / 子任务理由 / 协商结论 / `negotiated`），
  `/api/agent/status` 暴露能力清单与当前规划器；控制台「Agent 协作」面板按轮次渲染分工与协商结论。
- ✅ **实测**（`scripts/verify_collaboration.py`：真实检索 + 真实合成，仅 LLM 换成可编程替身）：
  | 场景 | 结果 |
  |---|---|
  | 单轮 + 动态分解 | 只叫 Retriever（而非固定三件套），1 轮 |
  | 多轮协商 | 同样只叫 Retriever，critic 发现缺 `live` → 补轮叫 LiveData，实时数据进入答复 |
  | LLM 输出非法 | 回退确定性全跑，链路不断 |
  | 人工介入 | 护栏未过 → `pending_human=true`、`status=pending` |
- 测试：`tests/test_planner_collaboration.py` **48 例**（离线零等待：LLM 用 stub、worker 用替身）。
  全量 **207 例**通过、回归评测 91%（与基线一致，唯一 FAIL 仍是 dev 嵌入下已知的 P2 哨兵）；
  **CI run `36533740677` 三 job 全绿**（提交 `425033f`）。
  （该 48 例为 P1-4 首落地时口径；**收尾后已增至 72 例**，见下。）
- ~~仍待补：**agent 间双向消息协商**（当前是「评审 → 补人」的单向补轮，非多轮对话协商）；
  子任务依赖编排（`Subtask.depends_on` 已预留，当前各 worker 仍并行）。~~
  → **✅ 两项均已于 2026-09-30 补完，见下「P1-4 收尾」。**
- 接口：**`Planner` / `AgentRegistry`（均已真实接入，不再是预留 seam）**。

### P1-4 收尾：子任务依赖编排 + agent 间双向消息协商——**已完成（2026-09-30）**
- ✅ **子任务依赖编排**（`Subtask.depends_on` 真的被消费，`orchestrator._dep_layers`）：
  一轮内按依赖**分层**，同层并行（`ThreadPoolExecutor.map`）、层间串行。
  三条边界是刻意写死的，直接决定「会不会把老行为改坏」：
  1. **无依赖 → 恰好一层，且顺序 = 计划顺序**（热门路径）。所以默认（确定性规划器不产出依赖）
     行为与旧版固定流水线**逐字等价**——这是向后兼容的硬前提，不是巧合。
  2. 依赖指向**本轮之外**（已执行 / 不在计划里）视为**已满足**；自依赖忽略。不等待、不报错。
  3. **成环不重排、不卡死**：降级成一层照跑，并在该轮记 `dep_cycles`。
     「顺序略有偏差」远好于「流程卡住」——与 AGE 那条「失败要可见、不要静默」同源的取舍。
- ✅ **双向消息协商**（不再只有「评审 → 补人」一个方向）：
  - 账本 `AgentContext.negotiation`（**append-only**）+ `BaseAgent.request_help` / `reply_to`：
    worker 可在 `on_run` 里**主动委托**，评审缺口则由 `EvidenceCritic` 登记为同一条账上的请求，
    两者走**同一本账、同一条调度路径**（`_ensure_request` / `_resolve_requests` / `_auto_reply`）。
  - **四种结局都要显式**：`provided`（跑完确有新证据）/ `declined`（跑完没证据，**明确说「我也没有」**）/
    `unavailable`（无人具备该能力，或本应服务者已执行过 → 当场关账）/ 悬空请求（**不允许存在**，
    轮次结束仍无人应答者一律由编排器关账）。
  - **沉默不算数**：被点名方跑完若无产出即记 `declined`，绝不「含糊放过」当作已补。
  - 被拒 / 无主的能力进「**别再提**」名单 → 只提「未执行且未被拒」的能力，必然收敛。
  - `crit.requests` 带 `by="EvidenceCritic"`；补轮来源按发起方区分记为 `negotiation`（评审发起）
    或 `delegation`（**worker 自发委托**）——两种来源在轨迹里可分辨。
- ✅ **可观测**：`collaboration.negotiation`（仅真发生协商时出现）= `requests` / `replies` /
  `declined` / `delegated` / `ledger`；`collaboration.rounds[i]` 在真分层时带 `layers`、
  成环时带 `dep_cycles`；审计 `audit()` 增 `declined_caps` / `delegated`。
- ✅ **LLM 侧**：`LLMPlanner` 现在解析子任务的 `depends_on`，但**只保留指向本次入选 agent 的依赖**，
  且丢弃自依赖——悬空 / 幻觉依赖一律安全丢弃（否则一条幻觉依赖就能把并行拖成死等）。
  prompt 里明确写「无依赖就别填」。
- ✅ **实测**（`scripts/verify_collaboration.py` 重写为**可断言 check 脚本**，7 场景 **28 项断言**，
  全部基于真实 worker 链路、只有 LLM 是替身）：本地 `EXIT=0`、28/28、stderr 干净；
  CI 新增 **`collab` job**（CI 增至**九 job**）常跑，报告 `collaboration_report.json` 作 artifact。
  真跑凭据（`::notice::` 可见）：B 场景 `requests=1 replies=1`，`LiveData` 应答 `provided`；
  E 场景分层 `[['Retriever'], ['LiveData']]` 且扁平化顺序 = 计划顺序；F 场景 worker 发起
  `by=Retriever` 的委托、被委托方 `declined`、`declined=['live']`、账本无悬空请求；
  G 场景无主能力 `unavailable` 且不产生空转补轮。
  > 其中 **F 场景的两条断言是被真实链路纠正过的**：初版断言「被委托方应答 provided」，
  > 但问题无实时诉求、`LiveData` 真实产出就是空——它**诚实地 declined** 才对。
  > 断言改成「没证据就 declined + 账本不留悬空请求」后，恰恰成了「沉默不算数」规则
  > 在真实链路上生效的证据；同时把「被点名方应答 provided」挪到 B 场景（那里确有产出）。
- 测试：`test_planner_collaboration.py` 48 → **72 例**（新增依赖分层 6 / `depends_on` 解析 5 /
  账本契约 7 / 双向协商 7）；全量 **332 例**通过（既有 48 例无回归；此为 P1-4 收尾当时的
  总数，后续 P2-9 收尾再增至 **358 例**）。
- **验收：`CI #23` / run `36677436460` 九 job 全绿**（`bge` 按 schedule 跳过属正常）。
  `collab` job 的 `::notice::` 注解凭据：**`共 28 项，失败 0 项；语料 63 片段；多轮场景轮次 2`**
  ——即「7 个场景 28 项断言在真实 worker 链路上全过」，不是「脚本跑完没报错」。

### P1-5 评估与护栏（合规刚需）——**核心已落地（2026-09-07）**
- ~~现状：仅 `trace_id` 基础留痕；无 golden 集/评测脚本、无幻觉抑制、置信度未真正拦截、无审计日志落盘。~~
- **已落地**：
  - **置信度护栏**（`ConfidenceGuardrail`）：低于阈值的答复自动追加「[需人工复核]」+ 判定原因，
    由 app / server 启动时自动安装；实测胡言乱语问题（bge 置信 0.47）被正确拦截，正常问答（≥0.61）直通。
  - **审计日志**（`kb_mcp_server/audit.py`）：每次问答追加一条 JSONL（trace_id / 问题 / 置信度 /
    护栏判定 / 耗时 / 失败 agent），线程安全、异常静默，默认项目根 `kb_audit.jsonl`（`KB_AUDIT_LOG=off` 关闭）。
  - **护栏/档位按嵌入后端校准**：bge 分数带整体高于 dev（无关问题 bge 也能到 ~0.47，dev 约 0.1），
    固定阈值会失真；现 bge 默认阈值 0.5 / dev 0.2，可用 `KB_GUARDRAIL_MIN_CONFIDENCE` 覆盖。
- 待补：审计检索/管理界面；护栏细分策略（低置信不同原因不同处置）。
- 接口：**`Guardrail`（`ConfidenceGuardrail` 已替换 `PassthroughGuardrail` 默认接入）**。

---

## P2 — 企业交付

### P2-6 多租户与权限
- 现状：仅单 `api_token` Bearer 鉴权 + 按 IP 限流，无租户隔离/RBAC/SSO。
- 待补：租户上下文贯穿检索/图谱/实时数据；RBAC；SSO。
- 接口：**`TenantProvider` / `TenantContext`（`AgentContext.tenant_id` 已预留）**
  —— `TenantProvider` **全仓库零引用**，仅有默认 `SingleTenantProvider` 类定义，本项未开始。

### P2-7 并发与部署
- 现状：
  - ✅ 已有：容器化三件套（`Dockerfile` 多阶段 + `docker-compose.yml` + `.dockerignore`）、
    `KB_DATA_DIR` 外部化数据目录、`HEALTHCHECK` 探 `/healthz`、Bearer 鉴权、按 IP 限流、
    结构化访问日志、`/api/metrics` 指标。
  - ❌ 仍是**单进程 `ThreadingHTTPServer`**，memory store 非进程安全，无队列/横向扩展/灰度。
- 待补：队列 / 横向扩展 / 灰度；进程安全存储；真 infra 部署（含验证 `docker build`，见 P0-3）。

### P2-8 知识抽取规模化
- 现状（**已推进一部分**）：
  - ✅ **结构感知分块**（`chunk_structured`）已落地：标题作上下文前缀 / 「一行多档」自动拆条 / Q-A 成对合并，
    可用 `KB_CHUNK_STRATEGY=plain` 回退旧长度切片；片段数 13 → 63。
  - ✅ **规模化验证已做**：992 条公开语料召回评测（见下方小节），bge 语义 Recall@5 = 95.3%。
  - ✅ **语料对齐已完成**：三份 SLA 文档零冲突（见「语料规范」）。
  - ❌ 未做：**增量摄取**、**去重**、**冲突消解告警**、**三元组人工审核**。
- 待补：承接「语料规范 · 后续建议」——摄取时加**冲突检测**（同档位出现不同数值时告警，不静默入库），
  文档补**元数据**（版本 / 生效日期 / 适用客户等级），再做增量与三元组审核。

### P2-9 MCP 真落地 + 动作型工具 —— **已落地（2026-09-29）**
- 原现状：14 工具只活在 `kb_mcp_server/server.py`（FastMCP stdio 后端），**未接真实 MCP host**；
  agent 只会检索不会执行动作（「改单 / 退款 / 建工单」全无）。
- **✅ 已落地（2026-09-29，三件事一起做完）**：

  **1) 动作型工具层**（`kb_mcp_server/actions.py` + `extensions.ActionTool/ActionToolRegistry`）
  - 4 个动作：`create_ticket`（write，与 SLA 知识联动）、`update_order`（write）、
    `request_refund`（**destructive**）、`list_tickets`（read）。
  - **三条安全底线**收口在 `ActionRunner`（校验 → 确认门 → 执行 → 审计），
    新动作注册即自动获得，不会漏：
    1. **参数契约**——缺必填参数直接拒（`rejected` + `missing`），不把半成品请求发给下游；
    2. **确认门**——destructive 动作未 `confirmed=true` **绝不执行**，只回
       `needs_confirmation` 并回显待执行参数。**`ActionAgent` 的 `confirmed` 恒为 False**：
       agent 自己确认自己的不可逆动作 = 没有确认门；
    3. **审计落盘**——每次尝试（含被拒 / 待确认）写 `kb_actions.jsonl`（`KB_ACTION_LOG` 可配/可关）。
       与问答审计分文件，合规可单独导出。**`list_tickets` 能回读刚建的工单**，
       证明「副作用真的发生了」而不是只返回了个 id。
  - 失败一律不抛异常，统一 `{ok,status,error}`（沿用全项目降级约定）。

  **2) 真实 MCP 协议端到端**（`scripts/check_mcp.py` + CI `mcp` job）
  - 此前「MCP 工具」只被单测**直接 import 函数**调用——那是函数测试，不是协议验证。
    现在用官方 SDK 起**真实 stdio 子进程**，`ClientSession` 走完整 JSON-RPC：
    `initialize → list_tools → call_tool`（16 个工具；检索 / 动作 / 多 agent 全链路），
    22 项断言全绿。**MCP 从「标题」变成「可被任意 MCP host 直接接入的实质能力」。**
  - **CI 实测通过（2026-09-29，run `36541763060`，job「MCP 协议（真实 stdio 端到端）」23s 全绿）**：
    该 job 与单测 / 容器 / PG 并列，每次推送都跑，**协议层回归不会退化到无人发现**。
  - 顺带修掉一个深坑：`rank_bm25`（连带 numpy）此前在**首次检索请求的工作线程里**惰性导入，
    实测该路径 DLL 首次加载被拖到 60s 量级，表现为「第一次检索请求卡死」且极难定位
    （同调用在进程内仅 0.2s）。已把可选重依赖提到 `retrieval.py` 模块导入期——
    代价落在**可观测的服务启动**，请求路径恒为热路径。

  **3) 接入编排 + Web + 前端**
  - `ActionAgent`（`gated_by=KB_AGENT_ACTIONS`）：默认不进编排；开启后识别动作意图并执行。
    新增 `extensions.agent_gate_open` 作为「有副作用的 agent 是否获准参与动态组队」的统一闸门。
  - **与 P1-4 闭环**：存在 `needs_confirmation` 动作时，若 `KB_AGENT_HITL=1`，
    答复一并标 `pending_human`（否则「agent 发起了退款」会静默通过）；
    合成层新增【执行动作】段，三种终态（已执行 / 待确认 / 被拒）可一眼区分且对未知动作类型安全。
  - MCP 工具 `list_actions` / `run_action`；Web `GET /api/actions`、`POST /api/action/run`；
    `agent_status` 回带动作层；前端新增动作面板（清单 + 风险分级 + 手动执行 + 动作审计）。
- 测试：`tests/test_actions.py` 53 例 + `tests/test_agents_mcp.py` 扩展工具清单；
  基线不变（dev 嵌入 91%，10/11）。
- 接口：~~`ActionTool` / `ActionToolRegistry`~~ → **已真实接入**，不再是零引用空壳。

### P2-9 收尾：动作执行结果回写 GraphStore —— **已完成（2026-09-30）**
- 原缺口：动作**只落审计与答复**，图谱里不留痕迹 → 「执行 → 关系 → 再检索」是断的。
  客户接着问「刚给 A 客户建的工单涉及哪个产品」，图谱答不出来。
  本体里的 `Ticket` / `Customer` / `Product` 与 `SUBMITTED_BY` / `ABOUT_PRODUCT` 早已就绪，
  但**只有「LLM 从文档抽取」这一条写入路径**。
- **✅ 已落地**：`kb_mcp_server/action_graph.py`（声明式 `Effect` 映射 + 本体校验 + 降级），
  由 `ActionRunner` 在**执行成功后**统一挂钩（与确认门 / 审计同一处收口 → 新动作注册即自动获得）。
  当前映射：`create_ticket` → `Ticket:<id>`（props: subject / priority / sla_response / status）
  `-SUBMITTED_BY→ Customer` `-ABOUT_PRODUCT→ Product`；关系按取值字段有无**条件建立**
  （空值不建空节点）。
- **三条底线（写死不可配置，与前三条闸门同性质）**：
  1. **只有真执行成功才写**——`needs_confirmation`（确认门拦住）与 `rejected` 一律不写。
     被拦下的动作在真实世界什么都没发生，图谱里也不该有它。
  2. **写失败不改动作结论**——图谱不可用 → `{"applied": false, "reason": "graph_error:..."}`，
     动作仍 `ok=true`。动作**已经发生**；记账失败若报成失败，会诱导调用方重试 = 第二次执行。
  3. **映射必须过本体校验**——统一走 `Triple.is_valid()`，写错的映射只进 `dropped` 并被丢弃，
     不可能污染图（本体驱动 D3 的意义就是不让图谱被随手扩张）。
- **默认关**（`KB_ACTION_GRAPH=0`，白名单判定 `1/true/yes/on`）：不开时零副作用，
  返回值与审计里**连 `graph` 字段都不出现**——默认行为与旧版逐字一致。
- **刻意不回写的动作**（不是遗漏）：`update_order` / `request_refund` —— 本体里没有
  `Order` / `Refund` 节点，且它们的真相在**实时后端**（P0-2）；运行态数据塞进知识图会污染检索。
  要写回来，先论证本体该不该扩。
- **方向语义（踩到）**：回写建的是 `Ticket → Customer` **出边**，所以「按客户查工单」是**入边**，
  必须 `direction=both` / `in`；用默认 `out` 会**静默返回空列表**——又一个
  「不报错、答案悄悄变错」。这条已钉成断言（单测 + AGE 阶段七各一条）。
- **可观测**：`run_action` 返回值与动作审计各多一段 `graph`；`list_actions`（MCP）暴露
  `graph_writeback` 开关状态；`graph.backend` 记录实际落到的后端（`age` / `memory`）——
  「没静默降级」的证据。
- **真跑（`age` job 新增阶段七，全部旁路对账）**：关掉开关时动作照常成功且 AGE 里零写入 →
  打开后 `Ticket` / `SUBMITTED_BY` / `ABOUT_PRODUCT` label 表真的多出行来 →
  **独立 Cypher 读回 props**（注意 AGE 把属性整体存进单个 `props` 属性，不是摊平成 `t.subject`）→
  从客户反查得到该工单。
- 顺手修掉 `check_age.py` 一处**假证据**：`neighbors()` 的元素是
  `{"depth","path","node"}`，节点名在 `node.name`；原写法读顶层 `n.get("name")` 恒为 None，
  于是 detail 永远是 `[None, ...]`，断言只剩「数量 ≥ 1」在把关。现改为真读 `node.name`
  并断言 `all(n_names)`。
- 测试：新增 `tests/test_action_graph.py` **26 例**；全量 332 → **358 例**。
- 接口：`ActionRunner.run(..., context=)` / `run_action(..., context=)` 为**新增可选参数**
  （`context` 是图谱回写的旁路信息，不进参数契约、不参与校验），既有签名未变。

### P2-10 自动化测试与 CI —— **已落地（2026-09-16）· CI 真实跑通（2026-09-29）**
- ~~现状：零自动化测试（仅有离线自检脚本）。~~
- **已落地**：
  - **pytest 单测 358 例**（`tests/`，**约 12s** 跑完，离线零外部依赖）：
    - `test_ingestion_storage.py`（43）：结构感知分块三要素（标题前缀 / 一行多档拆条 / Q-A 成对）、
      嵌入器单例、向量归一化、存储增删查与持久化、损坏文件容错、图谱本体约束、多格式解析、
      **PG 懒连接 / 注册时序 / 写库参数适配**（P0-3）。
    - `test_retrieval.py`（27）：**BM25 分数越界回归护栏**（守住 ROADMAP 记录的真 bug）、
      RRF 融合排序、MMR 去重、硬阈值与 BM25 强命中保留、分词、余弦边界。
    - `test_eval_guardrail.py`（36）：**数字边界断言**（守住「8 小时 被 48 小时蒙对」的假通过用例）、
      证据段剔除、禁止词反向断言、置信度护栏分级、审计日志线程安全与坏行容错。
    - `test_agents_mcp.py`（24）：Agent 模板方法异常兜底、AgentContext 黑板隔离、
      编排器 roster 与路由裁剪、**降级链路**（LLM 不可达回退模板 / 图谱不可用跳过）、
      合成输出契约、16 个 MCP 工具注册完整性（P2-9 后新增 `list_actions` / `run_action`）。
    - `test_adapters_resilience.py`（51）：**重试次数语义与指数退避封顶 / 熔断状态机 / 降级不抛异常 /
      失败不写缓存**（P0-2）；**可重试性分级**（4xx 除 408/429 不重试且不计熔断）、
      **`not_found` 终态**（404 单发 / 不计 failures / 不开熔断 / `probe` 判 ok）、
      `HTTPStatusError` 转换、`RetryPolicy.call(retryable=)` 三条契约（P0-2 收尾）。
    - `test_planner_collaboration.py`（72）：**动态任务分解的四条降级回退路径 / 协商只提未执行能力
      （收敛）/ 多轮补轮不重复执行 / 默认单轮等价旧行为 / HITL 开关**（P1-4）；**依赖分层**
      （无依赖=单层且顺序=计划顺序 / 同层并行层间串行 / 轮外依赖视为满足 / 自依赖忽略 /
      成环降级不卡死并记 `dep_cycles`）、**`LLMPlanner` 解析 `depends_on`**（悬空与自依赖必须丢弃）、
      **协商账本契约**、**双向协商**（worker 主动委托 / **沉默不算数**：无产出记 `declined` /
      无主能力关账 `unavailable` / 已执行者不重跑 / 轮次上限兜底）（P1-4 收尾）。
    - `test_actions.py`（53）：**参数契约 / 确认门（未确认的 destructive 绝不执行）/
      审计三终态全留痕 / 意图识别与参数抽取（含「订单号里的数字不得被当成金额」回归）/
      ActionAgent 三道闸 / 接入编排与 HITL 闭环 / 合成三终态渲染对未知动作安全**（P2-9）。
    - `test_action_graph.py`（26）：**动作结果回写图谱**（P2-9 收尾）——默认关且零写入 +
      返回值/审计里连 `graph` 字段都不出现、Ticket 节点与 props 真落图、关系条件建立、
      双向可达（反向需 `direction=both`）、**确认门拦住 / rejected / 只读 / 无映射一律不写**、
      **本体校验丢弃非法映射**、图谱报错时动作仍成功且留降级原因、重复回写幂等、
      默认映射本身必须是本体子集。
    - `test_baseline_gate.py`（11）：**基线校验脚本本身**——默认阈值 / `KB_BASELINE_MIN` 覆盖 /
      非法阈值必须炸 / 报告路径三种来源与优先级 / 恰好等于阈值放行 / 低于阈值必红 /
      报告缺失必失败。理由：该脚本**同时把守 `test` 与 `bge` 两个 job**，
      参数解析写错的表现是**静默失效**（CI 照样绿，只是不再拦任何东西），
      这类共享逻辑必须有测试（P2-10 补）。
    - `test_graph_age_cypher.py`（15）：**AGE `cypher()` 两条硬约束**——
      ① 列定义列表列数必须等于 `RETURN` 表达式数（`find_entities`=3 / `neighbors`=4 /
      `paths`=2 / `export_graph`=5；终止型 `nout=0` 收敛到 1 列）；
      ② 图名与 Cypher 原文必须是**常量字面量**（内联 `'kb_graph'` + 美元引用
      `$kb$…$kb$`，绝不出现 `$1/$2`），只有第三个 agtype 参数允许绑定。假连接抓 SQL 断言，
      **不需要真实 PG**。理由：这两条契约只在真机上才暴露（见 P2-10 补小节
      「`age` job 首跑连抓两个 bug」）。
  - **GitHub Actions CI**（`.github/workflows/ci.yml`）：单测 → 回归评测 → 基线校验，
    三段串行；通过率低于基线则 CI 变红**卡住合并**，评测报告作为 artifact 归档 30 天。
    另有八个独立 job：
    `docker`（镜像构建 + 容器冒烟）、`pg`（真实 PG + pgvector）、
    `mcp`（真实 stdio 子进程 + 官方客户端协议端到端，P2-9）、
    `load`（真实 HTTP 并发压测，P2-10 补）、
    `adapters`（**真实 HTTP 往返**：真实 socket 替身后端 + 真实 `urllib`，P0-2 收尾）、
    `collab`（**真实 worker 协作链路**：依赖分层 / 双向协商 28 项断言，P1-4 收尾）、
    `age`（真实 Apache AGE 图侧 + **动作回写**，P2-10 补 / P2-9 收尾）、
    `bge`（真实 bge 嵌入，**夜间 + 手动**，P2-10 补）。
    四 job 齐跑实测：**`CI #13` / run `36541763060`，40s 全绿**（单测 21s / 容器 32s / MCP 23s / PG 37s）。
    **七 job 齐跑实测：`CI #19` / run `36663136008`，全绿**——含新增的 `load` / `age` / `bge`
    （`bge` 按 schedule 限制跳过属正常）。
    **八 job 齐跑实测：`CI #21` / run `36673194795`，全绿**——新增 `adapters`（P0-2 收尾，
    真实 HTTP 往返）。
    **新增 `collab` job（P1-4 收尾）后 CI 增至九 job**，见下方 P1-4 收尾小节。
    **九 job 齐跑实测：`CI #23` / run `36677436460`，全绿**（`bge` 按 schedule 跳过属正常）。
  - `pytest.ini` + `tests/conftest.py`：在导入任何模块前隔离 `KB_DATA_DIR` / store 路径 /
    审计开关，保证测试绝不碰真实数据，本地与 CI 结果一致。
- **设计取舍（为何快口径不跑 bge）**：dev 嵌入下实测 **91%**（语义级断言前 82%；唯一 FAIL 是 P2 哨兵用例，
  受限于 dev 对 `P0/P1/P2` 字面相似片段的区分能力），bge 下 **100%**（11/11，2026-09-29 实测）。
  每次推送都装 torch + 下 1.3GB 模型会让单次运行从 **11s** 涨到数分钟。因此
  **职责分离**：推送时跑 dev 快口径（阈值 **0.72**）保「不许变差」，
  夜间/手动跑 bge 重口径（阈值 **0.95**）保「质量上限」。
  两者**共用 `scripts/check_baseline.py`**，只是 `KB_BASELINE_MIN` 与报告路径不同——
  不各写一套阈值逻辑。阈值贴着实测值会让 CI 噪音化进而被忽略，故留出余量。
- **CI 首跑成功（2026-09-29，run id 36526951159）**：`ci.yml` 此前从未进过仓库（一直是未跟踪文件），
  推送 `f43e331` 后触发 **CI #1**，7 步全绿（检出 → 配置 Python → 装依赖 → 单测 → 回归评测 → 校验基线 → 上传报告）。
  同时验证了**本地复现方法与 GitHub 结果一致**，日后不必等 CI 即可自查。

#### P2-10 补：把「没有验证」的三块补成硬验证（2026-09-29）

原「待补」三项（负载测试 / bge 独立 CI job / AGE 真跑）已全部落地。共同点是
**它们都属于「代码写了但没有任何自动化证据」的那一类**——正是本项目最警惕的状态。

> **验收：`CI #19` / run `36663136008` 七 job 全绿**（`bge` 按 schedule 跳过属正常；
> `age` job 六步全过）。AGE 真跑凭据（`::notice::` 注解输出）：
> `age=1.6.0`，顶点 `Document1 / IssueCategory3 / SLAClause2 / Product1`、
> 边 `MENTIONS3 / GOVERNED_BY2 / ABOUT_PRODUCT1` = **7 顶点 / 6 边**。
> 修复过程跨三个提交、三轮 CI 逐层收敛：`45ad583`（列定义契约）→
> `3a3c42b`（常量字面量）→ `75b655e`（旁路对账排除基表）。

**1) 负载测试**（`scripts/check_load.py` + CI `load` job）
- 起**真实 app 子进程**，用线程池打真实 HTTP，四个场景：
  只读并发 / 混合读写 / 限流门 / 指标自洽。
- 断言不是「跑完没报错」，而是：零错误、响应字段完整、**并发写入不丢数据**
  （上报片段数 == store 增量）、**限流在并发下精确**（恰好放行 N 个）、
  指标计数不少于实际请求数。
- 实测（开发机，两次高度一致）：**~150 req/s**，p50 29ms、p95 38–58ms、p99 ~515ms；
  40 并发写 + 80 并发读 **零丢失**；限流 **恰好 5 放行 / 19 拒绝**，`/healthz` 不受限流误伤。
- **顺手抓到一个真问题**：`app.py` 启动时会 `webbrowser.open()`，
  浏览器真的会加载控制台页面并打出一串 API 请求（`/`、`/api/status`…），
  这些请求**计入限流配额与指标计数** → 限流场景「放行 5」被观测成「放行 1」。
  已加 `KB_NO_BROWSER` 开关（headless / CI / 压测必须关）。
- **记录一个既有特征**：服务端没设 `protocol_version` → HTTP/1.0，**每请求新建连接、无 keep-alive**。
  吞吐受连接建立开销限制；改 HTTP/1.1 是一个明确的后续优化项。

**2) AGE 图侧真跑**（`scripts/check_age.py` + CI `age` job）
- 向量侧的 pgvector 早已在 `pg` job 跑通，**图侧一直是空白**：`pg` job 用的
  `pgvector/pgvector:pg16` 镜像不含 AGE，`setup_db.py --graph` 按设计降级成 memory 图，
  于是 `graph.py` 里那句「生产启用前请在目标 AGE 版本上跑一遍验证」一直没兑现。
- 现在用官方 **`apache/age:release_PG16_1.6.0`** service container（镜像自带
  `shared_preload_libraries=age`），跑七件事：扩展安装 / `create_graph` / **`ensure_schema` 幂等** /
  **`get_graph_store()` 未静默降级** / 经**真实摄取链路**写入 / **绕开 store 直接数
  `ag_catalog` 建出的 label 表行数**（顶点 / 边 / `Document` 节点）/ Cypher 读回 / `clear()`。
- **不需要 pgvector**：图存储与向量存储是两套独立连接。所以不必自建「装两个扩展」的镜像
  （那会让每次 CI 多花几分钟）——这是个刻意做的成本取舍。
- 最关键的一条断言是「**未静默降级**」：`get_graph_store()` 内部 `try/except` 会在
  连不上 AGE 时**悄悄换成 memory 图**，主链路照跑、CI 照绿。脚本把这条顶死。
- **首跑即连抓两个真 bug：AGE `cypher()` 的两条硬约束**（这就是「真跑」的价值）。
  AGE 在 `post_parse_analyze_hook` 里拦截 `cypher()` 做语法改写，于是：
  - **① 图名与 Cypher 原文必须是常量字面量，不能是绑定参数。**
    `_cypher()` 原本写 `ag_catalog.cypher(%s, %s)`，把图名与 Cypher 都当 `$1/$2` 传，
    AGE 读不到 Cypher 原文 → `a name constant is expected`。
    **修复**：图名内联 `'kb_graph'`、Cypher 用美元引用 `$kb$ … $kb$` 原样内联
    （美元引用天然免单引号双重转义，这也是 AGE 官方推荐形态）；只有可选的第 3 个
    agtype 参数映射允许是绑定参数。无参数时**不能**给 `execute` 传第二参数，否则
    Cypher 里的 `%` 会被 psycopg 当占位符。
  - **② 列定义列表列数必须等于 `RETURN` 表达式数。**
    原本恒声明单列 `(v agtype)`，而 `find_entities`（3 列）/ `neighbors`（4 列）/
    `paths`（2 列）/ `export_graph`（5 列）都是多列 `RETURN` → 报
    `return row and column definition list do not match`。
    **修复**：`_cypher(..., nout=1)` 按 `nout` 生成列定义列表；终止型语句
    （`DELETE` / `DETACH DELETE` 无 `RETURN`）按官方手册**仍需声明一列**（返回 0 行），
    故 `nout` 下限取 1。
  - 两条都**只在真机暴露**：本地 memory 后端根本不生成 SQL，全部单测自然全绿。
    这正是「代码写了但没有任何自动化证据」的典型后果。
  - **③ 旁路对账脚本自身的坑：AGE 基表继承导致计数翻倍。** 修好 ①② 后，最后一个
    红项是 `store.stats() 与原始表口径一致`：`stats()` 报 7 顶点 / 6 边，原始表算成
    14 / 12。原因是 AGE 用 `_ag_label_vertex` / `_ag_label_edge` 作**所有 label 表的
    继承父表**（每个图都有），父表 `count(*)` 会把子表行一并算进来。排除这两个保留名
    后两边一致——**「独立对账」的价值正在于此：连对账逻辑自己有错都会被抓出来**，
    而不是默认它对。
  - **防回归**：`tests/test_graph_age_cypher.py`（15 例，假连接抓 SQL，**不需要真实 PG**）。
  - **可诊断**：`check_age.py` 重写为「阶段隔离异常 + 报告无论成败必落盘 +
    `::error::` / `::notice::` 注解」——CI 无日志下载权限也能定位（**本轮就是靠注解
    直接读出上面两条错误**）；探针侧独立实现 `cypher_sql()` 内联逻辑 + `raw()` 自行
    `SET search_path`，保持「旁路交叉验证」而非复用产品代码。新增「多列 RETURN /
    5 列 RETURN / MERGE 关系空属性表」三条语法探针。

**3) bge 独立 CI job**（CI `bge` job）
- `eval_run.py` 新增 `--out`：bge 与 dev 是**两种口径的产物**，混在一个报告里
  会让「基线没变差」失去依据。
- `check_baseline.py` 阈值与报告路径参数化（`KB_BASELINE_MIN` / 参数或 `KB_EVAL_REPORT`），
  两个口径共用一份实现。
- 触发方式：`schedule`（每日 03:00 北京时间）+ `workflow_dispatch`，**不跟推送**。
  并用 `actions/cache` 缓存 1.3GB 模型，避免每次夜跑重下。
- 本地先验过：`--embedding bge` 一次跑到 **100%（11/11）**，avg_recall 1.0。

- **待补（原三项之外的剩余项）**：
  - **AGE 与 pgvector 同库**（决策 D2 的生产形态）——目前 AGE 单独验证，同库形态未验；
  - HTTP/1.1 keep-alive（吞吐优化，见上）；
  - 负载测试可再补「长时间稳定性 / 内存增长」场景。
  ~~集成测试（起 app 打真实 HTTP 请求）~~ → **已完成（2026-09-29）**：
  `docker` job 容器内真实调 `/healthz`、`/api/status`、`/api/ask`；
  `load` job 进一步做**并发**真实 HTTP 压测。


---

## 基线指标（已跑出，作为回归基线）

运行 `python eval_run.py`（离线、独立 store、每次重新播种，可复现）。

#### 第一轮（修复前）
| 指标 | 值 |
|---|---|
| 用例数 / 通过率 | 11 / **82%**（9/11） |
| 平均关键词召回 | 1.000 |
| 平均置信度 | 0.443 |
| 时延 p50 / p95 | 22ms / 434ms |
| 语料片段数 | 13（粗分块） |

#### 当前（结构感知分块 + 语料对齐后）
| 指标 | 值 | 变化 |
|---|---|---|
| 用例数 / 通过率 | 11 / **91%**（10/11） | +9pt |
| 平均关键词召回 | 0.909 | 见下方说明 |
| 平均置信度 | **0.565** | **+0.122** |
| 时延 p50 / p95 | 54ms / 385ms | 切片变多，检索略慢 |
| 语料片段数 | **63**（细分块） | 13 → 63 |

#### bge 真实嵌入（P0-3 Path A · 本轮）
| 指标 | 值 | 变化 |
|---|---|---|
| 用例数 / 通过率 | 11 / **100%**（11/11） | +9pt，**P2 哨兵转正** |
| 平均关键词召回 | **1.000** | +0.091 |
| 平均置信度 | **0.791** | **+0.226** |
| 时延 p50 / p95 | 678ms / 4846ms | bge CPU 编码成本（见下） |
| 嵌入 | BAAI/bge-large-zh-v1.5（1024 维，本地，出域不泄露） | dev 哈希 → bge |

> bge 下时延上升是 CPU 推理 + 逐条 `encode_one`（未批处理）所致；接 PG 后建议改为**批量编码**（见 P0-3 待补）。

### 本轮修掉的问题（bge 切换暴露的 2 个隐患）
1. **嵌入器非单例**（真 bug）：`get_embedder()` 每次 `new` 一个 `BGEEmbedder`，1.3GB 模型被反复重载，
   评测进程内存爆掉直接 **Segmentation fault**。已改为**进程内单例**，模型只加载一次、全局共享。
2. **BM25-only 分数越界**（真 bug）：`_fuse` 给「仅字面命中」合成 `0.3 + 0.5*_norm`（最高 0.8），
   反超真实向量余弦（0.64~0.72），导致标题行「物流配送常见问题」在 MMR 里压过真正的 P2 答案块。
   已改为 `0.3 * _norm`（区间 [0, 0.3]，恒低于向量命中）。
3. **golden 措辞过死**：`如何申请退货` 命中「7 个自然日」语义正确但字面不符「7 天」，断言已对齐。

### 本轮修掉的问题（P2-8 分块粒度）
- `chunk_text` 原先只在文本 >300 字时才切，`samples/sla说明.md` 全文 60 字 → **整篇一个 chunk**，
  三档 SLA 必然粘连。新增结构感知切片（`chunk_structured`）：
  1. 标题作为**上下文前缀**，保证每个 chunk 自包含；
  2. 「一行多档」自动拆条——`响应：P0 15 分钟，P1 1 小时，P2 4 小时` → 3 个独立 chunk；
  3. **Q/A 成对合并**，否则问句会单独成块、检索时命中没答案的 Q 行（这是修复过程中出现的回归，已修）。
- 可用 `KB_CHUNK_STRATEGY=plain` 退回旧的纯长度切片。

### 评测器本轮的两处加固
- **数字边界匹配**：期望「8 小时」时不再误命中「**4**8 小时」的子串。
  这条一加就暴露出一条**假通过**用例——`严重故障多久修复`原先是靠 `48 小时` 里的子串蒙对的。
- **断言只针对答案主体**：剔除【关系路径】证据段（它天然列出同类其他条款），
  保留【实时数据】与【知识依据】。

### 已知未通过 1 例 —— **已解决（换 bge 后转正）**
- `P2 问题多久响应` —— 未命中「4 小时」且误含「15 分钟」。
- **根因**：dev 哈希嵌入对 `P0/P1/P2` 这类**字面高度相似**的片段区分不了，排序近乎随机。
- **验证**：换 bge 后该用例通过（召回 1.00，正确命中「4 小时」且不含「15 分钟」）。
  佐证了 P0-3 换真实嵌入的必要性，本用例现作为**回归哨兵**长期保留。

### 规模化召回评测（P2-8 真实语料 · 2026-09-08）

用公开电商客服合成语料 `data/ecom_cs.jsonl`（魔搭 QingshanAI/ecom-customer-service-synthetic，
**992 条 QA，Apache-2.0**）验证 bge 在真实体量下的语义召回。脚本 `bench_scale.py`，
用 numpy 矩阵余弦（`Q @ C.T`，等价 pgvector 后端速度）绕开 memory 后端纯 Python 的 O(N*dim) 慢。

两条口径（语料本身含大量重复问题，严格口径会系统性低估）：
- **严格 Recall@k**：query 命中「自己那一条 chunk 的索引」。
- **语义 Recall@k**：query 命中「任意一条同义问题的答案 chunk」（ground-truth = 归一化 user 文本相同的组）。

| 口径 | 嵌入 | Recall@1 | Recall@3 | Recall@5 | top-1 相似度 |
|---|---|---|---|---|---|
| 严格 | bge | 52.0% | 76.7% | 84.7% | 0.749 |
| **语义** | **bge** | **73.3%** | **92.0%** | **95.3%** | 0.749 |
| 严格 | dev（词法） | 53.0% | 72.3% | 80.7% | 0.658 |
| 语义 | dev（词法） | 71.7% | 86.3% | 93.3% | 0.658 |

#### 精排指标（补齐 Recall 之外的排序质量）

> Recall 只回答「有没有召回到」，不回答「排在第几位」。MMR / 硬阈值 / 可选重排
> 这些下游环节影响的正是**排序**，缺了 MRR / NDCG 就无法量化它们到底有没有起作用。
> 均为语义口径（二值相关性，正确答案为一组同义 chunk）。

| 指标 | bge | dev（词法） | 差值 |
|---|---|---|---|
| **MRR** | **0.832** | 0.800 | +0.032 |
| NDCG@1 / @3 / @5 | 0.733 / 0.793 / **0.813** | 0.717 / 0.737 / 0.760 | +0.016 / +0.056 / +0.053 |
| P@1 / @3 / @5 | 0.733 / 0.439 / 0.325 | 0.717 / 0.394 / 0.291 | +0.016 / +0.045 / +0.034 |

**精排侧结论**：
1. **排序质量 bge 全面领先**，且增益随 k 增大而放大（NDCG@5 +0.053 > NDCG@1 +0.016）——
   说明 bge 的价值主要体现在**把正确答案前移**，而非仅仅"能召回到"。
2. **P@5 只有 0.325 是语料的必然属性，不是缺陷**：992 条语料里 382 条是同义重复，
   一个 query 天然对应多条正确答案，top-5 里"不相关的"很可能只是**别的同义表述**，
   语义口径下已计入相关；但语料中大部分 chunk 仍属不同问题，故 P@5 上限本就低。
   **读 Precision 必须连同语料的重复度一起读**，否则会得出"系统噪声很大"的错误结论。
3. NDCG@1 与 P@1 数值恒等（0.733）是二值相关性下的数学必然，可用作指标实现的**自检断言**。


**结论**：
1. **规模化语义召回达标**：992 chunk 上 bge 语义 Recall@5 = **95.3%**——「问一句，5 条内召回一个能答的 chunk」
   几乎总能命中。且这是**下界**：`bench_scale.py` 的语义 ground-truth 只认「原文完全同义（去标点后相等）」，
   像「这个衣服舒服吗 → 这件衣服面料舒服吗」这类**改写级正确召回**未计入，实际质量更高。
2. **严格口径失真**：语料有 **382/992 chunk 是同义重复问题**（「这款手机支持5G吗」×19、「这个商品有现货吗」×12），
   严格口径要求命中"自己那一条"，必然低估——评测必须用语义口径才公平。
3. **bge vs dev 的分野在语义**：严格 Recall@1 两者持平（52% vs 53%，因 query 原文就在自己 chunk，词法 n-gram 天然占优）；
   语义 Recall@3 bge 领先 dev **+5.7pt**（92.0% vs 86.3%），top-1 相似度区分度更高（0.749 vs 0.658）——
   这正是换真实嵌入的价值，与 P2 哨兵用例（P0/P1/P2 字面相似）结论一致。
4. **性能代价**：bge 纯 CPU 编码 992+300 条 = **826s**（dev 仅 0.8s）。规模化上线需**向量持久化缓存**（一次编码复用）
   + 批量编码 + GPU，或直接交 pgvector 后端（已在本评测用矩阵乘法预演其速度）。

---

## 语料规范（唯一权威口径 · 已对齐）

> 以下为准绳，**新增/修订 SLA 文档必须遵守**，否则会重新引入事实冲突。

### 档位编号：P0 起（P0 = 最高级）
| 档位 | 名称 | 工单响应时限 |
|---|---|---|
| P0 | 紧急（业务中断） | **15 分钟** |
| P1 | 高危（严重降级） | **1 小时** |
| P2 | 普通 | **4 小时** |

### 其他统一项
- **术语**：统一叫「**工单响应**」（不再用「故障响应」）。
- **可用性**：企业版 **99.95%**，标准版 **99.9%**；对应月度故障时长上限 **21.9 分钟**。
- **赔付**：月度可用性低于承诺值按比例补偿服务时长，上限 **当月费用 30%**。
- **维护窗口**：凌晨 **02:00-04:00**，提前 **24 小时**通知。

### 已对齐的文档（三份现在完全一致，零冲突）
- `samples/sla说明.md` —— 简版摘要（唯一生成入口：`make_samples.py`）
- `samples/sla手册.docx` —— 完整版（由 `make_samples.py` 生成，勿手改二进制）
- `test-docs/服务级别协议SLA.docx` —— 完整版（静态文件，已就地改写）

### 历史冲突（已消解）
- 原 `服务级别协议SLA.docx` 用 **P1/P2/P3** 体系（P1=业务中断 15 分钟 / P2=严重降级 30 分钟 / P3=2 小时），
  与另两份的 **P0/P1/P2** 体系**整体错位一级**，导致「P2」在两边指向不同严重度（普通 4 小时 vs 严重降级 30 分钟）。
  现已统一到 P0 起，数值亦对齐。

### 后续建议（防再次漂移）
- 摄取时加**冲突检测**：同一档位出现不同数值时告警，不静默入库。
- 文档补**元数据**：版本 / 生效日期 / 适用客户等级，便于多版本共存时按优先级取用。

> 注意：当前指标只测「关键词是否出现 + 是否混入错误分块」，属必要条件，
> 尚不足以度量答案自然度与完整性——需 P0-1 接 LLM 后再补语义级评测。

## 建议的下一步（按投入产出比排序 · 2026-09-29 复核）

**已完成的历史首步**（保留供追溯）：
1. ~~P1-5 + P0-1~~：`golden.jsonl` + `RegressionEvaluator` 已落地 → **✅ 已完成**。
2. ~~P0-3 Path A（bge 嵌入）~~：通过率 91% → 100% → **✅ 已完成**。
3. ~~P2-10 测试 + CI~~：Actions 流水线（当时 286 例单测 + 七 job）→ **✅ 已完成并于 2026-09-29 真实跑通**（2026-09-30 已增至 **358 例 / 九 job**）。
4. ~~P0-3 Path B（pgvector 生产存储）~~ → **✅ 已完成（2026-09-29）**：
   已在真实 PG 上跑通，并抓出/修掉 3 个只有真库才暴露的缺陷（见 P0-3 小节）。
5. ~~P0-2（重试 / 熔断 / 降级）~~ → **✅ 已完成（2026-09-29）**：
   `RetryPolicy` 不再是零引用空壳，外部后端不可用时整条问答链路平滑降级（见 P0-2 小节）。
6. ~~P1-4（动态任务分解 / 多轮协商 / 人工介入）~~ → **✅ 已完成（2026-09-29）**：
   `Planner` / `AgentRegistry` 不再只是预留 seam，多 agent 从固定 DAG 变为按问题临时组队（见 P1-4 小节）。
7. ~~P2-9（接真实 MCP host + 动作型工具）~~ → **✅ 已完成（2026-09-29）**：
   动作型工具（风险分级 / 参数契约 / 确认门 / 审计）+ 真实 MCP 协议端到端（CI `mcp` job，
   run `36541763060` 全绿）+ 接入编排与 HITL 闭环 + Web/前端动作面板，53 例单测（见 P2-9 小节）。

**下一批候选**：
| 序 | 事项 | 投入 | 为什么现在做 |
|---|---|---|---|
| ~~1~~ | ~~验证 `docker build`~~ | — | **✅ 已完成（2026-09-29）**：已并入 CI `docker` job，构建+启动+冒烟全绿 |
| ~~2~~ | ~~P0-3 Path B：起 PG + `setup_db.py --graph`~~ | 中 | **✅ 已完成（2026-09-29，run 36530049484 全绿）**：CI 新增 `pg` job（`pgvector/pgvector:pg16` service container）→ 建表 → 回归评测全跑在 PG → 校验 schema 与非空数据。AGE 图侧由 `age` job 单独验证（同库形态仍待补） |
| ~~1~~ | ~~P0-2：适配器重试 / 熔断 / 降级~~ | 中 | **✅ 已完成（2026-09-29）**：`RetryPolicy`/`CircuitBreaker` 已由 `adapters` 真实接入，重试+指数退避+按后端独立熔断+降级卡片全链路打通；收尾后 51 例单测 |
| ~~1~~ | ~~P1-4：LLM 动态任务分解~~ | 大 | **✅ 已完成（2026-09-29）**：动态分解 + 多轮协商 + 人工介入三件事落地，48 例单测，真实链路联调通过（见 P1-4 小节）；收尾后 **72 例** |
| ~~1~~ | ~~P2-9：接真实 MCP host + 动作型工具~~ | 大 | **✅ 已完成（2026-09-29，CI run 36541763060 四 job 全绿）**：动作型工具 + 真实 MCP 协议端到端 + 编排/HITL 闭环，53 例单测（见 P2-9 小节） |
| ~~1~~ | ~~P2-10 补：负载测试 + bge 独立 CI job + AGE 真跑~~ | 小 | **✅ 已完成（`CI #19` / run `36663136008` 七 job 全绿）**：CI 增至七 job（新增 `load` / `age` / `bge`，2026-09-30 又增至八 job）；负载实测 ~150 req/s 零丢数据、限流精确；AGE 用官方 `apache/age` 容器真跑（1.6.0，真落图 7 顶点 / 6 边）——**三轮 CI 连抓三类真 bug**（列定义列表列数 / 图名与 Cypher 必须是常量字面量 / 旁路对账被基表继承语义坑），已全部修复并补 15 例回归测试；bge 夜跑口径 100%（阈值 0.95）。顺手抓出 `webbrowser` 污染限流配额的真问题（见 P2-10 补小节） |
| ~~1~~ | ~~P0-2 收尾：接**真实** endpoint 联调~~ | 小 | **✅ 已完成（2026-09-30，`CI #21` / run `36673194795` 八 job 全绿）**：新增 CI `adapters` job——起**真实 socket + 真实 HTTP 协议**的替身后端 + 真实 app 子进程，26 项断言全部基于「服务端实际收到什么、被打多少次」（此前单测把 HTTP 层整个 monkeypatch 掉，一行 socket 都没走）。并抓出/修掉「可重试性一刀切」真缺陷：4xx（除 408/429）不重试也不计熔断、`404` 独立成 `not_found` 终态与 `degraded` 区分；单测 29 → **51 例**（见 P0-2 小节） |
| ~~2~~ | ~~P1-4 收尾：agent 间双向消息协商 / 子任务依赖编排~~ | 中 | **✅ 已完成（2026-09-30，`CI #23` / run `36677436460` 九 job 全绿）**：`Subtask.depends_on` 真的被消费（依赖分层：同层并行 / 层间串行 / 无依赖=单层且顺序不变 / 成环降级不卡死）；协商从「评审 → 补人」单向补轮升级为**双向账本**（`request_help` / `reply_to`，`provided` / `declined` / `unavailable`，**沉默不算数**，被拒进「别再提」名单）。新增 CI `collab` job（九 job），联调脚本重写为 **28 项断言** check 脚本，单测 48 → **72 例**（见 P1-4 收尾小节） |
| ~~3~~ | ~~P2-9 收尾：动作执行结果回写 GraphStore~~ | 中 | **✅ 已完成（2026-09-30）**：执行成功的动作把结果写回图谱（`Ticket` 节点 + `SUBMITTED_BY`/`ABOUT_PRODUCT`），打通「执行 → 关系 → 再检索」；默认关、写失败只降级、映射过本体校验；`age` job 新增阶段七在**真实 AGE** 上旁路对账验证（含「关掉开关零写入」）。单测 332 → **358 例**（见 P2-9 收尾小节） |
| 4 | AGE 与 pgvector **同库**（决策 D2 的生产形态） | 中 | 目前 AGE 单独验证；同库需自建含两个扩展的镜像（或改用 `apache/age` 基础镜像装 pgvector） |
| 5 | Web 服务 HTTP/1.1 keep-alive | 小 | 现为 HTTP/1.0，每请求新建连接；压测已记录该特征，改 HTTP/1.1 + 正确 `Content-Length` 即可提吞吐 |

> P2-9 / P2-10 补 / P0-2 收尾 / P1-4 收尾 / P2-9 收尾 均已完成并全部在 CI 上真实跑通，完成度已达 **~92–95%**；
> 本批剩余两项（AGE 同库 / keep-alive）做完可到 **~97%**。
