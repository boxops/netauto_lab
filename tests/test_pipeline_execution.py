"""
Tests for post-approval execution: the eng_agent picking up approved
approval_gate tasks and running config changes with check_mode=False.

All tests are pure unit tests — SQLite in-memory database, mocked agent.
"""
from __future__ import annotations

import json
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore
from engineering_agent.task_runner import EngTaskRunner


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """Temporary SQLite database for each test."""
    return TaskStore(db_path=str(tmp_path / "test.db"))


def _mock_runner(db):
    agent        = MagicMock()
    rate_limiter = MagicMock()
    rate_limiter.check_budget.return_value = None
    return EngTaskRunner(agent, db, rate_limiter)


def _make_approval_gate(db, commands="interface Ethernet1\n  no shutdown", device="spine1"):
    task = db.create_task(
        type="approval_gate",
        created_by="chaos_agent",
        assigned_to="human",
        title="APPROVAL REQUIRED: config_change on spine1",
        content={
            "fix_proposal": {
                "fix_type": "config_change",
                "device": device,
                "commands": commands,
                "risk": "low",
            },
            "device": device,
            "commands": commands,
        },
    )
    db.request_approval(task["id"], "chaos_agent")
    return task


# ── TaskStore.list_approved_unexecuted_gates ──────────────────────────────────

class TestListApprovedUnexecutedGates:
    def test_empty_when_no_tasks(self, db):
        assert db.list_approved_unexecuted_gates() == []

    def test_not_returned_when_still_awaiting(self, db):
        _make_approval_gate(db)
        # Still awaiting_approval — not complete yet
        assert db.list_approved_unexecuted_gates() == []

    def test_returned_after_approval(self, db):
        task = _make_approval_gate(db)
        db.approve_task(task["id"], "human")
        result = db.list_approved_unexecuted_gates()
        assert len(result) == 1
        assert result[0]["id"] == task["id"]

    def test_not_returned_after_execution_started(self, db):
        task = _make_approval_gate(db)
        db.approve_task(task["id"], "human")
        db.add_event(task["id"], "eng_agent", "execution_started")
        assert db.list_approved_unexecuted_gates() == []

    def test_not_returned_after_execution_complete(self, db):
        task = _make_approval_gate(db)
        db.approve_task(task["id"], "human")
        db.add_event(task["id"], "eng_agent", "execution_started")
        db.add_event(task["id"], "eng_agent", "execution_complete", {"status": "success"})
        assert db.list_approved_unexecuted_gates() == []

    def test_not_returned_when_rejected(self, db):
        task = _make_approval_gate(db)
        db.reject_task(task["id"], "human", "Not needed")
        assert db.list_approved_unexecuted_gates() == []

    def test_multiple_gates_all_returned(self, db):
        t1 = _make_approval_gate(db, device="spine1")
        t2 = _make_approval_gate(db, device="spine2")
        db.approve_task(t1["id"], "human")
        db.approve_task(t2["id"], "human")
        ids = {r["id"] for r in db.list_approved_unexecuted_gates(limit=10)}
        assert t1["id"] in ids
        assert t2["id"] in ids

    def test_limit_is_respected(self, db):
        for i in range(5):
            task = _make_approval_gate(db, device=f"device{i}")
            db.approve_task(task["id"], "human")
        assert len(db.list_approved_unexecuted_gates(limit=3)) == 3

    def test_only_approval_gate_type_returned(self, db):
        # A complete rca task should never appear in this query
        rca = db.create_task(type="rca", created_by="system", content={})
        db.claim_task(rca["id"], "ops_agent")
        db.start_task(rca["id"], "ops_agent")
        db.complete_task(rca["id"], "ops_agent", {"diagnosis": "test"})
        assert db.list_approved_unexecuted_gates() == []


# ── EngTaskRunner._execute_approved_gate ─────────────────────────────────────

