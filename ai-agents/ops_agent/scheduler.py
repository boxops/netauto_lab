"""
Ops experiment scheduler backed by APScheduler.

Schedules are kept in memory for the lifetime of the process.
Each job calls the OpsAgent with a preset scenario prompt.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler

if TYPE_CHECKING:
    from ops_agent.agent import OpsAgent

logger = logging.getLogger(__name__)

_JOB_META: dict[str, dict] = {}


class OpsScheduler:
    def __init__(self, agent: "OpsAgent") -> None:
        self._agent = agent
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._scheduler.start()
        logger.info("OpsScheduler started")

    def add_job(self, scenario: str, interval_minutes: int) -> dict:
        job_id = str(uuid.uuid4())[:8]
        session_id = f"scheduled-{job_id}"

        def _run() -> None:
            logger.info("Scheduled job %s running: %s", job_id, scenario[:80])
            _JOB_META[job_id]["last_run"] = datetime.now(timezone.utc).isoformat()
            try:
                self._agent.chat(scenario, session_id=session_id)
                _JOB_META[job_id]["last_status"] = "success"
            except Exception:
                logger.exception("Scheduled job %s failed", job_id)
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
        logger.info("Scheduled job %s every %d min", job_id, interval_minutes)
        return _JOB_META[job_id]

    def remove_job(self, job_id: str) -> bool:
        try:
            self._scheduler.remove_job(job_id)
            _JOB_META.pop(job_id, None)
            return True
        except Exception:
            return False

    def list_jobs(self) -> list[dict]:
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
