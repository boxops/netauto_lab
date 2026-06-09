"""
Tests for Phase 1: fast-path seed policies and expect_ne metric condition.

All tests are unit tests (no running stack required).
Run: python3 -m pytest tests/test_fast_path_seed.py -m unit -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore
from shared.policy_registry import PolicyRegistry, _DEFAULT_SEED
from shared.policy_resolver import PolicyResolver, ResolverResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def registry(db):
    return PolicyRegistry(db)


@pytest.fixture
def resolver():
    return PolicyResolver()


# ── Helper ────────────────────────────────────────────────────────────────────

def _make_alert(alertname: str, device: str = "leaf1", interface: str = "Ethernet1") -> dict:
    return {
        "labels": {
            "alertname": alertname,
            "sysName":   device,
            "ifDescr":   interface,
            "severity":  "critical",
            "instance":  "172.20.20.2:161",
        },
        "fingerprint": f"fp-{alertname}-001",
    }


# ── Seed policy presence ──────────────────────────────────────────────────────

@pytest.mark.unit
class TestSeedPoliciesHaveFastPath:
    def test_interface_admin_down_seed_has_conditions(self, registry, db):
        registry.seed_defaults()
        candidates = registry.get_fast_path_policies("InterfaceAdminDown")
        assert len(candidates) >= 1
        assert candidates[0]["conditions"] is not None

    def test_bgp_peer_down_seed_has_conditions(self, registry, db):
        registry.seed_defaults()
        candidates = registry.get_fast_path_policies("BGPPeerDown")
        assert len(candidates) >= 1
        assert candidates[0]["conditions"] is not None

    def test_default_seed_list_has_fast_path_entries(self):
        fast_path = [s for s in _DEFAULT_SEED if s.get("conditions")]
        assert len(fast_path) >= 2
        alertnames = {s["alertname"] for s in fast_path}
        assert "InterfaceAdminDown" in alertnames
        assert "BGPPeerDown" in alertnames

    def test_seed_conditions_are_valid_json(self):
        for seed in _DEFAULT_SEED:
            if seed.get("conditions"):
                parsed = json.loads(seed["conditions"])
                assert isinstance(parsed, list)
                assert len(parsed) >= 1
            if seed.get("rca_template"):
                parsed = json.loads(seed["rca_template"])
                assert isinstance(parsed, dict)
            if seed.get("fix_template"):
                parsed = json.loads(seed["fix_template"])
                assert isinstance(parsed, dict)


# ── Backfill for existing deployments ────────────────────────────────────────

@pytest.mark.unit
class TestBackfillFastPathFields:
    def _create_seeds_without_conditions(self, db: TaskStore) -> None:
        """Simulate a pre-existing deployment: seed policies have no conditions."""
        for seed in _DEFAULT_SEED:
            stripped = {k: v for k, v in seed.items()
                        if k not in ("conditions", "rca_template", "fix_template")}
            db.create_policy({**stripped, "tenant_id": "default"})

    def test_backfill_populates_conditions_on_existing_seeds(self, db):
        self._create_seeds_without_conditions(db)
        registry = PolicyRegistry(db)
        registry.seed_defaults()  # should detect existing and backfill
        fast = registry.get_fast_path_policies("InterfaceAdminDown")
        assert len(fast) >= 1
        assert fast[0]["conditions"] is not None

    def test_backfill_populates_bgp_peer_down(self, db):
        self._create_seeds_without_conditions(db)
        registry = PolicyRegistry(db)
        registry.seed_defaults()
        fast = registry.get_fast_path_policies("BGPPeerDown")
        assert len(fast) >= 1
        assert fast[0]["conditions"] is not None

    def test_backfill_does_not_overwrite_custom_conditions(self, db):
        custom_conditions = json.dumps([{"type": "metric", "query": "custom_query", "expect": "1"}])
        # Create the seed policy entry but with custom conditions already set
        db.create_policy({
            "name": "InterfaceAdminDown fast path — any device",
            "alertname": "InterfaceAdminDown",
            "fix_type": "config_change",
            "conditions": custom_conditions,
            "rca_template": json.dumps({"diagnosis": "custom", "confidence": "high"}),
            "fix_template": json.dumps({"fix_type": "config_change", "commands": "custom"}),
            "autonomy_level": "L3",
            "tenant_id": "default",
        })
        registry = PolicyRegistry(db)
        registry.seed_defaults()
        policies = db.list_policies(tenant_id="default")
        match = next(p for p in policies if p["name"] == "InterfaceAdminDown fast path — any device")
        assert json.loads(match["conditions"])[0]["query"] == "custom_query"

    def test_seed_defaults_returns_zero_on_second_call(self, db):
        registry = PolicyRegistry(db)
        count1 = registry.seed_defaults()
        count2 = registry.seed_defaults()  # second call on non-empty table
        assert count1 > 0
        assert count2 == 0


# ── expect_ne metric condition ────────────────────────────────────────────────

@pytest.mark.unit
class TestMetricExpectNe:
    def test_passes_when_value_differs_from_expect_ne(self, resolver):
        cond = {"type": "metric", "query": "bgp_peer_state{sysName=\"leaf1\"}", "expect_ne": "6"}
        ctx  = {"device": "leaf1", "interface": ""}
        with patch("shared.policy_resolver._prometheus_instant", return_value="3"):
            assert resolver._check_metric(cond, ctx) is True

    def test_fails_when_value_equals_expect_ne(self, resolver):
        cond = {"type": "metric", "query": "bgp_peer_state{sysName=\"leaf1\"}", "expect_ne": "6"}
        ctx  = {"device": "leaf1", "interface": ""}
        with patch("shared.policy_resolver._prometheus_instant", return_value="6"):
            assert resolver._check_metric(cond, ctx) is False

    def test_fails_on_empty_value(self, resolver):
        """Empty Prometheus response (series not found) must not satisfy expect_ne."""
        cond = {"type": "metric", "query": "bgp_peer_state{sysName=\"leaf1\"}", "expect_ne": "6"}
        ctx  = {"device": "leaf1", "interface": ""}
        with patch("shared.policy_resolver._prometheus_instant", return_value=""):
            assert resolver._check_metric(cond, ctx) is False

    def test_expect_ne_ignored_when_expect_also_set(self, resolver):
        """expect takes precedence over expect_ne when both are provided."""
        cond = {"type": "metric", "query": "some_metric", "expect": "2", "expect_ne": "6"}
        ctx  = {}
        with patch("shared.policy_resolver._prometheus_instant", return_value="2"):
            assert resolver._check_metric(cond, ctx) is True
        with patch("shared.policy_resolver._prometheus_instant", return_value="3"):
            assert resolver._check_metric(cond, ctx) is False


# ── InterfaceAdminDown fast path end-to-end ───────────────────────────────────

@pytest.mark.unit
class TestInterfaceAdminDownFastPath:
    def _make_policy(self, db: TaskStore) -> dict:
        db.create_policy({
            "name": "InterfaceAdminDown fast path — any device",
            "alertname": "InterfaceAdminDown",
            "fix_type": "config_change",
            "device_role": "",
            "environment": "",
            "min_confidence": "high",
            "max_risk": "low",
            "autonomy_level": "L3",
            "tenant_id": "default",
            "conditions": json.dumps([
                {
                    "type": "metric",
                    "query": 'interface_ifAdminStatus{{ifDescr="{interface}",sysName="{device}"}}',
                    "expect": "2",
                }
            ]),
            "rca_template": json.dumps({
                "diagnosis": "Interface {interface} on {device} is admin-down.",
                "confidence": "high",
                "affected_device": "{device}",
                "action": "no shutdown",
                "upstream_cause": "",
                "is_leaf_symptom": False,
            }),
            "fix_template": json.dumps({
                "fix_type": "config_change",
                "commands": "interface {interface}\n no shutdown",
                "risk": "low",
                "confidence": "high",
                "reason": "Restore admin-down interface {interface} on {device}.",
            }),
        })
        return db.list_policies()[0]

    @patch("shared.policy_resolver._prometheus_instant", return_value="2")
    def test_resolves_when_metric_confirms_admin_down(self, _mock_prom, db, resolver):
        self._make_policy(db)
        registry = PolicyRegistry(db)
        candidates = registry.get_fast_path_policies("InterfaceAdminDown")
        assert candidates, "Expected at least one fast-path candidate"
        alert = _make_alert("InterfaceAdminDown", device="spine2", interface="Ethernet1")
        result = resolver.resolve(alert, candidates[0])
        assert isinstance(result, ResolverResult)
        assert "no shutdown" in result.fix["commands"]
        assert result.rca["confidence"] == "high"

    @patch("shared.policy_resolver._prometheus_instant", return_value="1")
    def test_falls_through_when_interface_not_admin_down(self, _mock_prom, db, resolver):
        self._make_policy(db)
        registry = PolicyRegistry(db)
        candidates = registry.get_fast_path_policies("InterfaceAdminDown")
        alert = _make_alert("InterfaceAdminDown", device="spine2", interface="Ethernet1")
        result = resolver.resolve(alert, candidates[0])
        assert result is None

    @patch("shared.policy_resolver._prometheus_instant", return_value="2")
    def test_template_variables_substituted(self, _mock_prom, db, resolver):
        self._make_policy(db)
        candidates = PolicyRegistry(db).get_fast_path_policies("InterfaceAdminDown")
        alert = _make_alert("InterfaceAdminDown", device="leaf3", interface="Ethernet5")
        result = resolver.resolve(alert, candidates[0])
        assert "leaf3" in result.rca["diagnosis"]
        assert "Ethernet5" in result.fix["commands"]


# ── BGPPeerDown fast path end-to-end ─────────────────────────────────────────

@pytest.mark.unit
class TestBGPPeerDownFastPath:
    def _make_policy(self, db: TaskStore) -> None:
        db.create_policy({
            "name": "BGPPeerDown fast path — lab leaf",
            "alertname": "BGPPeerDown",
            "fix_type": "runbook",
            "device_role": "leaf",
            "environment": "lab",
            "min_confidence": "high",
            "max_risk": "low",
            "autonomy_level": "L4",
            "tenant_id": "default",
            "conditions": json.dumps([
                {
                    "type": "metric",
                    "query": 'bgp_peer_bgpPeerState{{sysName="{device}"}}',
                    "expect_ne": "6",
                }
            ]),
            "rca_template": json.dumps({
                "diagnosis": "BGP session on {device} is not Established.",
                "confidence": "high",
                "affected_device": "{device}",
                "action": "clear bgp neighbor",
                "upstream_cause": "",
                "is_leaf_symptom": False,
            }),
            "fix_template": json.dumps({
                "fix_type": "runbook",
                "commands": "clear ip bgp * soft",
                "risk": "low",
                "confidence": "high",
                "reason": "BGP soft-clear on {device}.",
            }),
        })

    @patch("shared.policy_resolver._prometheus_instant", return_value="3")
    def test_resolves_when_session_not_established(self, _mock_prom, db, resolver):
        self._make_policy(db)
        candidates = PolicyRegistry(db).get_fast_path_policies("BGPPeerDown", device_role="leaf")
        assert candidates
        alert = _make_alert("BGPPeerDown", device="leaf1")
        result = resolver.resolve(alert, candidates[0])
        assert isinstance(result, ResolverResult)
        assert "bgp" in result.fix["commands"].lower()

    @patch("shared.policy_resolver._prometheus_instant", return_value="6")
    def test_falls_through_when_session_established(self, _mock_prom, db, resolver):
        """If session is Established (6), the alert may be a false alarm — don't act."""
        self._make_policy(db)
        candidates = PolicyRegistry(db).get_fast_path_policies("BGPPeerDown", device_role="leaf")
        alert = _make_alert("BGPPeerDown", device="leaf1")
        result = resolver.resolve(alert, candidates[0])
        assert result is None

    @patch("shared.policy_resolver._prometheus_instant", return_value="3")
    def test_device_variable_substituted_in_reason(self, _mock_prom, db, resolver):
        self._make_policy(db)
        candidates = PolicyRegistry(db).get_fast_path_policies("BGPPeerDown", device_role="leaf")
        alert = _make_alert("BGPPeerDown", device="leaf2")
        result = resolver.resolve(alert, candidates[0])
        assert "leaf2" in result.fix["reason"]
