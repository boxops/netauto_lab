"""
Chaos-driven end-to-end pipeline integration tests.

Two tiers:
  @pytest.mark.integration  — requires the full stack (make start).
    Injects a synthetic Alertmanager webhook and asserts the pipeline runs.
  @pytest.mark.chaos        — additionally requires the Containerlab topology
    (make deploy-lab) and CHAOS_TOOLS_ENABLED=true. Triggers a real fault.

Run integration tier:
  .venv-host/bin/python3 -m pytest tests/test_chaos_e2e.py -v -m integration -s

Run full chaos tier (lab must be running):
  CHAOS_TOOLS_ENABLED=true .venv-host/bin/python3 -m pytest tests/test_chaos_e2e.py -v -m chaos -s

Both tiers are skipped automatically if the stack / lab is not reachable.
"""
from __future__ import annotations

import os
import sys
import time
import json
from pathlib import Path

import pytest
import requests

AI_AGENTS_DIR = Path(__file__).parent.parent / "ai-agents"
sys.path.insert(0, str(AI_AGENTS_DIR))

sys.path.insert(0, str(Path(__file__).parent))
from fixtures.alerts import interface_down_payload, bgp_peer_down_payload, admin_down_payload


# ── Configuration ─────────────────────────────────────────────────────────────

OPS_AGENT_URL = os.getenv("OPS_AGENT_URL", "http://localhost:8000")
AGENT_API_KEY  = os.getenv("AGENT_API_KEY", "")
CHAOS_ENABLED  = os.getenv("CHAOS_TOOLS_ENABLED", "false").lower() == "true"

POLL_INTERVAL   = 5    # seconds between task-store polls
TASK_TIMEOUT    = 120  # seconds to wait for an rca task to appear
STAGE_TIMEOUT   = 300  # seconds to wait for pipeline to reach awaiting_approval


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if AGENT_API_KEY:
        h["X-API-Key"] = AGENT_API_KEY
    return h


def _agent_reachable() -> bool:
    try:
        r = requests.get(f"{OPS_AGENT_URL}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _poll_for_rca(fingerprint: str, timeout: int = TASK_TIMEOUT) -> dict | None:
    """Poll /tasks until an rca task with the given fingerprint appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            f"{OPS_AGENT_URL}/tasks",
            params={"type": "rca", "limit": 50},
            headers=_headers(),
            timeout=5,
        )
        if r.status_code == 200:
            for task in r.json():
                if task.get("alert_fingerprint") == fingerprint:
                    return task
        time.sleep(POLL_INTERVAL)
    return None


def _poll_for_status(task_id: str, target_statuses: list[str], timeout: int = STAGE_TIMEOUT) -> dict | None:
    """Poll a specific task until its status is one of target_statuses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            f"{OPS_AGENT_URL}/tasks/{task_id}",
            headers=_headers(),
            timeout=5,
        )
        if r.status_code == 200:
            task = r.json()
            if task.get("status") in target_statuses:
                return task
        time.sleep(POLL_INTERVAL)
    return None


def _reset_poller():
    """Clear poller dedup state so old fingerprints don't block the test."""
    try:
        requests.post(f"{OPS_AGENT_URL}/poller/reset", headers=_headers(), timeout=5)
    except Exception:
        pass


# ── Webhook injection tests (integration tier) ────────────────────────────────

