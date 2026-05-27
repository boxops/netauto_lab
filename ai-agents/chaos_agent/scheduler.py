"""
Chaos experiment scheduler backed by APScheduler.

Schedules are kept in memory for the lifetime of the process.
Each job calls the ChaosAgent with a preset scenario prompt.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler

if TYPE_CHECKING:
    from chaos_agent.agent import ChaosAgent

logger = logging.getLogger(__name__)

_JOB_META: dict[str, dict] = {}


class ChaosScheduler:
    def __init__(self, agent: "ChaosAgent") -> None:
        self._agent = agent
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._scheduler.start()
        logger.info("ChaosScheduler started")

    def add_job(self, scenario: str, interval_minutes: int) -> dict:
        """Schedule a chaos scenario to run every interval_minutes minutes."""
        job_id = str(uuid.uuid4())[:8]
        session_id = f"scheduled-{job_id}"

        def _run() -> None:
            logger.info("Scheduled chaos job %s running: %s", job_id, scenario[:80])
            _JOB_META[job_id]["last_run"] = datetime.now(timezone.utc).isoformat()
            try:
                self._agent.chat(scenario, session_id=session_id)
                _JOB_META[job_id]["last_status"] = "success"
            except Exception:
                logger.exception("Scheduled chaos job %s failed", job_id)
                _JOB_META[job_id]["last_status"] = "error"

        job = self._scheduler.add_job(
            _run,
            trigger="interval",
            minutes=interval_minutes,
            id=job_id,
            replace_existing=True,
        )

        _JOB_META[job_id] = {
            "job_id": job_id,
            "scenario": scenario,
            "interval_minutes": interval_minutes,
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "last_run": None,
            "last_status": None,
        }
        logger.info("Scheduled chaos job %s every %d min", job_id, interval_minutes)
        return _JOB_META[job_id]

    def remove_job(self, job_id: str) -> bool:
        """Cancel a scheduled job. Returns True if removed, False if not found."""
        try:
            self._scheduler.remove_job(job_id)
            _JOB_META.pop(job_id, None)
            return True
        except Exception:
            return False

    def list_jobs(self) -> list[dict]:
        """Return all active scheduled jobs with their current next_run_time."""
        result = []
        for job_id, meta in _JOB_META.items():
            job = self._scheduler.get_job(job_id)
            entry = dict(meta)
            entry["next_run"] = (
                job.next_run_time.isoformat() if job and job.next_run_time else None
            )
            result.append(entry)
        return result

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
