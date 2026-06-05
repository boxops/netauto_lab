"""
Closed-loop alert poller for the Ops Agent.

Polls the alert-event-receiver every POLL_INTERVAL seconds for new firing alerts,
then cross-checks each event against live Prometheus state before investigating.

Key design decisions:
- 30-second startup grace period lets any in-flight resolved webhooks land before
  the first poll, preventing investigation of already-resolved alerts.
- Every firing event is validated against GET /api/v1/alerts before creating a task.
  If Prometheus no longer shows the alert as firing, it is silently skipped.
- At most MAX_PER_CYCLE new investigations are started per poll cycle to avoid
  token bursts hitting the OpenAI TPM (tokens-per-minute) limit.
- INTER_ALERT_DELAY seconds of sleep between consecutive investigations keeps the
  token rate below 30k TPM even at sustained alert volume.
- On OpenAI 429 rate-limit errors the investigation is retried once after
  RATE_LIMIT_BACKOFF seconds, then failed if the retry also errors.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import httpx

from shared.config import settings
from shared.rate_limiter import BudgetExceededError
from shared.pipeline_models import RcaResult
from shared.structured_output import parse_structured

logger = logging.getLogger(__name__)

POLL_INTERVAL          = 60    # seconds between full poll cycles
CRITICAL_POLL_INTERVAL = 15    # seconds — tight loop for critical alerts
STARTUP_DELAY          = 30    # seconds to wait before the very first poll
INTER_ALERT_DELAY      = 20    # seconds between consecutive investigations
MAX_PER_CYCLE          = 2     # max new investigations to start per poll cycle
MAX_CONCURRENT         = 2     # max simultaneous workflow investigations (prevents TPM bursts)
RATE_LIMIT_BACKOFF     = 70    # seconds to wait after a 429 before retrying
RETRY_BACKOFF          = 120   # seconds before retrying a failed RCA task

SEVERITIES = {"critical", "warning"}

# Semantic priority for alert types — lower number = higher priority.
# When a new alert arrives for a device that already has an active RCA, we
# compare priorities.  If the new alert has HIGHER priority (lower number)
# than the existing task's alert type, the new alert gets its own pipeline
# rather than being absorbed as a correlated side-note.
#
# Rule of thumb: "more specific / more directly actionable" = higher priority.
#   InterfaceAdminDown = deliberate config action — most actionable
#   InterfaceDown      = could be admin-down or physical — very specific
#   BGPPeerDown        = routing consequence — likely caused by interface event
#   DeviceDown         = broadest — often a consequence of upstream failure
_ALERT_PRIORITY: dict[str, int] = {
    "InterfaceAdminDown":        10,
    "InterfaceDown":             20,
    "InterfaceHighErrorRate":    25,
    "HighInterfaceUtilization":  30,
    "BGPPrefixCountDecreased":   35,
    "BGPPeerDown":               40,
    "DeviceDown":                50,
}

_ALERT_FOCUS = {
    "InterfaceDown": (
        "Interface is operationally down. CRITICAL: call get_device_metrics(device) and check "
        "interface_ifAdminStatus. If ifAdminStatus=2: admin-shutdown, fix is 'no shutdown'. "
        "If ifAdminStatus=1 + ifOperStatus=2: physical/remote failure, check peer interface via topology."
    ),
    "InterfaceAdminDown": (
        "Interface was admin-shutdown (ifAdminStatus=2). Determine if intentional or unintentional. "
        "If no maintenance window found: fix_type=config_change, COMMANDS='interface {ifDescr}\\n no shutdown'. "
        "Only use escalate_human with explicit evidence of planned maintenance."
    ),
    "BGPPeerDown":              "BGP session is not Established — check link state, config drift, route policy, and whether the peer interface is also down.",
    "DeviceDown":               "device is unreachable via ICMP — check reachability, upstream links, and power",
    "HighInterfaceUtilization": "interface utilization is high — identify the traffic source and affected flows",
    "InterfaceHighErrorRate":   "interface has elevated error rate — check for hardware or cabling issues",
    "BGPPrefixCountDecreased":  "BGP prefix count dropped significantly — possible route withdrawal or peering issue",
}


class AlertPoller:
    """
    Background thread that polls the alert-event-receiver and triggers
    the Ops Agent to investigate new firing alerts.
    """

    def __init__(self, agent, task_store, rate_limiter, workflow=None) -> None:
        self._agent        = agent
        self._task_store   = task_store
        self._rate_limiter = rate_limiter
        self._workflow     = workflow   # IncidentWorkflow instance; None when workflow mode is off
        # Limits simultaneous LLM investigations to MAX_CONCURRENT so parallel
        # alert storms don't exhaust the OpenAI tokens-per-minute budget at once.
        self._investigation_sem = threading.Semaphore(MAX_CONCURRENT)
        # fingerprint → seen_key string; survives within one process lifetime.
        # Pre-populated from the TaskStore on startup so container restarts don't
        # cause the same fingerprint to be re-processed.
        self._seen: dict[str, str] = {}
        self._seed_seen_from_store()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _seed_seen_from_store(self) -> None:
        """
        Pre-populate _seen from tasks that are already in progress or complete.
        Skips failed/rejected tasks so their fingerprints remain eligible for
        retry if the alert is still firing.
        """
        try:
            tasks = self._task_store.list_tasks(type="rca", limit=1000)
            seeded = 0
            for task in tasks:
                fp = task.get("alert_fingerprint") or ""
                if fp and task.get("status") not in ("failed", "rejected"):
                    self._seen[fp] = f"{fp}:firing"
                    seeded += 1
            logger.info("AlertPoller: seeded %d fingerprints from TaskStore", seeded)
        except Exception as exc:
            logger.warning("AlertPoller: failed to seed seen from store: %s", exc)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="AlertPoller", daemon=True
        )
        self._thread.start()
        logger.info(
            "AlertPoller started (interval=%ds startup_delay=%ds)",
            POLL_INTERVAL, STARTUP_DELAY,
        )

    def stop(self) -> None:
        self._stop.set()

    def push_alert(self, event: dict) -> bool:
        """
        Process a single alert event synchronously in a background thread.
        Called by the /webhook/alert endpoint for immediate, zero-polling ingestion.

        Returns True if the alert was accepted for investigation, False if
        it was deduplicated, filtered, or the budget was exceeded.
        """
        import threading as _t
        live_alerts = self._fetch_live_alerts()
        work = self._classify_event(event, live_alerts)
        if work is None:
            return False

        def _run():
            try:
                self._investigate(work)
            except Exception:
                logger.exception("AlertPoller.push_alert: investigation failed for %s",
                                 event.get("alertname"))

        _t.Thread(target=_run, name="webhook-alert", daemon=True).start()
        return True

    def reset_seen(self) -> int:
        """
        Clear the in-memory deduplication state and re-seed from the TaskStore.
        Call this after the task queue has been cleared so the poller will
        re-investigate any alerts that are still firing.
        Returns the number of fingerprints now in _seen after re-seeding.
        """
        self._seen.clear()
        self._seed_seen_from_store()
        return len(self._seen)

    # ── main loop ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        if self._stop.wait(STARTUP_DELAY):
            return
        ticks = 0
        while not self._stop.wait(CRITICAL_POLL_INTERVAL):
            ticks += 1
            try:
                self._poll_once(critical_only=True)     # critical alerts every 15 s
            except Exception:
                logger.exception("AlertPoller: error in priority sweep")
            if ticks % (POLL_INTERVAL // CRITICAL_POLL_INTERVAL) == 0:
                try:
                    self._poll_once(critical_only=False)  # all alerts every 60 s
                except Exception:
                    logger.exception("AlertPoller: error in normal sweep")

    def _poll_once(self, critical_only: bool = False) -> None:
        raw_events = self._fetch_events()
        if not raw_events:
            return

        # Deduplicate by fingerprint — keep only the MOST RECENT event per fp.
        # The receiver returns events oldest-first, so iterating and overwriting
        # means the last entry for each fp wins.
        #
        # Why this matters: the NDJSON log accumulates both firing AND resolved
        # events for the same fingerprint over time. Without deduplication, the
        # poller processes them in order:
        #   (1) firing  → adds fp to _seen
        #   (2) resolved → pops fp from _seen
        # leaving fp out of _seen every cycle, causing repeated Prometheus calls
        # and, once a new firing event arrives, repeated re-investigation.
        deduped: dict[str, dict] = {}
        for event in raw_events:
            fp = event.get("fingerprint", "")
            if fp:
                deduped[fp] = event  # overwrites → most recent wins
        events = list(deduped.values())

        # One Prometheus call per cycle — build the live (alertname, instance) set
        # used to validate every candidate event without extra round-trips.
        live_alerts = self._fetch_live_alerts()

        new_work: list[dict] = []
        for event in events:
            if critical_only and str(event.get("severity", "")).lower() != "critical":
                continue
            work = self._classify_event(event, live_alerts)
            if work is not None:
                new_work.append(work)
                if len(new_work) >= MAX_PER_CYCLE:
                    break

        for i, event in enumerate(new_work):
            if i > 0:
                self._stop.wait(INTER_ALERT_DELAY)
            if self._stop.is_set():
                break
            self._investigate(event)

    # ── event fetching and classification ─────────────────────────────────────

    def _fetch_events(self) -> list[dict]:
        try:
            resp = httpx.get(
                f"{settings.alert_event_receiver_url}/events",
                params={"limit": 100},
                timeout=10,
            )
            resp.raise_for_status()
            events = resp.json().get("events", [])
            # The event receiver stores device="" even when sysName is in labels.
            # Back-fill it here so topology correlation and incident grouping work
            # correctly on the polling path (webhook path already has device set).
            for e in events:
                if not e.get("device"):
                    labels = e.get("labels") or {}
                    e["device"] = (
                        labels.get("sysName")
                        or labels.get("agent_host")
                        or ""
                    )
            return events
        except Exception as exc:
            logger.warning("AlertPoller: failed to fetch events: %s", exc)
            return []

    def _fetch_live_alerts(self) -> set[tuple[str, str]]:
        """
        Return {(alertname, instance)} for every firing alert in Prometheus.
        One call per poll cycle. Falls back to an empty set on error;
        callers treat empty as "Prometheus unreachable — be permissive".
        """
        try:
            resp = httpx.get(
                f"{settings.prometheus_url}/api/v1/alerts",
                timeout=8,
            )
            resp.raise_for_status()
            alerts = resp.json().get("data", {}).get("alerts", [])
            return {
                (a["labels"].get("alertname", ""), a["labels"].get("instance", ""))
                for a in alerts
                if a.get("state") == "firing"
            }
        except Exception as exc:
            logger.warning("AlertPoller: failed to fetch live Prometheus alerts: %s", exc)
            return set()

    def _is_firing_in_prometheus(
        self, event: dict, live_alerts: set[tuple[str, str]]
    ) -> bool:
        """Check whether this event's alert is still firing, using the pre-fetched set."""
        alertname = event.get("alertname", "")
        instance  = event.get("instance", "")
        if not alertname:
            return False
        # Empty set means Prometheus was unreachable — be conservative, allow through
        if not live_alerts:
            return True
        for (a_name, a_instance) in live_alerts:
            if a_name != alertname:
                continue
            # Match on instance or treat blank instance as wildcard
            if not instance or instance == a_instance:
                return True
        return False

    def _classify_event(
        self, event: dict, live_alerts: set[tuple[str, str]]
    ) -> dict | None:
        """Return the event if it should be investigated, None otherwise."""
        fp        = event.get("fingerprint", "")
        alertname = event.get("alertname", "")
        severity  = str(event.get("severity", "")).lower()
        status    = str(event.get("alert_status", event.get("batch_status", ""))).lower()

        if not fp:
            return None
        if severity not in SEVERITIES:
            return None

        if status == "resolved":
            self._seen.pop(fp, None)
            if fp:
                self._try_close_incident(fp)
            return None

        if status != "firing":
            return None

        seen_key = f"{fp}:{status}"
        if self._seen.get(fp) == seen_key:
            return None

        if not self._is_firing_in_prometheus(event, live_alerts):
            logger.info(
                "AlertPoller: skipping %s fp=%s — no longer firing in Prometheus",
                alertname, fp[:12],
            )
            self._seen[fp] = seen_key
            return None

        self._seen[fp] = seen_key
        logger.info(
            "AlertPoller: new firing alert fp=%s alertname=%s severity=%s",
            fp[:12], alertname, severity,
        )
        return event

    # ── topology-aware correlation ────────────────────────────────────────────

    def _fetch_connected_devices(self, device: str) -> list[str]:
        """
        Return a list of device names directly connected to `device` via Nautobot cables.
        Falls back to empty list on any error so a Nautobot outage never blocks processing.
        """
        try:
            resp = httpx.get(
                f"{settings.nautobot_url}/api/dcim/cables/",
                params={"depth": 1, "limit": 200},
                headers={"Authorization": f"Token {settings.nautobot_token}"},
                timeout=5,
            )
            resp.raise_for_status()
            cables = resp.json().get("results", [])
            peers: set[str] = set()
            for cable in cables:
                for side in ("a_terminations", "b_terminations"):
                    for term in cable.get(side, []):
                        dev_name = (term.get("object") or {}).get("device", {})
                        if isinstance(dev_name, dict):
                            dev_name = dev_name.get("name", "")
                        if dev_name and dev_name != device:
                            peers.add(dev_name)
            return list(peers)
        except Exception as exc:
            logger.debug("AlertPoller: topology lookup failed for device=%s: %s", device, exc)
            return []

    def _find_upstream_rca(self, device: str) -> dict | None:
        """
        Return an active RCA task for a directly connected upstream device, or None.
        Used to link downstream effects (e.g. leaf1/Eth2 down) to an upstream root cause
        (e.g. spine2/Eth1 admin-shutdown) rather than spawning a parallel investigation.
        """
        if not device:
            return None
        peers = self._fetch_connected_devices(device)
        for peer in peers:
            task = self._task_store.get_active_rca_for_device(peer, minutes=15)
            if task:
                return task
        return None

    # ── maintenance window check ───────────────────────────────────────────────

    def _check_maintenance_window(self, device: str) -> bool:
        """
        Return True if the device appears to be in a maintenance window.

        Checks two signals in Nautobot (requires MAINTENANCE_CHECK_ENABLED=true):
        1. Device status matches one of the configured maintenance statuses.
        2. Device carries the configured maintenance tag.

        Falls back to False on any error so a Nautobot outage never suppresses
        alerts silently.
        """
        if not settings.maintenance_check_enabled or not device:
            return False
        try:
            resp = httpx.get(
                f"{settings.nautobot_url}/api/dcim/devices/",
                params={"name": device, "limit": 1},
                headers={"Authorization": f"Token {settings.nautobot_token}"},
                timeout=5,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return False
            dev = results[0]

            # Check device status slug
            status_slug = (dev.get("status") or {}).get("value", "").lower()
            maint_statuses = {s.strip().lower()
                              for s in settings.maintenance_statuses.split(",")}
            if status_slug in maint_statuses:
                logger.info(
                    "AlertPoller: device=%s in maintenance status=%s", device, status_slug
                )
                return True

            # Check for maintenance tag
            tags = [t.get("slug", "") for t in dev.get("tags", [])]
            if settings.maintenance_tag.lower() in tags:
                logger.info(
                    "AlertPoller: device=%s has maintenance tag", device
                )
                return True

        except Exception as exc:
            logger.debug(
                "AlertPoller: maintenance check failed for device=%s: %s — treating as not in maintenance",
                device, exc,
            )
        return False

    def _try_close_incident(self, fp: str) -> None:
        """
        When the primary alert for an incident resolves, mark the incident complete.
        Only fires for the fingerprint that originally created the incident
        (i.e. the incident's own alert_fingerprint).
        """
        try:
            incident = self._task_store.get_open_incident_for_fingerprint(fp)
            if not incident:
                return
            self._task_store.close_incident(
                incident["id"],
                resolution=f"Primary alert {fp[:12]} resolved — auto-closed",
            )
            logger.info(
                "AlertPoller: auto-closed incident=%s on resolve fp=%s",
                incident["id"], fp[:12],
            )
        except Exception as exc:
            logger.warning(
                "AlertPoller: failed to auto-close incident for fp=%s: %s", fp[:12], exc
            )

    # ── investigation ──────────────────────────────────────────────────────────

    def _investigate(self, event: dict) -> None:
        alertname   = event.get("alertname", "UnknownAlert")
        severity    = event.get("severity", "unknown")
        instance    = event.get("instance", "")
        labels      = event.get("labels", {})
        # Prefer sysName (device hostname) or agent_host (device IP) from the
        # Prometheus metric labels, which are richer than the Alertmanager instance
        # label (which is "telegraf:9273" for Telegraf-sourced metrics).
        device      = (
            event.get("device")
            or labels.get("sysName")
            or labels.get("agent_host")
            or instance.split(":")[0]
        )
        fp          = event.get("fingerprint", "")
        summary     = event.get("summary") or ""
        description = event.get("description") or ""

        focus = _ALERT_FOCUS.get(alertname, "investigate the alert and identify root cause")

        # If device looks like a raw IP, tell the agent to resolve it
        device_hint = (
            f"Note: '{device}' appears to be an IP address. Use get_all_devices() "
            f"to find the hostname, or search_nautobot('{device}') to resolve it."
            if device and device.replace(".", "").isdigit() else ""
        )

        prompt = (
            f"AUTOMATED ALERT INVESTIGATION\n\n"
            f"A new {severity.upper()} alert requires investigation:\n\n"
            f"  Alert:       {alertname}\n"
            f"  Device:      {device or 'unknown'}\n"
            f"  Instance:    {instance}\n"
            f"  Severity:    {severity}\n"
            f"  Summary:     {summary}\n"
            f"  Description: {description}\n"
            f"  Fingerprint: {fp}\n\n"
            f"Focus: {focus}\n"
            + (f"\n{device_hint}\n" if device_hint else "")
            + f"\nUse your full toolkit in this order:\n"
            f"1. get_active_alerts() — confirm what is currently firing\n"
            f"2. get_device_metrics(device) — check reachability and interface state\n"
            f"3. get_interface_events(device) / get_bgp_events(device) — check syslog for recent events\n"
            f"4. get_topology() — assess blast radius if relevant\n\n"
            f"End your response with:\n"
            f"DIAGNOSIS: <one sentence root cause>\n"
            f"AFFECTED: <device name or 'unknown'>\n"
            f"ACTION: <recommended next step>\n"
            f"CONFIDENCE: high | medium | low"
        )

        # Check budget BEFORE creating a task — avoids cluttering the queue
        # with tasks that immediately fail. On budget exhaustion we do NOT add
        # to _seen so the alert is retried automatically on the next poll cycle
        # (once the rolling-hour window has freed up capacity).
        try:
            self._rate_limiter.check_budget("ops_agent")
        except BudgetExceededError as exc:
            logger.warning(
                "AlertPoller: budget exceeded for %s (fp=%s) — will retry next cycle: %s",
                alertname, fp[:12], exc,
            )
            return

        # Defence-in-depth: verify the TaskStore has no active task for this
        # fingerprint before creating another one.
        if fp:
            existing = self._task_store.get_active_task_for_fingerprint(fp)
            if existing:
                logger.info(
                    "AlertPoller: task %s already exists for fp=%s (status=%s) — skipping",
                    existing["id"], fp[:12], existing["status"],
                )
                self._seen[fp] = f"{fp}:firing"
                return

        # Alert correlation: if there is already an active RCA for the same device
        # within the last 15 minutes, record this alert on it instead of spawning
        # a parallel pipeline — UNLESS the new alert has higher semantic priority
        # than the existing task (e.g. InterfaceAdminDown arriving after BGPPeerDown).
        # A higher-priority alert always gets its own investigation so the root cause
        # is never swallowed by a downstream-consequence task.
        if device:
            correlated = self._task_store.get_active_rca_for_device(device, minutes=15)
            if correlated:
                existing_alertname = correlated.get("title", "").split(":")[0].strip()
                existing_priority = _ALERT_PRIORITY.get(existing_alertname, 99)
                new_priority      = _ALERT_PRIORITY.get(alertname, 99)
                if new_priority < existing_priority:
                    # New alert is more specific / higher priority — give it its own pipeline.
                    # The existing task gets a note linking it as a downstream consequence.
                    self._task_store.add_event(
                        correlated["id"], "system", "downstream_consequence",
                        {"alertname": alertname, "fingerprint": fp, "summary": summary,
                         "note": f"{alertname} is a higher-priority alert for {device} — spawning dedicated pipeline"},
                    )
                    logger.info(
                        "AlertPoller: priority override — %s (p=%d) spawns new pipeline over %s (p=%d) device=%s",
                        alertname, new_priority, existing_alertname, existing_priority, device,
                    )
                    # fall through to create a new investigation below
                else:
                    self._task_store.add_event(
                        correlated["id"], "system", "alert_correlated",
                        {"alertname": alertname, "fingerprint": fp,
                         "summary": summary, "severity": severity},
                    )
                    self._seen[fp] = f"{fp}:firing"
                    logger.info(
                        "AlertPoller: correlated alert %s (fp=%s) onto existing task=%s for device=%s",
                        alertname, fp[:12], correlated["id"], device,
                    )
                    return

        # Topology-aware correlation: if a directly connected upstream device already
        # has an active RCA, this alert is likely a downstream effect of the same root
        # cause.  Link it onto that task rather than spawning a parallel investigation.
        upstream_rca = self._find_upstream_rca(device)
        if upstream_rca:
            self._task_store.add_event(
                upstream_rca["id"], "system", "downstream_alert",
                {"alertname": alertname, "fingerprint": fp,
                 "device": device, "summary": summary, "severity": severity,
                 "note": f"Downstream effect of root cause on {upstream_rca.get('title', upstream_rca['id'])}"},
            )
            self._seen[fp] = f"{fp}:firing"
            logger.info(
                "AlertPoller: downstream alert %s fp=%s device=%s linked to upstream task=%s",
                alertname, fp[:12], device, upstream_rca["id"],
            )
            return

        # Maintenance window: create a deprioritised, no-auto-execute task
        # rather than skipping the alert entirely — humans can still review it.
        in_maintenance = self._check_maintenance_window(device)
        if in_maintenance:
            logger.info(
                "AlertPoller: device=%s is in maintenance — creating low-priority task "
                "with auto-execute suppressed (alert=%s fp=%s)",
                device, alertname, fp[:12],
            )

        task_priority = "low" if in_maintenance else (
            "high" if severity == "critical" else "normal"
        )

        # Incident grouping: find or create an incident for this alert.
        # An incident groups all correlated alerts within a 30-minute window.
        incident_id: str | None = None
        if device:
            incident = self._task_store.get_open_incident_for_device(device, minutes=30)
            if incident:
                incident_id = incident["id"]
                self._task_store.add_device_to_incident(incident_id, device)
                logger.info(
                    "AlertPoller: linked alert %s (fp=%s) to existing incident=%s",
                    alertname, fp[:12], incident_id,
                )
            else:
                sev_to_severity = {"critical": "P1", "warning": "P2"}
                inc = self._task_store.create_incident(
                    severity=sev_to_severity.get(severity, "P3"),
                    impact=f"{alertname} on {device or instance}",
                    alert_fingerprint=fp,
                    device=device,
                    alertname=alertname,
                )
                incident_id = inc["id"]
                logger.info(
                    "AlertPoller: created new incident=%s for alert %s device=%s",
                    incident_id, alertname, device,
                )

        # ── Workflow path (WORKFLOW_ENABLED=true) ──────────────────────────────
        if settings.workflow_enabled and self._workflow is not None:
            sem = self._investigation_sem

            def _run_workflow():
                sem.acquire()
                try:
                    # Late topology re-check: alerts often arrive in bursts where the
                    # upstream device's RCA task wasn't created yet at classify time.
                    # After a 3-second yield the upstream task is usually visible.
                    time.sleep(3)
                    late_upstream = self._find_upstream_rca(device)
                    if late_upstream:
                        self._task_store.add_event(
                            late_upstream["id"], "system", "downstream_alert",
                            {"alertname": alertname, "fingerprint": fp,
                             "device": device, "summary": summary, "severity": severity,
                             "note": f"Late topology match: downstream effect of {late_upstream.get('title', late_upstream['id'])}"},
                        )
                        self._seen[fp] = f"{fp}:firing"
                        logger.info(
                            "AlertPoller: late topology match — %s fp=%s device=%s linked to upstream task=%s",
                            alertname, fp[:12], device, late_upstream["id"],
                        )
                        return
                    self._workflow.run(
                        event=event,
                        incident_id=incident_id,
                        in_maintenance=in_maintenance,
                        priority=task_priority,
                    )
                finally:
                    sem.release()

            threading.Thread(target=_run_workflow, daemon=True, name=f"wf-{fp[:8]}").start()
            logger.info(
                "AlertPoller: dispatched to workflow fp=%s alert=%s",
                fp[:12], alertname,
            )
            return

        # ── Legacy polling path ────────────────────────────────────────────────
        task = self._task_store.create_task(
            type="rca",
            created_by="system",
            assigned_to="ops_agent",
            title=f"{'[MAINT] ' if in_maintenance else ''}{alertname}: {device or instance}",
            alert_fingerprint=fp,
            priority=task_priority,
            maintenance_window=in_maintenance,
            do_not_auto_execute=in_maintenance,
            incident_id=incident_id,
            content={
                "alertname":   alertname,
                "severity":    severity,
                "device":      device,
                "instance":    instance,
                "summary":     summary,
                "description": description,
                "fingerprint": fp,
            },
        )
        task_id    = task["id"]
        session_id = f"alert-{fp[:12]}"

        self._task_store.claim_task(task_id, "ops_agent")
        self._task_store.start_task(task_id, "ops_agent")

        self._run_investigation(task_id, session_id, prompt, alertname, attempt=1, event=event)

    # ── investigation + handoff ────────────────────────────────────────────────

    def _run_investigation(
        self,
        task_id: str,
        session_id: str,
        prompt: str,
        alertname: str,
        attempt: int,
        event: dict | None = None,
    ) -> None:
        try:
            response, tool_calls = self._agent.chat_with_trace(
                prompt,
                session_id=session_id,
                task_id=task_id,
                task_type="rca",
            )
            # Parse structured fields — uses with_structured_output when available,
            # falls back to regex for Ollama/older models.
            rca, _, rca_parse_failed = parse_structured(
                self._agent.llm, prompt, RcaResult,
                session_config={"configurable": {"thread_id": session_id}},
            )
            if rca_parse_failed:
                self._task_store.add_event(task_id, "ops_agent", "parse_warning",
                                           {"stage": "rca", "detail": "structured output parsing failed — fields may be empty"})
            self._task_store.complete_task(
                task_id,
                "ops_agent",
                result={
                    "response":     response,
                    "tool_calls":   len(tool_calls),
                    "diagnosis":    rca.diagnosis,
                    "affected":     rca.affected,
                    "action":       rca.action,
                    "confidence":   rca.confidence,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info("AlertPoller: completed RCA task=%s alert=%s confidence=%s",
                        task_id, alertname, rca.confidence)

            no_action = any(kw in rca.action.lower() for kw in
                            ("no action", "no fix", "already resolved", "self-healed", "monitor only"))

            if no_action:
                pass  # pipeline ends here cleanly
            elif rca.confidence == "low":
                # Insufficient confidence to drive automated remediation.
                # Escalate directly to human review with the partial findings.
                self._escalate_low_confidence(task_id, event, rca)
            elif event:
                self._create_fix_proposal(task_id, event, rca, response)

        except Exception as exc:
            error_str = str(exc)

            # OpenAI 429 rate-limit — wait and retry once
            if "rate_limit_exceeded" in error_str or "429" in error_str:
                if attempt == 1:
                    logger.warning(
                        "AlertPoller: rate limit hit for task=%s, retrying in %ds",
                        task_id, RATE_LIMIT_BACKOFF,
                    )
                    self._task_store.add_event(
                        task_id, "ops_agent", "rate_limit_retry",
                        {"wait_seconds": RATE_LIMIT_BACKOFF, "attempt": attempt},
                    )
                    self._stop.wait(RATE_LIMIT_BACKOFF)
                    if not self._stop.is_set():
                        self._run_investigation(
                            task_id, session_id, prompt, alertname,
                            attempt=2, event=event,
                        )
                    return
                logger.warning(
                    "AlertPoller: rate limit retry also failed for task=%s", task_id
                )
                self._task_store.fail_task(
                    task_id, "ops_agent",
                    f"OpenAI TPM rate limit exceeded after retry. Error: {error_str[:200]}",
                )
                return

            self._task_store.fail_task(task_id, "ops_agent", error_str[:500])
            logger.exception(
                "AlertPoller: investigation failed task=%s alert=%s", task_id, alertname
            )
            # Remove from _seen so the poller can reinvestigate on next cycle
            # if the alert is still firing, then schedule a task-level retry.
            fp_val = (event or {}).get("fingerprint", "")
            if fp_val:
                self._seen.pop(fp_val, None)
            self._schedule_retry(task_id)

    def _schedule_retry(self, task_id: str) -> None:
        import threading as _t
        def _do_retry():
            self._stop.wait(RETRY_BACKOFF)
            if not self._stop.is_set():
                ok = self._task_store.retry_task(task_id, "ops_agent")
                if ok:
                    logger.info("AlertPoller: re-queued task=%s for retry", task_id)
        _t.Thread(target=_do_retry, daemon=True).start()

    def _escalate_low_confidence(
        self,
        parent_task_id: str,
        event: dict,
        rca: "RcaResult",
    ) -> None:
        """
        Create an approval_gate task directly when RCA confidence is low.
        Skips automated fix proposal and validation to avoid propagating
        an unreliable diagnosis through the pipeline.
        The human sees the partial RCA findings and can act or dismiss.
        """
        alertname   = event.get("alertname", "")
        fingerprint = event.get("fingerprint", "")
        severity    = event.get("severity", "warning")
        affected    = rca.affected or event.get("device", "unknown")

        try:
            gate = self._task_store.create_task(
                type="approval_gate",
                created_by="ops_agent",
                assigned_to="human",
                title=f"LOW CONFIDENCE — Manual review required: {alertname} on {affected}",
                parent_id=parent_task_id,
                alert_fingerprint=fingerprint,
                priority="high" if severity == "critical" else "normal",
                content={
                    "alertname":          alertname,
                    "alert":              event,
                    "escalation_reason":  "low_confidence_rca",
                    "rca": {
                        "diagnosis":          rca.diagnosis,
                        "affected_device":    affected,
                        "recommended_action": rca.action,
                        "confidence":         rca.confidence,
                    },
                    "reason": (
                        f"Ops Agent has low confidence in its diagnosis for {alertname} "
                        f"on {affected}. Automated remediation skipped. "
                        "Please investigate manually."
                    ),
                },
            )
            self._task_store.request_approval(gate["id"], "ops_agent")
            logger.info(
                "AlertPoller: low-confidence escalation gate=%s for alert %s device=%s",
                gate["id"], alertname, affected,
            )
        except Exception as exc:
            logger.error("AlertPoller: failed to create low-confidence gate: %s", exc)

    def _create_fix_proposal(
        self,
        parent_task_id: str,
        event: dict,
        rca: "RcaResult",
        full_response: str,
    ) -> None:
        alertname   = event.get("alertname", "")
        fingerprint = event.get("fingerprint", "")
        severity    = event.get("severity", "normal")
        affected    = rca.affected or event.get("device", "unknown")

        try:
            child = self._task_store.create_task(
                type="fix_proposal",
                created_by="ops_agent",
                assigned_to="eng_agent",
                title=f"Fix: {alertname} on {affected}",
                parent_id=parent_task_id,
                alert_fingerprint=fingerprint,
                priority="high" if severity == "critical" else "normal",
                content={
                    "alertname": alertname,
                    "alert":     event,
                    "rca": {
                        "diagnosis":          rca.diagnosis,
                        "affected_device":    affected,
                        "recommended_action": rca.action,
                        "confidence":         rca.confidence,
                        "full_response":      full_response[-3000:],
                    },
                },
            )
            logger.info(
                "AlertPoller: created fix_proposal task=%s (parent rca=%s)",
                child["id"], parent_task_id,
            )
        except Exception as exc:
            logger.error("AlertPoller: failed to create fix_proposal task: %s", exc)
