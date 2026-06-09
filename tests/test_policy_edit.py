"""
Tests for Phase 4: policy inline editing.

Unit tests verify the TaskStore update behaviour and JSON validation logic.
Integration tests (marked integration) exercise the HTTP endpoints and
require the running Docker stack (make start).

Run unit tests only: python3 -m pytest tests/test_policy_edit.py -m unit -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


def _create_policy(db: TaskStore, **overrides) -> dict:
    base = {
        "name":           "Test policy",
        "alertname":      "InterfaceDown",
        "fix_type":       "config_change",
        "device_role":    "leaf",
        "environment":    "lab",
        "autonomy_level": "L2",
        "tenant_id":      "default",
    }
    return db.create_policy({**base, **overrides})


# ── Unit tests: TaskStore update_policy ──────────────────────────────────────

@pytest.mark.unit
class TestPolicyUpdateFields:
    def test_update_autonomy_level(self, db):
        p = _create_policy(db)
        db.update_policy(p["id"], {"autonomy_level": "L4"})
        updated = db.get_policy(p["id"])
        assert updated["autonomy_level"] == "L4"

    def test_update_conditions(self, db):
        p = _create_policy(db)
        conditions = json.dumps([{"type": "metric", "query": "test_metric", "expect": "1"}])
        db.update_policy(p["id"], {"conditions": conditions})
        updated = db.get_policy(p["id"])
        assert updated["conditions"] == conditions

    def test_clear_conditions_with_none(self, db):
        conditions = json.dumps([{"type": "metric", "query": "test", "expect": "1"}])
        p = _create_policy(db, conditions=conditions)
        db.update_policy(p["id"], {"conditions": None})
        updated = db.get_policy(p["id"])
        assert not updated.get("conditions")

    def test_update_rca_and_fix_templates(self, db):
        p = _create_policy(db)
        rca = json.dumps({"diagnosis": "test diagnosis", "confidence": "high"})
        fix = json.dumps({"fix_type": "config_change", "commands": "no shutdown"})
        db.update_policy(p["id"], {"rca_template": rca, "fix_template": fix})
        updated = db.get_policy(p["id"])
        assert updated["rca_template"] == rca
        assert updated["fix_template"] == fix

    def test_update_min_confidence_and_max_risk(self, db):
        p = _create_policy(db)
        db.update_policy(p["id"], {"min_confidence": "high", "max_risk": "low"})
        updated = db.get_policy(p["id"])
        assert updated["min_confidence"] == "high"
        assert updated["max_risk"]       == "low"

    def test_update_description(self, db):
        p = _create_policy(db)
        db.update_policy(p["id"], {"description": "Updated description"})
        updated = db.get_policy(p["id"])
        assert updated["description"] == "Updated description"

    def test_update_does_not_affect_other_fields(self, db):
        p = _create_policy(db, alertname="BGPPeerDown", device_role="spine")
        db.update_policy(p["id"], {"autonomy_level": "L3"})
        updated = db.get_policy(p["id"])
        assert updated["alertname"]   == "BGPPeerDown"
        assert updated["device_role"] == "spine"


# ── Unit tests: JSON validation logic (pure Python, no HTTP) ─────────────────

@pytest.mark.unit
class TestJsonValidationLogic:
    """
    Test the same validation logic used in partial_policy_edit_save:
    valid JSON strings should parse, invalid ones should raise JSONDecodeError.
    """

    def test_valid_conditions_json_parses(self):
        conditions = '[{"type":"metric","query":"test","expect":"2"}]'
        parsed = json.loads(conditions)
        assert isinstance(parsed, list)

    def test_valid_rca_template_parses(self):
        rca = '{"diagnosis":"test","confidence":"high","affected_device":"{device}"}'
        parsed = json.loads(rca)
        assert parsed["confidence"] == "high"

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads("not-valid-json")

    def test_empty_string_is_valid_empty(self):
        # Empty string maps to None in the update — should not be json.loads'd
        assert "".strip() == ""
        # In the route: conditions.strip() or None → None (no json.loads called)
        assert ("".strip() or None) is None

    def test_whitespace_only_maps_to_none(self):
        assert ("   ".strip() or None) is None


# ── Integration tests (require running stack) ─────────────────────────────────

@pytest.mark.integration
class TestPolicyEditEndpoints:
    """
    Requires: make start (full Docker stack with ui container on port 7860).
    """

    @pytest.fixture
    def session(self):
        import requests
        return requests.Session()

    def _base_url(self) -> str:
        return "http://localhost:7860"

    def test_get_edit_form_returns_200_for_unknown_id(self, session):
        resp = session.get(f"{self._base_url()}/partials/policy-edit/does-not-exist")
        assert resp.status_code == 200
        assert "not found" in resp.text.lower()

    def test_post_edit_rejects_invalid_conditions_json(self, session):
        # Use a known policy id from the seeded defaults; or test without a real id
        resp = session.post(
            f"{self._base_url()}/partials/policy-edit/fake-id",
            data={"autonomy_level": "L2", "conditions": "not-valid-json"},
        )
        assert resp.status_code == 200
        assert "Invalid JSON" in resp.text
