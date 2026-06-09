"""
Tests for Phase 5: policy dry-run simulator.

Unit tests verify the PolicyRegistry query + get_fast_path_policies logic
used by the simulate endpoint. Conditions are never executed.

Run: python3 -m pytest tests/test_policy_simulate.py -m unit -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore
from shared.policy_registry import PolicyRegistry
from shared.pipeline_models import AutonomyDecision


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def registry(db):
    reg = PolicyRegistry(db)
    reg.seed_defaults()
    return reg


# ── Unit tests: simulate logic (no HTTP calls, no Prometheus) ─────────────────

@pytest.mark.unit
class TestSimulateGateDecision:
    """
    The simulate endpoint calls registry.query() for the autonomy gate decision.
    These tests exercise that logic directly with seeded policies.
    """

    def test_returns_autonomy_decision_object(self, registry):
        decision = registry.query(
            fix_type="config_change",
            alertname="InterfaceAdminDown",
            device_role="leaf",
            environment="lab",
            confidence="high",
            risk="low",
        )
        assert isinstance(decision, AutonomyDecision)
        assert decision.autonomy_level in {"L0", "L1", "L2", "L3", "L4", "L5"}

    def test_default_l2_when_no_match(self, db):
        """Fresh DB with no policies should fall back to default L2 decision."""
        registry = PolicyRegistry(db)  # empty — no seed
        decision = registry.query(fix_type="no_action", alertname="UnknownAlert")
        assert decision.autonomy_level == "L2"
        assert decision.requires_approval is True
        assert decision.allow_execution  is False

    def test_escalate_human_returns_l1(self, registry):
        decision = registry.query(fix_type="escalate_human")
        assert decision.autonomy_level == "L1"

    def test_l4_does_not_require_approval(self, registry, db):
        """A policy at L4 should set allow_execution=True and requires_approval=False."""
        db.create_policy({
            "name": "Test L4 policy",
            "alertname": "BGPPeerDown",
            "fix_type": "runbook",
            "device_role": "leaf",
            "environment": "lab",
            "autonomy_level": "L4",
            "min_confidence": "high",
            "max_risk": "low",
            "min_prior_successes": 0,
            "tenant_id": "default",
        })
        decision = registry.query(
            fix_type="runbook",
            alertname="BGPPeerDown",
            device_role="leaf",
            environment="lab",
            confidence="high",
            risk="low",
        )
        assert decision.autonomy_level == "L4"
        assert decision.allow_execution  is True
        assert decision.requires_approval is False


@pytest.mark.unit
class TestSimulateFastPathCandidates:
    """
    The simulate endpoint calls registry.get_fast_path_policies() to find
    fast-path candidates. Conditions are returned for display, never executed.
    """

    def test_returns_fast_path_candidates_for_matching_alertname(self, registry):
        candidates = registry.get_fast_path_policies("InterfaceAdminDown")
        assert len(candidates) >= 1
        assert all(c["conditions"] for c in candidates)

    def test_returns_empty_for_unknown_alertname(self, registry):
        candidates = registry.get_fast_path_policies("UnknownAlertXYZ")
        # No seed policy has conditions for an unknown alert
        assert len(candidates) == 0

    def test_returns_bgp_candidates_for_leaf(self, registry):
        candidates = registry.get_fast_path_policies("BGPPeerDown", device_role="leaf")
        assert len(candidates) >= 1

    def test_conditions_are_parseable_json(self, registry):
        """Conditions returned for display must be valid JSON strings."""
        candidates = registry.get_fast_path_policies("InterfaceAdminDown")
        for c in candidates:
            parsed = json.loads(c["conditions"])
            assert isinstance(parsed, list)
            for cond in parsed:
                assert "type" in cond

    def test_conditions_never_executed(self, registry, db):
        """
        Simulate logic must never call _prometheus_instant or any HTTP call.
        We verify by ensuring the method only reads from the DB and returns data.
        """
        from unittest.mock import patch
        with patch("shared.policy_resolver._prometheus_instant") as mock_prom, \
             patch("httpx.get") as mock_http:
            candidates = registry.get_fast_path_policies("InterfaceAdminDown")
            # get_fast_path_policies is a pure DB read — no HTTP should be called
            assert mock_prom.call_count == 0
            assert mock_http.call_count == 0

    def test_custom_fast_path_policy_appears_in_candidates(self, db):
        """Custom policies with conditions should appear alongside seed policies."""
        db.create_policy({
            "name": "Custom InterfaceDown policy",
            "alertname": "InterfaceDown",
            "fix_type": "config_change",
            "device_role": "spine",
            "environment": "lab",
            "autonomy_level": "L3",
            "tenant_id": "default",
            "conditions": json.dumps([{"type": "metric", "query": "custom_metric", "expect": "1"}]),
            "rca_template": json.dumps({"diagnosis": "test", "confidence": "high"}),
            "fix_template": json.dumps({"fix_type": "config_change", "commands": "test"}),
        })
        registry = PolicyRegistry(db)
        candidates = registry.get_fast_path_policies("InterfaceDown", device_role="spine")
        names = [c["name"] for c in candidates]
        assert "Custom InterfaceDown policy" in names

    def test_wildcard_policy_matches_any_device_role(self, db):
        """InterfaceAdminDown fast-path seed has device_role='' (wildcard)."""
        reg = PolicyRegistry(db)
        reg.seed_defaults()
        candidates_leaf  = reg.get_fast_path_policies("InterfaceAdminDown", device_role="leaf")
        candidates_spine = reg.get_fast_path_policies("InterfaceAdminDown", device_role="spine")
        candidates_any   = reg.get_fast_path_policies("InterfaceAdminDown", device_role="")
        assert len(candidates_leaf)  >= 1
        assert len(candidates_spine) >= 1
        assert len(candidates_any)   >= 1
