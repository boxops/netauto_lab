"""
Network Operations AI Agent

Capabilities:
- Monitor Prometheus alerts and investigate root causes
- Query Loki for log patterns and correlate with metrics
- Identify affected devices and suggest remediations
- Execute approved remediations via Ansible (check mode by default)
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from shared.config import settings
from shared.llm import get_llm
from shared.tools import OPS_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert Network Operations AI agent for a network automation platform.

You have access to the following systems:
- Nautobot (Source of Truth): device inventory, topology, IPAM, configuration management
- Prometheus: real-time metrics and alerts for network devices
- Loki: centralized syslog aggregation from all network devices
- Ansible: network automation playbook execution (check mode by default)

Your responsibilities:
1. Investigate network alerts and incidents
2. Correlate metrics, logs, and configuration data to identify root causes
3. Provide clear, actionable analysis with supporting evidence
4. Suggest and (with approval) execute remediation steps
5. Generate incident timelines and impact assessments

Safety rules:
- NEVER execute Ansible playbooks in live mode without explicit user approval
- Always use check_mode=True for Ansible unless the user says "approved" or "execute"
- Never expose credentials in your responses
- Be conservative about automated changes to production systems

When investigating:
1. First check active alerts
2. Query relevant Prometheus metrics
3. Check device logs in Loki
4. Correlate findings across systems
5. Present a clear summary with evidence and recommendations
"""


class OpsAgent:
    """Network Operations AI Agent."""

    def __init__(self) -> None:
        self.llm = get_llm(temperature=0.1)
        self.memory = MemorySaver()
        self.agent = create_react_agent(
            model=self.llm,
            tools=OPS_TOOLS,
            checkpointer=self.memory,
            prompt=SYSTEM_PROMPT,
        )

    def chat(self, message: str, session_id: str = "default") -> str:
        """
        Send a message to the ops agent and get a response.

        Args:
            message: User input text.
            session_id: Conversation session identifier for memory.

        Returns:
            Agent response text.
        """
        config = {"configurable": {"thread_id": session_id}}
        result = self.agent.invoke(
            {"messages": [HumanMessage(content=message)]},
            config=config,
        )
        last_message = result["messages"][-1]
        return last_message.content

    async def astream(self, message: str, session_id: str = "default") -> AsyncGenerator[str, None]:
        """
        Stream the agent's response token by token.

        Args:
            message: User input text.
            session_id: Conversation session identifier.

        Yields:
            Response text chunks.
        """
        config = {"configurable": {"thread_id": session_id}}
        async for chunk in self.agent.astream(
            {"messages": [HumanMessage(content=message)]},
            config=config,
            stream_mode="messages",
        ):
            for msg in chunk:
                if isinstance(msg, AIMessage) and msg.content:
                    yield msg.content
