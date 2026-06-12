"""
Unit tests for restart resilience (roadmap item 12):
- scheduled interval jobs survive a scheduler restart (scheduled_jobs table)
- chat checkpointer factory falls back to MemorySaver without the optional dep
"""
from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))

from shared.task_store import TaskStore

pytestmark = pytest.mark.unit


@pytest.fixture
def ts(tmp_path):
    return TaskStore(db_path=str(tmp_path / "sched-test.db"))


def _make_scheduler(ts):
    from ops_agent.scheduler import OpsScheduler
    return OpsScheduler(MagicMock(), task_store=ts)


class TestSchedulerPersistence:
    def test_job_survives_restart(self, ts):
        s1 = _make_scheduler(ts)
        try:
            created = s1.add_job("Flap Ethernet1 on leaf1 in check mode", 30)
        finally:
            s1.shutdown()

        s2 = _make_scheduler(ts)
        try:
            jobs = s2.list_jobs()
            assert len(jobs) == 1
            assert jobs[0]["job_id"] == created["job_id"]
            assert jobs[0]["scenario"] == created["scenario"]
            assert jobs[0]["interval_minutes"] == 30
            assert jobs[0]["next_run"] is not None  # re-registered with APScheduler
        finally:
            s2.shutdown()

    def test_removed_job_stays_removed_after_restart(self, ts):
        s1 = _make_scheduler(ts)
        try:
            created = s1.add_job("scenario", 15)
            assert s1.remove_job(created["job_id"]) is True
        finally:
            s1.shutdown()

        s2 = _make_scheduler(ts)
        try:
            assert s2.list_jobs() == []
        finally:
            s2.shutdown()

    def test_remove_unknown_job_returns_false(self, ts):
        s = _make_scheduler(ts)
        try:
            assert s.remove_job("nope") is False
        finally:
            s.shutdown()

    def test_scheduler_without_store_is_memory_only(self):
        from ops_agent.scheduler import OpsScheduler
        s = OpsScheduler(MagicMock())
        try:
            created = s.add_job("scenario", 5)
            assert s.list_jobs()[0]["job_id"] == created["job_id"]
        finally:
            s.shutdown()

    def test_cron_intent_jobs_are_not_persisted(self, ts):
        """chaos_schedule intents are re-registered from the intents table —
        the scheduled_jobs table must only hold interval jobs."""
        s1 = _make_scheduler(ts)
        try:
            s1.add_cron_job("int-1", "scenario", "*/5 * * * *", lambda ok: None)
            assert "int-1" in s1.list_cron_job_ids()
        finally:
            s1.shutdown()

        s2 = _make_scheduler(ts)
        try:
            assert s2.list_cron_job_ids() == set()
            assert s2.list_jobs() == []
        finally:
            s2.shutdown()


class TestChatCheckpointer:
    def test_fallback_to_memory_when_package_missing(self):
        from langgraph.checkpoint.memory import MemorySaver
        import shared.checkpoints as cp
        with patch.dict(sys.modules, {"langgraph.checkpoint.sqlite": None}):
            saver = cp.get_chat_checkpointer()
        assert isinstance(saver, MemorySaver)

    def test_returns_a_checkpointer(self, tmp_path, monkeypatch):
        """Whatever backend is available, the factory must return a usable saver."""
        monkeypatch.setenv("CHAT_CHECKPOINT_DB", str(tmp_path / "ckpt.db"))
        import shared.checkpoints as cp
        saver = cp.get_chat_checkpointer()
        assert hasattr(saver, "get_tuple") and hasattr(saver, "put")

    def test_sqlite_saver_used_when_available(self, tmp_path, monkeypatch):
        pytest.importorskip("langgraph.checkpoint.sqlite")
        monkeypatch.setenv("CHAT_CHECKPOINT_DB", str(tmp_path / "ckpt.db"))
        import shared.checkpoints as cp
        saver = cp.get_chat_checkpointer()
        assert type(saver).__name__ == "SqliteSaver"
        assert (tmp_path / "ckpt.db").exists()
