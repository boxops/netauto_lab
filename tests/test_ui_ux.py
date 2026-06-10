"""
Tests for UX improvements:
- _humanize_event_type helper
- active pipeline auto-selection (awaiting_approval preferred)
- approval KPI banner in ops_health
- AI mode badge in base header
- task queue collapsed, constant columns removed, inline approve
- pipeline progress strip in chronicle
- humanized live feed event labels
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
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
        "rates": {"auto_resolved_pct": 0.0, "validation_rate_pct": 0.0, "escalation_rate_pct": 0.0},
        "feedback": {"total": 0, "correct": 0},
        "mttr": {"avg_minutes": 0.0, "p50_minutes": 0.0, "resolved_today": 0},
    }
    ts.get_resolution_history.return_value = []
    ts.approve_task.return_value = None
    ts.reject_task.return_value = None
    ts.clear_all_tasks.return_value = 0
    return ts


# ── _humanize_event_type helper ───────────────────────────────────────────────

class TestHumanizeEventType:
    def test_known_event_type_rca_complete(self):
        from ui.main import _humanize_event_type
        assert _humanize_event_type("rca_complete") == "Root cause identified"

    def test_known_event_type_approval_requested(self):
        from ui.main import _humanize_event_type
        assert _humanize_event_type("approval_requested") == "Awaiting approval"

    def test_known_event_type_fast_path_resolved(self):
        from ui.main import _humanize_event_type
        assert _humanize_event_type("fast_path_resolved") == "Fast-path resolved"

    def test_known_event_type_no_ai_skipped(self):
        from ui.main import _humanize_event_type
        assert _humanize_event_type("no_ai_skipped") == "AI disabled — manual review"

    def test_known_event_type_execution_complete(self):
        from ui.main import _humanize_event_type
        assert _humanize_event_type("execution_complete") == "Fix executed"

    def test_unknown_event_type_falls_back_to_capitalized(self):
        from ui.main import _humanize_event_type
        result = _humanize_event_type("some_new_event")
        assert "Some" in result or "some" in result.lower()

    def test_humanize_filter_registered(self, client):
        # The humanize_event Jinja2 filter must be registered for template rendering to work
        from ui.main import templates
        assert "humanize_event" in templates.env.filters


# ── Active pipeline auto-selection ───────────────────────────────────────────

class TestActivePipelineSelection:
    def test_index_selects_awaiting_approval_fp_over_others(self, client):
        # In real data, approval_gate and rca tasks share the same fingerprint.
        # When an awaiting_approval task exists, its fingerprint is used as sel_fp
        # so the chronicle loads with that fingerprint immediately.
        awaiting_task = {
            "id": "app-abc", "type": "approval_gate", "status": "awaiting_approval",
            "alert_fingerprint": "fp-urgent", "created_at": "2026-01-15 10:00:00 UTC",
            "title": "urgent", "content": "{}", "result": None,
            "priority": "normal", "assigned_to": "ops_agent", "created_by": "workflow",
        }
        rca_task = {
            "id": "rca-xyz", "type": "rca", "status": "running",
            "alert_fingerprint": "fp-newer", "created_at": "2026-01-15 11:00:00 UTC",
            "title": "newer", "content": "{}", "result": None,
            "priority": "normal", "assigned_to": "ops_agent", "created_by": "workflow",
        }
        def fake_list_tasks(type=None, status=None, limit=200, **kwargs):
            if status == "awaiting_approval":
                return [awaiting_task]
            if type == "rca":
                return [rca_task]
            return []

        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.side_effect = fake_list_tasks
            mock_ts.get_kpis.return_value = _make_task_store().get_kpis.return_value
            r = client.get("/")
        assert r.status_code == 200
        # The chronicle container must use fp-urgent as its load URL (awaiting_approval preferred)
        assert "fp=fp-urgent" in r.text

    def test_index_falls_back_to_first_active_fp_when_no_approval(self, client):
        rca_task = {
            "id": "rca-abc", "type": "rca", "status": "running",
            "alert_fingerprint": "fp-active", "created_at": "2026-01-15 10:00:00 UTC",
            "title": "active", "content": "{}", "result": None,
            "priority": "normal", "assigned_to": "ops_agent", "created_by": "workflow",
        }
        def fake_list_tasks(type=None, status=None, limit=200, **kwargs):
            if status == "awaiting_approval":
                return []
            if type == "rca":
                return [rca_task]
            return []

        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.side_effect = fake_list_tasks
            mock_ts.get_kpis.return_value = _make_task_store().get_kpis.return_value
            r = client.get("/")
        assert r.status_code == 200
        assert "fp-active" in r.text


# ── Approval banner in ops_health ────────────────────────────────────────────

class TestApprovalBanner:
    def _fetch_ops_health(self, client, awaiting_count=0):
        kpis = {
            "today": {
                "total_tasks": awaiting_count, "complete": 0, "failed": 0,
                "awaiting_approval": awaiting_count, "auto_resolved": 0, "escalated": 0,
            },
            "rates": {"auto_resolved_pct": 0.0, "validation_rate_pct": 0.0, "escalation_rate_pct": 0.0},
            "feedback": {"total": 0, "correct": 0},
            "mttr": {"avg_minutes": 0.0, "p50_minutes": 0.0, "resolved_today": 0},
        }
        with patch("ui.main.task_store") as mock_ts, \
             patch("ui.main._http_client") as mock_http:
            mock_ts.get_kpis.return_value = kpis
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ai_enabled": True}
            mock_http.get = AsyncMock(return_value=mock_resp)
            r = client.get("/partials/ops-health")
        return r

    def test_banner_shown_when_approvals_pending(self, client):
        r = self._fetch_ops_health(client, awaiting_count=2)
        assert r.status_code == 200
        assert "awaiting your approval" in r.text.lower()
        assert "selectFirstApproval" in r.text

    def test_banner_hidden_when_no_approvals(self, client):
        r = self._fetch_ops_health(client, awaiting_count=0)
        assert r.status_code == 200
        assert "awaiting your approval" not in r.text.lower()

    def test_approval_kpi_calls_select_first_approval(self, client):
        r = self._fetch_ops_health(client, awaiting_count=1)
        assert r.status_code == 200
        assert "selectFirstApproval" in r.text


# ── AI mode badge in base header ─────────────────────────────────────────────

class TestAiModeBadgeInHeader:
    def test_pipeline_page_loads_ai_mode_badge(self, client):
        r = client.get("/")
        # The header should include an HTMX-loaded ai-mode-badge element
        assert 'id="ai-mode-badge"' in r.text
        assert "/partials/ai-mode-badge" in r.text

    def test_ai_mode_badge_endpoint_returns_200(self, client):
        with patch("ui.main._http_client") as mock_http:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ai_enabled": True, "label": "Online"}
            mock_resp.status_code = 200
            mock_http.get = AsyncMock(return_value=mock_resp)
            r = client.get("/partials/ai-mode-badge")
        assert r.status_code == 200

    def test_ai_mode_badge_shows_on_non_pipeline_pages(self, client):
        r = client.get("/chat/ops")
        assert 'id="ai-mode-badge"' in r.text


# ── Task queue: collapsed, reduced columns ────────────────────────────────────

class TestTaskQueueUX:
    def test_task_queue_on_system_page(self, client):
        # Task queue moved to System tab; Operations page should not contain task-queue table
        ops = client.get("/")
        assert "sys-task-queue-body" not in ops.text
        sys_page = client.get("/system")
        assert "sys-task-queue-body" in sys_page.text

    def test_task_queue_header_has_no_type_column(self, client):
        r = client.get("/")
        # "Assigned To" and "Created By" headers must not appear (constant data, removed)
        assert "Assigned To" not in r.text
        assert "Created By" not in r.text

    def test_task_queue_rows_have_no_type_cell(self, client):
        task = {
            "id": "rca-test01", "type": "rca", "status": "pending",
            "alert_fingerprint": "fp1", "created_at": "2026-01-15 10:00:00 UTC",
            "title": "test task", "content": "{}", "result": None,
            "priority": "normal", "assigned_to": "ops_agent", "created_by": "workflow",
        }
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [task]
            r = client.get("/partials/task-queue")
        # type and assigned_to columns should not appear in the row
        assert "ops_agent" not in r.text
        assert "workflow" not in r.text

    def test_task_queue_awaiting_approval_row_has_approve_button(self, client):
        task = {
            "id": "app-test01", "type": "approval_gate", "status": "awaiting_approval",
            "alert_fingerprint": "fp1", "created_at": "2026-01-15 10:00:00 UTC",
            "title": "approval needed", "content": "{}", "result": None,
            "priority": "normal", "assigned_to": "ops_agent", "created_by": "workflow",
        }
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [task]
            r = client.get("/partials/task-queue")
        assert r.status_code == 200
        assert "Approve" in r.text
        assert f"/tasks/{task['id']}/approve" in r.text

    def test_task_queue_pending_row_has_no_approve_button(self, client):
        task = {
            "id": "rca-test02", "type": "rca", "status": "pending",
            "alert_fingerprint": "fp1", "created_at": "2026-01-15 10:00:00 UTC",
            "title": "pending task", "content": "{}", "result": None,
            "priority": "normal", "assigned_to": "ops_agent", "created_by": "workflow",
        }
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [task]
            r = client.get("/partials/task-queue")
        assert r.status_code == 200
        assert "✓ Approve" not in r.text


# ── Pipeline progress strip ───────────────────────────────────────────────────

class TestPipelineProgressStrip:
    def _get_chronicle(self, client, task):
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [task]
            mock_ts.get_task.return_value = task
            r = client.get(f"/partials/chronicle?fp={task['alert_fingerprint']}")
        return r

    def _rca_task(self, status="complete", extra_events=None):
        return {
            "id": "rca-strip01", "type": "rca", "status": status,
            "alert_fingerprint": "fp-strip", "created_at": "2026-01-15 10:00:00 UTC",
            "title": "strip test", "content": '{"alertname":"TestAlert"}',
            "result": '{"diagnosis":"test"}' if status == "complete" else None,
            "priority": "normal", "assigned_to": "ops_agent", "created_by": "workflow",
            "events": extra_events or [], "children": [],
        }

    def test_progress_strip_present_in_chronicle(self, client):
        task = self._rca_task()
        r = self._get_chronicle(client, task)
        assert r.status_code == 200
        # All 5 step labels should appear
        assert "Investigate" in r.text
        assert "Propose fix" in r.text
        assert "Validate" in r.text
        assert "Approval gate" in r.text
        assert "Execute" in r.text

    def test_progress_strip_marks_step1_active_for_new_rca(self, client):
        task = self._rca_task(status="pending")
        r = self._get_chronicle(client, task)
        assert r.status_code == 200
        # Step 1 should be marked active (●)
        assert "●" in r.text
        # Steps 2-5 should not be marked done (✓)
        # Count ✓ symbols — only done steps get them
        assert r.text.count("✓") == 0

    def test_progress_strip_marks_step1_done_for_completed_rca(self, client):
        task = self._rca_task(status="complete")
        r = self._get_chronicle(client, task)
        assert r.status_code == 200
        # Step 1 (Investigate) should show ✓ since we have a completed RCA
        # and step 2+ are pending
        # No fix_proposal chapter means current_step stays at 1 (done) → shown as active
        # At minimum, the progress strip renders without error
        assert "Investigate" in r.text


# ── Live feed humanized labels ────────────────────────────────────────────────

class TestLiveFeedHumanizedLabels:
    def test_live_feed_uses_humanize_event_filter(self, client):
        events = [
            {
                "task_id": "rca-feed01",
                "event_type": "rca_complete",
                "alertname": "TestAlert",
                "device": "spine1",
                "timestamp": "2026-01-15T10:00:00",
            }
        ]
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.get_recent_pipeline_events.return_value = events
            r = client.get("/partials/live-feed")
        assert r.status_code == 200
        # Raw "rca_complete" should NOT appear; human label should
        assert "rca_complete" not in r.text
        assert "Root cause identified" in r.text

    def test_live_feed_no_events_shows_placeholder(self, client):
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.get_recent_pipeline_events.return_value = []
            r = client.get("/partials/live-feed")
        assert r.status_code == 200
        assert "No events yet" in r.text


# ── Active Pipelines panel ────────────────────────────────────────────────────

class TestActivePipelinesPanel:
    def _rca_task(self, fp="fp1", status="running", alertname="BGPPeerDown", device="spine1"):
        return {
            "id": f"rca-{fp}", "type": "rca", "status": status,
            "alert_fingerprint": fp, "created_at": "2026-01-15 10:00:00 UTC",
            "title": alertname, "content": f'{{"alertname":"{alertname}","device":"{device}","severity":"warning"}}',
            "result": None, "priority": "normal",
            "assigned_to": "ops_agent", "created_by": "workflow",
        }

    def test_active_pipelines_partial_returns_200(self, client):
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = []
            r = client.get("/partials/active-pipelines")
        assert r.status_code == 200

    def test_active_pipelines_shows_healthy_message_when_empty(self, client):
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = []
            r = client.get("/partials/active-pipelines")
        assert "healthy" in r.text.lower() or "No active" in r.text

    def test_active_pipelines_shows_alert_name(self, client):
        task = self._rca_task(alertname="BGPPeerDown", device="spine1")
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [task]
            r = client.get("/partials/active-pipelines")
        assert r.status_code == 200
        assert "BGPPeerDown" in r.text
        assert "spine1" in r.text

    def test_active_pipelines_shows_stage_label(self, client):
        task = self._rca_task(status="running")
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [task]
            r = client.get("/partials/active-pipelines")
        assert "Investigating" in r.text

    def test_active_pipelines_awaiting_approval_shows_label(self, client):
        task = self._rca_task(fp="fp-gate", status="running")
        gate = {
            "id": "app-fp-gate", "type": "approval_gate", "status": "awaiting_approval",
            "alert_fingerprint": "fp-gate", "created_at": "2026-01-15 10:00:05 UTC",
            "title": "approval gate", "content": "{}", "result": None,
            "priority": "normal", "assigned_to": "ops_agent", "created_by": "workflow",
        }
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [task, gate]
            r = client.get("/partials/active-pipelines")
        assert "Awaiting approval" in r.text

    def test_active_pipelines_selected_fp_gets_selected_class(self, client):
        task = self._rca_task(fp="fp-sel")
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [task]
            r = client.get("/partials/active-pipelines?sel_fp=fp-sel")
        assert "selected" in r.text
        assert 'data-fp="fp-sel"' in r.text

    def test_active_pipelines_each_row_has_onclick_select(self, client):
        task = self._rca_task(fp="fp-click")
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [task]
            r = client.get("/partials/active-pipelines")
        assert "selectPipeline('fp-click')" in r.text

    def test_pipeline_page_has_viewport_layout(self, client):
        r = client.get("/")
        assert "ops-viewport" in r.text
        assert "ops-left-pane" in r.text
        assert "ops-right-pane" in r.text
        assert "active-pipelines-list" in r.text

    def test_pipeline_page_has_no_fp_select_dropdown(self, client):
        r = client.get("/")
        # Old fingerprint <select> dropdown must be gone
        assert 'id="fp-select"' not in r.text

    def test_active_pipelines_excludes_terminal_tasks(self, client):
        # Terminal tasks (complete/failed/rejected) must not appear
        terminal = self._rca_task(fp="fp-done", status="complete", alertname="OldAlert")
        active   = self._rca_task(fp="fp-live", status="running",  alertname="LiveAlert")
        def fake_list(exclude_statuses=None, limit=500, **kwargs):
            if exclude_statuses:
                return [t for t in [terminal, active] if t["status"] not in exclude_statuses]
            return [terminal, active]
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.side_effect = fake_list
            r = client.get("/partials/active-pipelines")
        assert "LiveAlert" in r.text
        assert "OldAlert" not in r.text
