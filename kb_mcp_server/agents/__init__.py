"""多 agent 协作层（P7 / P2-9）。

多个职责 agent + 编排器，通过共享黑板（AgentContext）协作，
底层能力（向量检索 / 图谱 / 实时数据 / 动作工具 / 合成）全部复用 kb_mcp_server 既有实现。

注：`ActionAgent`（P2-9）**不在**下方便捷导出里——它由编排器单独持有、
受 `KB_AGENT_ACTIONS` 开关控制（有副作用，生命周期与只读 worker 不同）。
需要时从 `kb_mcp_server.agents.workers` 直接导入。
"""

from kb_mcp_server.agents.base import AgentContext, AgentResult, BaseAgent
from kb_mcp_server.agents.orchestrator import Orchestrator, get_orchestrator
from kb_mcp_server.agents.workers import (
    ActionAgent,
    GraphBuilderAgent,
    GraphReasonerAgent,
    LiveDataAgent,
    RetrieverAgent,
    SynthesizerAgent,
)

__all__ = [
    "AgentContext", "AgentResult", "BaseAgent",
    "Orchestrator", "get_orchestrator",
    "GraphBuilderAgent", "GraphReasonerAgent", "LiveDataAgent",
    "RetrieverAgent", "SynthesizerAgent", "ActionAgent",
]
