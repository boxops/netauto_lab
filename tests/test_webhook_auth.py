"""
Unit tests for the inbound Alertmanager webhook gate (ALERT_WEBHOOK_SECRET).

Tests shared.auth.require_webhook_secret via a minimal FastAPI app so the
heavyweight agent entry point (LLM singletons) is never imported.
"""
from __future__ import annotations

import sys
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))

from shared.auth import require_webhook_secret
from shared.config import settings

SECRET = "test-webhook-secret"

app = FastAPI()


@app.post("/webhook/alert")
async def fake_webhook(request: Request):
    await require_webhook_secret(request)
    return {"ok": True}


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def secret_set(monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_secret", SECRET)


@pytest.mark.unit
class TestWebhookSecretDisabled:
    def test_no_secret_accepts_unauthenticated(self, client, monkeypatch):
        monkeypatch.setattr(settings, "alert_webhook_secret", "")
        resp = client.post("/webhook/alert", json={"alerts": []})
        assert resp.status_code == 200


@pytest.mark.unit
class TestWebhookSecretEnabled:
    def test_missing_header_rejected(self, secret_set, client):
        resp = client.post("/webhook/alert", json={"alerts": []})
        assert resp.status_code == 401

    def test_wrong_secret_rejected(self, secret_set, client):
        resp = client.post(
            "/webhook/alert", json={"alerts": []},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_wrong_scheme_rejected(self, secret_set, client):
        resp = client.post(
            "/webhook/alert", json={"alerts": []},
            headers={"Authorization": SECRET},  # raw secret without "Bearer "
        )
        assert resp.status_code == 401

    def test_correct_secret_accepted(self, secret_set, client):
        resp = client.post(
            "/webhook/alert", json={"alerts": []},
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert resp.status_code == 200

    def test_agent_main_wires_the_gate(self):
        """ai-agents/main.py must call the gate inside the webhook handler."""
        main_src = (
            os.path.join(os.path.dirname(__file__), "..", "ai-agents", "main.py")
        )
        content = open(main_src).read()
        assert "require_webhook_secret" in content
