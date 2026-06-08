"""
Programmatic fast-path tests for Autonomy Policy conditions + template rendering.

Unit tests require no running services (mock HTTP via unittest.mock).
Integration tests require the full stack (make start) and are marked @pytest.mark.integration.

Run unit tests only:
    python3 -m pytest tests/test_policy_fast_path.py -m unit -v

Run all (requires running stack):
    python3 -m pytest tests/test_policy_fast_path.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore
from shared.policy_registry import PolicyRegistry
from shared.policy_resolver import PolicyResolver, ResolverResult


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def registry(db):
    return PolicyRegistry(db)


@pytest.fixture
def resolver():
    return PolicyResolver()


# Sample alert event (matches the shape alert_poller injects)
SAMPLE_ALERT = {
    "labels": {
        "alertname": "InterfaceDown",
        "sysName": "spine2",
        "ifDescr": "Ethernet1",
        "severity": "critical",
        "instance": "172.20.20.2:161",
    },
    "annotations": {"description": "Interface Ethernet1 on spine2 is down"},
    "fingerprint": "fp-fast-path-001",
}

_CONDITIONS = json.dumps([
    {"type": "metric", "query": "interface_ifOperStatus{ifDescr=\"Ethernet1\"}", "expect": "2"}
])
_RCA_TMPL = json.dumps({
    "diagnosis": "{interface} on {device} is administratively down",
    "confidence": "certain",
    "affected_device": "{device}",
    "upstream_cause": "",
    "is_leaf_symptom": False,
})
_FIX_TMPL = json.dumps({
    "fix_type": "config_change",
    "commands": "interface {interface}\n no shutdown",
    "risk": "low",
    "reason": "Restore admin-down interface {interface} on {device}",
    "confidence": "certain",
})


def _fast_policy(db: TaskStore, **overrides) -> dict:
    base = dict(
        name="InterfaceDown fast path",
        alertname="InterfaceDown",
        fix_type="config_change",
        device_role="spine",
        environment="lab",
        autonomy_level="L3",
        conditions=_CONDITIONS,
        rca_template=_RCA_TMPL,
        fix_template=_FIX_TMPL,
        tenant_id="default",
    )
    base.update(overrides)
    return db.create_policy(base)


# ── TestPolicySchema ──────────────────────────────────────────────────────────

@pytest.mark.unit
class TestPolicySchema:
    def test_create_policy_with_conditions(self, db):
        p = _fast_policy(db)
        stored = db.get_policy(p["id"])
        assert stored["conditions"] == _CONDITIONS
        assert stored["rca_template"] == _RCA_TMPL
        assert stored["fix_template"] == _FIX_TMPL

    def test_create_policy_without_conditions_is_valid(self, db):
        p = db.create_policy(dict(
            name="Legacy gate",
            fix_type="runbook",
            autonomy_level="L2",
            tenant_id="default",
        ))
        stored = db.get_policy(p["id"])
        assert stored["conditions"] is None
        assert stored["rca_template"] is None
        assert stored["fix_template"] is None

    def test_update_policy_adds_conditions(self, db):
        p = db.create_policy(dict(name="Plain policy", autonomy_level="L2", tenant_id="default"))
        db.update_policy(p["id"], {"conditions": _CONDITIONS, "rca_template": _RCA_TMPL, "fix_template": _FIX_TMPL})
        stored = db.get_policy(p["id"])
        assert stored["conditions"] == _CONDITIONS
        assert json.loads(stored["rca_template"])["confidence"] == "certain"


# ── TestPolicyResolver ────────────────────────────────────────────────────────

@pytest.mark.unit
class TestPolicyResolver:
    def test_no_conditions_returns_none(self, resolver, db):
        p = db.create_policy(dict(name="Gate only", autonomy_level="L2", tenant_id="default"))
        assert resolver.resolve(SAMPLE_ALERT, p) is None

    @patch("shared.policy_resolver._prometheus_instant", return_value="2")
    def test_metric_condition_passes(self, mock_prom, resolver, db):
        p = _fast_policy(db)
        result = resolver.resolve(SAMPLE_ALERT, p)
        assert isinstance(result, ResolverResult)
        assert result.policy_id == p["id"]
        assert result.rca["confidence"] == "certain"
        assert "Ethernet1" in result.rca["diagnosis"]
        assert "spine2" in result.rca["diagnosis"]

    @patch("shared.policy_resolver._prometheus_instant", return_value="1")
    def test_metric_condition_fails(self, mock_prom, resolver, db):
        p = _fast_policy(db)
        assert resolver.resolve(SAMPLE_ALERT, p) is None

    @patch("shared.policy_resolver._run_show", return_value="Ethernet1      disabled")
    def test_show_command_condition_passes(self, mock_show, resolver, db):
        conds = json.dumps([{"type": "show_command", "command": "show interfaces {interface} status", "expect_contains": "disabled"}])
        p = _fast_policy(db, conditions=conds)
        result = resolver.resolve(SAMPLE_ALERT, p)
        assert result is not None
        mock_show.assert_called_once_with("spine2", "show interfaces Ethernet1 status")

    @patch("shared.policy_resolver._run_show", return_value="Ethernet1      connected")
    def test_show_command_condition_fails(self, mock_show, resolver, db):
        conds = json.dumps([{"type": "show_command", "command": "show interfaces {interface} status", "expect_contains": "disabled"}])
        p = _fast_policy(db, conditions=conds)
        assert resolver.resolve(SAMPLE_ALERT, p) is None

    @patch("shared.policy_resolver._prometheus_instant", return_value="2")
    def test_template_context_substitution(self, mock_prom, resolver, db):
        p = _fast_policy(db)
        result = resolver.resolve(SAMPLE_ALERT, p)
        assert result.rca["affected"] == "spine2"
        assert "Ethernet1" in result.fix["commands"]
        assert "spine2" in result.fix["reason"]

    @patch("shared.policy_resolver._prometheus_instant")
    def test_multiple_conditions_all_must_pass(self, mock_prom, resolver, db):
        mock_prom.side_effect = ["2", "1"]  # first passes, second fails
        conds = json.dumps([
            {"type": "metric", "query": "q1", "expect": "2"},
            {"type": "metric", "query": "q2", "expect": "2"},
        ])
        p = _fast_policy(db, conditions=conds)
        assert resolver.resolve(SAMPLE_ALERT, p) is None

    @patch("shared.policy_resolver._prometheus_instant", side_effect=Exception("connection refused"))
    def test_resolver_exception_returns_none(self, mock_prom, resolver, db):
        p = _fast_policy(db)
        # Must not raise — exception is caught and treated as no-match
        result = resolver.resolve(SAMPLE_ALERT, p)
        assert result is None

    def test_invalid_conditions_json_returns_none(self, resolver, db):
        p = db.create_policy(dict(
            name="Bad JSON policy", autonomy_level="L2", tenant_id="default",
            conditions="not-valid-json",
            rca_template=_RCA_TMPL,
            fix_template=_FIX_TMPL,
        ))
        assert resolver.resolve(SAMPLE_ALERT, p) is None


# ── TestPolicyRegistryFastPath ────────────────────────────────────────────────

@pytest.mark.unit
class TestPolicyRegistryFastPath:
    def test_get_fast_path_policies_filters_by_alertname(self, registry, db):
        _fast_policy(db)                                # alertname=InterfaceDown + conditions
        _fast_policy(db, alertname="BGPPeerDown")       # wrong alertname
        db.create_policy(dict(name="Gate only", alertname="InterfaceDown", autonomy_level="L2", tenant_id="default"))  # no conditions
        result = registry.get_fast_path_policies("InterfaceDown")
        assert len(result) == 1
        assert result[0]["alertname"] == "InterfaceDown"

    def test_get_fast_path_policies_wildcard_alertname(self, registry, db):
        _fast_policy(db, alertname="")   # wildcard (empty alertname)
        result = registry.get_fast_path_policies("AnyAlert")
        assert len(result) == 1

    def test_get_fast_path_policies_disabled_excluded(self, registry, db):
        p = _fast_policy(db)
        db.update_policy(p["id"], {"enabled": 0})
        result = registry.get_fast_path_policies("InterfaceDown")
        assert result == []

    def test_specific_alertname_before_wildcard(self, registry, db):
        _fast_policy(db, alertname="", name="Wildcard fast")    # wildcard
        _fast_policy(db, alertname="InterfaceDown", name="Specific fast")  # specific
        result = registry.get_fast_path_policies("InterfaceDown")
        assert len(result) == 2
        assert result[0]["alertname"] == "InterfaceDown"  # specific first


# ── TestWorkflowFastPathRouting ───────────────────────────────────────────────

@pytest.mark.unit
class TestWorkflowFastPathRouting:
    """
    Test _node_policy_fast_path in isolation by injecting a mock PolicyResolver
    and a real TaskStore.
    """

    def _make_workflow(self, db):
        """Import and wire up only enough of IncidentWorkflow to test the node."""
        import unittest.mock as mock_module

        # Patch LangChain + tools so IncidentWorkflow.__init__ doesn't need a running LLM
        with mock_module.patch("ops_agent.workflow.create_react_agent", return_value=MagicMock()):
            with mock_module.patch("ops_agent.workflow.OPS_TOOLS", []):
                from ops_agent.workflow import IncidentWorkflow
                wf = IncidentWorkflow.__new__(IncidentWorkflow)
                wf._ts = db
                wf._policy_resolver = PolicyResolver()
                wf._policy_registry = PolicyRegistry(db)
                return wf

    def _base_state(self) -> dict:
        return {
            "alertname": "InterfaceDown",
            "tenant_id": "default",
            "rca_task_id": None,
            "event": SAMPLE_ALERT,
            "pipeline_decision": None,
            "fast_path_policy_id": None,
        }

    @patch("shared.policy_resolver._prometheus_instant", return_value="2")
    def test_fast_path_resolved_skips_investigate(self, mock_prom, db):
        _fast_policy(db)
        wf = self._make_workflow(db)
        state = self._base_state()
        result = wf._node_policy_fast_path(state)
        assert result["pipeline_decision"] == "fast_path_resolved"
        assert result["rca"]["confidence"] == "certain"
        assert "fix_proposal" in result

    @patch("shared.policy_resolver._prometheus_instant", return_value="1")
    def test_fast_path_not_resolved_routes_to_investigate(self, mock_prom, db):
        _fast_policy(db)
        wf = self._make_workflow(db)
        result = wf._node_policy_fast_path(self._base_state())
        assert result["pipeline_decision"] is None

    @patch("shared.policy_resolver._prometheus_instant", return_value="2")
    def test_fast_path_node_records_event_on_resolution(self, mock_prom, db):
        task = db.create_task(
            type="rca", created_by="test", assigned_to="ops_agent",
            title="InterfaceDown", alert_fingerprint="fp-wf-test",
            content={},
        )
        _fast_policy(db)
        wf = self._make_workflow(db)
        state = {**self._base_state(), "rca_task_id": task["id"]}
        wf._node_policy_fast_path(state)
        events = db.get_task_events(task["id"])
        event_types = [e["event_type"] for e in events]
        assert "fast_path_resolved" in event_types

    def test_no_fast_path_policies_returns_none_decision(self, db):
        # Only a gate-style policy (no conditions)
        db.create_policy(dict(name="Gate only", alertname="InterfaceDown", autonomy_level="L2", tenant_id="default"))
        wf = self._make_workflow(db)
        result = wf._node_policy_fast_path(self._base_state())
        assert result["pipeline_decision"] is None


# ── Integration tests ─────────────────────────────────────────────────────────

@pytest.mark.integration
class TestFastPathIntegration:
    """
    Require `make start` — ops-agent on :8000, UI on :7860.
    """

    OPS_URL = "http://localhost:8000"
    UI_URL  = "http://localhost:7860"

    def test_fast_path_policy_end_to_end(self):
        import time
        import httpx

        # Create policy with conditions via ops-agent REST API
        policy_resp = httpx.post(f"{self.OPS_URL}/policies", json={
            "name": "e2e fast-path test",
            "alertname": "InterfaceDown",
            "fix_type": "config_change",
            "autonomy_level": "L3",
            "conditions": json.dumps([
                {"type": "metric", "query": "up", "expect": "1"}  # always passes
            ]),
            "rca_template": _RCA_TMPL,
            "fix_template": _FIX_TMPL,
        }, timeout=10)
        assert policy_resp.status_code == 200, policy_resp.text
        policy_id = policy_resp.json()["id"]

        try:
            # Inject alert webhook
            webhook_resp = httpx.post(f"{self.OPS_URL}/webhook/alert", json={
                "alerts": [{
                    "labels": {"alertname": "InterfaceDown", "sysName": "spine2",
                               "ifDescr": "Ethernet1", "severity": "critical", "instance": "172.20.20.2:161"},
                    "annotations": {"description": "Test alert for fast-path e2e"},
                    "fingerprint": "fp-e2e-fast-path-001",
                    "status": "firing",
                }]
            }, timeout=10)
            assert webhook_resp.status_code == 200

            # Poll for the rca task to reach awaiting_approval (fast path should be quick)
            deadline = time.time() + 60
            task_id = None
            while time.time() < deadline:
                tasks_resp = httpx.get(f"{self.OPS_URL}/tasks", params={"type": "rca"}, timeout=5)
                for t in tasks_resp.json():
                    if t.get("alert_fingerprint") == "fp-e2e-fast-path-001":
                        task_id = t["id"]
                        if t["status"] == "awaiting_approval":
                            content = t.get("content") or {}
                            if isinstance(content, str):
                                content = json.loads(content)
                            assert content.get("confidence") == "certain", \
                                f"Expected 'certain' confidence from template, got: {content.get('confidence')}"
                            return
                time.sleep(2)
            pytest.fail(f"Task {task_id} never reached awaiting_approval within 60s")
        finally:
            httpx.delete(f"{self.OPS_URL}/policies/{policy_id}", timeout=5)

    def test_fast_path_fallback_to_ai(self):
        import time
        import httpx

        # Policy with a condition that will never match (metric value "999")
        policy_resp = httpx.post(f"{self.OPS_URL}/policies", json={
            "name": "e2e fallback test",
            "alertname": "InterfaceDown",
            "fix_type": "config_change",
            "autonomy_level": "L3",
            "conditions": json.dumps([
                {"type": "metric", "query": "up", "expect": "999"}  # will never match
            ]),
            "rca_template": _RCA_TMPL,
            "fix_template": _FIX_TMPL,
        }, timeout=10)
        assert policy_resp.status_code == 200
        policy_id = policy_resp.json()["id"]

        try:
            webhook_resp = httpx.post(f"{self.OPS_URL}/webhook/alert", json={
                "alerts": [{
                    "labels": {"alertname": "InterfaceDown", "sysName": "spine2",
                               "ifDescr": "Ethernet1", "severity": "critical", "instance": "172.20.20.2:161"},
                    "annotations": {"description": "Test alert for fallback e2e"},
                    "fingerprint": "fp-e2e-fallback-001",
                    "status": "firing",
                }]
            }, timeout=10)
            assert webhook_resp.status_code == 200

            # Poll for the task — it should go through AI and have non-"certain" confidence
            deadline = time.time() + 300
            task_id = None
            while time.time() < deadline:
                tasks_resp = httpx.get(f"{self.OPS_URL}/tasks", params={"type": "rca"}, timeout=5)
                for t in tasks_resp.json():
                    if t.get("alert_fingerprint") == "fp-e2e-fallback-001":
                        task_id = t["id"]
                        if t["status"] in ("awaiting_approval", "complete"):
                            content = t.get("content") or {}
                            if isinstance(content, str):
                                content = json.loads(content)
                            # AI path should NOT produce "certain" confidence (that's template-only)
                            assert content.get("confidence") != "certain", \
                                "Fast path fired when conditions should have failed"
                            return
                time.sleep(5)
            pytest.fail(f"Task {task_id} never reached awaiting_approval within 300s")
        finally:
            httpx.delete(f"{self.OPS_URL}/policies/{policy_id}", timeout=5)
