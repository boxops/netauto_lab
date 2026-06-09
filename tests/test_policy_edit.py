"""
Tests for policy inline editing.

Unit tests verify the TaskStore update behaviour.
Integration tests (marked integration) exercise the HTTP endpoints and
require the running Docker stack (make start).

Note: the edit route now uses structured form fields (c_type_N, rca_diagnosis, etc.)
assembled by _parse_condition_rows/_parse_rca_template/_parse_fix_template.
Raw conditions/rca_template/fix_template JSON strings are no longer accepted
directly; see tests/test_policy_form_parsing.py for those helpers.

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

    def test_post_edit_with_structured_fields_returns_200(self, session):
        # POSTing structured condition fields to a non-existent policy id still
        # returns 200 (update_policy on unknown id is a no-op) and refreshes the list.
        resp = session.post(
            f"{self._base_url()}/partials/policy-edit/fake-id",
            data={
                "autonomy_level": "L2",
                "min_confidence": "low",
                "max_risk": "high",
                "c_type_0": "metric",
                "c_query_0": "test_metric",
                "c_match_0": "eq",
                "c_value_0": "1",
                "rca_diagnosis": "test diagnosis",
                "rca_confidence": "high",
                "fix_commands": "no shutdown",
                "fix_type_val": "config_change",
                "fix_risk": "low",
                "fix_confidence": "high",
            },
        )
        assert resp.status_code == 200
