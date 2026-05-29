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

logger = logging.getLogger(__name__)

# Structured-tail keys the ops agent is prompted to emit
_RCA_KEYS = {"DIAGNOSIS", "AFFECTED", "ACTION", "CONFIDENCE"}

POLL_INTERVAL       = 60    # seconds between poll cycles
STARTUP_DELAY       = 30    # seconds to wait before the very first poll
INTER_ALERT_DELAY   = 20    # seconds between consecutive investigations
MAX_PER_CYCLE       = 2     # max new investigations to start per poll cycle
RATE_LIMIT_BACKOFF  = 70    # seconds to wait after a 429 before retrying

SEVERITIES = {"critical", "warning"}

_ALERT_FOCUS = {
    "InterfaceDown":            "interface is operationally down — check link state on both sides",
    "InterfaceAdminDown":       "interface was admin-shutdown — determine if intentional or a chaos/config event",
    "BGPPeerDown":              "BGP session is not Established — check for link flaps, config drift, or route policy issues",
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

    def __init__(self, agent, task_store, rate_limiter) -> None:
        self._agent        = agent
        self._task_store   = task_store
        self._rate_limiter = rate_limiter
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
        # Grace period: let any in-flight resolved webhooks arrive before the
        # first poll so we don't process already-resolved firing events.
        if self._stop.wait(STARTUP_DELAY):
            return
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("AlertPoller: unhandled error in poll cycle")
            self._stop.wait(POLL_INTERVAL)

    def _poll_once(self) -> None:
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
            return resp.json().get("events", [])
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

        task = self._task_store.create_task(
            type="rca",
            created_by="system",
            assigned_to="ops_agent",
            title=f"{alertname}: {device or instance}",
            alert_fingerprint=fp,
            priority="high" if severity == "critical" else "normal",
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

    # ── structured output parsing ──────────────────────────────────────────────

    @staticmethod
    def _parse_tail(text: str, expected_keys: set) -> dict:
        """Extract KEY: value pairs from anywhere in an agent response."""
        import re
        result = {}
        for line in text.split("\n"):
            m = re.match(r"^([A-Z][A-Z_]+):\s*(.+)$", line.strip())
            if m and m.group(1) in expected_keys:
                result[m.group(1)] = m.group(2).strip()
        return result

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
            parsed = self._parse_tail(response, _RCA_KEYS)
            self._task_store.complete_task(
                task_id,
                "ops_agent",
                result={
                    "response":     response,
                    "tool_calls":   len(tool_calls),
                    "diagnosis":    parsed.get("DIAGNOSIS", ""),
                    "affected":     parsed.get("AFFECTED", ""),
                    "action":       parsed.get("ACTION", ""),
                    "confidence":   parsed.get("CONFIDENCE", ""),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info("AlertPoller: completed RCA task=%s alert=%s", task_id, alertname)

            # Escalate to engineering agent unless ops explicitly recommends no action
            action_lower = parsed.get("ACTION", "").lower()
            no_action = any(kw in action_lower for kw in
                            ("no action", "no fix", "already resolved", "self-healed", "monitor only"))
            if not no_action and event:
                self._create_fix_proposal(task_id, event, parsed, response)

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

    def _create_fix_proposal(
        self,
        parent_task_id: str,
        event: dict,
        parsed_rca: dict,
        full_response: str,
    ) -> None:
        alertname      = event.get("alertname", "")
        fingerprint    = event.get("fingerprint", "")
        severity       = event.get("severity", "normal")
        affected       = parsed_rca.get("AFFECTED", "") or event.get("device", "unknown")

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
                    "alertname":    alertname,
                    "alert":        event,
                    "rca": {
                        "diagnosis":          parsed_rca.get("DIAGNOSIS", ""),
                        "affected_device":    affected,
                        "recommended_action": parsed_rca.get("ACTION", ""),
                        "confidence":         parsed_rca.get("CONFIDENCE", ""),
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
