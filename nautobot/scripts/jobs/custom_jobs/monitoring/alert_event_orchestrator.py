"""Purpose: Convert alert intake events into approval-gated remediation proposals."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from django.conf import settings
from django.contrib.auth import get_user_model
from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, StringVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.choices import JobResultStatusChoices
from nautobot.extras.models import JobResult


name = "Monitoring"


DEFAULT_EVENT_RECEIVER_URL = "http://alert-event-receiver:8770"


ALERT_ACTION_POLICY = {
    "DeviceDown": {
        "playbook": "health_check.yml",
        "risk": "medium",
        "priority": "high",
        "summary": "Validate reachability and gather diagnostics before any disruptive action.",
    },
    "BGPPeerDown": {
        "playbook": "validate_bgp.yml",
        "risk": "medium",
        "priority": "high",
        "summary": "Collect BGP state and neighbor evidence; escalate before session resets.",
    },
    "InterfaceHighErrorRate": {
        "playbook": "health_check.yml",
        "risk": "low",
        "priority": "medium",
        "summary": "Collect interface counters and error trends for root-cause triage.",
    },
    "HighInterfaceUtilization": {
        "playbook": "validate_routing.yml",
        "risk": "low",
        "priority": "medium",
        "summary": "Validate pathing and hotspot interfaces before traffic engineering changes.",
    },
}


class AlertEventOrchestrator(Job):
    """Generate semi-automated, approval-gated response proposals from alert events."""

    receiver_url = StringVar(
        description="Base URL for the alert event receiver service",
        default=DEFAULT_EVENT_RECEIVER_URL,
    )
    event_limit = IntegerVar(
        description="Maximum number of recent alert events to evaluate",
        default=50,
        min_value=1,
        max_value=200,
    )
    severities = StringVar(
        description="Comma-separated severities to include (example: critical,warning)",
        default="critical,warning",
        required=False,
    )
    include_resolved = BooleanVar(
        description="Include resolved alerts in proposal generation",
        default=False,
        required=False,
    )
    dry_run = BooleanVar(
        description="Preview proposals without creating automation tasks",
        default=True,
        required=False,
    )
    queue_pending_intents = BooleanVar(
        description=(
            "Create Nautobot PENDING JobResult intent records for operator approval "
            "(no execution is triggered)"
        ),
        default=False,
        required=False,
    )
    max_intents = IntegerVar(
        description="Maximum number of pending intents to create in one run",
        default=25,
        min_value=1,
        max_value=200,
        required=False,
    )

    class Meta:
        name = "Alert Event Orchestrator"
        description = (
            "Consume alert intake events and generate approval-gated response proposals "
            "for closed-loop operations. No live remediation is executed by this job."
        )
        has_sensitive_variables = False
        soft_time_limit = 300
        time_limit = 600
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
        ]

    def _safe_create_file(self, filename: str, content: str) -> None:
        try:
            self.create_file(filename, content)
        except Exception:
            self.logger.info(f"Artifact not attached (non-interactive run): {filename}")

    def _fetch_recent_events(self, receiver_url: str, event_limit: int) -> list[dict]:
        qs = urllib.parse.urlencode({"limit": event_limit})
        endpoint = f"{receiver_url.rstrip('/')}/events?{qs}"
        req = urllib.request.Request(endpoint, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload.get("events", [])

    def _lookup_device(self, event: dict) -> Device | None:
        device_hint = (event.get("device") or "").strip()
        instance_hint = (event.get("instance") or "").strip()

        if device_hint:
            return Device.objects.filter(name=device_hint).first()

        if instance_hint:
            # Alertmanager commonly carries host:port in instance labels.
            host_part = instance_hint.split(":", 1)[0]
            dev = Device.objects.filter(name=host_part).first()
            if dev:
                return dev
            return Device.objects.filter(primary_ip4__host=host_part).first()

        return None

    def _build_proposal(self, event: dict) -> dict:
        alertname = event.get("alertname", "UnknownAlert")
        policy = ALERT_ACTION_POLICY.get(
            alertname,
            {
                "playbook": "health_check.yml",
                "risk": "low",
                "priority": "medium",
                "summary": "Run baseline diagnostics and route to operator review.",
            },
        )

        device = self._lookup_device(event)
        labels = event.get("labels", {})
        annotations = event.get("annotations", {})

        return {
            "fingerprint": event.get("fingerprint", ""),
            "alertname": alertname,
            "severity": event.get("severity", "unknown"),
            "category": event.get("category", "unknown"),
            "status": event.get("alert_status", event.get("batch_status", "unknown")),
            "instance": event.get("instance", ""),
            "device": device.name if device else (event.get("device") or "unmapped"),
            "location": device.location.name if device and device.location else "unknown",
            "platform": device.platform.network_driver if device and device.platform else "unknown",
            "recommended_playbook": policy["playbook"],
            "recommended_scope": [device.name] if device else [],
            "requires_approval": True,
            "default_check_mode": True,
            "risk": policy["risk"],
            "priority": policy["priority"],
            "proposal_summary": policy["summary"],
            "operator_context": {
                "summary": event.get("summary") or annotations.get("summary", ""),
                "description": event.get("description") or annotations.get("description", ""),
                "runbook": event.get("runbook") or annotations.get("runbook", ""),
                "labels": labels,
            },
        }

    def _to_markdown(self, proposals: list[dict]) -> str:
        lines = [
            "# Alert Event Orchestration Proposals",
            "",
            "| Alert | Severity | Device | Playbook | Approval | Check Mode | Risk |",
            "|---|---|---|---|---|---|---|",
        ]
        for p in proposals:
            lines.append(
                f"| {p['alertname']} | {p['severity']} | {p['device']} | {p['recommended_playbook']} | "
                f"{p['requires_approval']} | {p['default_check_mode']} | {p['risk']} |"
            )
        return "\n".join(lines) + "\n"

    def _resolve_request_user(self):
        """Best-effort user resolution for non-interactive and UI-triggered runs."""
        user = getattr(self, "user", None)
        if user is not None:
            return user

        request = getattr(self, "request", None)
        req_user = getattr(request, "user", None)
        if req_user is not None and getattr(req_user, "is_authenticated", False):
            return req_user

        User = get_user_model()
        fallback = User.objects.filter(is_superuser=True, is_active=True).order_by("id").first()
        return fallback

    def _intent_name(self, proposal: dict) -> str:
        return (
            f"[PENDING_APPROVAL] {proposal.get('alertname', 'UnknownAlert')}"
            f" -> {proposal.get('device', 'unmapped')}"
            f" ({proposal.get('fingerprint', 'no-fingerprint')[:12]})"
        )

    def _intent_exists(self, proposal: dict) -> bool:
        return JobResult.objects.filter(
            status=JobResultStatusChoices.STATUS_PENDING,
            name=self._intent_name(proposal),
            job_model=self.job_model,
        ).exists()

    def _create_pending_intent(self, proposal: dict, user) -> JobResult:
        kwargs_payload = {
            "intent_type": "closed_loop_alert_response",
            "requires_approval": True,
            "default_check_mode": True,
            "proposal": proposal,
        }

        return JobResult.objects.create(
            name=self._intent_name(proposal),
            job_model=self.job_model,
            user=user,
            status=JobResultStatusChoices.STATUS_PENDING,
            result={
                "state": "AWAITING_APPROVAL",
                "summary": proposal.get("proposal_summary", ""),
                "recommended_playbook": proposal.get("recommended_playbook", ""),
            },
            task_kwargs=kwargs_payload,
            celery_kwargs={"queued_by": "AlertEventOrchestrator", "queued": False},
        )

    def run(
        self,
        receiver_url=DEFAULT_EVENT_RECEIVER_URL,
        event_limit=50,
        severities="critical,warning",
        include_resolved=False,
        dry_run=True,
        queue_pending_intents=False,
        max_intents=25,
    ):
        wanted_severities = {
            s.strip().lower() for s in severities.split(",") if s.strip()
        }
        try:
            events = self._fetch_recent_events(receiver_url, event_limit)
        except Exception as exc:
            self.logger.error(f"Failed to fetch alert events: {exc}")
            return

        filtered_events = []
        for event in events:
            sev = str(event.get("severity", "")).lower()
            status = str(event.get("alert_status", event.get("batch_status", ""))).lower()
            if wanted_severities and sev not in wanted_severities:
                continue
            if not include_resolved and status == "resolved":
                continue
            filtered_events.append(event)

        seen = set()
        deduped_events = []
        for event in filtered_events:
            key = (event.get("fingerprint", ""), event.get("alert_status", ""), event.get("instance", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped_events.append(event)

        proposals = [self._build_proposal(event) for event in deduped_events]

        created_intents = 0
        skipped_existing_intents = 0
        if queue_pending_intents:
            if dry_run:
                self.logger.warning(
                    "queue_pending_intents=True ignored because dry_run=True. "
                    "Set dry_run=False to persist pending intent records."
                )
            else:
                request_user = self._resolve_request_user()
                if request_user is None:
                    self.logger.warning(
                        "No valid user resolved for intent creation; skipping pending intent writes."
                    )
                else:
                    for proposal in proposals:
                        if created_intents >= max_intents:
                            break
                        if self._intent_exists(proposal):
                            skipped_existing_intents += 1
                            continue
                        self._create_pending_intent(proposal, request_user)
                        created_intents += 1

        payload = {
            "job": "Alert Event Orchestrator",
            "dry_run": dry_run,
            "receiver_url": receiver_url,
            "evaluated_events": len(events),
            "filtered_events": len(deduped_events),
            "proposal_count": len(proposals),
            "queue_pending_intents": queue_pending_intents,
            "created_intents": created_intents,
            "skipped_existing_intents": skipped_existing_intents,
            "proposals": proposals,
        }

        self.logger.info(
            f"Alert orchestration complete: {len(events)} events evaluated, "
            f"{len(proposals)} proposal(s) generated."
        )

        if dry_run:
            self.logger.info("DRY RUN mode enabled. No automation tasks were created.")
        elif queue_pending_intents:
            self.logger.info(
                f"Pending approval intents created: {created_intents}; "
                f"duplicates skipped: {skipped_existing_intents}."
            )

        self._safe_create_file("alert_orchestration_proposals.json", json.dumps(payload, indent=2, sort_keys=True))
        self._safe_create_file("alert_orchestration_proposals.md", self._to_markdown(proposals))


register_jobs(AlertEventOrchestrator)