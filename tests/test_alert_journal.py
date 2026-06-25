"""
Unit tests for the Alert Journal decision ledger (shared/alert_journal.py)
and its recording call sites in the alert poller.

The journal is the keystone of the Operations visibility redesign: every alert
ingress must produce exactly one decision record, including the previously
silent paths (dedup, severity filter, suppression, budget deferral, …).
"""
from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))

from shared.task_store import TaskStore
from shared.alert_journal import AlertJournal

pytestmark = pytest.mark.unit


@pytest.fixture
def ts(tmp_path):
    return TaskStore(db_path=str(tmp_path / "journal-test.db"))


@pytest.fixture
def journal(ts):
    return AlertJournal(ts)


def _event(fp="fp-1", alertname="InterfaceDown", device="leaf1",
           severity="critical", status="firing", source=None):
    e = {
        "fingerprint":  fp,
        "alertname":    alertname,
        "device":       device,
        "severity":     severity,
        "alert_status": status,
        "labels":       {"sysName": device},
    }
    if source:
        e["_source"] = source
    return e


# ── Journal store ──────────────────────────────────────────────────────────────

class TestJournalStore:
    def test_record_and_read_back(self, journal):
        journal.record("investigating", _event(), reason="pipeline opened",
                       ref_task_id="t1")
        rows = journal.for_fingerprint("fp-1")
        assert len(rows) == 1
        assert rows[0]["decision"] == "investigating"
        assert rows[0]["device"] == "leaf1"
        assert rows[0]["ref_task_id"] == "t1"

    def test_source_defaults_to_poller_webhook_override(self, journal):
        journal.record("deduplicated", _event(fp="a"))
        journal.record("deduplicated", _event(fp="b", source="webhook"))
        assert journal.for_fingerprint("a")[0]["source"] == "poller"
        assert journal.for_fingerprint("b")[0]["source"] == "webhook"

    def test_record_never_raises(self, ts):
        journal = AlertJournal(ts)
        journal._connect = MagicMock(side_effect=RuntimeError("db gone"))
        journal.record("investigating", _event())  # must not raise

    def test_latest_per_fingerprint_collapses_history(self, journal):
        journal.record("deduplicated", _event(fp="x"))
        journal.record("investigating", _event(fp="x"), ref_task_id="t9")
        journal.record("fast_path", _event(fp="y"))
        rows = journal.latest_per_fingerprint()
        by_fp = {r["fingerprint"]: r for r in rows}
        assert by_fp["x"]["decision"] == "investigating"
        assert by_fp["x"]["record_count"] == 2
        assert by_fp["y"]["decision"] == "fast_path"

    def test_category_filter_dropped(self, journal):
        journal.record("suppressed_by_intent", _event(fp="s"))
        journal.record("investigating", _event(fp="i"))
        rows = journal.latest_per_fingerprint(category="dropped")
        assert [r["fingerprint"] for r in rows] == ["s"]

    def test_funnel_counts(self, journal):
        journal.record("investigating", _event(fp="a"))
        journal.record("fast_path", _event(fp="b"))
        journal.record("deduplicated", _event(fp="b"))
        journal.record("suppressed_by_intent", _event(fp="c"))
        f = journal.funnel(hours=1)
        assert f["alerts"] == 3
        assert f["investigated"] == 1
        assert f["fast_path"] == 1
        assert f["dropped"] == 2  # dedup + suppressed

    def test_prune_removes_old_rows(self, journal, ts):
        from sqlalchemy import text
        journal.record("investigating", _event())
        with ts._lock, ts._connect() as conn:
            conn.execute(text(
                "UPDATE alert_journal SET received_at='2020-01-01 00:00:00 UTC'"))
        assert journal.prune(days=14) == 1
        assert journal.for_fingerprint("fp-1") == []


# ── Poller call sites ──────────────────────────────────────────────────────────

def _make_poller(ts):
    """AlertPoller with mocked agent/limiter — no network, no LLM."""
    from ops_agent.alert_poller import AlertPoller
    with patch("ops_agent.alert_poller.TopologyCorrelator", MagicMock()):
        poller = AlertPoller(MagicMock(), ts, MagicMock(), workflow=None)
    return poller


