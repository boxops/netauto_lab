"""Lightweight shared helpers for custom Nautobot jobs."""

from __future__ import annotations

import json
from datetime import datetime, timezone


class FrameworkJobMixin:
    """Small structured-result helper used by discovery/reporting jobs.

    The existing jobs already call these methods directly, so this mixin keeps
    the API stable without introducing a larger framework layer.
    """

    def begin_framework_run(self, inputs: dict | None = None) -> None:
        self.framework_run_started_at = datetime.now(timezone.utc)
        self.framework_inputs = inputs or {}
        self.framework_events = []

    def record_event(self, level: str, message: str, context: dict | None = None) -> None:
        event = {
            "level": level,
            "message": message,
            "context": context or {},
        }
        self.framework_events.append(event)
        log = getattr(self.logger, level, self.logger.info)
        log(message)

    def record_success(self, target: str, message: str, details: dict | None = None) -> None:
        self.record_event(
            "info",
            f"{target}: {message}",
            {"target": target, "details": details or {}},
        )

    def record_failure(self, target: str, message: str, details: dict | None = None) -> None:
        self.record_event(
            "error",
            f"{target}: {message}",
            {"target": target, "details": details or {}},
        )

    def record_skipped(self, target: str, message: str, details: dict | None = None) -> None:
        self.record_event(
            "warning",
            f"{target}: {message}",
            {"target": target, "details": details or {}},
        )

    def finalize_framework_run(self, filename_prefix: str = "framework_run") -> None:
        payload = {
            "started_at": self.framework_run_started_at.isoformat() if getattr(self, "framework_run_started_at", None) else None,
            "inputs": getattr(self, "framework_inputs", {}),
            "events": getattr(self, "framework_events", []),
        }
        try:
            self.create_file(f"{filename_prefix}.json", json.dumps(payload, indent=2, sort_keys=True))
        except Exception:
            self.logger.info(
                f"Framework run summary for {filename_prefix}: {json.dumps(payload, sort_keys=True)}"
            )
