"""
Tests for pipeline improvement features:
  - TaskStore.retry_task
  - TaskStore.get_active_rca_for_device
  - TaskStore.count_successful_executions
  - TaskStore.get_resolution_history
  - TaskStore.list_tasks priority_filter
  - TaskStore.get_kpis mttr block
  - UI pending-approvals partial
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


def _make_rca(db, device="spine1", fp="fp1", status="running", priority="normal"):
    task = db.create_task(
        type="rca", created_by="system", assigned_to="ops_agent",
        title=f"RCA {device}", alert_fingerprint=fp,
        priority=priority,
        content={"device": device, "alertname": "BGPPeerDown"},
    )
    if status in ("claimed", "running", "complete", "failed"):
        db.claim_task(task["id"], "ops_agent")
    if status in ("running", "complete", "failed"):
        db.start_task(task["id"], "ops_agent")
    if status == "complete":
        db.complete_task(task["id"], "ops_agent", {"diagnosis": "test"})
    if status == "failed":
        db.fail_task(task["id"], "ops_agent", "timeout")
    return task


def _make_gate_with_execution(db, device="spine1", fix_type="config_change",
                               exec_status="success", alert_resolved=True):
    gate = db.create_task(
        type="approval_gate", created_by="eng_agent", assigned_to="human",
        title="APPROVAL REQUIRED",
        content=json.dumps({
            "device": device,
            "fix_proposal": {"fix_type": fix_type, "device": device},
        }),
    )
    db.request_approval(gate["id"], "eng_agent")
    db.approve_task(gate["id"], "human")
    db.add_event(gate["id"], "eng_agent", "execution_started", {"device": device})
    db.add_event(gate["id"], "eng_agent", "execution_complete",
                 {"status": exec_status, "device": device, "changes_applied": "test fix"})
    if alert_resolved is not None:
        db.add_event(gate["id"], "eng_agent", "execution_verified",
                     {"alert_resolved": alert_resolved, "ttr_seconds": 300})
    return gate


# ── retry_task ────────────────────────────────────────────────────────────────

class TestRetryTask:
    def test_retries_failed_task(self, db):
        task = _make_rca(db, status="failed")
        assert db.retry_task(task["id"], "ops_agent") is True
        refreshed = db.get_task(task["id"])
        assert refreshed["status"] == "pending"
        assert refreshed["retry_count"] == 1

    def test_retry_writes_event(self, db):
        task = _make_rca(db, status="failed")
        db.retry_task(task["id"], "ops_agent")
        events = db.get_task(task["id"])["events"]
        assert any(e["event_type"] == "retry_scheduled" for e in events)

    def test_retry_limit_at_2(self, db):
        task = _make_rca(db, status="failed")
        assert db.retry_task(task["id"], "ops_agent") is True   # retry 1
        db.fail_task(task["id"], "ops_agent", "again")
        assert db.retry_task(task["id"], "ops_agent") is True   # retry 2
        db.fail_task(task["id"], "ops_agent", "again")
        assert db.retry_task(task["id"], "ops_agent") is False  # limit reached

    def test_retry_non_failed_task_returns_false(self, db):
        task = _make_rca(db, status="running")
        assert db.retry_task(task["id"], "ops_agent") is False

    def test_retry_nonexistent_task_returns_false(self, db):
        assert db.retry_task("rca-notexist", "ops_agent") is False


# ── get_active_rca_for_device ─────────────────────────────────────────────────

class TestGetActiveRcaForDevice:
    def test_returns_active_rca(self, db):
        task = _make_rca(db, device="leaf1", status="running")
        result = db.get_active_rca_for_device("leaf1", minutes=15)
        assert result is not None
        assert result["id"] == task["id"]

    def test_returns_none_for_unknown_device(self, db):
        _make_rca(db, device="spine1", status="running")
        assert db.get_active_rca_for_device("leaf99", minutes=15) is None

    def test_does_not_return_complete_tasks(self, db):
        _make_rca(db, device="leaf2", status="complete")
        assert db.get_active_rca_for_device("leaf2", minutes=15) is None

    def test_does_not_return_failed_tasks(self, db):
        _make_rca(db, device="leaf3", status="failed")
        assert db.get_active_rca_for_device("leaf3", minutes=15) is None

    def test_empty_device_returns_none(self, db):
        assert db.get_active_rca_for_device("", minutes=15) is None


# ── list_tasks priority_filter ────────────────────────────────────────────────

class TestListTasksPriorityFilter:
    def test_filters_critical_and_high(self, db):
        _make_rca(db, device="d1", priority="critical")
        _make_rca(db, device="d2", priority="high")
        _make_rca(db, device="d3", priority="normal")
        results = db.list_tasks(type="rca", priority_filter={"critical", "high"})
        priorities = {r["priority"] for r in results}
        assert "critical" in priorities
        assert "high" in priorities
        assert "normal" not in priorities

    def test_no_filter_returns_all(self, db):
        _make_rca(db, device="d1", priority="critical")
        _make_rca(db, device="d2", priority="normal")
        results = db.list_tasks(type="rca")
        assert len(results) == 2


# ── count_successful_executions ───────────────────────────────────────────────

class TestCountSuccessfulExecutions:
    def test_counts_successful_gates(self, db):
        _make_gate_with_execution(db, device="spine1", fix_type="config_change",
                                   exec_status="success")
        _make_gate_with_execution(db, device="spine1", fix_type="config_change",
                                   exec_status="success")
        assert db.count_successful_executions("spine1", "config_change") == 2

    def test_ignores_failed_executions(self, db):
        _make_gate_with_execution(db, device="spine2", exec_status="failed")
        assert db.count_successful_executions("spine2", "config_change") == 0

    def test_ignores_different_device(self, db):
        _make_gate_with_execution(db, device="leaf1", exec_status="success")
        assert db.count_successful_executions("leaf9", "config_change") == 0

    def test_zero_when_no_gates(self, db):
        assert db.count_successful_executions("spine1", "config_change") == 0


# ── get_resolution_history ────────────────────────────────────────────────────

class TestGetResolutionHistory:
    def test_returns_resolution(self, db):
        _make_gate_with_execution(db, device="leaf1")
        results = db.get_resolution_history(alertname="BGPPeerDown", device="leaf1")
        assert len(results) == 1
        assert results[0]["exec_status"] == "success"

    def test_returns_verification_data(self, db):
        _make_gate_with_execution(db, device="leaf2", alert_resolved=True)
        r = db.get_resolution_history("BGPPeerDown", "leaf2")[0]
        assert r["alert_resolved"] is True
        assert r["ttr_seconds"] == 300

    def test_empty_for_unknown_device(self, db):
        assert db.get_resolution_history("BGPPeerDown", "unknown99") == []

    def test_limit_respected(self, db):
        for _ in range(4):
            _make_gate_with_execution(db, device="spine3")
        assert len(db.get_resolution_history("BGPPeerDown", "spine3", limit=2)) == 2


# ── get_kpis mttr ─────────────────────────────────────────────────────────────

class TestGetKpisMttr:
    def test_mttr_zero_when_no_verifications(self, db):
        kpis = db.get_kpis()
        assert kpis["mttr"]["avg_minutes"] == 0.0
        assert kpis["mttr"]["resolved_today"] == 0

    def test_mttr_computed_from_verified_resolutions(self, db):
        # Create a gate with a 5-minute TTR
        _make_gate_with_execution(db, device="spine1", alert_resolved=True)
        kpis = db.get_kpis()
        assert kpis["mttr"]["resolved_today"] == 1
        assert kpis["mttr"]["avg_minutes"] == 5.0   # 300 seconds / 60

    def test_mttr_ignores_unresolved(self, db):
        _make_gate_with_execution(db, device="spine2", alert_resolved=False)
        kpis = db.get_kpis()
        assert kpis["mttr"]["resolved_today"] == 0


# ── UI pending-approvals partial ──────────────────────────────────────────────

class TestPendingApprovalsBadge:
    @pytest.fixture(scope="class")
    def client(self):
        from unittest.mock import patch, MagicMock
        with (
            patch("shared.activity_store.ActivityStore"),
            patch("shared.task_store.TaskStore"),
        ):
            from ui.main import app
            from fastapi.testclient import TestClient
            with TestClient(app) as c:
                yield c

    def test_badge_returns_200(self, client):
        r = client.get("/partials/pending-approvals")
        assert r.status_code == 200

    def test_badge_empty_when_no_approvals(self, client):
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = []
            r = client.get("/partials/pending-approvals")
        # No tasks → no badge HTML
        assert "approval-badge" not in r.text or r.text.strip() == ""

    def test_badge_shows_count_when_approvals_exist(self, client):
        with patch("ui.main.task_store") as mock_ts:
            mock_ts.list_tasks.return_value = [{"id": "app-1"}, {"id": "app-2"}]
            r = client.get("/partials/pending-approvals")
        assert "2" in r.text

    def test_base_template_has_badge_span(self, client):
        r = client.get("/")
        assert "approval-badge" in r.text