class TestEngExecutionRunner:
    def _run_gate(self, db, commands="interface Et1\n  no shutdown",
                  device="spine1", response="Fix applied.\nEXECUTION_STATUS: success\n"
                                            "DEVICE: spine1\nCHANGES_APPLIED: interface brought up"):
        task = _make_approval_gate(db, commands=commands, device=device)
        db.approve_task(task["id"], "human")

        runner = _mock_runner(db)
        runner._agent.chat_with_trace.return_value = (response, [MagicMock(), MagicMock()])

        gate = db.list_approved_unexecuted_gates()[0]
        runner._execute_approved_gate(gate)
        return task["id"], runner

    def test_execution_started_event_is_written_first(self, db):
        task_id, _ = self._run_gate(db)
        task = db.get_task(task_id)
        event_types = [e["event_type"] for e in task["events"]]
        assert "execution_started" in event_types

    def test_execution_complete_event_is_written(self, db):
        task_id, _ = self._run_gate(db)
        task = db.get_task(task_id)
        event_types = [e["event_type"] for e in task["events"]]
        assert "execution_complete" in event_types

    def test_agent_chat_with_trace_is_called(self, db):
        _, runner = self._run_gate(db)
        runner._agent.chat_with_trace.assert_called_once()

    def test_chat_prompt_contains_check_mode_false(self, db):
        _, runner = self._run_gate(db)
        call_args = runner._agent.chat_with_trace.call_args
        prompt = call_args[0][0]
        assert "check_mode=False" in prompt

    def test_chat_prompt_contains_device(self, db):
        _, runner = self._run_gate(db, device="leaf2")
        prompt = runner._agent.chat_with_trace.call_args[0][0]
        assert "leaf2" in prompt

    def test_no_double_execution_on_second_poll(self, db):
        task = _make_approval_gate(db)
        db.approve_task(task["id"], "human")
        runner = _mock_runner(db)
        runner._agent.chat_with_trace.return_value = (
            "EXECUTION_STATUS: success\nDEVICE: spine1\nCHANGES_APPLIED: done", []
        )
        runner._poll_approved_gates()
        runner._poll_approved_gates()  # second poll — must not run again
        assert runner._agent.chat_with_trace.call_count == 1

    def test_no_commands_skips_agent_call(self, db):
        task = _make_approval_gate(db, commands="none")
        db.approve_task(task["id"], "human")
        runner = _mock_runner(db)
        gate = db.list_approved_unexecuted_gates()[0]
        runner._execute_approved_gate(gate)
        runner._agent.chat_with_trace.assert_not_called()
        task_detail = db.get_task(task["id"])
        event_types = [e["event_type"] for e in task_detail["events"]]
        assert "execution_started" in event_types
        assert "execution_complete" in event_types

    def test_agent_exception_writes_execution_failed_event(self, db):
        task = _make_approval_gate(db)
        db.approve_task(task["id"], "human")
        runner = _mock_runner(db)
        runner._agent.chat_with_trace.side_effect = RuntimeError("network error")
        gate = db.list_approved_unexecuted_gates()[0]
        runner._execute_approved_gate(gate)
        task_detail = db.get_task(task["id"])
        event_types = [e["event_type"] for e in task_detail["events"]]
        assert "execution_failed" in event_types

    def test_budget_exceeded_skips_execution_without_event(self, db):
        from shared.rate_limiter import BudgetExceededError
        task = _make_approval_gate(db)
        db.approve_task(task["id"], "human")
        runner = _mock_runner(db)
        runner._rate_limiter.check_budget.side_effect = BudgetExceededError("over limit")
        gate = db.list_approved_unexecuted_gates()[0]
        runner._execute_approved_gate(gate)
        # execution_started must NOT have been written — gate stays unexecuted for retry
        task_detail = db.get_task(task["id"])
        event_types = [e["event_type"] for e in task_detail["events"]]
        assert "execution_started" not in event_types

    def test_gate_not_returned_after_failed_execution(self, db):
        task = _make_approval_gate(db)
        db.approve_task(task["id"], "human")
        runner = _mock_runner(db)
        runner._agent.chat_with_trace.side_effect = RuntimeError("timeout")
        gate = db.list_approved_unexecuted_gates()[0]
        runner._execute_approved_gate(gate)
        # execution_started was written before the agent call, so gate is NOT retried
        assert db.list_approved_unexecuted_gates() == []
