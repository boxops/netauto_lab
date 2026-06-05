"""
Approval gate notification dispatcher.

Sends notifications when a task enters awaiting_approval, based on the
NOTIFICATION_CHANNEL setting:
  - webhook:    Generic HTTP POST (existing APPROVAL_WEBHOOK_URL behaviour)
  - slack:      Slack incoming webhook with formatted blocks
  - pagerduty:  PagerDuty Events API v2 — creates/resolves incidents

All channels are tried in order; failures are logged but never raise so
a notification failure never blocks the pipeline.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)

# ── helpers ────────────────────────────────────────────────────────────────────

def _sign_payload(body: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _risk_emoji(risk: str) -> str:
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(risk.lower(), "⚪")


def _approval_url(task_id: str) -> str:
    return f"{settings.agent_ui_url}/pipeline?task={task_id}"


# ── channel implementations ────────────────────────────────────────────────────

def _send_generic_webhook(task: dict) -> None:
    """Send HMAC-signed JSON payload to APPROVAL_WEBHOOK_URL."""
    if not settings.approval_webhook_url:
        return
    content = {}
    try:
        content = json.loads(task.get("content") or "{}")
    except Exception:
        pass

    payload = {
        "event":        "approval_required",
        "task_id":      task["id"],
        "task_type":    task.get("type", ""),
        "title":        task.get("title", ""),
        "priority":     task.get("priority", "normal"),
        "created_at":   task.get("created_at", ""),
        "approve_url":  _approval_url(task["id"]),
        "fix_proposal": content.get("fix_proposal", {}),
        "rca":          content.get("rca", {}),
    }
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if settings.approval_webhook_secret:
        headers["X-Hub-Signature-256"] = _sign_payload(body, settings.approval_webhook_secret)

    resp = httpx.post(settings.approval_webhook_url, content=body, headers=headers, timeout=10)
    resp.raise_for_status()
    logger.info("Notification: generic webhook delivered for task=%s", task["id"])


def _send_slack(task: dict) -> None:
    """Post a formatted Slack block message via incoming webhook."""
    if not settings.slack_webhook_url:
        return
    content = {}
    try:
        content = json.loads(task.get("content") or "{}")
    except Exception:
        pass

    fix    = content.get("fix_proposal", {})
    rca    = content.get("rca", {})
    risk   = fix.get("risk", "unknown")
    device = fix.get("device") or rca.get("affected_device", "unknown")

    payload = {
        "text": f"Approval required: {task.get('title', task['id'])}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{_risk_emoji(risk)} Approval Required"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Alert:*\n{content.get('alertname', 'N/A')}"},
                    {"type": "mrkdwn", "text": f"*Device:*\n{device}"},
                    {"type": "mrkdwn", "text": f"*Risk:*\n{risk.upper()}"},
                    {"type": "mrkdwn", "text": f"*Fix type:*\n{fix.get('fix_type', 'N/A')}"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Diagnosis:* {rca.get('diagnosis', 'N/A')}\n"
                        f"*Proposed fix:* {fix.get('reason', 'N/A')}"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Review & Approve"},
                        "url":  _approval_url(task["id"]),
                        "style": "primary",
                    },
                ],
            },
        ],
    }
    resp = httpx.post(settings.slack_webhook_url, json=payload, timeout=10)
    resp.raise_for_status()
    logger.info("Notification: Slack message delivered for task=%s", task["id"])


def _send_pagerduty(task: dict) -> None:
    """Create a PagerDuty incident via Events API v2."""
    if not settings.pagerduty_routing_key:
        return
    content = {}
    try:
        content = json.loads(task.get("content") or "{}")
    except Exception:
        pass

    fix    = content.get("fix_proposal", {})
    rca    = content.get("rca", {})
    risk   = fix.get("risk", "unknown")
    device = fix.get("device") or rca.get("affected_device", "unknown")

    severity_map = {"high": "critical", "medium": "warning", "low": "info"}
    pd_severity  = severity_map.get(risk.lower(), "warning")

    payload = {
        "routing_key":  settings.pagerduty_routing_key,
        "event_action": "trigger",
        "dedup_key":    f"netauto-approval-{task['id']}",
        "payload": {
            "summary":   task.get("title", f"Approval required: {task['id']}"),
            "source":    device,
            "severity":  pd_severity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "custom_details": {
                "task_id":    task["id"],
                "fix_type":   fix.get("fix_type"),
                "commands":   fix.get("commands"),
                "diagnosis":  rca.get("diagnosis"),
                "approve_url": _approval_url(task["id"]),
            },
        },
        "links": [{"href": _approval_url(task["id"]), "text": "Review & Approve"}],
    }
    resp = httpx.post(
        "https://events.pagerduty.com/v2/enqueue",
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    logger.info("Notification: PagerDuty event delivered for task=%s", task["id"])


# ── public dispatcher ──────────────────────────────────────────────────────────

def notify_approval_required(task: dict) -> None:
    """
    Dispatch approval-required notifications across all configured channels.
    Each channel is attempted independently; failures in one do not block others.
    """
    for name, fn in [
        ("generic_webhook", _send_generic_webhook),
        ("slack",           _send_slack),
        ("pagerduty",       _send_pagerduty),
    ]:
        try:
            fn(task)
        except Exception as exc:
            logger.warning(
                "Notification: %s delivery failed for task=%s: %s",
                name, task.get("id", "?"), exc,
            )
