"""
Unit tests for the FastAPI + Jinja2 + HTMX UI (ai-agents/ui/main.py).

All tests are pure unit tests — no running services or Docker required.
External dependencies (ActivityStore, TaskStore, httpx) are mocked.
"""
from __future__ import annotations

import json
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Make shared/ importable when running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))

# ── Fixtures / mocks ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    """TestClient wrapping the UI FastAPI app with all stores mocked."""
    with (
        patch("shared.activity_store.ActivityStore") as mock_as_cls,
        patch("shared.task_store.TaskStore") as mock_ts_cls,
    ):
        mock_store = _make_activity_store()
        mock_task_store = _make_task_store()
        mock_as_cls.return_value = mock_store
        mock_ts_cls.return_value = mock_task_store

        from ui.main import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


def _make_activity_store():
    s = MagicMock()
    s.get_recent.return_value = []
    s.summary.return_value = {"total": 0, "success": 0, "failed": 0, "by_agent": {}}
    s.get_tool_calls.return_value = []
    s.record.return_value = None
    s.record_tool_calls.return_value = None
    return s


def _make_task_store():
    ts = MagicMock()
    ts.list_tasks.return_value = []
    ts.get_task.return_value = None
    ts.get_task_chain.return_value = []
    ts.get_kpis.return_value = {
        "today": {
            "total_tasks": 0, "complete": 0, "failed": 0,
            "awaiting_approval": 0, "auto_resolved": 0, "escalated": 0,
        },
        "rates": {
            "auto_resolved_pct": 0.0,
            "validation_rate_pct": 0.0,
            "escalation_rate_pct": 0.0,
        },
        "feedback": {"total": 0, "correct": 0},
    }
    ts.approve_task.return_value = None
    ts.reject_task.return_value = None
    ts.clear_all_tasks.return_value = 0
    return ts


# ── Page route tests ──────────────────────────────────────────────────────────

class TestPageRoutes:
    def test_pipeline_returns_200(self, client):
        r = client.get("/")
        assert r.status_code == 200

    def test_pipeline_is_html(self, client):
        r = client.get("/")
        assert "text/html" in r.headers["content-type"]

    def test_pipeline_contains_nav_tabs(self, client):
        r = client.get("/")
        assert "Pipeline" in r.text
        assert "Ops Agent" in r.text
        assert "Engineering Agent" in r.text
        assert "Chaos Agent" in r.text
        assert "Activity" in r.text

    def test_ops_chat_page_returns_200(self, client):
        r = client.get("/chat/ops")
        assert r.status_code == 200

    def test_engineering_chat_page_returns_200(self, client):
        r = client.get("/chat/engineering")
        assert r.status_code == 200

    def test_chaos_chat_page_returns_200(self, client):
        r = client.get("/chat/chaos")
        assert r.status_code == 200

    def test_chaos_chat_contains_schedule_section(self, client):
        r = client.get("/chat/chaos")
        assert "Schedule Chaos Run" in r.text

    def test_ops_chat_does_not_contain_schedule_section(self, client):
        r = client.get("/chat/ops")
        assert "Schedule Chaos Run" not in r.text

    def test_unknown_agent_returns_404(self, client):
        r = client.get("/chat/unknown")
        assert r.status_code == 404

    def test_activity_page_returns_200(self, client):
        r = client.get("/activity")
        assert r.status_code == 200

    def test_activity_page_is_html(self, client):
        r = client.get("/activity")
        assert "text/html" in r.headers["content-type"]


# ── Partial route tests ───────────────────────────────────────────────────────

