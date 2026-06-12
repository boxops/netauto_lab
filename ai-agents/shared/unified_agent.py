"""
Unified AI agent combining operations, engineering, and validation capabilities.

Replaces the three separate agent classes (OpsAgent, EngineeringAgent, ChaosAgent)
with a single UnifiedAgent that has access to all tools and a combined system prompt.
The agent decides which tools to use based on the user's request.

Tool set: union of OPS_TOOLS (22) + get_runbook (1) + CHAOS_TOOLS (1–4)
  = 23 tools by default, 26 with CHAOS_TOOLS_ENABLED=true

Rate limiting: interactive /chat calls are tracked under AGENT_NAME = "ai_agent".
Pipeline budget keys ("ops_agent", "eng_agent", "chaos_agent") remain separate
inside the IncidentWorkflow nodes and task runners.
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from shared.checkpoints import get_chat_checkpointer
from shared.config import settings
from shared.llm import get_llm
from shared.tools import OPS_TOOLS, ENG_TOOLS
from shared.rate_limiter import BudgetExceededError

logger = logging.getLogger(__name__)

AGENT_NAME = "ai_agent"

# ── Tool set ──────────────────────────────────────────────────────────────────
# Union of all three tool sets, first-occurrence wins (OPS_TOOLS order preserved).

from ops_agent.chaos_tools import CHAOS_TOOLS as _CHAOS_TOOLS

_tool_map: dict = {}
for _t in OPS_TOOLS + ENG_TOOLS + _CHAOS_TOOLS:
    _tool_map.setdefault(_t.name, _t)
ALL_TOOLS = list(_tool_map.values())

# ── System prompt ─────────────────────────────────────────────────────────────

_CHAOS_TOOLS_SECTION = """
### Tier 4 — Lab Chaos / Validation Actions (only when CHAOS_TOOLS_ENABLED=true; lab environments only)
- shutdown_interface(device, interface, check_mode)  → admin-shut an interface to simulate link failure
- restore_interface(device, interface, check_mode)   → re-enable a shut interface
- flap_bgp_neighbor(device, neighbor_ip, method, check_mode) → simulate a BGP session drop
- verify_bgp_state(device, neighbor_ip)              → read-only BGP session state check

**NEVER use these tools in production. Always assess blast radius first.**
Always structure chaos experiment proposals with:
  Goal / Pre-conditions / Procedure / Expected signals / Success criteria / Rollback steps
""" if settings.chaos_tools_enabled else ""

UNIFIED_SYSTEM_PROMPT = f"""You are an expert network operations and engineering AI agent for a
multi-vendor network automation lab. You perform root cause analysis, configuration generation,
fix validation, and controlled lab experiments.

You support Arista EOS, Cisco IOS/IOS-XR/NX-OS, Nokia SR Linux, and Juniper JunOS.
Always reason step-by-step and cite tool results in your answers.
Always query Nautobot first to ground your answers in actual inventory data.

## Safety Rules
- NEVER apply configuration changes without the user explicitly saying "approved", "execute", or "apply".
- Always default to check_mode=True for run_config_commands.
- Never expose credentials or tokens in responses.
- Always assess blast radius BEFORE proposing any disruptive action.
- Chaos/destructive tools are for lab environments only — never suggest them for production.

## Tool Guide

### Tier 0 — Runbook Library (check FIRST for alert-driven tasks)
- get_runbook(alertname)  → canonical fix procedure for a known alert type.
  Call this BEFORE any other tool when responding to an automated fix request.
  If a runbook matches, follow its steps rather than re-deriving the fix from scratch.
  This reduces token usage by 60-80% and produces consistent, tested procedures.

