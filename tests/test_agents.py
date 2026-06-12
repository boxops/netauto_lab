"""
tests/test_agents.py
Unit tests for the unified AI agent: configuration, tools, safety guardrails,
and application entry points. All checks are static — no live services required.

The platform runs ONE unified agent (ai-agents/main.py, shared/unified_agent.py);
the historical three-agent layout (ops/eng/chaos services) is retired.

Run with:
    pytest tests/test_agents.py -v
"""

import pytest
import sys
from pathlib import Path

# Add ai-agents to path so shared modules can be imported
AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Config / settings
# ---------------------------------------------------------------------------

class TestAgentConfig:
    def test_shared_config_module_exists(self):
        assert (AI_AGENTS_DIR / "shared" / "config.py").exists()

    def test_shared_tools_module_exists(self):
        assert (AI_AGENTS_DIR / "shared" / "tools.py").exists()

    def test_shared_llm_module_exists(self):
        assert (AI_AGENTS_DIR / "shared" / "llm.py").exists()

    def test_unified_agent_module_exists(self):
        assert (AI_AGENTS_DIR / "shared" / "unified_agent.py").exists()

    def test_workflow_module_exists(self):
        assert (AI_AGENTS_DIR / "ops_agent" / "workflow.py").exists()

    def test_ui_app_exists(self):
        assert (AI_AGENTS_DIR / "ui" / "main.py").exists()

    def test_requirements_file_exists(self):
        assert (AI_AGENTS_DIR / "requirements.txt").exists()

    def test_requirements_has_langchain(self):
        content = (AI_AGENTS_DIR / "requirements.txt").read_text()
        assert "langchain" in content.lower()

    def test_requirements_has_fastapi(self):
        content = (AI_AGENTS_DIR / "requirements.txt").read_text()
        assert "fastapi" in content.lower()

    def test_requirements_has_pynautobot(self):
        content = (AI_AGENTS_DIR / "requirements.txt").read_text()
        assert "pynautobot" in content.lower()


# ---------------------------------------------------------------------------
# Tools module — static analysis
# ---------------------------------------------------------------------------

class TestToolsDefinition:
    """Ensure tools.py defines the expected functions."""

    def test_tools_file_has_nautobot_tool(self):
        content = (AI_AGENTS_DIR / "shared" / "tools.py").read_text()
        assert "get_device_info" in content or "nautobot" in content.lower()

    def test_tools_file_has_prometheus_tool(self):
        content = (AI_AGENTS_DIR / "shared" / "tools.py").read_text()
        assert "prometheus" in content.lower() or "query_prometheus" in content

    def test_tools_file_has_ops_tools(self):
        content = (AI_AGENTS_DIR / "shared" / "tools.py").read_text()
        assert "OPS_TOOLS" in content

    def test_alertmanager_url_not_derived_from_prometheus(self):
        """Alertmanager must use its own URL, not a port-replace hack on Prometheus URL."""
        content = (AI_AGENTS_DIR / "shared" / "tools.py").read_text()
        assert "prometheus_url.replace" not in content, (
            "Alertmanager URL must not be derived by replacing the Prometheus port — "
            "use settings.alertmanager_url instead"
        )

    def test_alertmanager_url_in_config(self):
        """shared/config.py must declare a dedicated alertmanager_url setting."""
        content = (AI_AGENTS_DIR / "shared" / "config.py").read_text()
        assert "alertmanager_url" in content

    def test_search_nautobot_does_not_use_extras_search(self):
        """search_nautobot must not call /api/extras/search/ (returns 404 in this Nautobot version)."""
        content = (AI_AGENTS_DIR / "shared" / "tools.py").read_text()
        assert "extras/search/" not in content, (
            "extras/search/ returns 404 — search must query dcim/devices/, "
            "ipam/prefixes/, ipam/vlans/, circuits/circuits/ separately"
        )


# ---------------------------------------------------------------------------
# Agent safety rules
# ---------------------------------------------------------------------------

