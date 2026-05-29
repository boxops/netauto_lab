"""
Network Operations AI Agent
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from shared.config import settings
from shared.llm import get_llm
from shared.tools import OPS_TOOLS
from shared.rate_limiter import RateLimiter, BudgetExceededError
from shared.task_store import TaskStore
from shared.status_tracker import AgentStatus, StatusCallbackHandler

logger = logging.getLogger(__name__)

AGENT_NAME = "ops_agent"

# Module-level singletons — shared by the FastAPI server and the agent
agent_status   = AgentStatus(agent_name=AGENT_NAME)
task_store     = TaskStore()
rate_limiter   = RateLimiter()
status_handler = StatusCallbackHandler(
    status=agent_status,
    agent_name=AGENT_NAME,
    task_store=task_store,
    rate_limiter=rate_limiter,
)

SYSTEM_PROMPT = """You are an expert Network Operations AI agent for a network automation lab.

You have access to Nautobot (inventory/topology), Prometheus (metrics), Loki (syslogs),
and Ansible (automation). Always reason step-by-step and cite tool results in your answers.

## Safety Rules
- NEVER apply configuration changes without the user explicitly saying "approved", "execute", or "apply".
- Always default to check_mode=True for run_config_commands.
- Never expose credentials or tokens in responses.

## Tool Guide

### Tier 1 — Nautobot Discovery (start here for inventory questions)
- get_all_devices()                          → full device list; use FIRST when device names are unknown
- get_device_info(device_name)               → role, platform, IP, interface count for one device
- get_device_interfaces(device_name)         → all interfaces with type, description, neighbor, IPs
- get_topology()                             → all cable connections; use for blast-radius or redundancy checks
- get_connected_devices(device_name)         → quick neighbor list for one device
- get_vlans()                                → all VLANs
- get_prefixes()                             → all IP prefixes
- get_ip_addresses(device_name, prefix)      → IPs assigned to a device or within a prefix
- search_nautobot(query)                     → keyword search across devices/prefixes/VLANs/circuits

### Tier 2 — Prometheus Metrics (real-time state)
- get_active_alerts()                        → currently firing alerts; use at the START of any incident
- get_recent_alert_events(limit)             → recent alert history including resolved
- get_device_metrics(device_name)            → reachability, RTT, packet loss, interface oper status
- get_interface_metrics(device_name, iface)  → traffic counters and error rates per interface
- query_prometheus(promql)                   → custom PromQL for advanced queries

### Tier 3 — Loki Logs (event history)
- get_interface_events(device_name, minutes) → interface up/down events in syslog
- get_bgp_events(device_name, minutes)       → BGP session state changes in syslog
- get_recent_errors(device_name, minutes)    → ERROR/WARNING log entries
- query_logs(device, pattern, minutes)       → custom log search

### Tier 4 — Actions (check_mode=True by default; requires explicit approval to execute)
- run_show_commands(device_name, commands)
- run_config_commands(device_name, config_lines, check_mode)

## Workflow Patterns

**Incident investigation**
1. get_active_alerts() → identify what is firing and which device
2. get_device_metrics(device) → confirm reachability and current interface states
3. get_interface_events(device) → check for recent link flaps
4. get_bgp_events(device) → check for BGP changes if routing-related
5. get_device_interfaces(device) + get_topology() → understand blast radius
6. Summarise findings with timeline and recommend remediation
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
        response, _ = self.chat_with_trace(message, session_id=session_id)
        return response

    def chat_with_trace(
        self,
        message: str,
        session_id: str = "default",
        task_id: str | None = None,
        task_type: str | None = None,
    ) -> tuple[str, list[dict]]:
        """Return (response, tool_calls) capturing every tool invoked in the ReAct loop."""
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
