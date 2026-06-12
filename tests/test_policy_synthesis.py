"""
Unit tests for policy synthesis (shared/policy_synthesizer.py).

Repeated, identical, verified AI resolutions of the same alert pattern are
compiled into DRAFT fast-path policies (created disabled, pending review).
"""
from __future__ import annotations

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))

from shared.task_store import TaskStore
from shared.policy_synthesizer import PolicySynthesizer, DRAFT_PREFIX, _generalize_commands

pytestmark = pytest.mark.unit


@pytest.fixture
def ts(tmp_path):
    return TaskStore(db_path=str(tmp_path / "synth-test.db"))


def _resolved_task(
    ts, device="leaf1", interface="Ethernet1", alertname="InterfaceAdminDown",
    fix_type="config_change", commands=None, resolved=True, fast_path=False,
    device_role="leaf",
):
    commands = commands if commands is not None else f"interface {interface}\n no shutdown"
    task = ts.create_task(
        type="rca", created_by="test", assigned_to="ops_agent",
        title=f"{alertname}: {device}", alert_fingerprint=f"fp-{device}-{interface}",
        content={
            "alertname":   alertname,
            "device":      device,
            "device_role": device_role,
            "commands":    commands,
            "fix_proposal": {
                "fix_type": fix_type, "commands": commands,
                "risk": "low", "confidence": "high",
            },
            "rca": {
                "diagnosis": f"Interface {interface} on {device} is administratively down.",
                "affected_device": device, "action": "no shutdown",
            },
        },
    )
    tid = task["id"]
    if fast_path:
        ts.add_event(tid, "workflow", "fast_path_resolved", {"policy_id": "p0"})
    ts.add_event(tid, "workflow", "execution_verified", {
        "alert_resolved": resolved, "ttr_seconds": 30,
        "alertname": alertname, "device": device,
    })
    return task


def _drafts(ts):
    return [p for p in ts.list_policies() if p["name"].startswith(DRAFT_PREFIX)]


class TestGeneralization:
    def test_device_and_interface_replaced(self):
        out = _generalize_commands("interface Ethernet12\n no shutdown\n! leaf1", "leaf1")
        assert out == "interface {interface}\n no shutdown\n! {device}"

    def test_bgp_commands_pass_through(self):
        assert _generalize_commands("clear ip bgp * soft", "leaf1") == "clear ip bgp * soft"


class TestSynthesis:
    def test_three_identical_successes_create_disabled_draft(self, ts):
        for i, dev in enumerate(("leaf1", "leaf2", "leaf3")):
            _resolved_task(ts, device=dev, interface=f"Ethernet{i+1}")
        created = PolicySynthesizer(ts, min_successes=3).synthesize()
        assert len(created) == 1

        draft = _drafts(ts)[0]
        assert not draft["enabled"]
        assert draft["alertname"] == "InterfaceAdminDown"
        assert draft["autonomy_level"] == "L3"
        fix_tmpl = json.loads(draft["fix_template"])
        assert fix_tmpl["commands"] == "interface {interface}\n no shutdown"
        conditions = json.loads(draft["conditions"])
        assert "{device}" in conditions[0]["query"]
        rca_tmpl = json.loads(draft["rca_template"])
        assert rca_tmpl["affected_device"] == "{device}"

    def test_below_threshold_creates_nothing(self, ts):
        _resolved_task(ts, device="leaf1")
        _resolved_task(ts, device="leaf2")
        assert PolicySynthesizer(ts, min_successes=3).synthesize() == []

    def test_unresolved_tasks_do_not_count(self, ts):
        for dev in ("leaf1", "leaf2", "leaf3"):
            _resolved_task(ts, device=dev, resolved=False)
        assert PolicySynthesizer(ts, min_successes=3).synthesize() == []

    def test_fast_path_resolutions_excluded(self, ts):
        for dev in ("leaf1", "leaf2", "leaf3"):
            _resolved_task(ts, device=dev, fast_path=True)
        assert PolicySynthesizer(ts, min_successes=3).synthesize() == []

    def test_inconsistent_fixes_are_not_compiled(self, ts):
        _resolved_task(ts, device="leaf1")
        _resolved_task(ts, device="leaf2")
        _resolved_task(ts, device="leaf3", commands="shutdown\nno shutdown\nwr mem")
        assert PolicySynthesizer(ts, min_successes=3).synthesize() == []

    def test_unknown_alertname_never_compiled(self, ts):
        for dev in ("leaf1", "leaf2", "leaf3"):
            _resolved_task(ts, device=dev, alertname="MysteryAlert")
        assert PolicySynthesizer(ts, min_successes=3).synthesize() == []

    def test_idempotent_second_run_creates_no_duplicate(self, ts):
        for i, dev in enumerate(("leaf1", "leaf2", "leaf3")):
            _resolved_task(ts, device=dev, interface=f"Ethernet{i+1}")
        synth = PolicySynthesizer(ts, min_successes=3)
        assert len(synth.synthesize()) == 1
        assert synth.synthesize() == []
        assert len(_drafts(ts)) == 1

    def test_existing_fast_path_policy_blocks_synthesis(self, ts):
        ts.create_policy({
            "name": "Operator-written fast path",
            "alertname": "InterfaceAdminDown",
            "device_role": "",
            "autonomy_level": "L3",
            "conditions": json.dumps([{"type": "metric", "query": "x", "expect": "2"}]),
            "tenant_id": "default",
        })
        for dev in ("leaf1", "leaf2", "leaf3"):
            _resolved_task(ts, device=dev)
        assert PolicySynthesizer(ts, min_successes=3).synthesize() == []

    def test_draft_description_records_provenance(self, ts):
        for dev in ("leaf1", "leaf2", "leaf3"):
            _resolved_task(ts, device=dev)
        PolicySynthesizer(ts, min_successes=3).synthesize()
        draft = _drafts(ts)[0]
        assert "3 verified AI resolutions" in draft["description"]
        assert "Disabled until reviewed" in draft["description"]
