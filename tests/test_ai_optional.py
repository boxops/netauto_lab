"""
Tests for Phase 2: AI-optional mode (ai_enabled setting).

When ai_enabled=False, the pipeline only runs programmatic fast-path policies.
Alerts with no matching fast-path policy are placed in awaiting_approval with
a no_ai_skipped task event — they never reach the LLM investigation node.

Run: python3 -m pytest tests/test_ai_optional.py -m unit -v
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


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


def _make_incident_state(db: TaskStore, rca_task_id: str | None = None) -> dict:
    return {
        "alertname":          "InterfaceDown",
        "severity":           "critical",
        "device":             "leaf1",
        "instance":           "172.20.20.3:161",
        "summary":            "Interface down on leaf1",
        "description":        "",
        "fingerprint":        "fp-ai-optional-001",
        "event":              {},
        "blast_radius":       [],
        "is_leaf_symptom":    False,
        "intent_match":       None,
        "rca_task_id":        rca_task_id,
        "rca":                None,
        "fix_proposal":       None,
        "validation":         None,
        "pipeline_decision":  None,
        "fast_path_policy_id": None,
        "in_maintenance":     False,
        "do_not_auto_execute": False,
        "priority":           "high",
        "session_id":         "sess-001",
        "tenant_id":          "default",
        "incident_id":        None,
        "error":              None,
    }


# ── Routing behaviour ─────────────────────────────────────────────────────────

@pytest.mark.unit
class TestRouteAfterFastPath:
    """
    _route_after_fast_path is an instance method on IncidentWorkflow.
    We test it directly to avoid building the full LangGraph.
    """

    def _make_workflow(self, db: TaskStore) -> object:
        from unittest.mock import MagicMock
        from shared.rate_limiter import RateLimiter
        from shared.status_tracker import StatusCallbackHandler

        rate_limiter    = MagicMock(spec=RateLimiter)
        status_handler  = MagicMock(spec=StatusCallbackHandler)
        status_handler.clear_context = MagicMock()

        with patch("ops_agent.workflow.get_llm"), \
             patch("ops_agent.workflow.TopologyCorrelator"), \
             patch("ops_agent.workflow.IntentRegistry"), \
             patch("ops_agent.workflow.LearningEngine"), \
             patch("ops_agent.chaos_tools.CHAOS_TOOLS", []):
            from ops_agent.workflow import IncidentWorkflow
            return IncidentWorkflow(db, rate_limiter, status_handler)

    def test_routes_to_investigate_when_ai_enabled(self, db):
        wf    = self._make_workflow(db)
        state = _make_incident_state(db)
        # pipeline_decision not set → not fast_path_resolved
        with patch("ops_agent.workflow.settings") as mock_settings:
            mock_settings.ai_enabled = True
            result = wf._route_after_fast_path(state)
        assert result == "investigate"

    def test_routes_to_no_ai_when_ai_disabled(self, db):
        wf    = self._make_workflow(db)
        state = _make_incident_state(db)
        with patch("ops_agent.workflow.settings") as mock_settings:
            mock_settings.ai_enabled = False
            result = wf._route_after_fast_path(state)
        assert result == "no_ai"

    def test_routes_to_fast_path_resolved_regardless_of_ai_enabled(self, db):
        """Fast path takes priority over ai_enabled — if conditions matched, execute."""
        wf    = self._make_workflow(db)
        state = {**_make_incident_state(db), "pipeline_decision": "fast_path_resolved"}
        with patch("ops_agent.workflow.settings") as mock_settings:
            mock_settings.ai_enabled = False
            result = wf._route_after_fast_path(state)
        assert result == "fast_path_resolved"


# ── no_ai_gate node ───────────────────────────────────────────────────────────

@pytest.mark.unit
class TestNoAiGateNode:
    def _make_workflow(self, db: TaskStore) -> object:
        from unittest.mock import MagicMock
        from shared.rate_limiter import RateLimiter
        from shared.status_tracker import StatusCallbackHandler

        rate_limiter   = MagicMock(spec=RateLimiter)
        status_handler = MagicMock(spec=StatusCallbackHandler)
        status_handler.clear_context = MagicMock()

        with patch("ops_agent.workflow.get_llm"), \
             patch("ops_agent.workflow.TopologyCorrelator"), \
             patch("ops_agent.workflow.IntentRegistry"), \
             patch("ops_agent.workflow.LearningEngine"), \
             patch("ops_agent.chaos_tools.CHAOS_TOOLS", []):
            from ops_agent.workflow import IncidentWorkflow
            return IncidentWorkflow(db, rate_limiter, status_handler)

    def test_records_no_ai_skipped_event(self, db):
        wf    = self._make_workflow(db)
        state = _make_incident_state(db)
        result = wf._node_no_ai_gate(state)
        task_id = result["rca_task_id"]
        assert task_id is not None
        events = db.get_task_events(task_id)
        event_types = [e["event_type"] for e in events]
        assert "no_ai_skipped" in event_types

    def test_creates_task_when_none_exists(self, db):
        wf    = self._make_workflow(db)
        state = _make_incident_state(db, rca_task_id=None)
        result = wf._node_no_ai_gate(state)
        assert result["rca_task_id"] is not None
        task = db.get_task(result["rca_task_id"])
        assert task is not None
        assert task["type"] == "rca"

    def test_uses_existing_task_when_provided(self, db):
        existing = db.create_task(
            type="rca", created_by="alert_poller", assigned_to="ops_agent",
            title="existing task", alert_fingerprint="fp-001",
            content={"alertname": "InterfaceDown", "device": "leaf1"},
        )
        wf    = self._make_workflow(db)
        state = _make_incident_state(db, rca_task_id=existing["id"])
        result = wf._node_no_ai_gate(state)
        assert result["rca_task_id"] == existing["id"]
        # Should not have created a second task
        all_tasks = db.list_tasks()
        assert len(all_tasks) == 1

    def test_task_placed_in_awaiting_approval(self, db):
        wf    = self._make_workflow(db)
        state = _make_incident_state(db)
        result = wf._node_no_ai_gate(state)
        task = db.get_task(result["rca_task_id"])
        assert task["status"] == "awaiting_approval"

    def test_pipeline_decision_is_no_ai(self, db):
        wf    = self._make_workflow(db)
        state = _make_incident_state(db)
        result = wf._node_no_ai_gate(state)
        assert result["pipeline_decision"] == "no_ai"

    def test_no_ai_event_contains_alertname_and_device(self, db):
        wf    = self._make_workflow(db)
        state = _make_incident_state(db)
        result = wf._node_no_ai_gate(state)
        events = db.get_task_events(result["rca_task_id"])
        no_ai_event = next(e for e in events if e["event_type"] == "no_ai_skipped")
        detail = json.loads(no_ai_event["detail"]) if isinstance(no_ai_event["detail"], str) else no_ai_event["detail"]
        assert detail.get("alertname") == "InterfaceDown"
        assert detail.get("device") == "leaf1"

    def test_task_title_prefixed_with_no_ai(self, db):
        wf    = self._make_workflow(db)
        state = _make_incident_state(db)
        result = wf._node_no_ai_gate(state)
        task = db.get_task(result["rca_task_id"])
        assert "[NO-AI]" in task["title"]
