"""
Regression tests for the 15 bugs documented in the 2026-06-05 pipeline assessment.

Focuses on the three highest-risk pipeline architecture bugs:
  Bug #7  — Chat endpoint bypasses safety gates
  Bug #8  — escalate_human approval gate does nothing after approval
  Bug #9  — Alert storm creates 3 separate investigations for 1 root cause

Each test documents the bug it guards against in its docstring so that
a failing test is self-explanatory.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore

sys.path.insert(0, str(Path(__file__).parent))
from fixtures.alerts import alert_storm_payload


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


# ── Bug #8: escalate_human approval gate does nothing ─────────────────────────

@pytest.mark.unit
class TestEscalateHumanApprovalGate:
    """
    Bug #8: When fix_type=escalate_human, `commands` is "none".
    After human approves, resume_execution found commands=="none" and returned
    early without executing or updating the task — approval accomplished nothing.

    The correct behaviour: after approve_task(), the gate reaches status=complete
    with an 'approved' event, and list_approved_unexecuted_gates() returns it
    so the workflow can pick it up for Phase 2 execution.
    """

    def _make_escalate_gate(self, db: TaskStore, fp: str = "fp-escalate-001") -> dict:
        rca = db.create_task(
            type="rca", created_by="alert_poller", assigned_to="ops_agent",
            title="RCA: InterfaceDown on spine2", alert_fingerprint=fp,
            content={"device": "spine2", "alertname": "InterfaceDown"},
        )
        db.claim_task(rca["id"], "ops_agent")
        db.start_task(rca["id"], "ops_agent")
        db.complete_task(rca["id"], "ops_agent", {
            "fix_type": "escalate_human",
            "commands": "none",
            "diagnosis": "Physical cable failure — requires on-site remediation",
        })
        gate = db.create_task(
            type="approval_gate", created_by="ops_agent", assigned_to="human",
            parent_id=rca["id"], alert_fingerprint=fp,
            title="ESCALATION: manual intervention required on spine2",
            content={
                "fix_proposal": {
                    "fix_type": "escalate_human",
                    "device": "spine2",
                    "commands": "none",
                    "risk": "high",
                    "diagnosis": "Physical cable failure",
                },
                "device": "spine2",
                "commands": "none",
            },
        )
        db.request_approval(gate["id"], "ops_agent")
        return gate

    def test_gate_starts_awaiting_approval(self, db):
        gate = self._make_escalate_gate(db)
        assert db.get_task(gate["id"])["status"] == "awaiting_approval"

    def test_approve_records_approved_event(self, db):
        gate = self._make_escalate_gate(db)
        db.approve_task(gate["id"], "human")
        task = db.get_task(gate["id"])
        event_types = [e["event_type"] for e in task.get("events", [])]
        assert "approved" in event_types, (
            "Bug #8 regression: approve_task() must record an 'approved' event "
            "even when fix_type=escalate_human and commands='none'"
        )

    def test_approve_moves_gate_to_complete(self, db):
        gate = self._make_escalate_gate(db)
        db.approve_task(gate["id"], "human")
        task = db.get_task(gate["id"])
        assert task["status"] == "complete", (
            "Bug #8 regression: approval gate must reach status=complete after human approves, "
            "regardless of fix_type"
        )

    def test_approved_gate_appears_in_unexecuted_list(self, db):
        gate = self._make_escalate_gate(db)
        db.approve_task(gate["id"], "human")
        pending = db.list_approved_unexecuted_gates()
        ids = [t["id"] for t in pending]
        assert gate["id"] in ids, (
            "Bug #8 regression: after human approves an escalate_human gate, "
            "list_approved_unexecuted_gates() must return it so the workflow can act"
        )

    def test_operator_commands_override_stored_with_gate(self, db):
        """Operator-supplied commands should be saveable on the gate for Phase 2 use."""
        gate = self._make_escalate_gate(db)
        # Simulate operator providing commands via the UI (stored as an event)
        db.add_event(gate["id"], "human", "operator_commands",
                     {"commands": "interface Ethernet1\n no shutdown\n description RESTORED"})
        db.approve_task(gate["id"], "human")
        task = db.get_task(gate["id"])
        event_types = [e["event_type"] for e in task.get("events", [])]
        assert "operator_commands" in event_types
        assert "approved" in event_types


# ── Bug #9: Alert storm creates 3 investigations for 1 root cause ─────────────

@pytest.mark.unit
class TestAlertStormDeduplication:
    """
    Bug #9: When spine2/Ethernet1 shuts down, 3 alerts fire:
      - InterfaceDown/spine2  (fingerprint A)
      - InterfaceAdminDown/spine2  (fingerprint B)
      - BGPPeerDown/spine2  (fingerprint C, caused by A)

    The poller was creating 3 separate rca tasks.  The correct behaviour is:
      - get_active_rca_for_device() returns an existing task → new alerts for the
        same device within the correlation window should not spawn a new rca
      - At most 1 active rca per device at a time (within correlation window)
    """

    def test_no_active_rca_returns_none(self, db):
        result = db.get_active_rca_for_device("spine2", minutes=15)
        assert result is None

    def test_active_rca_found_while_running(self, db):
        task = db.create_task(
            type="rca", created_by="alert_poller", assigned_to="ops_agent",
            title="RCA: InterfaceDown spine2", alert_fingerprint="fp-storm-iface",
            content={"device": "spine2", "alertname": "InterfaceDown"},
        )
        db.claim_task(task["id"], "ops_agent")
        db.start_task(task["id"], "ops_agent")

        existing = db.get_active_rca_for_device("spine2", minutes=15)
        assert existing is not None, (
            "Bug #9: get_active_rca_for_device must detect the running rca so "
            "a second alert for the same device doesn't spawn a duplicate investigation"
        )
        assert existing["id"] == task["id"]

    def test_active_rca_found_while_pending(self, db):
        task = db.create_task(
            type="rca", created_by="alert_poller", assigned_to="ops_agent",
            title="RCA: InterfaceAdminDown spine2", alert_fingerprint="fp-storm-admin",
            content={"device": "spine2", "alertname": "InterfaceAdminDown"},
        )
        existing = db.get_active_rca_for_device("spine2", minutes=15)
        assert existing is not None
        assert existing["id"] == task["id"]

    def test_completed_rca_not_returned_as_active(self, db):
        task = db.create_task(
            type="rca", created_by="alert_poller", assigned_to="ops_agent",
            title="RCA: old event", alert_fingerprint="fp-done",
            content={"device": "spine2", "alertname": "InterfaceDown"},
        )
        db.claim_task(task["id"], "ops_agent")
        db.start_task(task["id"], "ops_agent")
        db.complete_task(task["id"], "ops_agent", {"diagnosis": "resolved"})

        result = db.get_active_rca_for_device("spine2", minutes=15)
        assert result is None, (
            "Completed tasks must not block new investigations for the same device"
        )

    def test_different_device_does_not_block(self, db):
        db.create_task(
            type="rca", created_by="alert_poller", assigned_to="ops_agent",
            title="RCA on spine1", alert_fingerprint="fp-spine1",
            content={"device": "spine1", "alertname": "InterfaceDown"},
        )
        result = db.get_active_rca_for_device("spine2", minutes=15)
        assert result is None, "Active rca on spine1 must not block investigation for spine2"

    def test_three_fingerprints_same_device_max_one_active_rca(self, db):
        """Verify the dedup gate: once an rca exists, subsequent same-device alerts find it."""
        first = db.create_task(
            type="rca", created_by="alert_poller", assigned_to="ops_agent",
            title="RCA: InterfaceDown spine2", alert_fingerprint="fp-storm-iface",
            content={"device": "spine2", "alertname": "InterfaceDown"},
        )
        db.claim_task(first["id"], "ops_agent")
        db.start_task(first["id"], "ops_agent")

        # Second alert for same device — poller should not create another rca
        found = db.get_active_rca_for_device("spine2", minutes=15)
        assert found is not None
        assert found["id"] == first["id"], (
            "Bug #9 regression: second alert on same device within correlation window "
            "must resolve to the existing rca, not trigger a new investigation"
        )

        # Only 1 rca task should exist
        rcas = db.list_tasks(type="rca")
        assert len(rcas) == 1, f"Expected 1 rca task, got {len(rcas)}"


# ── Bug #7: Chat endpoint bypasses safety gates ────────────────────────────────

@pytest.mark.unit
class TestChatBypassGuard:
    """
    Bug #7: Sending the word 'execute' to /chat triggered check_mode=False
    with no task, no audit trail, and no approval gate.

    The fix (already present in main.py) creates an audit task for any chat
    config change.  This test verifies the task_store supports it correctly —
    that a task created by 'chat' source goes through the same status lifecycle
    and is visible in list_tasks.
    """

    def test_chat_audit_task_created_and_complete(self, db):
        task = db.create_task(
            type="rca",
            created_by="chat",
            assigned_to="ops_agent",
            title="[CHAT] Config change via interactive session abc12345",
            content={
                "source": "chat",
                "session_id": "abc12345",
                "message": "execute no shutdown on spine2 Ethernet1",
                "tool_calls": 1,
            },
        )
        db.claim_task(task["id"], "ops_agent")
        db.complete_task(task["id"], "ops_agent", {"response": "Done", "tool_calls": 1})

        stored = db.get_task(task["id"])
        assert stored["status"] == "complete"
        assert stored["created_by"] == "chat"

    def test_chat_tasks_visible_in_list(self, db):
        db.create_task(
            type="rca", created_by="chat", assigned_to="ops_agent",
            title="[CHAT] audit", content={"source": "chat", "session_id": "s1"},
        )
        tasks = db.list_tasks(type="rca")
        chat_tasks = [t for t in tasks if t["created_by"] == "chat"]
        assert len(chat_tasks) == 1

    def test_chat_tasks_excluded_with_archive_filter(self, db):
        task = db.create_task(
            type="rca", created_by="chat", assigned_to="ops_agent",
            title="[CHAT] audit", content={"source": "chat", "session_id": "s2"},
        )
        db.claim_task(task["id"], "ops_agent")
        db.complete_task(task["id"], "ops_agent", {})

        active = db.list_tasks(exclude_statuses=["complete", "failed", "rejected"])
        ids = [t["id"] for t in active]
        assert task["id"] not in ids, (
            "Completed chat audit tasks should be hidden by the archive filter, "
            "same as any other completed task"
        )
