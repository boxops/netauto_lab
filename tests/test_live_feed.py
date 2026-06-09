"""
Tests for Phase 3: live event feed.

Covers:
  - TaskStore.get_recent_pipeline_events() — DB query and enrichment
  - GET /partials/live-feed — HTML partial endpoint

Run: python3 -m pytest tests/test_live_feed.py -m unit -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

from shared.task_store import TaskStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return TaskStore(db_path=str(tmp_path / "test.db"))


def _make_task(db: TaskStore, alertname: str = "InterfaceDown", device: str = "leaf1") -> dict:
    return db.create_task(
        type="rca",
        created_by="alert_poller",
        assigned_to="ops_agent",
        title=f"RCA: {alertname} on {device}",
        alert_fingerprint=f"fp-{alertname}-{device}",
        content={"alertname": alertname, "device": device},
    )


# ── DB method: get_recent_pipeline_events ────────────────────────────────────

@pytest.mark.unit
class TestGetRecentPipelineEvents:
    def test_returns_empty_when_no_events(self, db):
        assert db.get_recent_pipeline_events() == []

    def test_returns_only_feed_event_types(self, db):
        task = _make_task(db)
        # Add a mix of feed-relevant and feed-irrelevant event types
        db.add_event(task["id"], "workflow", "created",           {"assigned_to": "ops_agent"})
        db.add_event(task["id"], "workflow", "rca_complete",      {"confidence": "high"})
        db.add_event(task["id"], "workflow", "fast_path_resolved",{"policy_id": "p1"})
        db.add_event(task["id"], "workflow", "claimed",           None)

        results = db.get_recent_pipeline_events()
        returned_types = {r["event_type"] for r in results}
        assert "rca_complete"       in returned_types
        assert "fast_path_resolved" in returned_types
        assert "created"            not in returned_types
        assert "claimed"            not in returned_types

    def test_enriches_with_device_and_alertname(self, db):
        task = _make_task(db, alertname="BGPPeerDown", device="spine1")
        db.add_event(task["id"], "workflow", "rca_complete", {"confidence": "high"})

        results = db.get_recent_pipeline_events()
        assert len(results) == 1
        assert results[0]["device"]    == "spine1"
        assert results[0]["alertname"] == "BGPPeerDown"

    def test_limit_respected(self, db):
        task = _make_task(db)
        for i in range(30):
            db.add_event(task["id"], "workflow", "rca_complete", {"i": i})

        results = db.get_recent_pipeline_events(limit=10)
        assert len(results) == 10

    def test_ordered_newest_first(self, db):
        task = _make_task(db)
        db.add_event(task["id"], "workflow", "rca_complete",      {"seq": 1})
        db.add_event(task["id"], "workflow", "auto_approved",     {"seq": 2})
        db.add_event(task["id"], "workflow", "execution_complete",{"seq": 3})

        results = db.get_recent_pipeline_events()
        ids = [r["id"] for r in results]
        assert ids == sorted(ids, reverse=True)

    def test_all_feed_event_types_returned(self, db):
        task = _make_task(db)
        feed_types = [
            "fast_path_resolved", "rca_complete", "auto_approved",
            "approval_requested", "execution_complete", "execution_verified",
            "no_ai_skipped",
        ]
        for et in feed_types:
            db.add_event(task["id"], "workflow", et, {"x": 1})

        results = db.get_recent_pipeline_events(limit=50)
        returned_types = {r["event_type"] for r in results}
        assert returned_types == set(feed_types)

    def test_detail_parsed_from_json(self, db):
        task = _make_task(db)
        db.add_event(task["id"], "workflow", "rca_complete", {"confidence": "high", "diagnosis": "test"})

        results = db.get_recent_pipeline_events()
        assert isinstance(results[0]["detail"], dict)
        assert results[0]["detail"]["confidence"] == "high"

    def test_tenant_id_filter(self, db):
        task_a = db.create_task(
            type="rca", created_by="alert_poller", assigned_to="ops_agent",
            title="task-a", alert_fingerprint="fp-a",
            content={"alertname": "BGPPeerDown", "device": "leaf1"},
            tenant_id="tenant-a",
        )
        task_b = db.create_task(
            type="rca", created_by="alert_poller", assigned_to="ops_agent",
            title="task-b", alert_fingerprint="fp-b",
            content={"alertname": "InterfaceDown", "device": "leaf2"},
            tenant_id="tenant-b",
        )
        db.add_event(task_a["id"], "workflow", "rca_complete", {})
        db.add_event(task_b["id"], "workflow", "rca_complete", {})

        results_a = db.get_recent_pipeline_events(tenant_id="tenant-a")
        assert all(r["task_id"] == task_a["id"] for r in results_a)

        results_b = db.get_recent_pipeline_events(tenant_id="tenant-b")
        assert all(r["task_id"] == task_b["id"] for r in results_b)


# ── HTTP partial endpoint (requires running stack — fastapi not in host venv) ─

@pytest.mark.integration
class TestLiveFeedEndpoint:
    """
    Integration tests for the /partials/live-feed endpoint.
    Requires: make start (full Docker stack with ui container running on port 7860).
    """

    @pytest.fixture
    def client(self):
        import requests
        return requests.Session()

    def test_live_feed_returns_200(self, client):
        resp = client.get("http://localhost:7860/partials/live-feed")
        assert resp.status_code == 200

    def test_live_feed_html_structure(self, client):
        resp = client.get("http://localhost:7860/partials/live-feed")
        assert "live-feed" in resp.text
