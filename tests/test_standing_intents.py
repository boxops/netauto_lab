"""
Unit tests for Standing Intents: CRUD, workflow routing, monitor evaluator,
and chaos_schedule APScheduler sync.

Run with:
    python3 -m pytest tests/test_standing_intents.py -v -m unit
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore
from shared.intent_registry import IntentRegistry, IntentEvaluator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def registry(db):
    return IntentRegistry(db)


# ---------------------------------------------------------------------------
# TestIntentCRUD
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestIntentCRUD:

    def test_create_suppress_intent(self, db):
        intent = db.create_intent({
            "name": "suppress leaf1 flap",
            "intent_type": "suppress",
            "device": "leaf1",
            "alertname": "InterfaceDown",
            "description": "Known flapping link",
        })
        assert intent["id"].startswith("int-")
        assert intent["intent_type"] == "suppress"
        assert intent["device"] == "leaf1"
        assert intent["alertname"] == "InterfaceDown"
        assert intent["description"] == "Known flapping link"
        assert intent["enabled"] == 1

    def test_create_monitor_intent(self, db):
        intent = db.create_intent({
            "name": "watch leaf1 admin status",
            "intent_type": "monitor",
            "device": "leaf1",
            "metric_query": 'interface_ifAdminStatus{sysName="leaf1",ifDescr="Ethernet1"}',
            "threshold": ">= 2",
        })
        row = db.get_intent(intent["id"])
        assert row["metric_query"] == 'interface_ifAdminStatus{sysName="leaf1",ifDescr="Ethernet1"}'
        assert row["threshold"] == ">= 2"
        assert row["intent_type"] == "monitor"

    def test_create_chaos_schedule_intent(self, db):
        intent = db.create_intent({
            "name": "bgp flap every 30 min",
            "intent_type": "chaos_schedule",
            "schedule": "*/30 * * * *",
            "action": "Simulate BGP flap on leaf1 check_mode=True",
        })
        row = db.get_intent(intent["id"])
        assert row["schedule"] == "*/30 * * * *"
        assert row["action"] == "Simulate BGP flap on leaf1 check_mode=True"
        assert row["intent_type"] == "chaos_schedule"

    def test_get_matching_intents_device_wildcard(self, db):
        db.create_intent({
            "name": "suppress any device",
            "intent_type": "suppress",
            "device": "",
            "alertname": "InterfaceDown",
        })
        rows = db.get_matching_intents(device="spine1", alertname="InterfaceDown")
        assert len(rows) == 1
        assert rows[0]["name"] == "suppress any device"

    def test_get_matching_intents_alertname_filter(self, db):
        db.create_intent({
            "name": "suppress leaf1 bgp",
            "intent_type": "suppress",
            "device": "leaf1",
            "alertname": "BGPPeerDown",
        })
        rows = db.get_matching_intents(device="leaf1", alertname="InterfaceDown")
        assert all(r["alertname"] != "BGPPeerDown" for r in rows)

    def test_get_matching_intents_disabled_excluded(self, db):
        intent = db.create_intent({
            "name": "disabled suppress",
            "intent_type": "suppress",
            "device": "leaf1",
            "alertname": "InterfaceDown",
        })
        db.update_intent(intent["id"], {"enabled": 0})
        rows = db.get_matching_intents(device="leaf1", alertname="InterfaceDown")
        assert not any(r["id"] == intent["id"] for r in rows)

    def test_touch_intent_updates_timestamp(self, db):
        intent = db.create_intent({
            "name": "monitor leaf1",
            "intent_type": "monitor",
            "device": "leaf1",
            "metric_query": "up",
            "threshold": "< 1",
        })
        assert db.get_intent(intent["id"])["last_triggered_at"] is None
        db.touch_intent(intent["id"])
        assert db.get_intent(intent["id"])["last_triggered_at"] is not None

    def test_update_intent_fields(self, db):
        intent = db.create_intent({
            "name": "monitor leaf1",
            "intent_type": "monitor",
            "metric_query": "up",
            "threshold": "< 1",
        })
        db.update_intent(intent["id"], {"metric_query": "up{job='telegraf'}", "threshold": "== 0"})
        updated = db.get_intent(intent["id"])
        assert updated["metric_query"] == "up{job='telegraf'}"
        assert updated["threshold"] == "== 0"


# ---------------------------------------------------------------------------
# TestWorkflowIntentRouting
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestWorkflowIntentRouting:

    def test_suppress_returns_suppress_type(self, db, registry):
        db.create_intent({
            "name": "suppress leaf1 flap",
            "intent_type": "suppress",
            "device": "leaf1",
            "alertname": "InterfaceDown",
        })
        matches = registry.matching(device="leaf1", alertname="InterfaceDown")
        assert len(matches) == 1
        assert matches[0].intent_type == "suppress"

    def test_escalate_returns_escalate_type(self, db, registry):
        db.create_intent({
            "name": "escalate spine",
            "intent_type": "escalate",
            "device": "spine1",
            "alertname": "InterfaceDown",
        })
        matches = registry.matching(device="spine1", alertname="InterfaceDown")
        assert any(m.intent_type == "escalate" for m in matches)

    def test_no_match_returns_empty_list(self, registry):
        matches = registry.matching(device="leaf99", alertname="BGPPeerDown")
        assert matches == []

    def test_wildcard_device_matches_any_device(self, db, registry):
        db.create_intent({
            "name": "suppress all",
            "intent_type": "suppress",
            "device": "",
            "alertname": "InterfaceDown",
        })
        matches = registry.matching(device="leaf99", alertname="InterfaceDown")
        assert len(matches) >= 1
        assert any(m.intent_type == "suppress" for m in matches)


# ---------------------------------------------------------------------------
# TestMonitorEvaluator
# ---------------------------------------------------------------------------

def _make_prom_response(value: float):
    return MagicMock(
        raise_for_status=MagicMock(),
        json=MagicMock(return_value={
            "data": {"result": [{"value": ["1", str(value)]}]}
        }),
    )


@pytest.mark.unit
class TestMonitorEvaluator:

    @pytest.mark.parametrize("op,metric_val,threshold_val,should_breach", [
        ("<",   0.5, 1.0, True),
        ("<",   1.5, 1.0, False),
        ("<=",  1.0, 1.0, True),
        (">",   2.0, 1.0, True),
        (">",   0.5, 1.0, False),
        (">=",  1.0, 1.0, True),
        ("==",  2.0, 2.0, True),
        ("==",  1.0, 2.0, False),
        ("!=",  1.0, 2.0, True),
        ("!=",  2.0, 2.0, False),
    ])
    def test_threshold_operators(self, op, metric_val, threshold_val, should_breach):
        assert IntentEvaluator._threshold_breached(
            metric_val, f"{op} {threshold_val}"
        ) == should_breach

    def test_monitor_creates_rca_on_breach(self, db, registry, tmp_path):
        intent = db.create_intent({
            "name": "watch admin status",
            "intent_type": "monitor",
            "device": "leaf1",
            "metric_query": "interface_ifAdminStatus",
            "threshold": ">= 2",
        })
        evaluator = IntentEvaluator(
            intent_registry=registry,
            task_store=db,
            alert_poller=MagicMock(),
            prometheus_url="http://fake:9090",
        )
        with patch("httpx.get", return_value=_make_prom_response(2.0)):
            evaluator._evaluate_one(db.get_intent(intent["id"]))

        tasks = db.list_tasks(type="rca")
        assert any("watch admin status" in (t.get("title") or "") for t in tasks)

    def test_monitor_skips_if_active_task_exists(self, db, registry):
        intent = db.create_intent({
            "name": "watch dup",
            "intent_type": "monitor",
            "device": "leaf1",
            "metric_query": "up",
            "threshold": "< 1",
        })
        fp = f"intent:{intent['id']}"
        db.create_task(
            type="rca",
            created_by="test",
            assigned_to="ops_agent",
            title="[Intent] watch dup",
            alert_fingerprint=fp,
            content={"alertname": "intent:watch dup", "device": "leaf1",
                     "fingerprint": fp, "summary": "dup", "description": "",
                     "severity": "warning", "instance": "", "labels": {}},
        )
        evaluator = IntentEvaluator(
            intent_registry=registry,
            task_store=db,
            alert_poller=MagicMock(),
            prometheus_url="http://fake:9090",
        )
        before = len(db.list_tasks(type="rca"))
        with patch("httpx.get", return_value=_make_prom_response(0.0)):
            evaluator._evaluate_one(db.get_intent(intent["id"]))
        assert len(db.list_tasks(type="rca")) == before

    def test_monitor_no_task_when_not_breached(self, db, registry):
        intent = db.create_intent({
            "name": "watch leaf1 up",
            "intent_type": "monitor",
            "device": "leaf1",
            "metric_query": "up",
            "threshold": "< 1",
        })
        evaluator = IntentEvaluator(
            intent_registry=registry,
            task_store=db,
            alert_poller=MagicMock(),
            prometheus_url="http://fake:9090",
        )
        with patch("httpx.get", return_value=_make_prom_response(1.0)):
            evaluator._evaluate_one(db.get_intent(intent["id"]))
        tasks = db.list_tasks(type="rca")
        assert not any("watch leaf1 up" in (t.get("title") or "") for t in tasks)


# ---------------------------------------------------------------------------
# TestChaosScheduleEval
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestChaosScheduleEval:

    def _make_scheduler_mock(self, existing_ids: set[str] | None = None):
        mock = MagicMock()
        mock.list_cron_job_ids.return_value = existing_ids or set()
        return mock

    def _make_evaluator(self, db, registry, scheduler):
        return IntentEvaluator(
            intent_registry=registry,
            task_store=db,
            alert_poller=MagicMock(),
            prometheus_url="http://fake:9090",
            scheduler=scheduler,
        )

    def test_sync_adds_job_for_new_enabled_intent(self, db, registry):
        intent = db.create_intent({
            "name": "bgp chaos",
            "intent_type": "chaos_schedule",
            "schedule": "*/30 * * * *",
            "action": "Simulate BGP flap check_mode=True",
        })
        sched = self._make_scheduler_mock(existing_ids=set())
        ev = self._make_evaluator(db, registry, sched)
        ev._sync_chaos_jobs()
        sched.add_cron_job.assert_called_once()
        args = sched.add_cron_job.call_args
        assert args[0][0] == intent["id"]
        assert args[0][2] == "*/30 * * * *"

    def test_sync_removes_job_when_intent_gone(self, db, registry):
        sched = self._make_scheduler_mock(existing_ids={"orphan-id"})
        ev = self._make_evaluator(db, registry, sched)
        ev._sync_chaos_jobs()
        sched.remove_cron_job.assert_called_once_with("orphan-id")

    def test_on_fire_fn_updates_last_triggered_at(self, db, registry):
        intent = db.create_intent({
            "name": "chaos fire test",
            "intent_type": "chaos_schedule",
            "schedule": "*/5 * * * *",
            "action": "Run something",
        })
        captured_fn = []

        def _fake_add_cron_job(intent_id, scenario, cron_expr, on_fire_fn):
            captured_fn.append(on_fire_fn)

        sched = self._make_scheduler_mock(existing_ids=set())
        sched.add_cron_job.side_effect = _fake_add_cron_job

        ev = self._make_evaluator(db, registry, sched)
        ev._sync_chaos_jobs()

        assert len(captured_fn) == 1
        assert db.get_intent(intent["id"])["last_triggered_at"] is None
        captured_fn[0](True)
        assert db.get_intent(intent["id"])["last_triggered_at"] is not None

    def test_sync_skips_intent_with_empty_schedule(self, db, registry):
        db.create_intent({
            "name": "chaos no schedule",
            "intent_type": "chaos_schedule",
            "schedule": "",
            "action": "Do something",
        })
        sched = self._make_scheduler_mock(existing_ids=set())
        ev = self._make_evaluator(db, registry, sched)
        ev._sync_chaos_jobs()
        sched.add_cron_job.assert_not_called()