class TestPollerJournaling:
    def test_severity_filter_recorded_once(self, ts):
        poller = _make_poller(ts)
        journal = AlertJournal(ts)
        ev = _event(severity="info")
        assert poller._classify_event(ev, None) is None
        assert poller._classify_event(ev, None) is None  # second pass — guarded
        rows = journal.for_fingerprint("fp-1")
        assert len(rows) == 1
        assert rows[0]["decision"] == "severity_filtered"

    def test_dedup_recorded_on_second_ingress(self, ts):
        poller = _make_poller(ts)
        poller._is_firing_in_prometheus = lambda *a, **k: True
        journal = AlertJournal(ts)
        ev = _event()
        assert poller._classify_event(ev, None) is not None   # first: accepted
        assert poller._classify_event(ev, None) is None       # second: dedup
        decisions = [r["decision"] for r in journal.for_fingerprint("fp-1")]
        assert decisions == ["deduplicated"]

    def test_not_firing_recorded(self, ts):
        poller = _make_poller(ts)
        poller._is_firing_in_prometheus = lambda *a, **k: False
        journal = AlertJournal(ts)
        assert poller._classify_event(_event(), None) is None
        assert journal.for_fingerprint("fp-1")[0]["decision"] == "not_firing"

    def test_resolved_recorded(self, ts):
        poller = _make_poller(ts)
        journal = AlertJournal(ts)
        poller._classify_event(_event(status="resolved"), None)
        assert journal.for_fingerprint("fp-1")[0]["decision"] == "resolved_cleared"

    def test_budget_deferred_recorded(self, ts):
        from shared.rate_limiter import BudgetExceededError
        poller = _make_poller(ts)
        poller._rate_limiter.check_budget.side_effect = BudgetExceededError("over budget")
        journal = AlertJournal(ts)
        poller._investigate(_event())
        assert journal.for_fingerprint("fp-1")[0]["decision"] == "budget_deferred"

    def test_already_active_recorded_with_task_link(self, ts):
        task = ts.create_task(type="rca", created_by="t", assigned_to="ops_agent",
                              title="x", alert_fingerprint="fp-1", content={})
        poller = _make_poller(ts)
        journal = AlertJournal(ts)
        poller._investigate(_event())
        row = journal.for_fingerprint("fp-1")[0]
        assert row["decision"] == "already_active"
        assert row["ref_task_id"] == task["id"]

    def test_webhook_source_stamped(self, ts):
        poller = _make_poller(ts)
        poller._fetch_live_alerts = lambda: None
        poller._is_firing_in_prometheus = lambda *a, **k: False
        journal = AlertJournal(ts)
        accepted = poller.push_alert(_event())
        assert accepted is False  # not firing → dropped (and journaled)
        assert journal.for_fingerprint("fp-1")[0]["source"] == "webhook"


# ── Workflow call sites (suppression — the worst silent path) ──────────────────

class TestWorkflowJournaling:
    def test_suppress_intent_writes_journal_record(self, ts):
        from unittest import mock
        with mock.patch("ops_agent.workflow.create_react_agent", MagicMock()), \
             mock.patch("ops_agent.workflow.OPS_TOOLS", []):
            from ops_agent.workflow import IncidentWorkflow
            from shared.intent_registry import IntentRegistry
            wf = IncidentWorkflow.__new__(IncidentWorkflow)
            wf._ts = ts
            wf._journal = AlertJournal(ts)
            wf._intent_registry = IntentRegistry(ts)
            wf._topology = MagicMock()
            wf._topology.is_leaf_symptom.return_value = (False, [])

        ts.create_intent({
            "name": "leaf1 maintenance", "intent_type": "suppress",
            "device": "leaf1", "alertname": "InterfaceDown",
            "tenant_id": "default", "enabled": True,
        })
        state = {
            "alertname": "InterfaceDown", "device": "leaf1",
            "tenant_id": "default", "event": _event(),
            "fingerprint": "fp-1", "severity": "critical",
        }
        result = wf._node_check_intents(state)
        assert result["pipeline_decision"] == "no_action"

        rows = AlertJournal(ts).for_fingerprint("fp-1")
        assert len(rows) == 1
        assert rows[0]["decision"] == "suppressed_by_intent"
        assert "leaf1 maintenance" in rows[0]["reason"]
        assert rows[0]["ref_id"]  # intent id linked
