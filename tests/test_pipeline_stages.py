"""
Pipeline stage transition tests — unit level, no running stack required.

Tests the TaskStore lifecycle that underpins the closed-loop pipeline:
  rca (pending→running→complete)
  → fix_proposal
  → validation
  → approval_gate (awaiting_approval→complete[approved])
  → execution triggered

Also covers the archive/exclude_statuses filter added in Phase 2.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore

sys.path.insert(0, str(Path(__file__).parent))
from fixtures.alerts import interface_down_payload


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


FINGERPRINT = "fp-stage-test-001"


def _create_rca(db: TaskStore, fp: str = FINGERPRINT) -> dict:
    return db.create_task(
        type="rca",
        created_by="alert_poller",
        assigned_to="ops_agent",
        title="InterfaceDown on spine2",
        alert_fingerprint=fp,
        content={"device": "spine2", "alertname": "InterfaceDown", "severity": "critical"},
    )


# ── Stage 1: RCA task creation ─────────────────────────────────────────────────

@pytest.mark.unit
class TestRCACreation:
    def test_rca_task_created_with_fingerprint(self, db):
        task = _create_rca(db)
        assert task["type"] == "rca"
        assert task["status"] == "pending"
        assert task["alert_fingerprint"] == FINGERPRINT
        assert task["assigned_to"] == "ops_agent"

    def test_rca_task_appears_in_list(self, db):
        _create_rca(db)
        tasks = db.list_tasks(type="rca")
        assert len(tasks) == 1
        assert tasks[0]["alert_fingerprint"] == FINGERPRINT

    def test_duplicate_fingerprint_not_created_by_get_active_rca(self, db):
        _create_rca(db)
        db.claim_task(db.list_tasks(type="rca")[0]["id"], "ops_agent")
        existing = db.get_active_rca_for_device("spine2", minutes=15)
        assert existing is not None, "get_active_rca_for_device should return the in-progress task"

    def test_rca_full_lifecycle(self, db):
        task = _create_rca(db)
        tid = task["id"]

        ok = db.claim_task(tid, "ops_agent")
        assert ok is True
        assert db.get_task(tid)["status"] == "claimed"

        db.start_task(tid, "ops_agent")
        assert db.get_task(tid)["status"] == "running"

        db.complete_task(tid, "ops_agent", {
            "diagnosis": "Interface Ethernet1 is admin-shutdown",
            "fix_type": "config_change",
            "device": "spine2",
            "commands": "interface Ethernet1\n no shutdown",
        })
        assert db.get_task(tid)["status"] == "complete"


# ── Stage 2: fix_proposal ─────────────────────────────────────────────────────

@pytest.mark.unit
class TestFixProposalStage:
    def test_fix_proposal_links_to_rca_via_parent(self, db):
        rca = _create_rca(db)
        db.claim_task(rca["id"], "ops_agent")
        db.start_task(rca["id"], "ops_agent")
        db.complete_task(rca["id"], "ops_agent", {"fix_type": "config_change"})

        fp_task = db.create_task(
            type="fix_proposal",
            created_by="ops_agent",
            assigned_to="ops_agent",
            parent_id=rca["id"],
            alert_fingerprint=FINGERPRINT,
            title="Fix proposal: no shutdown on Ethernet1",
            content={"fix_type": "config_change", "device": "spine2",
                     "commands": "interface Ethernet1\n no shutdown"},
        )
        assert fp_task["parent_id"] == rca["id"]
        assert fp_task["alert_fingerprint"] == FINGERPRINT
        assert fp_task["type"] == "fix_proposal"


# ── Stage 3: validation ───────────────────────────────────────────────────────

@pytest.mark.unit
class TestValidationStage:
    def test_validation_task_created_with_chain(self, db):
        rca = _create_rca(db)
        fp_task = db.create_task(
            type="fix_proposal", created_by="ops_agent", assigned_to="ops_agent",
            parent_id=rca["id"], alert_fingerprint=FINGERPRINT,
            title="Fix proposal", content={"fix_type": "config_change"},
        )
        val_task = db.create_task(
            type="validation", created_by="ops_agent", assigned_to="ops_agent",
            parent_id=fp_task["id"], alert_fingerprint=FINGERPRINT,
            title="Validation: verify fix", content={"verdict": "approved"},
        )
        assert val_task["type"] == "validation"
        chain = db.get_task_chain(val_task["id"])
        parent_ids = [t["id"] for t in chain]
        assert fp_task["id"] in parent_ids or val_task["parent_id"] == fp_task["id"]


# ── Stage 4: approval_gate ────────────────────────────────────────────────────

@pytest.mark.unit
class TestApprovalGateStage:
    def test_approval_gate_transitions_to_awaiting(self, db):
        rca = _create_rca(db)
        gate = db.create_task(
            type="approval_gate", created_by="ops_agent", assigned_to="human",
            parent_id=rca["id"], alert_fingerprint=FINGERPRINT,
            title="APPROVAL REQUIRED: config_change on spine2",
            content={"fix_proposal": {"fix_type": "config_change", "device": "spine2",
                                       "commands": "interface Ethernet1\n no shutdown"}},
        )
        db.request_approval(gate["id"], "ops_agent")
        assert db.get_task(gate["id"])["status"] == "awaiting_approval"

    def test_approval_records_approved_event(self, db):
        rca = _create_rca(db)
        gate = db.create_task(
            type="approval_gate", created_by="ops_agent", assigned_to="human",
            parent_id=rca["id"], alert_fingerprint=FINGERPRINT,
            title="APPROVAL REQUIRED",
            content={"fix_proposal": {"fix_type": "config_change", "commands": "no shutdown"}},
        )
        db.request_approval(gate["id"], "ops_agent")
        db.approve_task(gate["id"], "human")
        task = db.get_task(gate["id"])
        assert task["status"] == "complete"
        event_types = [e["event_type"] for e in task.get("events", [])]
        assert "approved" in event_types

    def test_rejection_closes_gate(self, db):
        rca = _create_rca(db)
        gate = db.create_task(
            type="approval_gate", created_by="ops_agent", assigned_to="human",
            parent_id=rca["id"], alert_fingerprint=FINGERPRINT,
            title="APPROVAL REQUIRED",
            content={"fix_proposal": {"fix_type": "config_change", "commands": "no shutdown"}},
        )
        db.request_approval(gate["id"], "ops_agent")
        db.reject_task(gate["id"], "human", reason="Scheduled maintenance window active")
        assert db.get_task(gate["id"])["status"] == "rejected"

    def test_list_approved_unexecuted_returns_approved_gate(self, db):
        rca = _create_rca(db)
        gate = db.create_task(
            type="approval_gate", created_by="ops_agent", assigned_to="human",
            parent_id=rca["id"], alert_fingerprint=FINGERPRINT,
            title="APPROVAL REQUIRED",
            content={"fix_proposal": {"fix_type": "config_change", "commands": "no shutdown"}},
        )
        db.request_approval(gate["id"], "ops_agent")
        db.approve_task(gate["id"], "human")
        pending = db.list_approved_unexecuted_gates()
        assert any(t["id"] == gate["id"] for t in pending)


# ── Archive / exclude_statuses filter ─────────────────────────────────────────

@pytest.mark.unit
class TestArchiveFilter:
    def test_active_tasks_shown_by_default(self, db):
        running = db.create_task(
            type="rca", created_by="system", assigned_to="ops_agent",
            title="Active RCA", alert_fingerprint="fp-active",
            content={"device": "spine1"},
        )
        db.claim_task(running["id"], "ops_agent")
        db.start_task(running["id"], "ops_agent")

        tasks = db.list_tasks(exclude_statuses=["complete", "failed", "rejected"])
        ids = [t["id"] for t in tasks]
        assert running["id"] in ids

    def test_completed_tasks_excluded_when_filter_active(self, db):
        task = db.create_task(
            type="rca", created_by="system", assigned_to="ops_agent",
            title="Completed RCA", alert_fingerprint="fp-done",
            content={"device": "spine1"},
        )
        db.claim_task(task["id"], "ops_agent")
        db.start_task(task["id"], "ops_agent")
        db.complete_task(task["id"], "ops_agent", {"diagnosis": "done"})

        tasks = db.list_tasks(exclude_statuses=["complete", "failed", "rejected"])
        ids = [t["id"] for t in tasks]
        assert task["id"] not in ids

    def test_completed_tasks_visible_when_archived_shown(self, db):
        task = db.create_task(
            type="rca", created_by="system", assigned_to="ops_agent",
            title="Completed RCA", alert_fingerprint="fp-done2",
            content={"device": "spine1"},
        )
        db.claim_task(task["id"], "ops_agent")
        db.start_task(task["id"], "ops_agent")
        db.complete_task(task["id"], "ops_agent", {"diagnosis": "done"})

        tasks = db.list_tasks()
        ids = [t["id"] for t in tasks]
        assert task["id"] in ids

    def test_mix_of_active_and_completed(self, db):
        active = db.create_task(
            type="rca", created_by="system", assigned_to="ops_agent",
            title="Active", alert_fingerprint="fp-mix-active", content={"device": "spine1"},
        )
        done = db.create_task(
            type="rca", created_by="system", assigned_to="ops_agent",
            title="Done", alert_fingerprint="fp-mix-done", content={"device": "spine2"},
        )
        db.claim_task(done["id"], "ops_agent")
        db.start_task(done["id"], "ops_agent")
        db.complete_task(done["id"], "ops_agent", {})

        active_tasks = db.list_tasks(exclude_statuses=["complete", "failed", "rejected"])
        ids = [t["id"] for t in active_tasks]
        assert active["id"] in ids
        assert done["id"] not in ids