class TestPartialRoutes:
    def test_agent_status_partial_returns_200(self, client):
        with patch("ui.main.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get.side_effect = Exception("unreachable")
            r = client.get("/partials/agent-status")
        assert r.status_code == 200

    def test_status_bar_partial_returns_200(self, client):
        with patch("ui.main.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get.side_effect = Exception("unreachable")
            r = client.get("/partials/status-bar")
        assert r.status_code == 200

    def test_fingerprints_partial_returns_200(self, client):
        r = client.get("/partials/fingerprints")
        assert r.status_code == 200

    def test_pipeline_partial_no_fp_returns_200(self, client):
        r = client.get("/partials/pipeline")
        assert r.status_code == 200

    def test_pipeline_partial_with_fp_returns_200(self, client):
        r = client.get("/partials/pipeline?fp=abc123")
        assert r.status_code == 200

    def test_task_queue_partial_returns_200(self, client):
        r = client.get("/partials/task-queue")
        assert r.status_code == 200

    def test_task_queue_with_filters_returns_200(self, client):
        r = client.get("/partials/task-queue?status=pending&type=rca")
        assert r.status_code == 200

    def test_task_detail_not_found_returns_200(self, client):
        r = client.get("/partials/task/nonexistent-id")
        assert r.status_code == 200
        assert "not found" in r.text.lower()

    def test_task_detail_found_returns_task_info(self, client):
        task = {
            "id": "rca-test1234",
            "type": "rca",
            "status": "complete",
            "priority": "normal",
            "created_by": "system",
            "assigned_to": "ops_agent",
            "title": "Test RCA",
            "content": '{"alertname": "TestAlert"}',
            "result": '{"diagnosis": "Test diagnosis"}',
            "created_at": "2026-01-15 10:00:00 UTC",
            "claimed_at": "2026-01-15 10:00:05 UTC",
            "completed_at": "2026-01-15 10:01:30 UTC",
            "events": [],
            "feedback": [],
            "children": [],
        }
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.get_task.return_value = task
            mock_ts.get_task_chain.return_value = [task]
            r = client.get("/partials/task/rca-test1234")
        assert r.status_code == 200
        assert "rca-test1234" in r.text

    def test_cost_kpis_partial_returns_200(self, client):
        with patch("ui.main.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get.side_effect = Exception("unreachable")
            r = client.get("/partials/cost-kpis")
        assert r.status_code == 200

    def test_activity_partial_returns_200(self, client):
        r = client.get("/partials/activity")
        assert r.status_code == 200

    def test_activity_partial_with_agent_filter(self, client):
        r = client.get("/partials/activity?agent=Ops")
        assert r.status_code == 200

    def test_activity_detail_not_found_returns_200(self, client):
        r = client.get("/partials/activity/99999")
        assert r.status_code == 200

    def test_schedules_partial_returns_200(self, client):
        with patch("ui.main.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get.side_effect = Exception("unreachable")
            r = client.get("/partials/schedules")
        assert r.status_code == 200


# ── Task action tests ─────────────────────────────────────────────────────────

class TestTaskActions:
    def test_approve_not_found_returns_200_with_error(self, client):
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.get_task.return_value = None
            r = client.post("/tasks/nonexistent/approve")
        assert r.status_code == 200
        assert "not found" in r.text.lower()

    def test_approve_wrong_status_returns_200_with_warning(self, client):
        task = {"id": "t1", "status": "pending"}
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.get_task.return_value = task
            r = client.post("/tasks/t1/approve")
        assert r.status_code == 200
        assert "awaiting" in r.text.lower()

    def test_approve_awaiting_calls_store_and_returns_ok(self, client):
        task = {"id": "app-abc123", "status": "awaiting_approval"}
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.get_task.return_value = task
            mock_ts.approve_task.return_value = None
            r = client.post("/tasks/app-abc123/approve")
        assert r.status_code == 200
        assert "approved" in r.text.lower()
        mock_ts.approve_task.assert_called_once_with("app-abc123", "human")

    def test_reject_not_found_returns_200_with_error(self, client):
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.get_task.return_value = None
            r = client.post("/tasks/nonexistent/reject")
        assert r.status_code == 200
        assert "not found" in r.text.lower()

    def test_reject_calls_store_and_returns_ok(self, client):
        task = {"id": "t2", "status": "awaiting_approval"}
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.get_task.return_value = task
            mock_ts.reject_task.return_value = None
            r = client.post("/tasks/t2/reject")
        assert r.status_code == 200
        assert "rejected" in r.text.lower()
        mock_ts.reject_task.assert_called_once_with("t2", "human", "Rejected via UI")

    def test_clear_without_confirm_returns_warning(self, client):
        r = client.post("/tasks/clear", data={"confirmed": "no"})
        assert r.status_code == 200
        assert "confirmed" in r.text.lower()

    def test_clear_with_confirm_calls_store(self, client):
        with (
            patch("ui.main.task_store") as mock_ts,
            patch("ui.main.httpx.AsyncClient") as mock_cls,
        ):
            mock_ts.clear_all_tasks.return_value = 5
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.side_effect = Exception("unreachable")
            r = client.post("/tasks/clear", data={"confirmed": "yes"})
        assert r.status_code == 200
        assert "5" in r.text
        mock_ts.clear_all_tasks.assert_called_once()


# ── Chat action tests ─────────────────────────────────────────────────────────

class TestChatAction:
    def _mock_chat_client(self, response_text="Agent reply"):
        mock_cls = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": response_text, "tool_calls": []}
        mock_resp.raise_for_status.return_value = None
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_cls

    def test_ops_chat_post_returns_200(self, client):
        with patch("ui.main.httpx.AsyncClient", self._mock_chat_client()):
            r = client.post("/chat/ops", data={"message": "hello", "session_id": "s1"})
        assert r.status_code == 200

    def test_chat_response_contains_message_and_reply(self, client):
        with patch("ui.main.httpx.AsyncClient", self._mock_chat_client("This is my answer")):
            r = client.post("/chat/ops", data={"message": "test question", "session_id": "s1"})
        assert "test question" in r.text
        assert "This is my answer" in r.text

    def test_chat_unknown_agent_returns_404(self, client):
        r = client.post("/chat/badagent", data={"message": "hi", "session_id": ""})
        assert r.status_code == 404

    def test_chat_agent_offline_returns_warning(self, client):
        with patch("ui.main.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post.side_effect = Exception("connection refused")
            r = client.post("/chat/ops", data={"message": "hello", "session_id": ""})
        assert r.status_code == 200
        assert "⚠️" in r.text

    def test_chat_budget_exceeded_returns_warning(self, client):
        mock_cls = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"detail": "hourly token limit reached"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("ui.main.httpx.AsyncClient", mock_cls):
            r = client.post("/chat/ops", data={"message": "hi", "session_id": ""})
        assert r.status_code == 200
        assert "Budget limit" in r.text


# ── Helper function unit tests ────────────────────────────────────────────────

class TestHelpers:
    def test_truncate_short_string_unchanged(self):
        from ui.main import _truncate
        assert _truncate("short", 20) == "short"

    def test_truncate_long_string_ends_with_ellipsis(self):
        from ui.main import _truncate
        result = _truncate("a" * 200, 50)
        assert result.endswith("...")
        assert len(result) == 50

    def test_truncate_empty_string(self):
        from ui.main import _truncate
        assert _truncate("") == ""

    def test_truncate_none_treated_as_empty(self):
        from ui.main import _truncate
        assert _truncate(None) == ""  # type: ignore

    def test_age_none_returns_dash(self):
        from ui.main import _age
        assert _age(None) == "—"

    def test_age_seconds(self):
        from ui.main import _age
        from datetime import datetime, timezone, timedelta
        ts = datetime.now(timezone.utc) - timedelta(seconds=30)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
        result = _age(ts_str)
        assert result.endswith("s")
        assert "30" in result or "29" in result or "31" in result  # allow ±1s

    def test_age_minutes(self):
        from ui.main import _age
        from datetime import datetime, timezone, timedelta
        ts = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=10)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
        result = _age(ts_str)
        assert "m" in result

    def test_age_hours(self):
        from ui.main import _age
        from datetime import datetime, timezone, timedelta
        ts = datetime.now(timezone.utc) - timedelta(hours=2, minutes=15)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
        result = _age(ts_str)
        assert "h" in result

    def test_age_bad_format_returns_original(self):
        from ui.main import _age
        assert _age("not-a-date") == "not-a-date"


# ── Static file tests ─────────────────────────────────────────────────────────

class TestStaticFiles:
    def test_htmx_js_is_served(self, client):
        r = client.get("/static/htmx.min.js")
        assert r.status_code == 200
        assert "javascript" in r.headers.get("content-type", "")

    def test_style_css_is_served(self, client):
        r = client.get("/static/style.css")
        assert r.status_code == 200
        assert "css" in r.headers.get("content-type", "")
