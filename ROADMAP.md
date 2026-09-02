# 待解决优先级 · 上线路线图（ROADMAP）

> 当前状态：**能跑的架构原型 / 离线 demo 级**，完成度约 20–25%。
> 本文件列出通往「真正上线的 B2B 多 agent 协作共同体」的待办，按优先级排序；
> 每项标注了**已预留的拓展接口**（位于 `kb_mcp_server/extensions.py`），补做时直接实现接口并注册即可，无需改动编排/合成主链路。

---

## P0 — 不补就不能算「能用」

### P0-1 真实 LLM 接入与效果评估
- 现状：demo 强制关 LLM；`llm` 路由模式仅在本地跑过一次超时熔断，答案质量/时延/成本零评估。
- 待补：在真机跑 `KB_AGENT_MODE=llm` + `KB_LLM_ENABLED=1`，配 golden 集做回归（见 P1-5）。
- 接口：**`Evaluator` / `load_golden()`**（已预留，P1-5）。

### P0-2 实时数据真实接入（订单/库存/CRM）
- 现状：适配器真实 HTTP 路径已写，但 demo 强制 `KB_API_MOCK=1`；无重试/超时/鉴权/降级。
- 待补：接真实 endpoint，加重试与熔断，调用失败降级到兜底答案。
- 接口：**`RetryPolicy`**（已预留）。

### P0-3 生产级存储与向量真跑通
- 现状：memory + dev 哈希嵌入（语义弱）；prod 路径 pgvector + bge + AGE 本机未起 PG，属「代码写了未验证」。
- 待补：起 PG，跑 `setup_db.py --graph`，接 bge 嵌入，做规模化召回评测。
- 接口：**`VectorStore` / `GraphStore` / `Embedder` 抽象已存在**，无需新增，直接接实现。

---

## P1 — 让「多 agent」名副其实

### P1-4 从静态 DAG 升级为可 emergent 的「共同体」
- 现状：`Orchestrator` 是固定流水线 `GraphBuilder → [Retriever ∥ GraphReasoner ∥ LiveData] → Synthesizer`，llm 模式只是裁剪 agent；无 agent 间消息协商、动态分解、human-in-the-loop。
- 待补：LLM 动态任务分解 / 多轮协商 / 人工介入；agent 按能力动态组队。
- 接口：**`Planner`（已用 `DeterministicPlanner` 默认接入）／ `AgentRegistry`（agent 已自注册并打 `capabilities` 标签）**。

### P1-5 评估与护栏（合规刚需）
- 现状：仅 `trace_id` 基础留痕；无 golden 集/评测脚本、无幻觉抑制、置信度未真正拦截、无审计日志落盘。
- 待补：golden 回归脚本；`Guardrail` 实现（低置信度 → 标记需人工复核 / 拦截）；审计日志落盘。
- 接口：**`Guardrail`（已用 `PassthroughGuardrail` 默认接入合成链路）／ `Evaluator` + `load_golden()`（已预留）**。

---

## P2 — 企业交付

### P2-6 多租户与权限
- 现状：仅单 `api_token` Bearer，无租户隔离/RBAC/SSO。
- 待补：租户上下文贯穿检索/图谱/实时数据；RBAC；SSO。
- 接口：**`TenantProvider` / `TenantContext`（`AgentContext.tenant_id` 已预留）**。

### P2-7 并发与部署
- 现状：单进程 `ThreadingHTTPServer`，memory store 非进程安全；发布沙箱为单端口 demo。
- 待补：队列/横向扩展/灰度；进程安全存储；真 infra 部署。

### P2-8 知识抽取规模化
- 现状：规则+LLM 抽取未在真实体量验证。
- 待补：增量/去重/冲突消解/三元组人工审核。

### P2-9 MCP 真落地 + 动作型工具
- 现状：14 工具只活在 `server.py`，未接真实 MCP host；agent 只会检索不会执行动作。
- 待补：接入真实 MCP host；实现「改单/退款/建工单」等动作。
- 接口：**`ActionTool` / `ActionToolRegistry`（已预留，区别于检索型工具）**。

### P2-10 自动化测试与 CI
- 现状：零自动化测试（仅有离线自检脚本）。
- 待补：单测/集成/负载测试 + CI 流水线。

---

## 建议的首步（投入产出比最高）
1. **P1-5 + P0-1**：写 `golden.jsonl` + 一个 `Evaluator` 实现，跑出基线指标 —— 没有度量就没有「上线」。
2. **P0-3**：在一台有 PG 的机器上跑通 age + bge 生产路径。
3. 做完这两步，完成度可到 ~45%。
