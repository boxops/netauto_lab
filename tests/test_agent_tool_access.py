"""
tests/test_agent_tool_access.py

Integration tests verifying each agent's tools can reach the services they
are supposed to access.  Tests call the tool *functions* directly — no LLM
calls, no OpenAI tokens consumed.

Services under test
-------------------
  Ops Agent        → Nautobot, Prometheus, Alertmanager, Loki, Ansible,
                      Alert Event Receiver (docker-internal)
  Engineering      → Nautobot, Prometheus, Ansible
  Chaos Agent      → same as Ops + 4 chaos-specific Ansible tools

Run the full suite (requires running stack):
    pytest tests/test_agent_tool_access.py -v

Run just tool-set coverage (no services needed):
    pytest tests/test_agent_tool_access.py -v -k "Coverage"
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Load .env from the repo root so tests can run without pre-exported vars
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).parent.parent
_env_file = _REPO_ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Service URLs — default to localhost ports exposed by docker compose
# ---------------------------------------------------------------------------
NAUTOBOT_URL = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
# Support both the compose-internal name and the superuser token from .env
NAUTOBOT_TOKEN = (
    os.getenv("NAUTOBOT_TOKEN")
    or os.getenv("NAUTOBOT_SUPERUSER_API_TOKEN", "")
)
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "http://localhost:9093")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")

# A device and prefix that exist in the lab Nautobot instance
KNOWN_DEVICE = "leaf1"
KNOWN_PREFIX = "10.0.0.0/8"

# The ops-agent container that can reach docker-internal services
OPS_AGENT_CONTAINER = "netauto-ai-ops-agent-1"
ENG_AGENT_CONTAINER = "netauto-ai-eng-agent-1"
CHAOS_AGENT_CONTAINER = "netauto-ai-chaos-agent-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_service_urls(monkeypatch):
    """Redirect all tool settings to localhost for the host test runner."""
    from shared import config
    monkeypatch.setattr(config.settings, "nautobot_url", NAUTOBOT_URL)
    monkeypatch.setattr(config.settings, "nautobot_token", NAUTOBOT_TOKEN)
    monkeypatch.setattr(config.settings, "prometheus_url", PROMETHEUS_URL)
    monkeypatch.setattr(config.settings, "alertmanager_url", ALERTMANAGER_URL)
    monkeypatch.setattr(config.settings, "loki_url", LOKI_URL)


def _ok(result_json: str) -> dict:
    """Assert the tool returned valid JSON with no 'error' key."""
    data = json.loads(result_json)
    assert "error" not in data, f"Tool returned an error: {data.get('error')}"
    return data


def _docker_python(container: str, script: str) -> str:
    """Run a Python one-liner inside a container and return stdout."""
    result = subprocess.run(
        ["docker", "exec", container, "python3", "-c", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"docker exec failed on {container}:\n{result.stderr}"
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Nautobot — tools: get_device_info, get_connected_devices,
#                   search_nautobot, get_available_ips
# ---------------------------------------------------------------------------

class TestNautobotToolAccess:
    """All four Nautobot tools can reach the live API."""

    def test_get_device_info_returns_device(self):
        from shared.tools import get_device_info
        result = _ok(get_device_info.func(KNOWN_DEVICE))
        assert result.get("name") == KNOWN_DEVICE

    def test_get_connected_devices_returns_neighbors(self):
        from shared.tools import get_connected_devices
        result = _ok(get_connected_devices.func(KNOWN_DEVICE))
        assert "device" in result
        assert "neighbors" in result

    def test_search_nautobot_finds_devices(self):
        from shared.tools import search_nautobot
        result = _ok(search_nautobot.func("leaf"))
        assert result["results"]["devices"]["count"] > 0, (
            "Expected at least one device matching 'leaf'"
        )

    def test_search_nautobot_returns_all_categories(self):
        from shared.tools import search_nautobot
        result = _ok(search_nautobot.func("leaf"))
        for category in ("devices", "prefixes", "vlans", "circuits"):
            assert category in result["results"], (
                f"Missing category '{category}' in search results"
            )

    def test_get_available_ips_returns_ips(self):
        from shared.tools import get_available_ips
        result = _ok(get_available_ips.func(KNOWN_PREFIX, 2))
        assert isinstance(result, list), "Expected a list of available IPs"
        assert len(result) > 0, "Expected at least one available IP"


# ---------------------------------------------------------------------------
# Prometheus — tool: query_prometheus
# ---------------------------------------------------------------------------

class TestPrometheusToolAccess:
    """query_prometheus can reach the live Prometheus API."""

    def test_query_up_metric_returns_series(self):
        from shared.tools import query_prometheus
        result = _ok(query_prometheus.func("up", 5))
        assert result["status"] == "success"
        assert result["series_count"] > 0, "Expected at least one 'up' series"

    def test_query_returns_results_list(self):
        from shared.tools import query_prometheus
        result = _ok(query_prometheus.func("up", 5))
        assert isinstance(result["results"], list)
        assert "metric" in result["results"][0]


# ---------------------------------------------------------------------------
# Alertmanager — tool: get_active_alerts
# ---------------------------------------------------------------------------

class TestAlertmanagerToolAccess:
    """get_active_alerts reaches the live Alertmanager API."""

    def test_get_active_alerts_reaches_alertmanager(self):
        from shared.tools import get_active_alerts
        result = _ok(get_active_alerts.func())
        assert "active_alerts" in result
        assert "count" in result

    def test_get_active_alerts_returns_structured_alerts(self):
        from shared.tools import get_active_alerts
        result = _ok(get_active_alerts.func())
        for alert in result["active_alerts"]:
            assert "name" in alert
            assert "state" in alert


# ---------------------------------------------------------------------------
# Loki — tool: query_logs
# ---------------------------------------------------------------------------

class TestLokiToolAccess:
    """query_logs reaches the live Loki API."""

    def test_query_logs_returns_structure(self):
        from shared.tools import query_logs
        # Even with no matching logs the tool must return a valid structure
        result = _ok(query_logs.func(KNOWN_DEVICE, "", 5))
        assert "log_count" in result
        assert "logs" in result
        assert isinstance(result["logs"], list)

    def test_query_logs_does_not_raise_on_empty_results(self):
        from shared.tools import query_logs
        # A device that almost certainly has no logs — should return empty, not error
        result = _ok(query_logs.func("nonexistent-device-xyz", "", 1))
        assert result["log_count"] == 0


# ---------------------------------------------------------------------------
# Ansible — tool: run_ansible_playbook
# ---------------------------------------------------------------------------

class TestAnsibleToolAccess:
    """run_ansible_playbook can reach the netauto-ansible-1 container."""

    def test_ansible_health_check_runs_in_check_mode(self):
        from shared.tools import run_ansible_playbook
        result = _ok(run_ansible_playbook.func(
            playbook="health_check",
            devices=[KNOWN_DEVICE],
            check_mode=True,
        ))
        # return_code present means ansible-playbook was actually invoked
        assert "return_code" in result
        assert result["check_mode"] is True

    def test_ansible_returns_stdout(self):
        from shared.tools import run_ansible_playbook
        result = _ok(run_ansible_playbook.func(
            playbook="health_check",
            devices=[KNOWN_DEVICE],
            check_mode=True,
        ))
        assert "stdout" in result

    def test_chaos_shutdown_interface_playbook_runs_in_check_mode(self):
        """Verify the new chaos shutdown playbook is reachable via the tool."""
        from shared.tools import run_ansible_playbook
        result = _ok(run_ansible_playbook.func(
            playbook="chaos_shutdown_interface",
            devices=[KNOWN_DEVICE],
            check_mode=True,
            extra_vars={"target_interface": "Ethernet1", "target_device": KNOWN_DEVICE},
        ))
        assert result["check_mode"] is True
        assert "return_code" in result

    def test_chaos_restore_interface_playbook_runs_in_check_mode(self):
        """Verify the new chaos restore playbook is reachable via the tool."""
        from shared.tools import run_ansible_playbook
        result = _ok(run_ansible_playbook.func(
            playbook="chaos_restore_interface",
            devices=[KNOWN_DEVICE],
            check_mode=True,
            extra_vars={"target_interface": "Ethernet1", "target_device": KNOWN_DEVICE},
        ))
        assert result["check_mode"] is True
        assert "return_code" in result


# ---------------------------------------------------------------------------
# Alert Event Receiver — tool: get_recent_alert_events
# This service has no host port mapping; tested via docker exec into the
# ops-agent container which shares the Docker internal network.
# ---------------------------------------------------------------------------

class TestAlertEventReceiverAccess:
    """get_recent_alert_events reaches the docker-internal alert-event-receiver."""

    def test_get_recent_alert_events_from_agent_container(self):
        """Run the tool from inside the ops-agent container."""
        script = (
            "import sys; sys.path.insert(0, '/app'); "
            "from shared.tools import get_recent_alert_events; "
            "import json; "
            "r = json.loads(get_recent_alert_events.func(5)); "
            "assert 'error' not in r, f'Tool error: {r}'; "
            "assert 'events' in r or 'alert_events' in r or 'count' in r, "
            "f'Unexpected shape: {list(r.keys())}'; "
            "print('OK', r.get('count', len(r.get('events', []))))"
        )
        out = _docker_python(OPS_AGENT_CONTAINER, script)
        assert out.startswith("OK"), f"Unexpected output: {out}"


# ---------------------------------------------------------------------------
# Chaos-specific tools — shutdown/restore interface, BGP flap/verify
# All run in check_mode=True so they never touch real device config.
# ---------------------------------------------------------------------------

class TestChaosToolAccess:
    """Chaos tools can invoke Ansible playbooks (check mode only)."""

    def test_shutdown_interface_check_mode(self):
        from chaos_agent.chaos_tools import shutdown_interface
        result = _ok(shutdown_interface.func(KNOWN_DEVICE, "Ethernet1", check_mode=True))
        assert result["check_mode"] is True
        assert result["chaos_action"] == "shutdown_interface"
        assert "return_code" in result

    def test_restore_interface_check_mode(self):
        from chaos_agent.chaos_tools import restore_interface
        result = _ok(restore_interface.func(KNOWN_DEVICE, "Ethernet1", check_mode=True))
        assert result["check_mode"] is True
        assert result["chaos_action"] == "restore_interface"
        assert "return_code" in result

    def test_flap_bgp_neighbor_check_mode(self):
        from chaos_agent.chaos_tools import flap_bgp_neighbor
        result = _ok(flap_bgp_neighbor.func(
            "spine1", "10.0.0.1", method="soft", check_mode=True
        ))
        assert result["check_mode"] is True
        assert result["chaos_action"] == "flap_bgp_neighbor"

    def test_verify_bgp_state_runs_without_check_mode(self):
        """verify_bgp_state is read-only — check_mode=False is correct."""
        from chaos_agent.chaos_tools import verify_bgp_state
        result = _ok(verify_bgp_state.func("spine1", "10.0.0.1"))
        assert result["chaos_action"] == "verify_bgp_state"
        assert "return_code" in result


# ---------------------------------------------------------------------------
# Agent tool-set coverage — assert each agent has exactly the right tools
# ---------------------------------------------------------------------------

class TestOpsAgentToolCoverage:
    """Ops agent must have access to all its required tools."""

    REQUIRED_TOOLS = {
        "get_device_info",
        "get_connected_devices",
        "query_prometheus",
        "get_active_alerts",
        "get_recent_alert_events",
        "query_logs",
        "run_ansible_playbook",
        "search_nautobot",
    }

    def test_ops_tools_contain_all_required(self):
        from shared.tools import OPS_TOOLS
        names = {t.name for t in OPS_TOOLS}
        missing = self.REQUIRED_TOOLS - names
        assert not missing, f"Ops agent is missing tools: {missing}"

    def test_ops_tools_do_not_contain_chaos_tools(self):
        from shared.tools import OPS_TOOLS
        names = {t.name for t in OPS_TOOLS}
        chaos = {"shutdown_interface", "restore_interface", "flap_bgp_neighbor", "verify_bgp_state"}
        assert not (chaos & names), "Ops agent should not have chaos-specific tools"


class TestEngineeringAgentToolCoverage:
    """Engineering agent must have access to its tools and NOT alertmanager/loki."""

    REQUIRED_TOOLS = {
        "get_device_info",
        "get_connected_devices",
        "search_nautobot",
        "get_available_ips",
        "query_prometheus",
        "run_ansible_playbook",
    }

    EXCLUDED_TOOLS = {
        "get_active_alerts",
        "get_recent_alert_events",
        "query_logs",
    }

    def test_eng_tools_contain_all_required(self):
        from shared.tools import ENG_TOOLS
        names = {t.name for t in ENG_TOOLS}
        missing = self.REQUIRED_TOOLS - names
        assert not missing, f"Engineering agent is missing tools: {missing}"

    def test_eng_tools_exclude_alertmanager_and_loki(self):
        from shared.tools import ENG_TOOLS
        names = {t.name for t in ENG_TOOLS}
        present = self.EXCLUDED_TOOLS & names
        assert not present, (
            f"Engineering agent should NOT have these tools (no alertmanager/loki access): {present}"
        )


class TestChaosAgentToolCoverage:
    """Chaos agent must have all Ops tools plus the 4 dedicated chaos tools."""

    REQUIRED_OPS_TOOLS = {
        "get_device_info",
        "get_connected_devices",
        "query_prometheus",
        "get_active_alerts",
        "get_recent_alert_events",
        "query_logs",
        "run_ansible_playbook",
        "search_nautobot",
    }

    REQUIRED_CHAOS_TOOLS = {
        "shutdown_interface",
        "restore_interface",
        "flap_bgp_neighbor",
        "verify_bgp_state",
    }

    def test_chaos_agent_has_all_ops_tools(self):
        from shared.tools import OPS_TOOLS
        from chaos_agent.chaos_tools import CHAOS_TOOLS
        all_tools = {t.name for t in OPS_TOOLS + CHAOS_TOOLS}
        missing = self.REQUIRED_OPS_TOOLS - all_tools
        assert not missing, f"Chaos agent is missing ops tools: {missing}"

    def test_chaos_agent_has_all_chaos_tools(self):
        from chaos_agent.chaos_tools import CHAOS_TOOLS
        names = {t.name for t in CHAOS_TOOLS}
        missing = self.REQUIRED_CHAOS_TOOLS - names
        assert not missing, f"Chaos agent is missing chaos tools: {missing}"

    def test_chaos_tools_all_default_to_check_mode(self):
        """All disruptive chaos tools must have check_mode=True as default."""
        from chaos_agent import chaos_tools
        for tool_name in ("shutdown_interface", "restore_interface", "flap_bgp_neighbor"):
            fn = getattr(chaos_tools, tool_name).func
            defaults = fn.__defaults__ or ()
            # check_mode is the last positional arg; True must be in defaults
            assert True in defaults, (
                f"{tool_name} must default check_mode to True"
            )
