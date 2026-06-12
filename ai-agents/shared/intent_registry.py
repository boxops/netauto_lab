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
from datetime import datetime, timezone

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

    def seed_defaults(self, tenant_id: str = "default") -> int:
        """
        Create built-in monitor intents if none exist yet for this tenant.
        Idempotent — safe to call on every startup. Returns number inserted.
        """
        existing = self._store.list_intents(tenant_id=tenant_id)
        if existing:
            return 0
        seeds = [
            {
                "name":             "Nautobot config drift monitor",
                "description":      "Detect configuration drift on all devices via Nautobot Golden Config compliance records",
                "intent_type":      "monitor",
                "device":           "",
                "alertname":        "",
                "metric_query":     "nautobot://plugins/golden-config/config-compliance/?compliance=false",
                "threshold":        "",
                "interval_seconds": 300,
                "cooldown_minutes": 60,
                "priority":         "normal",
                "enabled":          True,
                "tenant_id":        tenant_id,
            },
        ]
        for seed in seeds:
            self._store.create_intent(seed)
        logger.info("IntentRegistry: seeded %d default intents for tenant=%s", len(seeds), tenant_id)
        return len(seeds)

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
        # per-intent last-evaluation timestamps (in-memory; resets on restart)
        self._last_run: dict[str, float] = {}

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
            # Tight loop — per-intent interval_seconds gates individual firings
            self._stop.wait(60)

    def _evaluate_all(self) -> None:
        intents = self._registry.get_monitor_intents(tenant_id=self._tenant_id)
        now = time.monotonic()
        for intent in intents:
            iid = intent["id"]
            interval = max(60, int(intent.get("interval_seconds") or 300))
            last = self._last_run.get(iid, 0.0)
            if now - last < interval:
                continue  # not yet due for this intent
            self._last_run[iid] = now
            try:
                self._evaluate_one(intent)
            except Exception:
                logger.exception("IntentEvaluator: failed to evaluate intent %s", iid)
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
        """Query Prometheus or Nautobot and create RCA task(s) if a threshold is breached."""
        query = intent.get("metric_query", "").strip()
        if not query:
            return

        # nautobot:// prefix routes to the Nautobot API path instead of Prometheus
        if query.startswith("nautobot://"):
            self._evaluate_nautobot_intent(intent)
            return

        threshold = intent.get("threshold", "").strip()
        device    = intent.get("device", "")

        value = self._query_prometheus(query)
        if value is None:
            return

        if not self._threshold_breached(value, threshold):
            return

        # Threshold breached — check if there's already an active task for this intent
        existing = self._store.get_active_task_for_fingerprint(f"intent:{intent['id']}")
        if existing:
            return

        # Cooldown: don't re-trigger within cooldown_minutes of last trigger
        if self._in_cooldown(intent):
            return

        logger.info(
            "IntentEvaluator: intent '%s' threshold breached (value=%s, threshold=%s)",
            intent["name"], value, threshold,
        )

        priority = intent.get("priority") or "normal"
        self._store.touch_intent(intent["id"])
        self._store.create_task(
            type="rca",
            created_by="intent_evaluator",
            assigned_to="ops_agent",
            title=f"[Intent] {intent['name']}",
            alert_fingerprint=f"intent:{intent['id']}",
            priority=priority,
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

    def _evaluate_nautobot_intent(self, intent: dict) -> None:
        """
        Evaluate a monitor intent whose metric_query uses the nautobot:// scheme.

        The path after nautobot:// is appended to the Nautobot API base URL and
        called as a GET request. Any results returned are treated as a threshold
        breach — one RCA task is created per unique device in the response.

        Example metric_query:
            nautobot://plugins/golden-config/config-compliance/?compliance=false
        """
        from shared.config import settings as _cfg
        path = intent["metric_query"][len("nautobot://"):]

        try:
            resp = httpx.get(
                f"{_cfg.nautobot_url}/api/{path}",
                headers={"Authorization": f"Token {_cfg.nautobot_token}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.debug("IntentEvaluator: Nautobot API query failed for path '%s'", path)
            return

        results = data.get("results", [])
        if not results:
            return  # nothing flagged — no tasks needed

        # Group records by device name so each device gets its own pipeline task
        by_device: dict[str, list] = {}
        for record in results:
            dev = record.get("device") or {}
            dev_name = dev.get("name", "") if isinstance(dev, dict) else ""
            if not dev_name:
                continue
            by_device.setdefault(dev_name, []).append(record)

        priority = intent.get("priority") or "normal"
        for dev_name, records in by_device.items():
            fp = f"intent:{intent['id']}:device:{dev_name}"
            if self._store.get_active_task_for_fingerprint(fp):
                continue  # pipeline already open for this device
            if self._in_cooldown(intent):
                continue

            # Collect non-compliant rule names for the task title / description
            features: list[str] = []
            for r in records:
                if r.get("compliance", True):
                    continue
                rule = r.get("rule") or {}
                feature = rule.get("feature") or rule.get("slug") or rule.get("name") or "unknown"
                if isinstance(feature, dict):
                    feature = feature.get("name") or feature.get("slug") or "unknown"
                features.append(str(feature))

            if not features:
                continue  # records present but all compliant — nothing to do

            title_rules = ", ".join(features[:3]) + ("…" if len(features) > 3 else "")
            logger.info(
                "IntentEvaluator: config drift on '%s' — non-compliant rules: %s",
                dev_name, features,
            )
            self._store.touch_intent(intent["id"])
            self._store.create_task(
                type="rca",
                created_by="intent_evaluator",
                assigned_to="ops_agent",
                title=f"[ConfigDrift] {dev_name}: {title_rules}",
                alert_fingerprint=fp,
                priority=priority,
                content={
                    "alertname":              "ConfigDrift",
                    "severity":               "warning",
                    "device":                 dev_name,
                    "instance":               dev_name,
                    "summary":                f"Config drift on {dev_name}: {len(features)} non-compliant rule(s)",
                    "description":            (
                        f"Nautobot compliance check found drift on {dev_name}. "
                        f"Non-compliant rules: {', '.join(features)}. "
                        f"Call get_config_compliance('{dev_name}') for the exact diff."
                    ),
                    "fingerprint":            fp,
                    "labels":                 {
                        "intent_id":    intent["id"],
                        "intent_type":  "monitor",
                        "alertname":    "ConfigDrift",
                    },
                    "non_compliant_features": features,
                },
                tenant_id=self._tenant_id,
            )

    def _in_cooldown(self, intent: dict) -> bool:
        """Return True if the intent was triggered recently and is still in its cooldown window."""
        cooldown = int(intent.get("cooldown_minutes") or 0)
        if cooldown <= 0:
            return False
        last_ts = intent.get("last_triggered_at")
        if not last_ts:
            return False
        try:
            last_dt = datetime.fromisoformat(last_ts)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            elapsed_minutes = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            return elapsed_minutes < cooldown
        except (ValueError, TypeError):
            return False

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
