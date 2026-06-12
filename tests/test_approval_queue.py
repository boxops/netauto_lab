"""
Unit tests for the Approvals queue view (grouped pending-approval cards).

No running services required — the UI module's store singletons are patched.
"""
from __future__ import annotations

import json
import sys
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))

pytestmark = pytest.mark.unit


def _gate_task(task_id, alertname, device, risk, created_at, commands="interface Ethernet1\n no shutdown"):
    return {
        "id": task_id,
        "title": f"APPROVAL REQUIRED: config_change on {device} [risk={risk}]",
        "status": "awaiting_approval",
        "priority": "high",
        "alert_fingerprint": f"fp-{task_id}",
        "created_at": created_at,
        "content": json.dumps({
            "alertname": alertname,
            "device": device,
            "commands": commands,
            "risk_confirmed": risk,
            "validation_verdict": "approve",
            "autonomy_level": "L2",
            "reason": f"Pipeline complete — human approval required to execute fix on {device}.",
            "fix_proposal": {"fix_type": "config_change", "risk": risk, "confidence": "high"},
        }),
    }


TASKS = [
    _gate_task("t1", "InterfaceDown",  "leaf1",  "low",  "2026-06-12 10:00:00 UTC"),
    _gate_task("t2", "BGPPeerDown",    "spine1", "high", "2026-06-12 10:05:00 UTC"),
    _gate_task("t3", "InterfaceDown",  "leaf2",  "low",  "2026-06-12 09:55:00 UTC"),
]


@pytest.fixture
def ui_main():
    with (
        patch("shared.activity_store.ActivityStore", MagicMock()),
        patch("shared.task_store.TaskStore", MagicMock()),
        patch("shared.kb_store.KBStore", MagicMock()),
    ):
        import ui.main as m
        yield m


@pytest.fixture
def mock_store(ui_main):
    store = MagicMock()
    store.list_tasks.return_value = list(TASKS)
    with patch.object(ui_main, "task_store", store):
        yield store


@pytest.fixture
def client(ui_main, mock_store):
    with TestClient(ui_main.app, raise_server_exceptions=True) as c:
        yield c


class TestApprovalQueueContext:
    def test_groups_by_alertname(self, ui_main, mock_store):
        ctx = ui_main._approval_queue_context()
        assert ctx["total"] == 3
        names = [name for name, _ in ctx["groups"]]
        assert set(names) == {"InterfaceDown", "BGPPeerDown"}

    def test_highest_risk_group_first(self, ui_main, mock_store):
        ctx = ui_main._approval_queue_context()
        assert ctx["groups"][0][0] == "BGPPeerDown"  # high risk outranks low

    def test_cards_within_group_oldest_first(self, ui_main, mock_store):
        ctx = ui_main._approval_queue_context()
        ifdown = dict(ctx["groups"])["InterfaceDown"]
        assert [c["task_id"] for c in ifdown] == ["t3", "t1"]  # 09:55 before 10:00

    def test_card_fields_extracted(self, ui_main, mock_store):
        ctx = ui_main._approval_queue_context()
        card = dict(ctx["groups"])["BGPPeerDown"][0]
        assert card["device"] == "spine1"
        assert card["risk"] == "high"
        assert card["confidence"] == "high"
        assert card["fix_type"] == "config_change"
        assert card["autonomy_level"] == "L2"

    def test_malformed_content_does_not_crash(self, ui_main, mock_store):
        broken = dict(TASKS[0], id="bad", content="{not json")
        mock_store.list_tasks.return_value = [broken]
        ctx = ui_main._approval_queue_context()
        assert ctx["total"] == 1
        assert ctx["groups"][0][0] == "Other"


class TestApprovalQueueRoutes:
    def test_approvals_page_returns_200(self, client):
        resp = client.get("/approvals")
        assert resp.status_code == 200
        assert "Approval queue" in resp.text

    def test_queue_partial_renders_groups_and_cards(self, client):
        resp = client.get("/partials/approval-queue")
        assert resp.status_code == 200
        assert "BGPPeerDown" in resp.text
        assert "spine1" in resp.text
        assert "Approve all (2)" in resp.text  # InterfaceDown group bulk button

    def test_queue_partial_empty_state(self, client, mock_store):
        mock_store.list_tasks.return_value = []
        resp = client.get("/partials/approval-queue")
        assert resp.status_code == 200
        assert "Nothing waiting" in resp.text

    def test_bulk_approve_approves_every_task_in_group(self, client, mock_store):
        mock_store.get_task.side_effect = lambda tid: next(
            (t for t in TASKS if t["id"] == tid), None
        )
        resp = client.post("/approvals/group/approve", data={"alertname": "InterfaceDown"})
        assert resp.status_code == 200
        approved_ids = [c.args[0] for c in mock_store.approve_task.call_args_list]
        assert set(approved_ids) == {"t1", "t3"}
        assert "Approved 2 task(s)" in resp.text

    def test_bulk_approve_skips_non_awaiting_tasks(self, client, mock_store):
        moved_on = dict(TASKS[2], status="complete")
        mock_store.get_task.side_effect = lambda tid: {
            "t1": TASKS[0], "t3": moved_on,
        }.get(tid)
        resp = client.post("/approvals/group/approve", data={"alertname": "InterfaceDown"})
        assert resp.status_code == 200
        approved_ids = [c.args[0] for c in mock_store.approve_task.call_args_list]
        assert approved_ids == ["t1"]
        assert "Approved 1 task(s)" in resp.text