class TestAgentSafety:
    """The agent must enforce check_mode and not execute live actions by default."""

    def test_unified_prompt_mentions_check_mode(self):
        content = (AI_AGENTS_DIR / "shared" / "unified_agent.py").read_text()
        assert "check_mode" in content or "check mode" in content.lower()

    def test_unified_prompt_mentions_approval(self):
        content = (AI_AGENTS_DIR / "shared" / "unified_agent.py").read_text()
        assert "approv" in content.lower() or "confirm" in content.lower()

    def test_workflow_prompt_mentions_check_mode(self):
        content = (AI_AGENTS_DIR / "ops_agent" / "agent.py").read_text()
        assert "check_mode" in content or "check mode" in content.lower()

    def test_action_tools_default_check_mode(self):
        content = (AI_AGENTS_DIR / "shared" / "tools.py").read_text()
        assert "check_mode: bool = True" in content, (
            "Action tools must default check_mode to True — the approval gate "
            "exists precisely to gate check_mode=False execution"
        )

    def test_chaos_tools_gated_by_setting(self):
        """Destructive chaos tools must be excluded unless CHAOS_TOOLS_ENABLED."""
        content = (AI_AGENTS_DIR / "ops_agent" / "chaos_tools.py").read_text()
        assert "chaos_tools_enabled" in content


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------

class TestLLMFactory:
    def test_llm_module_has_get_llm(self):
        content = (AI_AGENTS_DIR / "shared" / "llm.py").read_text()
        assert "get_llm" in content

    def test_llm_module_supports_openai(self):
        content = (AI_AGENTS_DIR / "shared" / "llm.py").read_text()
        assert "openai" in content.lower()

    def test_llm_module_supports_ollama(self):
        content = (AI_AGENTS_DIR / "shared" / "llm.py").read_text()
        assert "ollama" in content.lower()


# ---------------------------------------------------------------------------
# Application entry points
# ---------------------------------------------------------------------------

class TestEntryPoints:
    """ai-agents/main.py is the single live FastAPI app (see Dockerfile CMD)."""

    def test_unified_main_exists(self):
        assert (AI_AGENTS_DIR / "main.py").exists()

    def test_unified_main_has_fastapi(self):
        content = (AI_AGENTS_DIR / "main.py").read_text()
        assert "FastAPI" in content

    def test_unified_main_has_core_endpoints(self):
        content = (AI_AGENTS_DIR / "main.py").read_text()
        for path in ("/health", "/chat", "/webhook/alert", "/workflow/resume"):
            assert path in content, f"main.py missing {path} endpoint"

    def test_chat_endpoint_does_not_block_event_loop(self):
        """The sync ReAct loop must run via run_in_threadpool so /health and
        /webhook/alert stay responsive during long chats."""
        content = (AI_AGENTS_DIR / "main.py").read_text()
        assert "run_in_threadpool" in content

    def test_no_legacy_per_agent_apps(self):
        """The 3-agent era is over: no per-agent FastAPI apps may exist."""
        assert not (AI_AGENTS_DIR / "ops_agent" / "main.py").exists()
        assert not (AI_AGENTS_DIR / "engineering_agent").exists()
        assert not (AI_AGENTS_DIR / "chaos_agent").exists()

    def test_dockerfile_runs_unified_main(self):
        content = (AI_AGENTS_DIR / "Dockerfile").read_text()
        assert "main:app" in content


class TestUIIntegration:
    def test_ui_points_at_unified_agent(self):
        content = (AI_AGENTS_DIR / "ui" / "main.py").read_text()
        assert "OPS_AGENT_URL" in content

    def test_ui_has_login_route(self):
        content = (AI_AGENTS_DIR / "ui" / "main.py").read_text()
        assert "/login" in content and "UI_PASSWORD" in content


# ---------------------------------------------------------------------------
# Dockerfiles
# ---------------------------------------------------------------------------

class TestDockerfiles:
    def test_agent_dockerfile_exists(self):
        assert (AI_AGENTS_DIR / "Dockerfile").exists()

    def test_agent_ui_dockerfile_exists(self):
        assert (AI_AGENTS_DIR / "Dockerfile.ui").exists()

    def test_agent_dockerfile_installs_requirements(self):
        content = (AI_AGENTS_DIR / "Dockerfile").read_text()
        assert "requirements.txt" in content
