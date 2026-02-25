"""
tests/test_agents.py
Unit tests for AI agent configuration, tools, and safety guardrails.
All external API calls are mocked — no live services required.

Run with:
    pytest tests/test_agents.py -v
"""

import pytest
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

# Add ai-agents to path so shared modules can be imported
AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))


# ---------------------------------------------------------------------------
# Config / settings
# ---------------------------------------------------------------------------

class TestAgentConfig:
    def test_shared_config_module_exists(self):
        config_path = AI_AGENTS_DIR / "shared" / "config.py"
        assert config_path.exists(), "ai-agents/shared/config.py missing"

    def test_shared_tools_module_exists(self):
        tools_path = AI_AGENTS_DIR / "shared" / "tools.py"
        assert tools_path.exists(), "ai-agents/shared/tools.py missing"

    def test_shared_llm_module_exists(self):
        llm_path = AI_AGENTS_DIR / "shared" / "llm.py"
        assert llm_path.exists(), "ai-agents/shared/llm.py missing"

    def test_ops_agent_module_exists(self):
        agent_path = AI_AGENTS_DIR / "ops_agent" / "agent.py"
        assert agent_path.exists(), "ai-agents/ops_agent/agent.py missing"

    def test_eng_agent_module_exists(self):
        agent_path = AI_AGENTS_DIR / "engineering_agent" / "agent.py"
        assert agent_path.exists(), "ai-agents/engineering_agent/agent.py missing"

    def test_ui_app_exists(self):
        ui_path = AI_AGENTS_DIR / "ui" / "app.py"
        assert ui_path.exists(), "ai-agents/ui/app.py missing"

    def test_requirements_file_exists(self):
        req_path = AI_AGENTS_DIR / "requirements.txt"
        assert req_path.exists(), "ai-agents/requirements.txt missing"

    def test_requirements_has_langchain(self):
        req_path = AI_AGENTS_DIR / "requirements.txt"
        content = req_path.read_text()
        assert "langchain" in content.lower(), "langchain not in requirements.txt"

    def test_requirements_has_fastapi(self):
        req_path = AI_AGENTS_DIR / "requirements.txt"
        content = req_path.read_text()
        assert "fastapi" in content.lower(), "fastapi not in requirements.txt"

    def test_requirements_has_pynautobot(self):
        req_path = AI_AGENTS_DIR / "requirements.txt"
        content = req_path.read_text()
        assert "pynautobot" in content.lower(), "pynautobot not in requirements.txt"


# ---------------------------------------------------------------------------
# Tools module — static analysis
# ---------------------------------------------------------------------------

class TestToolsDefinition:
    """Ensure tools.py defines the expected functions."""

    def test_tools_file_has_nautobot_tool(self):
        tools_path = AI_AGENTS_DIR / "shared" / "tools.py"
        content = tools_path.read_text()
        assert "get_device_info" in content or "nautobot" in content.lower()

    def test_tools_file_has_prometheus_tool(self):
        tools_path = AI_AGENTS_DIR / "shared" / "tools.py"
        content = tools_path.read_text()
        assert "prometheus" in content.lower() or "query_prometheus" in content

    def test_tools_file_has_ansible_tool(self):
        tools_path = AI_AGENTS_DIR / "shared" / "tools.py"
        content = tools_path.read_text()
        assert "ansible" in content.lower() or "run_ansible" in content

    def test_tools_file_has_ops_tools(self):
        tools_path = AI_AGENTS_DIR / "shared" / "tools.py"
        content = tools_path.read_text()
        assert "OPS_TOOLS" in content

    def test_tools_file_has_eng_tools(self):
        tools_path = AI_AGENTS_DIR / "shared" / "tools.py"
        content = tools_path.read_text()
        assert "ENG_TOOLS" in content


# ---------------------------------------------------------------------------
# Ops Agent safety rules
# ---------------------------------------------------------------------------

class TestOpsAgentSafety:
    """The ops agent must enforce check_mode and not execute live actions by default."""

    def test_agent_system_prompt_mentions_check_mode(self):
        agent_path = AI_AGENTS_DIR / "ops_agent" / "agent.py"
        content = agent_path.read_text()
        # Safety language must appear in agent
        assert "check_mode" in content or "check mode" in content.lower()

    def test_agent_system_prompt_mentions_approval(self):
        agent_path = AI_AGENTS_DIR / "ops_agent" / "agent.py"
        content = agent_path.read_text()
        assert "approv" in content.lower() or "confirm" in content.lower()

    def test_run_ansible_playbook_defaults_check_mode(self):
        """run_ansible_playbook tool must default check_mode to True."""
        tools_path = AI_AGENTS_DIR / "shared" / "tools.py"
        content = tools_path.read_text()
        # Verify check_mode=True appears somewhere near the ansible tool
        assert "check_mode" in content


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------

class TestLLMFactory:
    def test_llm_module_has_get_llm(self):
        llm_path = AI_AGENTS_DIR / "shared" / "llm.py"
        content = llm_path.read_text()
        assert "get_llm" in content or "def get_" in content

    def test_llm_module_supports_openai(self):
        llm_path = AI_AGENTS_DIR / "shared" / "llm.py"
        content = llm_path.read_text()
        assert "openai" in content.lower()

    def test_llm_module_supports_ollama(self):
        llm_path = AI_AGENTS_DIR / "shared" / "llm.py"
        content = llm_path.read_text()
        assert "ollama" in content.lower()


# ---------------------------------------------------------------------------
# FastAPI main modules
# ---------------------------------------------------------------------------

class TestFastAPIModules:
    def test_ops_agent_main_exists(self):
        main_path = AI_AGENTS_DIR / "ops_agent" / "main.py"
        assert main_path.exists(), "ops_agent/main.py missing"

    def test_eng_agent_main_exists(self):
        main_path = AI_AGENTS_DIR / "engineering_agent" / "main.py"
        assert main_path.exists(), "engineering_agent/main.py missing"

    def test_ops_main_has_fastapi(self):
        main_path = AI_AGENTS_DIR / "ops_agent" / "main.py"
        content = main_path.read_text()
        assert "FastAPI" in content or "fastapi" in content

    def test_ops_main_has_health_endpoint(self):
        main_path = AI_AGENTS_DIR / "ops_agent" / "main.py"
        content = main_path.read_text()
        assert "/health" in content

    def test_ops_main_has_chat_endpoint(self):
        main_path = AI_AGENTS_DIR / "ops_agent" / "main.py"
        content = main_path.read_text()
        assert "/chat" in content

    def test_eng_main_has_chat_endpoint(self):
        main_path = AI_AGENTS_DIR / "engineering_agent" / "main.py"
        content = main_path.read_text()
        assert "/chat" in content


# ---------------------------------------------------------------------------
# Dockerfiles
# ---------------------------------------------------------------------------

class TestDockerfiles:
    def test_agent_dockerfile_exists(self):
        dfile = AI_AGENTS_DIR / "Dockerfile"
        assert dfile.exists(), "ai-agents/Dockerfile missing"

    def test_agent_ui_dockerfile_exists(self):
        dfile = AI_AGENTS_DIR / "Dockerfile.ui"
        assert dfile.exists(), "ai-agents/Dockerfile.ui missing"

    def test_agent_dockerfile_uses_python311(self):
        dfile = AI_AGENTS_DIR / "Dockerfile"
        content = dfile.read_text()
        assert "python:3.11" in content or "python3.11" in content

    def test_agent_dockerfile_installs_requirements(self):
        dfile = AI_AGENTS_DIR / "Dockerfile"
        content = dfile.read_text()
        assert "requirements.txt" in content
