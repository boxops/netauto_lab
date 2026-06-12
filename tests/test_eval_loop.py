"""
Unit tests for the self-grading eval loop (shared/eval_engine.py).

Chaos injections are ground truth; the grader scores the pipeline's response
(detection, device attribution, cause attribution, resolution) into the
accuracy ledger. All tests run against a temporary SQLite TaskStore.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai-agents"))

from shared.task_store import TaskStore
from shared.eval_engine import EvalStore, EvalGrader

pytestmark = pytest.mark.unit


@pytest.fixture
def ts(tmp_path):
    return TaskStore(db_path=str(tmp_path / "eval-test.db"))


@pytest.fixture
def es(ts):
    return EvalStore(ts)


def _grader(ts, es, min_age=0, window=1800):
    return EvalGrader(ts, es, min_age_seconds=min_age, match_window_seconds=window)


def _pipeline_task(ts, device="leaf1", alertname="InterfaceAdminDown",
                   resolved=True, fast_path=False, diagnosis=None):
    task = ts.create_task(
        type="rca", created_by="test", assigned_to="ops_agent",
        title=f"{alertname}: {device}",
        alert_fingerprint=f"fp-{device}",
        content={"device": device, "alertname": alertname},
    )
    tid = task["id"]
    ts.add_event(tid, "workflow", "rca_complete", {
        "diagnosis": diagnosis or (
            f"Interface Ethernet1 on {device} is administratively shut down "
            "(ifAdminStatus=2). Restore with 'no shutdown'."
        ),
        "affected_device": device,
        "action": "no shutdown",
        "confidence": "high",
    })
    if fast_path:
        ts.add_event(tid, "workflow", "fast_path_resolved", {"policy_id": "p1"})
    if resolved is not None:
        ts.add_event(tid, "workflow", "execution_verified", {
            "alert_resolved": resolved,
            "ttr_seconds": 42,
            "alertname": alertname,
            "device": device,
        })
    return task


# ── EvalStore basics ───────────────────────────────────────────────────────────

class TestEvalStore:
    def test_record_and_pending(self, es):
        inj = es.record_injection("interface_down", "leaf1", "Ethernet1", source="test")
        pending = es.pending_injections(min_age_seconds=0)
        assert [p["id"] for p in pending] == [inj["id"]]

    def test_pending_respects_min_age(self, es):
        es.record_injection("interface_down", "leaf1", "Ethernet1")
        assert es.pending_injections(min_age_seconds=3600) == []

    def test_save_grade_marks_injection_graded(self, es):
        inj = es.record_injection("interface_down", "leaf1", "Ethernet1")
        es.save_grade(inj, {"detected": 1})
        assert es.pending_injections(0) == []
        assert es.recent_grades()[0]["detected"] == 1


# ── Grading ────────────────────────────────────────────────────────────────────

class TestGrading:
    def test_full_marks_for_correct_pipeline_response(self, ts, es):
        inj = es.record_injection("interface_down", "leaf1", "Ethernet1")
        _pipeline_task(ts, device="leaf1", resolved=True)
        assert _grader(ts, es).grade_pending() == 1

        g = es.recent_grades()[0]
        assert g["detected"] == 1
        assert g["correct_device"] == 1
        assert g["correct_cause"] == 1
        assert g["resolved"] == 1
        assert g["ttr_seconds"] == 42
        assert g["ttd_seconds"] is not None

    def test_missed_injection_graded_after_window(self, ts, es):
        es.record_injection("interface_down", "leaf9", "Ethernet1")
        assert _grader(ts, es, window=0).grade_pending() == 1
        g = es.recent_grades()[0]
        assert g["detected"] == 0
        assert g["correct_device"] == 0

    def test_no_task_within_open_window_stays_pending(self, ts, es):
        es.record_injection("interface_down", "leaf9", "Ethernet1")
        assert _grader(ts, es, window=3600).grade_pending() == 0
        assert len(es.pending_injections(0)) == 1

    def test_task_for_other_device_is_not_matched(self, ts, es):
        es.record_injection("interface_down", "leaf1", "Ethernet1")
        _pipeline_task(ts, device="spine1")
        assert _grader(ts, es, window=0).grade_pending() == 1
        assert es.recent_grades()[0]["detected"] == 0

    def test_wrong_cause_diagnosis_scores_zero_cause(self, ts, es):
        es.record_injection("bgp_flap", "leaf1", "10.0.0.1")
        _pipeline_task(
            ts, device="leaf1", alertname="BGPPeerDown",
            diagnosis="Optical transceiver failure suspected on uplink.",
        )
        _grader(ts, es).grade_pending()
        g = es.recent_grades()[0]
        assert g["detected"] == 1
        assert g["correct_device"] == 1
        assert g["correct_cause"] == 0

    def test_bgp_cause_signature_matches(self, ts, es):
        es.record_injection("bgp_flap", "spine1", "10.0.0.2")
        _pipeline_task(
            ts, device="spine1", alertname="BGPPeerDown",
            diagnosis="BGP session to 10.0.0.2 is not Established; soft clear required.",
        )
        _grader(ts, es).grade_pending()
        assert es.recent_grades()[0]["correct_cause"] == 1

    def test_fast_path_flag_recorded(self, ts, es):
        es.record_injection("interface_down", "leaf1", "Ethernet1")
        _pipeline_task(ts, device="leaf1", fast_path=True)
        _grader(ts, es).grade_pending()
        assert es.recent_grades()[0]["fast_path"] == 1

    def test_unresolved_execution_scores_zero_resolved(self, ts, es):
        es.record_injection("interface_down", "leaf1", "Ethernet1")
        _pipeline_task(ts, device="leaf1", resolved=False)
        _grader(ts, es).grade_pending()
        assert es.recent_grades()[0]["resolved"] == 0


# ── Ledger summary ─────────────────────────────────────────────────────────────

class TestSummary:
    def test_summary_aggregates_per_fault_type(self, ts, es):
        for device, resolved in (("leaf1", True), ("leaf2", False)):
            es.record_injection("interface_down", device, "Ethernet1")
            _pipeline_task(ts, device=device, resolved=resolved)
        es.record_injection("bgp_flap", "leaf9", "10.0.0.1")  # missed
        _grader(ts, es, window=0).grade_pending()

        rows = {r["fault_type"]: r for r in es.summary()}
        ifd = rows["interface_down"]
        assert ifd["injections"] == 2
        assert ifd["detected_pct"] == 100
        assert ifd["correct_device_pct"] == 100
        assert ifd["resolved_pct"] == 50
        bgp = rows["bgp_flap"]
        assert bgp["injections"] == 1
        assert bgp["detected_pct"] == 0

    def test_summary_empty_without_grades(self, es):
        assert es.summary() == []


# ── Chaos tool wiring ──────────────────────────────────────────────────────────

class TestChaosToolWiring:
    def test_record_injection_skips_errors(self):
        import ops_agent.chaos_tools as ct
        with patch.object(ct, "_eval_store", MagicMock()) as mock_store:
            ct._record_injection("interface_down", "leaf1", "Ethernet1",
                                 {"error": "job failed"})
            mock_store.record_injection.assert_not_called()

    def test_record_injection_records_success(self):
        import ops_agent.chaos_tools as ct
        with patch.object(ct, "_eval_store", MagicMock()) as mock_store:
            ct._record_injection("interface_down", "leaf1", "Ethernet1",
                                 {"status": "SUCCESS"})
            mock_store.record_injection.assert_called_once()

    def test_shutdown_tool_source_contains_recording(self):
        src = open(os.path.join(os.path.dirname(__file__), "..",
                                "ai-agents", "ops_agent", "chaos_tools.py")).read()
        assert src.count("_record_injection(") >= 3  # def + 2 call sites


# ── UI ledger partial ──────────────────────────────────────────────────────────

class TestEvalLedgerPartial:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        with (
            patch("shared.activity_store.ActivityStore", MagicMock()),
            patch("shared.task_store.TaskStore", MagicMock()),
            patch("shared.kb_store.KBStore", MagicMock()),
        ):
            import ui.main as ui_main
            mock_es = MagicMock()
            mock_es.summary.return_value = [{
                "fault_type": "interface_down", "injections": 4,
                "detected_pct": 100, "correct_device_pct": 100,
                "correct_cause_pct": 75, "resolved_pct": 75,
                "fast_path": 2, "avg_ttd_seconds": 65, "avg_ttr_seconds": 180,
            }]
            mock_es.recent_grades.return_value = []
            with patch.object(ui_main, "eval_store", mock_es):
                with TestClient(ui_main.app, raise_server_exceptions=True) as c:
                    yield c

    def test_ledger_partial_renders_summary(self, client):
        resp = client.get("/partials/eval-ledger")
        assert resp.status_code == 200
        assert "interface_down" in resp.text
        assert "75%" in resp.text

    def test_ledger_empty_state(self, client):
        import ui.main as ui_main
        ui_main.eval_store.summary.return_value = []
        resp = client.get("/partials/eval-ledger")
        assert resp.status_code == 200
        assert "No graded chaos injections yet" in resp.text
