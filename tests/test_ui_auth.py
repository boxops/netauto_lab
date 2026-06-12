"""
Unit tests for the web UI session authentication (UI_PASSWORD).

All tests are pure unit tests — no running services or Docker required.
Auth is toggled by monkeypatching ui.main.UI_PASSWORD after import, since the
module reads the env var at import time.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))

PASSWORD = "test-ui-password"


@pytest.fixture(scope="module")
def ui_main():
    with (
        patch("shared.activity_store.ActivityStore", MagicMock()),
        patch("shared.task_store.TaskStore", MagicMock()),
        patch("shared.kb_store.KBStore", MagicMock()),
    ):
        import ui.main as ui_main
        yield ui_main


@pytest.fixture
def client(ui_main):
    with TestClient(ui_main.app, raise_server_exceptions=False, follow_redirects=False) as c:
        yield c


@pytest.fixture
def auth_enabled(ui_main):
    original = ui_main.UI_PASSWORD
    ui_main.UI_PASSWORD = PASSWORD
    ui_main.templates.env.globals["ui_auth_enabled"] = True
    try:
        yield
    finally:
        ui_main.UI_PASSWORD = original
        ui_main.templates.env.globals["ui_auth_enabled"] = bool(original)


# ── Auth disabled (default dev/lab mode) ──────────────────────────────────────

@pytest.mark.unit
class TestAuthDisabled:
    def test_index_accessible_without_login(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_login_page_redirects_home_when_auth_disabled(self, client):
        resp = client.get("/login")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"


# ── Auth enabled ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestAuthEnabled:
    def test_page_redirects_to_login(self, auth_enabled, client):
        resp = client.get("/")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_htmx_partial_gets_hx_redirect(self, auth_enabled, client):
        resp = client.get("/partials/status-bar", headers={"HX-Request": "true"})
        assert resp.status_code == 401
        assert resp.headers["HX-Redirect"] == "/login"

    def test_static_assets_stay_public(self, auth_enabled, client):
        resp = client.get("/static/htmx.min.js")
        assert resp.status_code == 200

    def test_login_page_renders(self, auth_enabled, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "password" in resp.text.lower()

    def test_wrong_password_rejected(self, auth_enabled, client):
        resp = client.post("/login", data={"password": "nope"})
        assert resp.status_code == 401
        assert "clano_session" not in resp.cookies

    def test_correct_password_sets_session_and_grants_access(self, auth_enabled, client):
        resp = client.post("/login", data={"password": PASSWORD})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert "clano_session" in resp.cookies

        client.cookies.update(resp.cookies)
        resp2 = client.get("/")
        assert resp2.status_code == 200

    def test_forged_session_cookie_rejected(self, auth_enabled, client):
        client.cookies.set("clano_session", "forged-token")
        resp = client.get("/")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_logout_clears_session(self, auth_enabled, client):
        login = client.post("/login", data={"password": PASSWORD})
        client.cookies.update(login.cookies)
        resp = client.post("/logout")
        assert resp.status_code == 303
        # Cookie deletion is signalled via an expired Set-Cookie header
        assert 'clano_session="";' in resp.headers.get("set-cookie", "")

    def test_approval_action_requires_session(self, auth_enabled, client):
        resp = client.post("/tasks/some-task-id/approve")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
