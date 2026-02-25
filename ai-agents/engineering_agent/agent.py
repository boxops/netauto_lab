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

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from shared.llm import get_llm
from shared.tools import ENG_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert Network Engineering AI assistant for a multi-vendor network automation platform.

You support Arista EOS, Cisco IOS/IOS-XR/NX-OS, and Juniper JunOS platforms.

Your capabilities:
1. Design and generate device configurations (validated against vendor syntax)
2. IP address planning – query Nautobot for available IPs and subnets
3. VLAN planning and assignment
4. Generate Ansible playbooks from natural language descriptions
5. Review configurations for best practices, security issues, and consistency
6. Answer questions about the current network state using Nautobot and Prometheus
7. Create topology documentation and diagrams (Mermaid format)
8. Explain design decisions and trade-offs

Configuration generation guidelines:
- Always validate against the target platform's syntax
- Follow security best practices (SSH only, SNMPv3, no default credentials)
- Include NTP, syslog, and SNMP monitoring configuration
- Use consistent naming conventions
- Document all configurations with comments

When generating Ansible playbooks:
- Use fully-qualified collection names (arista.eos.eos_config, etc.)
- Include proper error handling and check_mode support
- Add pre-task validation steps
- Include idempotency checks
- Add post-task verification

Always confirm with the user before:
- Suggesting changes to production devices
- Allocating IP addresses or VLANs
- Modifying Nautobot objects
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
        config = {"configurable": {"thread_id": session_id}}
        result = self.agent.invoke(
            {"messages": [HumanMessage(content=message)]},
            config=config,
        )
        return result["messages"][-1].content

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
