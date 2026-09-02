"""多 agent 协作层（P7）。

四个职责 agent + 编排器，通过共享黑板（AgentContext）协作，
底层能力（向量检索 / 图谱 / 实时数据 / 合成）全部复用 kb_mcp_server 既有实现。
"""

from kb_mcp_server.agents.base import AgentContext, AgentResult, BaseAgent
from kb_mcp_server.agents.orchestrator import Orchestrator, get_orchestrator
from kb_mcp_server.agents.workers import (
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
    "RetrieverAgent", "SynthesizerAgent",
]
