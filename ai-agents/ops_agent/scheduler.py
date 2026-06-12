"""
Ops experiment scheduler backed by APScheduler.

Interval jobs (created via POST /schedule) are persisted to the scheduled_jobs
table and re-registered on startup, so scheduled experiments survive agent
restarts. APScheduler's own pickle-based job stores are deliberately not used:
our jobs are closures over the agent, and pickled callables break silently
whenever the code moves.

Cron jobs for chaos_schedule intents are NOT persisted here — the standing
intents table is their source of truth and the IntentEvaluator re-registers
them on every evaluation cycle.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

if TYPE_CHECKING:
    from ops_agent.agent import OpsAgent

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OpsScheduler:
    def __init__(self, agent: "OpsAgent", task_store=None) -> None:
        self._agent = agent
        self._ts = task_store
        self._meta: dict[str, dict] = {}
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._scheduler.start()
        if self._ts is not None:
            self._init_table()
            restored = self._restore_jobs()
            logger.info("OpsScheduler started (%d job(s) restored)", restored)
        else:
            logger.info("OpsScheduler started (no persistence — in-memory only)")

    # ── persistence ────────────────────────────────────────────────────────────

    def _init_table(self) -> None:
        with self._ts._lock, self._ts._connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    job_id           TEXT PRIMARY KEY,
                    scenario         TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    session_id       TEXT NOT NULL,
                    created_at       TEXT NOT NULL,
                    last_run         TEXT,
                    last_status      TEXT
                )
            """))

    def _restore_jobs(self) -> int:
        with self._ts._lock, self._ts._connect() as conn:
            rows = conn.execute(text("SELECT * FROM scheduled_jobs")).fetchall()
        for r in rows:
            meta = dict(r._mapping)
            self._register(meta)
            self._meta[meta["job_id"]] = {**meta, "next_run": self._next_run(meta["job_id"])}
        return len(rows)

    def _persist(self, meta: dict) -> None:
        if self._ts is None:
            return
        row = {k: meta.get(k) for k in (
            "job_id", "scenario", "interval_minutes", "session_id",
            "created_at", "last_run", "last_status",
        )}
        with self._ts._lock, self._ts._connect() as conn:
            conn.execute(text(
                "DELETE FROM scheduled_jobs WHERE job_id = :job_id"), {"job_id": row["job_id"]})
            conn.execute(text(
                "INSERT INTO scheduled_jobs "
                "(job_id, scenario, interval_minutes, session_id, created_at, last_run, last_status) "
                "VALUES (:job_id, :scenario, :interval_minutes, :session_id, "
                " :created_at, :last_run, :last_status)"), row)

    def _unpersist(self, job_id: str) -> None:
        if self._ts is None:
            return
        with self._ts._lock, self._ts._connect() as conn:
            conn.execute(text("DELETE FROM scheduled_jobs WHERE job_id = :id"), {"id": job_id})

    # ── interval jobs (POST /schedule) ─────────────────────────────────────────

    def _register(self, meta: dict) -> None:
        """Attach an APScheduler interval job for the given metadata."""
        job_id   = meta["job_id"]
        scenario = meta["scenario"]
        session  = meta["session_id"]

        def _run() -> None:
            logger.info("Scheduled job %s running: %s", job_id, scenario[:80])
            self._meta[job_id]["last_run"] = _now_iso()
            try:
                self._agent.chat(scenario, session_id=session)
                self._meta[job_id]["last_status"] = "success"
            except Exception:
                logger.exception("Scheduled job %s failed", job_id)
                self._meta[job_id]["last_status"] = "error"
            self._persist(self._meta[job_id])

        self._scheduler.add_job(
            _run,
            trigger="interval",
            minutes=meta["interval_minutes"],
            id=job_id,
            replace_existing=True,
        )

    def _next_run(self, job_id: str) -> str | None:
        job = self._scheduler.get_job(job_id)
        return job.next_run_time.isoformat() if job and job.next_run_time else None

    def add_job(self, scenario: str, interval_minutes: int) -> dict:
        job_id = str(uuid.uuid4())[:8]
        meta = {
            "job_id":           job_id,
            "scenario":         scenario,
            "interval_minutes": interval_minutes,
            "session_id":       f"scheduled-{job_id}",
            "created_at":       _now_iso(),
            "last_run":         None,
            "last_status":      None,
        }
        self._meta[job_id] = meta
        self._register(meta)
        self._persist(meta)
        meta["next_run"] = self._next_run(job_id)
        logger.info("Scheduled job %s every %d min", job_id, interval_minutes)
        return meta

    def remove_job(self, job_id: str) -> bool:
        try:
            self._scheduler.remove_job(job_id)
            self._meta.pop(job_id, None)
            self._unpersist(job_id)
            return True
        except Exception:
            return False

    def list_jobs(self) -> list[dict]:
        result = []
        for job_id, meta in self._meta.items():
            entry = dict(meta)
            entry["next_run"] = self._next_run(job_id)
            result.append(entry)
        return result

    # ── cron jobs (chaos_schedule intents — re-registered by IntentEvaluator) ──

    def add_cron_job(
        self,
        intent_id: str,
        scenario: str,
        cron_expr: str,
        on_fire_fn: Callable[[bool], None],
    ) -> None:
        """Register a cron-triggered job for a chaos_schedule intent."""
        def _run() -> None:
            logger.info("Chaos intent %s firing: %s", intent_id, scenario[:80])
            try:
                self._agent.chat(scenario, session_id=f"intent-{intent_id}")
                on_fire_fn(True)
            except Exception:
                logger.exception("Chaos intent %s failed", intent_id)
                on_fire_fn(False)

        self._scheduler.add_job(
            _run,
            trigger=CronTrigger.from_crontab(cron_expr, timezone="UTC"),
            id=f"intent-{intent_id}",
            replace_existing=True,
        )
        logger.info("Chaos intent %s scheduled: %s", intent_id, cron_expr)

    def remove_cron_job(self, intent_id: str) -> None:
        """Remove a cron job registered for a chaos_schedule intent."""
        try:
            self._scheduler.remove_job(f"intent-{intent_id}")
            logger.info("Chaos intent %s cron job removed", intent_id)
        except Exception:
            pass

    def list_cron_job_ids(self) -> set[str]:
        """Return set of intent IDs that currently have active cron jobs."""
        return {
            j.id.removeprefix("intent-")
            for j in self._scheduler.get_jobs()
            if j.id.startswith("intent-")
        }

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