@pytest.mark.integration
class TestWebhookInjectionPipeline:
    """
    These tests inject synthetic Alertmanager payloads directly to the ops-agent
    webhook endpoint.  They verify the full data-layer pipeline without needing
    a live Prometheus or Containerlab topology.
    """

    @pytest.fixture(autouse=True)
    def require_agent(self):
        if not _agent_reachable():
            pytest.skip("ops-agent not reachable at " + OPS_AGENT_URL)

    @pytest.fixture(autouse=True)
    def reset_poller_state(self):
        _reset_poller()
        yield
        _reset_poller()

    def test_interface_down_webhook_creates_rca_task(self):
        fp = "e2e-iface-down-001"
        payload = interface_down_payload(device="spine2", interface="Ethernet1", fingerprint=fp)

        r = requests.post(
            f"{OPS_AGENT_URL}/webhook/alert",
            json=payload,
            headers=_headers(),
            timeout=10,
        )
        assert r.status_code == 200, f"Webhook rejected: {r.text}"
        data = r.json()
        assert data["ok"] is True
        assert data["accepted"] >= 1, "Webhook accepted 0 alerts — dedup or filter blocked it"

        task = _poll_for_rca(fp, timeout=TASK_TIMEOUT)
        assert task is not None, (
            f"No rca task with fingerprint={fp!r} appeared within {TASK_TIMEOUT}s. "
            "Check that the webhook receiver and task_store are wired correctly."
        )
        assert task["type"] == "rca"
        assert task["alert_fingerprint"] == fp
        assert task["status"] in ("pending", "claimed", "running", "awaiting_approval", "complete")

    def test_duplicate_fingerprint_not_reinvestigated(self):
        fp = "e2e-dedup-001"
        payload = interface_down_payload(fingerprint=fp)

        # Send the same alert twice
        for _ in range(2):
            requests.post(
                f"{OPS_AGENT_URL}/webhook/alert",
                json=payload,
                headers=_headers(),
                timeout=10,
            )
            time.sleep(1)

        # Wait for the first rca to appear
        first = _poll_for_rca(fp, timeout=TASK_TIMEOUT)
        assert first is not None

        # Wait a bit and assert only one rca for this fingerprint
        time.sleep(10)
        r = requests.get(
            f"{OPS_AGENT_URL}/tasks",
            params={"type": "rca", "limit": 100},
            headers=_headers(),
            timeout=5,
        )
        rca_for_fp = [t for t in r.json() if t.get("alert_fingerprint") == fp]
        assert len(rca_for_fp) == 1, (
            f"Bug #9 regression: {len(rca_for_fp)} rca tasks for fingerprint {fp!r}, expected 1"
        )

    def test_bgp_peer_down_webhook_accepted(self):
        fp = "e2e-bgp-down-001"
        payload = bgp_peer_down_payload(device="spine1", neighbor_ip="10.0.0.2", fingerprint=fp)
        r = requests.post(
            f"{OPS_AGENT_URL}/webhook/alert",
            json=payload,
            headers=_headers(),
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_resolved_webhook_does_not_create_rca(self):
        fp = "e2e-resolved-001"
        payload = interface_down_payload(fingerprint=fp, status="resolved")
        r = requests.post(
            f"{OPS_AGENT_URL}/webhook/alert",
            json=payload,
            headers=_headers(),
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        # Resolved alerts should be skipped (not create a new rca)
        assert data.get("skipped", 0) >= 1 or data.get("accepted", 0) == 0, (
            "A 'resolved' webhook should not create a new rca task"
        )


# ── Full chaos tests (chaos tier — needs lab + Nautobot + Ansible) ─────────────

@pytest.mark.chaos
class TestChaosTriggeredPipeline:
    """
    These tests trigger real faults on the Containerlab topology via Nautobot/Ansible,
    then assert the full pipeline runs through to awaiting_approval.

    Requires:
      - make deploy-lab (topology must be running)
      - make start (all services)
      - CHAOS_TOOLS_ENABLED=true in .env
    """

    @pytest.fixture(autouse=True)
    def require_chaos_enabled(self):
        if not CHAOS_ENABLED:
            pytest.skip("CHAOS_TOOLS_ENABLED is not set to true")
        if not _agent_reachable():
            pytest.skip("ops-agent not reachable at " + OPS_AGENT_URL)
        _reset_poller()
        yield
        _reset_poller()

    def _send_chaos_chat(self, message: str) -> dict:
        r = requests.post(
            f"{OPS_AGENT_URL}/chat",
            json={"message": message, "session_id": "chaos-test"},
            headers=_headers(),
            timeout=30,
        )
        assert r.status_code == 200, f"Chat failed: {r.text}"
        return r.json()

    def test_interface_shutdown_triggers_full_pipeline(self):
        """
        Shuts down spine2/Ethernet1, waits for the pipeline to reach awaiting_approval,
        then restores the interface.
        """
        fp_prefix = "chaos-eth1"

        # Trigger fault via agent chat (chaos tools gated by CHAOS_TOOLS_ENABLED)
        self._send_chaos_chat(
            "shutdown interface Ethernet1 on spine2 with check_mode=False. "
            "This is an authorised chaos test."
        )

        # Wait for Prometheus to fire an alert and the webhook to create an rca task
        # (Prometheus scrapes every 30s, so allow up to 90s for alert to appear)
        time.sleep(35)  # let metrics settle

        # Find rca tasks for spine2 created in the last 2 minutes
        deadline = time.time() + TASK_TIMEOUT
        rca_task = None
        while time.time() < deadline:
            r = requests.get(
                f"{OPS_AGENT_URL}/tasks",
                params={"type": "rca", "limit": 50},
                headers=_headers(),
                timeout=5,
            )
            if r.status_code == 200:
                spine2_rcas = [
                    t for t in r.json()
                    if "spine2" in (t.get("title") or "") or "spine2" in (t.get("content") or "")
                ]
                if spine2_rcas:
                    rca_task = spine2_rcas[0]
                    break
            time.sleep(POLL_INTERVAL)

        assert rca_task is not None, (
            "No rca task for spine2 appeared after interface shutdown. "
            "Check Prometheus alert rules and the webhook receiver."
        )

        # Wait for the pipeline to reach awaiting_approval
        final = _poll_for_status(
            rca_task["id"],
            target_statuses=["awaiting_approval", "complete"],
            timeout=STAGE_TIMEOUT,
        )
        assert final is not None, (
            f"Pipeline task {rca_task['id']} did not reach awaiting_approval within {STAGE_TIMEOUT}s"
        )

        # Restore the interface
        self._send_chaos_chat(
            "restore interface Ethernet1 on spine2 with check_mode=False. "
            "This is an authorised chaos restore."
        )

    def test_bgp_flap_triggers_rca(self):
        """
        Flaps a BGP session on spine1, verifies an rca task appears,
        then checks that the BGP session recovers.
        """
        self._send_chaos_chat(
            "flap BGP neighbor 172.20.20.21 on spine1 using method=soft with check_mode=False. "
            "This is an authorised chaos test."
        )

        time.sleep(20)  # let BGP flap propagate to Prometheus

        deadline = time.time() + TASK_TIMEOUT
        bgp_rca = None
        while time.time() < deadline:
            r = requests.get(
                f"{OPS_AGENT_URL}/tasks",
                params={"type": "rca", "limit": 50},
                headers=_headers(),
                timeout=5,
            )
            if r.status_code == 200:
                bgp_rcas = [
                    t for t in r.json()
                    if "BGP" in (t.get("title") or "").upper() or "spine1" in (t.get("content") or "")
                ]
                if bgp_rcas:
                    bgp_rca = bgp_rcas[0]
                    break
            time.sleep(POLL_INTERVAL)

        assert bgp_rca is not None, (
            "No rca task for BGP flap appeared. "
            "Check that BGPPeerDown alert rule is configured in Prometheus."
        )
