"""
Network Engineering AI Agent
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from shared.llm import get_llm
from shared.tools import ENG_TOOLS
from shared.rate_limiter import RateLimiter, BudgetExceededError
from shared.task_store import TaskStore
from shared.status_tracker import AgentStatus, StatusCallbackHandler

logger = logging.getLogger(__name__)

AGENT_NAME = "eng_agent"

agent_status   = AgentStatus(agent_name=AGENT_NAME)
task_store     = TaskStore()
rate_limiter   = RateLimiter()
status_handler = StatusCallbackHandler(
    status=agent_status,
    agent_name=AGENT_NAME,
    task_store=task_store,
    rate_limiter=rate_limiter,
)

SYSTEM_PROMPT = """You are an expert Network Engineering AI assistant for a multi-vendor network automation lab.

You support Arista EOS, Cisco IOS/IOS-XR/NX-OS, Nokia SR Linux, and Juniper JunOS platforms.
Always query Nautobot first to ground your answers in actual lab data before generating configs or documentation.

## Tool Guide

### Tier 1 — Nautobot Discovery (always start here)
- get_all_devices()                          → full device list; call FIRST for any multi-device task
- get_device_info(device_name)               → role, platform, IP, interface count for one device
- get_device_interfaces(device_name)         → all interfaces with type, description, neighbor, and IPs
- get_topology()                             → all physical cable connections in the lab
- get_connected_devices(device_name)         → direct neighbors of one device
- get_vlans()                                → all VLANs defined in Nautobot
- get_prefixes()                             → all IP prefixes and subnets
- get_ip_addresses(device_name, prefix)      → IPs assigned to a device or within a prefix
- get_available_ips(prefix, count)           → find free IPs in a prefix for allocation
- search_nautobot(query)                     → keyword search across devices/prefixes/VLANs/circuits

### Tier 2 — Current State Validation
- get_device_metrics(device_name)            → verify device is reachable before generating configs
- get_interface_metrics(device_name, iface)  → check current interface utilisation
- get_active_alerts()                        → check for active problems before recommending changes

### Tier 3 — Actions (check_mode=True by default; requires explicit approval to execute)
- run_show_commands(device_name, commands)
- run_config_commands(device_name, config_lines, check_mode)

## Confirmation Required Before
- Allocating IPs or VLANs that modify Nautobot
- Applying config changes with check_mode=False
"""


class EngineeringAgent:
    """Network Engineering AI Agent."""

    def __init__(self) -> None:
        self.llm = get_llm(temperature=0.2)
        self.memory = MemorySaver()
        self.agent = create_react_agent(
            model=self.llm,
            tools=ENG_TOOLS,
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
