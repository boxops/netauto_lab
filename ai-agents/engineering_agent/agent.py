"""
Network Engineering AI Agent

Capabilities:
- Design and generate device configurations from natural language
- IP address and VLAN planning via Nautobot
- Generate Ansible playbooks
- Review configurations for best practices
- Answer network design questions
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from shared.llm import get_llm
from shared.tools import ENG_TOOLS

logger = logging.getLogger(__name__)

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

### Tier 3 — Automation
- run_ansible_playbook(playbook, devices, check_mode, extra_vars)
  Always check_mode=True unless the user explicitly approves execution.

## Workflow Patterns

**"Find all devices and their interfaces / generate interface descriptions"**
1. get_all_devices() → get device names and roles
2. get_device_interfaces(device) for each device → get interface details and neighbors
3. Use description and connected_to fields to generate standardised descriptions

**"Design config for a new device"**
1. get_all_devices() + get_topology() → understand existing topology and naming
2. get_vlans() → see existing VLANs to reference
3. get_prefixes() → understand IP addressing scheme
4. get_available_ips(prefix) → allocate management IP
5. Generate config using lab conventions (device names, AS numbers, VLAN IDs)

**"Plan IP addressing for a new subnet"**
1. get_prefixes() → review existing prefixes to avoid overlap
2. get_ip_addresses(prefix=parent_prefix) → see what's already allocated
3. get_available_ips(prefix, count) → find free addresses

**"Generate an Ansible playbook"**
1. get_all_devices() → confirm target device names
2. get_device_info(device) → confirm platform (determines Ansible collection to use)
3. Generate playbook with fully-qualified collection names and check_mode support

**"Document the topology"**
1. get_topology() → all cable connections
2. get_all_devices() → device roles and platforms
3. get_vlans() + get_prefixes() → layer 2/3 context
4. Produce Mermaid diagram and written description

## Configuration Standards
- Always validate syntax against the target platform
- Security: SSH only, SNMPv3, no default credentials
- Include NTP, syslog, and SNMP monitoring in all device configs
- Use consistent naming: interfaces as shown in Nautobot, descriptions as "peer_device:peer_interface"
- Ansible playbooks: use fully-qualified collection names, idempotency checks, pre/post validation tasks

## Confirmation Required Before
- Allocating IPs or VLANs that modify Nautobot
- Generating configs that would change production behaviour
- Running Ansible in live mode (check_mode=False)
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
        self, message: str, session_id: str = "default"
    ) -> tuple[str, list[dict]]:
        """Return (response, tool_calls) capturing every tool invoked in the ReAct loop."""
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
        config = {"configurable": {"thread_id": session_id}}
        async for chunk in self.agent.astream(
            {"messages": [HumanMessage(content=message)]},
            config=config,
            stream_mode="messages",
        ):
            for msg in chunk:
                if isinstance(msg, AIMessage) and msg.content:
                    yield msg.content
