"""
Standing intent registry for proactive network monitoring.

Implements the intent layer from docs/autonomous-agent-framework.md (Principle 1.1:
Intent over Instructions). Standing intents can suppress, escalate, or proactively
monitor network state independent of Prometheus alerts.

Intent types:
  suppress       — skip pipeline investigation for matching alerts (e.g. known flapping link)
  escalate       — force approval gate for matching alerts regardless of risk
  monitor        — proactively poll Prometheus metric and open an incident on threshold breach
  chaos_schedule — run a chaos scenario on a cron expression
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from shared.pipeline_models import StandingIntentMatch

logger = logging.getLogger(__name__)


class IntentRegistry:
    """
    CRUD wrapper around the standing_intents table in TaskStore.
    Stateless — all data lives in the database.
    """

    def __init__(self, task_store) -> None:
        self._store = task_store

    def matching(
        self,
        device: str,
        alertname: str,
        tenant_id: str = "default",
    ) -> list[StandingIntentMatch]:
        """Return enabled intents that match device + alertname (wildcards supported)."""
        rows = self._store.get_matching_intents(
            device=device, alertname=alertname, tenant_id=tenant_id
        )
        results = []
        for r in rows:
            results.append(StandingIntentMatch(
                intent_id=r["id"],
                intent_type=r["intent_type"],
                action=r.get("action", ""),
                reason=f"Standing intent '{r['name']}' matched "
                       f"(device='{device}', alertname='{alertname}').",
            ))
        return results

    def create_intent(self, data: dict) -> dict:
        return self._store.create_intent(data)

    def list_intents(self, tenant_id: str = "default") -> list[dict]:
        return self._store.list_intents(tenant_id=tenant_id)

    def update_intent(self, intent_id: str, data: dict) -> dict | None:
        return self._store.update_intent(intent_id, data)

    def delete_intent(self, intent_id: str) -> None:
        self._store.delete_intent(intent_id)

    def get_intent(self, intent_id: str) -> dict | None:
        return self._store.get_intent(intent_id)

    def get_monitor_intents(self, tenant_id: str = "default") -> list[dict]:
        """Return all enabled monitor-type intents for the evaluator."""
        all_intents = self._store.list_intents(tenant_id=tenant_id)
        return [i for i in all_intents
                if i.get("enabled", 1) and i.get("intent_type") == "monitor"]

    def get_chaos_schedule_intents(self, tenant_id: str = "default") -> list[dict]:
        """Return all enabled chaos_schedule intents for the evaluator."""
        all_intents = self._store.list_intents(tenant_id=tenant_id)
        return [i for i in all_intents
                if i.get("enabled", 1) and i.get("intent_type") == "chaos_schedule"]


class IntentEvaluator:
    """
    Background thread that proactively evaluates 'monitor' standing intents against
    live Prometheus metrics. Creates RCA tasks when a threshold is breached.

    This implements the proactive intent layer from the CLANO framework —
    the system detects degraded state before Alertmanager fires.
    """

    def __init__(
        self,
        intent_registry: IntentRegistry,
        task_store,
        alert_poller,
        prometheus_url: str,
        evaluation_interval: int = 300,
        tenant_id: str = "default",
        scheduler=None,
    ) -> None:
        self._registry    = intent_registry
        self._store       = task_store
        self._poller      = alert_poller
        self._prom_url    = prometheus_url.rstrip("/")
        self._interval    = evaluation_interval
        self._tenant_id   = tenant_id
        self._scheduler   = scheduler
        self._thread: threading.Thread | None = None
        self._stop        = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="intent-evaluator")
        self._thread.start()
        logger.info("IntentEvaluator started (interval=%ds)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        # Stagger startup to avoid competing with AlertPoller's initial burst
        time.sleep(30)
        while not self._stop.is_set():
            try:
                self._evaluate_all()
            except Exception:
                logger.exception("IntentEvaluator: evaluation cycle failed")
            self._stop.wait(self._interval)

    def _evaluate_all(self) -> None:
        intents = self._registry.get_monitor_intents(tenant_id=self._tenant_id)
        for intent in intents:
            try:
                self._evaluate_one(intent)
            except Exception:
                logger.exception("IntentEvaluator: failed to evaluate intent %s", intent["id"])
        if self._scheduler is not None:
            try:
                self._sync_chaos_jobs()
            except Exception:
                logger.exception("IntentEvaluator: chaos job sync failed")

    def _sync_chaos_jobs(self) -> None:
        """Register/remove APScheduler cron jobs to match enabled chaos_schedule intents."""
        intents = self._registry.get_chaos_schedule_intents(tenant_id=self._tenant_id)
        wanted  = {i["id"] for i in intents}
        current = self._scheduler.list_cron_job_ids()

        for intent in intents:
            iid = intent["id"]
            if not intent.get("schedule") or not intent.get("action"):
                continue
            if iid not in current:
                def _on_fire(success: bool, _id: str = iid) -> None:
                    if success:
                        self._store.touch_intent(_id)
                self._scheduler.add_cron_job(
                    iid, intent["action"], intent["schedule"], _on_fire
                )

        for iid in current - wanted:
            self._scheduler.remove_cron_job(iid)

    def _evaluate_one(self, intent: dict) -> None:
        """Query Prometheus and create an RCA task if the threshold is breached."""
        query     = intent.get("metric_query", "").strip()
        threshold = intent.get("threshold", "").strip()
        device    = intent.get("device", "")

        if not query:
            return

        value = self._query_prometheus(query)
        if value is None:
            return

        if not self._threshold_breached(value, threshold):
            return

        # Threshold breached — check if there's already an active task for this intent
        existing = self._store.get_active_task_for_fingerprint(f"intent:{intent['id']}")
        if existing:
            return

        logger.info(
            "IntentEvaluator: intent '%s' threshold breached (value=%s, threshold=%s)",
            intent["name"], value, threshold,
        )

        self._store.touch_intent(intent["id"])
        self._store.create_task(
            type="rca",
            created_by="intent_evaluator",
            assigned_to="ops_agent",
            title=f"[Intent] {intent['name']}",
            alert_fingerprint=f"intent:{intent['id']}",
            priority="normal",
            content={
                "alertname":   f"intent:{intent['name']}",
                "severity":    "warning",
                "device":      device,
                "instance":    "",
                "summary":     intent.get("description") or intent["name"],
                "description": f"Standing intent triggered: metric={query} value={value} threshold={threshold}",
                "fingerprint": f"intent:{intent['id']}",
                "labels":      {"intent_id": intent["id"], "intent_type": "monitor"},
            },
            tenant_id=self._tenant_id,
        )

    def _query_prometheus(self, query: str) -> float | None:
        try:
            resp = httpx.get(
                f"{self._prom_url}/api/v1/query",
                params={"query": query},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("data", {}).get("result", [])
            if results:
                return float(results[0]["value"][1])
        except Exception:
            logger.debug("IntentEvaluator: Prometheus query failed for '%s'", query)
        return None

    @staticmethod
    def _threshold_breached(value: float, threshold: str) -> bool:
        """Evaluate a simple threshold expression like '< 1', '>= 95', '== 0'."""
        threshold = threshold.strip()
        if not threshold:
            return False
        try:
            op, rhs = threshold.split(None, 1)
            rhs_f = float(rhs)
            return {
                "<":  value <  rhs_f,
                "<=": value <= rhs_f,
                ">":  value >  rhs_f,
                ">=": value >= rhs_f,
                "==": value == rhs_f,
                "!=": value != rhs_f,
            }.get(op, False)
        except (ValueError, KeyError):
            return False
