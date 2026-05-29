"""
Network Chaos Monkey AI Agent
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
from shared.rate_limiter import RateLimiter, BudgetExceededError
from shared.task_store import TaskStore
from shared.status_tracker import AgentStatus, StatusCallbackHandler

logger = logging.getLogger(__name__)

AGENT_NAME = "chaos_agent"

agent_status   = AgentStatus(agent_name=AGENT_NAME)
task_store     = TaskStore()
rate_limiter   = RateLimiter()
status_handler = StatusCallbackHandler(
    status=agent_status,
    agent_name=AGENT_NAME,
    task_store=task_store,
    rate_limiter=rate_limiter,
)

SYSTEM_PROMPT = """You are a Chaos Monkey AI agent for a network automation lab.

Your objective is to help operators design and run safe, controlled chaos experiments that
validate detection, observability, and recovery workflows. This agent is lab-only.

## Safety Rules
- NEVER suggest actions for production environments.
- Always default to check_mode=True. Only set check_mode=False when the user says "approved", "execute", or "apply".
- Always assess blast radius BEFORE proposing any disruptive action.

## Tool Guide

### Tier 1 — Nautobot Discovery
- get_all_devices(), get_device_info(), get_device_interfaces(), get_topology(), get_connected_devices()

### Tier 2 — Prometheus
- get_active_alerts(), get_device_metrics(), get_interface_metrics(), query_prometheus()

### Tier 3 — Loki
- get_interface_events(), get_bgp_events(), get_recent_errors()

### Tier 4 — Chaos Actions (always check_mode=True by default)
- shutdown_interface(device, interface, check_mode)
- restore_interface(device, interface, check_mode)
- flap_bgp_neighbor(device, neighbor_ip, method, check_mode)
- verify_bgp_state(device, neighbor_ip)

### Tier 4 — General Actions
- run_show_commands(device_name, commands)
- run_config_commands(device_name, config_lines, check_mode)

## Experiment Report Format
Always structure proposals with: Goal, Pre-conditions, Procedure, Expected signals,
Success criteria, Rollback steps.
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
        response, _ = self.chat_with_trace(message, session_id=session_id)
        return response

    def chat_with_trace(
        self,
        message: str,
        session_id: str = "default",
        task_id: str | None = None,
        task_type: str | None = None,
    ) -> tuple[str, list[dict]]:
        rate_limiter.check_budget(AGENT_NAME)

        status_handler.set_context(
            session_id=session_id,
            task_id=task_id,
            task_type=task_type,
        )
        config = {
            "configurable": {"thread_id": session_id},
            "callbacks": [status_handler],
        }
        try:
            result = self.agent.invoke(
                {"messages": [HumanMessage(content=message)]},
                config=config,
            )
        finally:
            status_handler.clear_context()

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
        config = {
            "configurable": {"thread_id": session_id},
            "callbacks": [status_handler],
        }
        status_handler.set_context(session_id=session_id)
        try:
            async for chunk in self.agent.astream(
                {"messages": [HumanMessage(content=message)]},
                config=config,
                stream_mode="messages",
            ):
                for msg in chunk:
                    if isinstance(msg, AIMessage) and msg.content:
                        yield msg.content
        finally:
            status_handler.clear_context()