### Tier 1 — Nautobot Discovery (start here for inventory questions)
- get_all_devices()                          → full device list; call FIRST for any multi-device task
- get_device_info(device_name)               → role, platform, IP, interface count for one device
- get_device_interfaces(device_name)         → all interfaces with type, description, neighbor, IPs
- get_topology()                             → all cable connections; use for blast-radius or redundancy checks
- get_connected_devices(device_name)         → quick neighbor list for one device
- get_vlans()                                → all VLANs
- get_prefixes()                             → all IP prefixes and subnets
- get_ip_addresses(device_name, prefix)      → IPs assigned to a device or within a prefix
- get_available_ips(prefix, count)           → find free IPs in a prefix for allocation
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
{_CHAOS_TOOLS_SECTION}
### Tier 4 — Actions (check_mode=True by default; requires explicit approval to execute)
- run_show_commands(device_name, commands)
- run_config_commands(device_name, config_lines, check_mode)

## Workflow Patterns

**Incident investigation**
1. get_active_alerts() → identify what is firing and which device
2. get_device_metrics(device) → confirm reachability and current interface states
3. get_interface_events(device) / get_bgp_events(device) → check syslog for recent events
4. get_device_interfaces(device) + get_topology() → understand blast radius
5. Summarise findings with timeline and recommend remediation

**Configuration generation**
1. get_all_devices() / get_device_info(device) → confirm platform and current state
2. get_device_interfaces(device) → understand existing interface layout
3. get_prefixes() / get_available_ips() → use real IPAM data for addressing
4. run_show_commands(device, cmds) → read current running-config if needed
5. run_config_commands(device, config_lines, check_mode=True) → simulate the change first

**Confirmation Required Before**
- Allocating IPs or VLANs that modify Nautobot
- Applying config changes with check_mode=False (requires "approved", "execute", or "apply")
"""


# ── Agent class ───────────────────────────────────────────────────────────────

class UnifiedAgent:
    """
    Single LangGraph ReAct agent with all network tools and a combined system prompt.
    Accepts rate_limiter and status_handler as constructor parameters so the
    calling main.py controls the singleton lifecycle.
    """

    def __init__(self, rate_limiter, status_handler) -> None:
        self.llm            = get_llm(temperature=0.1)
        self.memory         = get_chat_checkpointer()
        self._rate_limiter  = rate_limiter
        self._status_handler = status_handler
        self.agent = create_react_agent(
            model=self.llm,
            tools=ALL_TOOLS,
            checkpointer=self.memory,
            prompt=UNIFIED_SYSTEM_PROMPT,
        )

    def chat(self, message: str, session_id: str = "default") -> str:
        response, _ = self.chat_with_trace(message, session_id=session_id)
        return response

    def chat_with_trace(
        self,
        message:    str,
        session_id: str = "default",
        task_id:    str | None = None,
        task_type:  str | None = None,
    ) -> tuple[str, list[dict]]:
        """Return (response, tool_calls) capturing every tool invoked in the ReAct loop."""
        self._rate_limiter.check_budget(AGENT_NAME)

        self._status_handler.set_context(
            session_id=session_id,
            task_id=task_id,
            task_type=task_type,
        )
        config = {
            "configurable": {"thread_id": session_id},
            "callbacks": [self._status_handler],
        }
        try:
            result = self.agent.invoke(
                {"messages": [HumanMessage(content=message)]},
                config=config,
            )
        finally:
            self._status_handler.clear_context()

        tool_calls: list[dict] = []
        for msg in result["messages"]:
            if isinstance(msg, ToolMessage):
                tool_calls.append({
                    "tool_name":      msg.name,
                    "output_summary": (msg.content or "")[:300],
                })
            elif isinstance(msg, AIMessage) and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append({
                        "tool_name":    tc.get("name", ""),
                        "input_summary": str(tc.get("args", ""))[:200],
                    })
        return result["messages"][-1].content, tool_calls

    async def astream(self, message: str, session_id: str = "default") -> AsyncGenerator[str, None]:
        config = {
            "configurable": {"thread_id": session_id},
            "callbacks": [self._status_handler],
        }
        self._status_handler.set_context(session_id=session_id)
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
            self._status_handler.clear_context()
