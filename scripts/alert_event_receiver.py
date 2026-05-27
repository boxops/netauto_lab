#!/usr/bin/env python3
"""Alertmanager webhook receiver for observability event intake.

This service ingests Alertmanager webhook payloads, normalizes a compact
event record per alert, and appends them to an NDJSON event log for
downstream correlation and automation workflows.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


HOST = os.environ.get("ALERT_EVENT_RECEIVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("ALERT_EVENT_RECEIVER_PORT", "8770"))
EVENT_LOG_PATH = os.environ.get("ALERT_EVENT_LOG_PATH", "/tmp/alert_events.ndjson")

_WRITE_LOCK = threading.Lock()


class _Stats:
    def __init__(self) -> None:
        self.total_events = 0
        self.total_batches = 0
        self.last_event_at = ""

    def to_dict(self) -> dict:
        return {
            "total_events": self.total_events,
            "total_batches": self.total_batches,
            "last_event_at": self.last_event_at,
        }


STATS = _Stats()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_alert(alert: dict, batch_meta: dict) -> dict:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    return {
        "received_at": _utc_now(),
        "receiver": batch_meta.get("receiver", ""),
        "group_key": batch_meta.get("groupKey", ""),
        "batch_status": batch_meta.get("status", ""),
        "alert_status": alert.get("status", ""),
        "starts_at": alert.get("startsAt", ""),
        "ends_at": alert.get("endsAt", ""),
        "fingerprint": alert.get("fingerprint", ""),
        "alertname": labels.get("alertname", ""),
        "severity": labels.get("severity", ""),
        "category": labels.get("category", ""),
        "instance": labels.get("instance", ""),
        "device": labels.get("device", ""),
        "summary": annotations.get("summary", ""),
        "description": annotations.get("description", ""),
        "runbook": annotations.get("runbook", ""),
        "labels": labels,
        "annotations": annotations,
    }


def _append_events(events: list[dict]) -> None:
    os.makedirs(os.path.dirname(EVENT_LOG_PATH), exist_ok=True)
    with _WRITE_LOCK:
        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        STATS.total_batches += 1
        STATS.total_events += len(events)
        STATS.last_event_at = _utc_now()


def _read_recent_events(limit: int) -> list[dict]:
    if not os.path.exists(EVENT_LOG_PATH):
        return []

    with _WRITE_LOCK:
        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    events = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


class AlertWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _json_response(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if self.path == "/health":
            self._json_response(200, {"status": "ok", "stats": STATS.to_dict()})
            return
        if parsed.path == "/events":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = min(max(limit, 1), 200)
            events = _read_recent_events(limit)
            self._json_response(
                200,
                {
                    "count": len(events),
                    "events": events,
                },
            )
            return
        self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/alertmanager/webhook":
            self._json_response(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json_response(400, {"error": "invalid JSON"})
            return

        alerts = payload.get("alerts", [])
        if not isinstance(alerts, list):
            self._json_response(400, {"error": "payload.alerts must be a list"})
            return

        batch_meta = {
            "receiver": payload.get("receiver", ""),
            "groupKey": payload.get("groupKey", ""),
            "status": payload.get("status", ""),
        }
        events = [_normalize_alert(alert, batch_meta) for alert in alerts]
        _append_events(events)

        self._json_response(
            202,
            {
                "accepted": True,
                "ingested": len(events),
                "event_log_path": EVENT_LOG_PATH,
            },
        )


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), AlertWebhookHandler)
    print(f"Alert event receiver listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()