"""
Unit tests for the Operations visibility UI: decision funnel, action stream,
and the inspector's decision-journal banner.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))

pytestmark = pytest.mark.unit


def _journal_row(fp, decision, alertname="InterfaceDown", device="leaf1",
                 reason="", ref_task_id="", count=1):
    return {
        "fingerprint": fp, "decision": decision, "alertname": alertname,
        "device": device, "severity": "critical", "source": "poller",
        "reason": reason, "ref_task_id": ref_task_id, "ref_id": "",
        "received_at": "2026-06-12 10:00:00 UTC", "record_count": count,
    }


@pytest.fixture
def ui(request):
    with (
        patch("shared.activity_store.ActivityStore", MagicMock()),
        patch("shared.task_store.TaskStore", MagicMock()),
        patch("shared.kb_store.KBStore", MagicMock()),
    ):
        import ui.main as ui_main
        mock_journal = MagicMock()
        mock_journal.latest_per_fingerprint.return_value = []
        mock_journal.for_fingerprint.return_value = []
        mock_journal.funnel.return_value = {
            "alerts": 5, "investigated": 2, "fast_path": 1,
            "dropped": 2, "linked": 0, "by_decision": {},
        }
        mock_ts = MagicMock()
        mock_ts.list_tasks.return_value = []
        mock_ts.get_task.return_value = None
        with (
            patch.object(ui_main, "alert_journal", mock_journal),
            patch.object(ui_main, "task_store", mock_ts),
        ):
            yield ui_main


@pytest.fixture
def client(ui):
    with TestClient(ui.app, raise_server_exceptions=True) as c:
        yield c


class TestFunnel:
    def test_funnel_renders_counts(self, ui, client):
        resp = client.get("/partials/ops-funnel")
        assert resp.status_code == 200
        assert "alerts (24h)" in resp.text
        assert "dropped/suppressed" in resp.text

    def test_funnel_segments_filter_the_stream(self, ui, client):
        resp = client.get("/partials/ops-funnel")
        assert 'hx-get="/partials/action-stream?category=dropped"' in resp.text


class TestActionStream:
    def test_empty_state(self, ui, client):
        resp = client.get("/partials/action-stream")
        assert resp.status_code == 200
        assert "No alert activity recorded yet" in resp.text

    def test_dropped_alert_shows_reason_inline(self, ui, client):
        ui.alert_journal.latest_per_fingerprint.return_value = [
            _journal_row("fp-s", "suppressed_by_intent",
                         reason="Standing intent 'leaf1 maintenance' matched."),
        ]
        resp = client.get("/partials/action-stream")
        assert "Suppressed by intent" in resp.text
        assert "leaf1 maintenance" in resp.text

    def test_task_state_overrides_decision_chip(self, ui, client):
        ui.alert_journal.latest_per_fingerprint.return_value = [
            _journal_row("fp-1", "investigating", ref_task_id="t1"),
        ]
        ui.task_store.get_task.return_value = {"id": "t1", "status": "awaiting_approval"}
        resp = client.get("/partials/action-stream")
        assert "Awaiting your approval" in resp.text

    def test_needs_me_filter_excludes_non_gated(self, ui, client):
        ui.alert_journal.latest_per_fingerprint.return_value = [
            _journal_row("fp-1", "investigating", ref_task_id="t1"),
            _journal_row("fp-2", "deduplicated"),
        ]
        ui.task_store.get_task.return_value = {"id": "t1", "status": "running"}
        resp = client.get("/partials/action-stream?category=needs_me")
        assert "Nothing waiting on you" in resp.text

    def test_text_filter_matches_device(self, ui, client):
        ui.alert_journal.latest_per_fingerprint.return_value = [
            _journal_row("fp-1", "investigating", device="leaf1"),
            _journal_row("fp-2", "investigating", device="spine9"),
        ]
        resp = client.get("/partials/action-stream?q=spine9")
        assert "spine9" in resp.text
        assert "leaf1" not in resp.text

    def test_record_count_badge(self, ui, client):
        ui.alert_journal.latest_per_fingerprint.return_value = [
            _journal_row("fp-1", "deduplicated", count=4),
        ]
        resp = client.get("/partials/action-stream")
        assert "×4" in resp.text


class TestInspectorJournalBanner:
    def test_banner_renders_for_suppressed_alert_without_task(self, ui, client):
        """The dead-end case: no pipeline ran — banner must explain why."""
        ui.alert_journal.for_fingerprint.return_value = [
            _journal_row("fp-s", "suppressed_by_intent",
                         reason="Standing intent 'leaf1 maintenance' matched."),
        ]
        ui.task_store.list_tasks.return_value = []
        resp = client.get("/partials/chronicle?fp=fp-s")
        assert resp.status_code == 200
        assert "Decision journal" in resp.text
        assert "leaf1 maintenance" in resp.text
        assert "No pipeline ran for this alert" in resp.text

    def test_no_banner_without_journal_records(self, ui, client):
        resp = client.get("/partials/chronicle?fp=fp-none")
        assert "Decision journal" not in resp.text
        assert "No pipeline stages found" in resp.text

    def test_operations_page_includes_funnel_and_stream(self, ui, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert 'id="ops-funnel"' in resp.text
        assert 'id="action-stream"' in resp.text
        assert "Action Stream" in resp.text
