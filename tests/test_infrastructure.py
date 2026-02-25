"""
tests/test_infrastructure.py
Integration/smoke tests that verify all services are reachable and healthy.

Run with:
    pytest tests/test_infrastructure.py -v

These tests require the full stack to be running (`make start`).
Mark as integration tests to skip in unit-only runs:
    pytest tests/test_infrastructure.py -v -m integration
"""

import pytest
import requests
import os

pytestmark = pytest.mark.integration

NAUTOBOT_URL = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
NAUTOBOT_TOKEN = os.getenv("NAUTOBOT_TOKEN", "")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "http://localhost:9093")
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
OPS_AGENT_URL = os.getenv("OPS_AGENT_URL", "http://localhost:8000")
ENG_AGENT_URL = os.getenv("ENG_AGENT_URL", "http://localhost:8001")
AGENT_UI_URL = os.getenv("AGENT_UI_URL", "http://localhost:7860")

TIMEOUT = 10


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def get(url: str, path: str = "", **kwargs) -> requests.Response:
    return requests.get(f"{url}{path}", timeout=TIMEOUT, **kwargs)


# ---------------------------------------------------------------------------
# Nautobot
# ---------------------------------------------------------------------------

class TestNautobot:
    def test_nautobot_healthy(self):
        """Nautobot health endpoint returns 200."""
        resp = get(NAUTOBOT_URL, "/health/")
        assert resp.status_code == 200, f"Nautobot health check failed: {resp.status_code}"

    def test_nautobot_api_accessible(self):
        """Nautobot REST API is accessible."""
        headers = {"Authorization": f"Token {NAUTOBOT_TOKEN}"} if NAUTOBOT_TOKEN else {}
        resp = get(NAUTOBOT_URL, "/api/", headers=headers)
        assert resp.status_code in (200, 403), f"Unexpected status: {resp.status_code}"

    def test_nautobot_api_with_token(self):
        """Nautobot API returns device list with valid token."""
        if not NAUTOBOT_TOKEN:
            pytest.skip("NAUTOBOT_TOKEN not set")
        resp = get(
            NAUTOBOT_URL,
            "/api/dcim/devices/",
            headers={"Authorization": f"Token {NAUTOBOT_TOKEN}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data

    def test_nautobot_plugins_loaded(self):
        """Nautobot Golden Config plugin API is accessible."""
        if not NAUTOBOT_TOKEN:
            pytest.skip("NAUTOBOT_TOKEN not set")
        resp = get(
            NAUTOBOT_URL,
            "/api/plugins/",
            headers={"Authorization": f"Token {NAUTOBOT_TOKEN}"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

class TestPrometheus:
    def test_prometheus_healthy(self):
        """Prometheus /-/healthy returns 200."""
        resp = get(PROMETHEUS_URL, "/-/healthy")
        assert resp.status_code == 200

    def test_prometheus_ready(self):
        """Prometheus /-/ready returns 200."""
        resp = get(PROMETHEUS_URL, "/-/ready")
        assert resp.status_code == 200

    def test_prometheus_api_query(self):
        """Prometheus API instant query works."""
        resp = get(PROMETHEUS_URL, "/api/v1/query", params={"query": "up"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "result" in data["data"]

    def test_prometheus_targets(self):
        """Prometheus has at least one active target."""
        resp = get(PROMETHEUS_URL, "/api/v1/targets")
        assert resp.status_code == 200
        data = resp.json()
        active = data["data"]["activeTargets"]
        assert len(active) > 0, "No active Prometheus targets found"

    def test_prometheus_rules_loaded(self):
        """Alert rules are loaded."""
        resp = get(PROMETHEUS_URL, "/api/v1/rules")
        assert resp.status_code == 200
        data = resp.json()
        groups = data["data"]["groups"]
        assert len(groups) > 0, "No alert rule groups loaded"


# ---------------------------------------------------------------------------
# Alertmanager
# ---------------------------------------------------------------------------

class TestAlertmanager:
    def test_alertmanager_healthy(self):
        """Alertmanager /-/healthy returns 200."""
        resp = get(ALERTMANAGER_URL, "/-/healthy")
        assert resp.status_code == 200

    def test_alertmanager_api(self):
        """Alertmanager API returns status."""
        resp = get(ALERTMANAGER_URL, "/api/v2/status")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Grafana
# ---------------------------------------------------------------------------

class TestGrafana:
    def test_grafana_healthy(self):
        """Grafana /api/health returns ok."""
        resp = get(GRAFANA_URL, "/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("database") == "ok"

    def test_grafana_dashboards_provisioned(self):
        """All four dashboards are provisioned in Grafana."""
        grafana_user = os.getenv("GRAFANA_ADMIN_USER", "admin")
        grafana_pass = os.getenv("GRAFANA_ADMIN_PASSWORD", "admin")
        resp = get(
            GRAFANA_URL,
            "/api/search",
            auth=(grafana_user, grafana_pass),
            params={"type": "dash-db"},
        )
        assert resp.status_code == 200
        uids = {d["uid"] for d in resp.json()}
        expected = {"network-overview", "device-detail", "interface-analytics", "bgp-monitoring"}
        missing = expected - uids
        assert not missing, f"Missing dashboards: {missing}"


# ---------------------------------------------------------------------------
# Loki
# ---------------------------------------------------------------------------

class TestLoki:
    def test_loki_ready(self):
        """Loki /ready returns 200."""
        resp = get(LOKI_URL, "/ready")
        assert resp.status_code == 200

    def test_loki_labels(self):
        """Loki label query endpoint is accessible."""
        resp = get(LOKI_URL, "/loki/api/v1/labels")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# AI Agents
# ---------------------------------------------------------------------------

class TestAIAgents:
    def test_ops_agent_health(self):
        """Ops Agent /health returns ok."""
        resp = get(OPS_AGENT_URL, "/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"

    def test_engineering_agent_health(self):
        """Engineering Agent /health returns ok."""
        resp = get(ENG_AGENT_URL, "/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"

    def test_agent_ui_accessible(self):
        """Gradio Agent UI is accessible."""
        resp = get(AGENT_UI_URL)
        assert resp.status_code == 200
