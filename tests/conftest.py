"""
Shared pytest fixtures for the network automation stack test suite.
"""

import os
import pytest
import requests
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NAUTOBOT_URL = os.getenv("NAUTOBOT_URL", "http://localhost:8080")
NAUTOBOT_TOKEN = os.getenv("NAUTOBOT_TOKEN", "testtoken")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
OPS_AGENT_URL = os.getenv("OPS_AGENT_URL", "http://localhost:8000")
ENG_AGENT_URL = os.getenv("ENG_AGENT_URL", "http://localhost:8001")
AGENT_UI_URL = os.getenv("AGENT_UI_URL", "http://localhost:7860")


# ---------------------------------------------------------------------------
# HTTP session fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def http_session():
    """Reusable requests session."""
    session = requests.Session()
    session.timeout = 10
    return session


@pytest.fixture(scope="session")
def nautobot_headers():
    return {
        "Authorization": f"Token {NAUTOBOT_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Mock LLM fixture (prevents OpenAI calls in unit tests)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm():
    with patch("ai_agents.shared.llm.get_llm") as mock:
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content="Mocked LLM response")
        mock.return_value = llm
        yield llm


# ---------------------------------------------------------------------------
# Mock Nautobot client fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_nautobot():
    with patch("pynautobot.api") as mock:
        nb = MagicMock()
        mock.return_value = nb
        yield nb
