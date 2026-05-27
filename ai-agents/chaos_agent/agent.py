"""
Network Chaos Monkey AI Agent

Capabilities:
- Propose controlled chaos experiments in lab environments
- Assess blast radius before suggesting disruptive actions
- Recommend simulation-first checks and rollback strategies
- Execute approved low-risk playbooks in check mode by default
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from chaos_agent.chaos_tools import CHAOS_TOOLS
from shared.llm import get_llm
from shared.tools import OPS_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Chaos Monkey AI agent for a network automation lab.

Your objective is to help operators run safe, controlled chaos experiments to validate detection,
observability, and recovery workflows.

Primary responsibilities:
1. Propose realistic chaos experiments aligned to lab goals.
2. Estimate blast radius and call out likely impact before any action.
3. Prefer simulation and check-mode validation first.
4. Require explicit approval language before recommending execution actions.
5. Always include rollback and verification guidance.

Safety rules:
- This agent is lab-only. Do not suggest actions for production environments.
- Default to check_mode=True for Ansible playbook execution.
- Never perform live disruptive actions unless the user explicitly approves.
- Never expose credentials, secrets, or tokens.
- If requested action is too broad or unsafe, propose a narrower scoped alternative.

Dedicated chaos tools available (use these for precise, reversible disruption):
- shutdown_interface(device, interface, check_mode) — admin-shut a link
- restore_interface(device, interface, check_mode) — undo a shutdown
- flap_bgp_neighbor(device, neighbor_ip, method, check_mode) — clear a BGP session
- verify_bgp_state(device, neighbor_ip) — confirm BGP session state

When responding, provide:
- Goal of experiment
- Preconditions and safety checks
- Step-by-step procedure using the specific chaos tools
- Expected signals in Prometheus/Loki/Nautobot
- Rollback steps
"""


class ChaosAgent:
    """Network Chaos Monkey AI Agent."""

    def __init__(self) -> None:
        self.llm = get_llm(temperature=0.1)
        self.memory = MemorySaver()
        self.agent = create_react_agent(
            model=self.llm,
            tools=OPS_TOOLS + CHAOS_TOOLS,
            checkpointer=self.memory,
            prompt=SYSTEM_PROMPT,
        )

    def chat(self, message: str, session_id: str = "default") -> str:
        """Send a message to the chaos agent and get a response."""
        response, _ = self.chat_with_trace(message, session_id=session_id)
        return response

    def chat_with_trace(
        self, message: str, session_id: str = "default"
    ) -> tuple[str, list[dict]]:
        """Send a message and return (response, tool_calls) where tool_calls captures
        every tool invocation made during the ReAct loop."""
        config = {"configurable": {"thread_id": session_id}}
        result = self.agent.invoke(
            {"messages": [HumanMessage(content=message)]},
            config=config,
        )
        tool_calls: list[dict] = []
        for msg in result["messages"]:
            if isinstance(msg, ToolMessage):
                tool_calls.append({
                    "tool_name": msg.name,
                    "output_summary": (msg.content or "")[:300],
                })
            elif isinstance(msg, AIMessage) and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append({
                        "tool_name": tc.get("name", ""),
                        "input_summary": str(tc.get("args", ""))[:200],
                    })
        return result["messages"][-1].content, tool_calls

    async def astream(self, message: str, session_id: str = "default") -> AsyncGenerator[str, None]:
        """Stream the chaos agent response."""
        config = {"configurable": {"thread_id": session_id}}
        async for chunk in self.agent.astream(
            {"messages": [HumanMessage(content=message)]},
            config=config,
            stream_mode="messages",
        ):
            for msg in chunk:
                if isinstance(msg, AIMessage) and msg.content:
                    yield msg.content
