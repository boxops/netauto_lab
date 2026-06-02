"""
Shared task queue and feedback store for multi-agent closed-loop automation.

Storage backend is selected by the TASK_DB_URL environment variable:
  SQLite (default):  sqlite:///./activity.db
  PostgreSQL:        postgresql+psycopg2://agent:pass@agent-postgres:5432/agent_tasks

All public method signatures are unchanged from the original SQLite implementation
so no caller code needs to be modified when switching backends.

Task lifecycle:
    pending → claimed → running → complete | failed | rejected
                                → awaiting_approval → complete | rejected

Task types:
    rca            – Root Cause Analysis, assigned to ops_agent
    fix_proposal   – Remediation plan, assigned to eng_agent
    validation     – Chaos agent verifies the fix proposal
    approval_gate  – Human must approve before execution
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

from sqlalchemy import create_engine, event as sa_event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

# ── URL resolution ─────────────────────────────────────────────────────────────

_DEFAULT_DB   = os.environ.get("ACTIVITY_DB_PATH", "./activity.db")
# Treat empty string the same as unset — docker-compose passes "" when the
# variable is defined but blank in .env (e.g. TASK_DB_URL=)
_TASK_DB_URL  = os.environ.get("TASK_DB_URL", "").strip() or f"sqlite:///{_DEFAULT_DB}"


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @sa_event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


# ── Schema ─────────────────────────────────────────────────────────────────────

_DDL_COMMON = [
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id                  TEXT PRIMARY KEY,
        parent_id           TEXT REFERENCES tasks(id),
        incident_id         TEXT REFERENCES tasks(id),
        alert_fingerprint   TEXT,
        type                TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'pending',
        priority            TEXT NOT NULL DEFAULT 'normal',
        created_by          TEXT NOT NULL,
        assigned_to         TEXT,
        title               TEXT,
        content             TEXT NOT NULL,
        result              TEXT,
        created_at          TEXT NOT NULL,
        claimed_at          TEXT,
        completed_at        TEXT,
        retry_count         INTEGER NOT NULL DEFAULT 0,
        maintenance_window  INTEGER NOT NULL DEFAULT 0,
        do_not_auto_execute INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT NOT NULL REFERENCES tasks(id),
        timestamp   TEXT NOT NULL,
        agent       TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        detail      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_feedback (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT NOT NULL REFERENCES tasks(id),
        from_agent  TEXT NOT NULL,
        verdict     TEXT NOT NULL,
        confidence  REAL,
        notes       TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_assigned  ON tasks(assigned_to, status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_type      ON tasks(type, status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_alert     ON tasks(alert_fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_task_events_tid ON task_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_tid    ON task_feedback(task_id)",
]

# Safe migrations — IGNORE errors so they are idempotent on existing DBs.
_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN maintenance_window INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN do_not_auto_execute INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN incident_id TEXT",
]

_VALID_STATUSES = frozenset({
    "pending", "claimed", "running",
    "awaiting_approval", "complete", "failed", "rejected",
})
_VALID_TYPES = frozenset({
    "rca", "fix_proposal", "validation", "approval_gate", "incident",
})
_VALID_PRIORITIES = frozenset({"critical", "high", "normal", "low"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _short_id(prefix: str = "") -> str:
    uid = str(uuid.uuid4())[:8]
    return f"{prefix}-{uid}" if prefix else uid


def _row_to_dict(row) -> dict:
    """Convert a SQLAlchemy Row to a plain dict."""
    return dict(row._mapping)


# ── TaskStore ──────────────────────────────────────────────────────────────────

class TaskStore:
    """Thread-safe task queue backed by SQLite (default) or PostgreSQL."""

    def __init__(
        self,
        db_path: str | None = None,
        db_url:  str | None = None,
    ) -> None:
        """
        Accepts either:
          db_path="./activity.db"               (backward-compatible SQLite)
          db_url="postgresql+psycopg2://..."    (PostgreSQL)
          db_url="sqlite:///./activity.db"      (explicit SQLite)
        The TASK_DB_URL env var is the production override for both.
        """
        if db_url:
            url = db_url
        elif db_path:
            url = f"sqlite:///{db_path}"
        else:
            url = _TASK_DB_URL

        self._engine  = _make_engine(url)
        self._dialect = self._engine.dialect.name  # "sqlite" or "postgresql"
        self._lock    = threading.Lock()
        self._init_schema()

    # ── private helpers ───────────────────────────────────────────────────────

    @contextmanager
    def _connect(self) -> Generator[Any, None, None]:
        with self._engine.begin() as conn:
            yield conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            for stmt in _DDL_COMMON:
                conn.execute(text(stmt))
            for migration in _MIGRATIONS:
                try:
                    conn.execute(text(migration))
                except Exception:
                    pass  # column already exists

    def _jex(self, column: str, path: str) -> str:
        """Return dialect-specific JSON extraction SQL for a TEXT column."""
        if self._dialect == "postgresql":
            parts = [p for p in path.lstrip("$.").split(".") if p]
            expr = f"({column})::json"
            for p in parts[:-1]:
                expr = f"({expr}->'{p}')"
            if parts:
                expr = f"{expr}->>'{parts[-1]}'"
            return expr
        return f"json_extract({column}, '{path}')"

    # ── create ────────────────────────────────────────────────────────────────

    def create_task(
        self,
        *,
        type: str,
        created_by: str,
        content: dict | str,
        title: str = "",
        assigned_to: str | None = None,
        parent_id: str | None = None,
        alert_fingerprint: str | None = None,
        priority: str = "normal",
        maintenance_window: bool = False,
        do_not_auto_execute: bool = False,
        incident_id: str | None = None,
    ) -> dict:
        if type not in _VALID_TYPES:
            raise ValueError(f"Invalid task type {type!r}. Valid: {_VALID_TYPES}")
        if priority not in _VALID_PRIORITIES:
            raise ValueError(f"Invalid priority {priority!r}. Valid: {_VALID_PRIORITIES}")

        task_id     = _short_id(type[:3])
        content_str = json.dumps(content) if isinstance(content, dict) else content
        ts          = _now()

        row = {
            "id":                  task_id,
            "parent_id":           parent_id,
            "incident_id":         incident_id,
            "alert_fingerprint":   alert_fingerprint,
            "type":                type,
            "status":              "pending",
            "priority":            priority,
            "created_by":          created_by,
            "assigned_to":         assigned_to,
            "title":               title,
            "content":             content_str,
            "result":              None,
            "created_at":          ts,
            "claimed_at":          None,
            "completed_at":        None,
            "retry_count":         0,
            "maintenance_window":  int(maintenance_window),
            "do_not_auto_execute": int(do_not_auto_execute),
        }

        with self._lock, self._connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO tasks (
                        id, parent_id, incident_id, alert_fingerprint, type, status, priority,
                        created_by, assigned_to, title, content, result,
                        created_at, claimed_at, completed_at,
                        retry_count, maintenance_window, do_not_auto_execute
                    ) VALUES (
                        :id, :parent_id, :incident_id, :alert_fingerprint, :type, :status, :priority,
                        :created_by, :assigned_to, :title, :content, :result,
                        :created_at, :claimed_at, :completed_at,
                        :retry_count, :maintenance_window, :do_not_auto_execute
                    )
                """),
                row,
            )
            conn.execute(
                text("""
                    INSERT INTO task_events (task_id, timestamp, agent, event_type, detail)
                    VALUES (:task_id, :ts, :agent, :event_type, :detail)
                """),
                {
                    "task_id":    task_id,
                    "ts":         ts,
                    "agent":      created_by,
                    "event_type": "created",
                    "detail":     json.dumps({"assigned_to": assigned_to, "priority": priority}),
                },
            )

        # Notify via RabbitMQ (no-op when RABBITMQ_URL is not set)
        from shared.task_bus import publish_task as _publish
        _publish(type, task_id, priority)

        return dict(row)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def claim_task(self, task_id: str, agent: str) -> bool:
        ts = _now()
        with self._lock, self._connect() as conn:
            result = conn.execute(
                text("UPDATE tasks SET status='claimed', claimed_at=:ts "
                     "WHERE id=:id AND status='pending'"),
                {"ts": ts, "id": task_id},
            )
            if result.rowcount == 0:
                return False
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,NULL)"),
                {"task_id": task_id, "ts": ts, "agent": agent, "event_type": "claimed"},
            )
        return True

    def start_task(self, task_id: str, agent: str) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE tasks SET status='running' WHERE id=:id"),
                {"id": task_id},
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,NULL)"),
                {"task_id": task_id, "ts": ts, "agent": agent, "event_type": "started"},
            )

    def complete_task(self, task_id: str, agent: str, result: dict | str) -> None:
        result_str = json.dumps(result) if isinstance(result, dict) else result
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE tasks SET status='complete', result=:result, completed_at=:ts "
                     "WHERE id=:id"),
                {"result": result_str, "ts": ts, "id": task_id},
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,NULL)"),
                {"task_id": task_id, "ts": ts, "agent": agent, "event_type": "completed"},
            )

    def fail_task(self, task_id: str, agent: str, error: str) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE tasks SET status='failed', completed_at=:ts WHERE id=:id"),
                {"ts": ts, "id": task_id},
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,:detail)"),
                {
                    "task_id":    task_id,
                    "ts":         ts,
                    "agent":      agent,
                    "event_type": "failed",
                    "detail":     json.dumps({"error": error}),
                },
            )

    def request_approval(self, task_id: str, agent: str) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE tasks SET status='awaiting_approval' WHERE id=:id"),
                {"id": task_id},
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,NULL)"),
                {"task_id": task_id, "ts": ts, "agent": agent,
                 "event_type": "approval_requested"},
            )

    def approve_task(self, task_id: str, approved_by: str) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE tasks SET status='complete', completed_at=:ts WHERE id=:id"),
                {"ts": ts, "id": task_id},
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,NULL)"),
                {"task_id": task_id, "ts": ts, "agent": approved_by, "event_type": "approved"},
            )

    def reject_task(self, task_id: str, rejected_by: str, reason: str = "") -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE tasks SET status='rejected', completed_at=:ts WHERE id=:id"),
                {"ts": ts, "id": task_id},
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,:detail)"),
                {
                    "task_id":    task_id,
                    "ts":         ts,
                    "agent":      rejected_by,
                    "event_type": "rejected",
                    "detail":     json.dumps({"reason": reason}) if reason else None,
                },
            )

    # ── event logging ─────────────────────────────────────────────────────────

    def add_event(
        self,
        task_id: str,
        agent: str,
        event_type: str,
        detail: dict | str | None = None,
    ) -> None:
        if task_id is None:
            return
        ts = _now()
        if isinstance(detail, dict):
            detail_str: str | None = json.dumps(detail)
        elif isinstance(detail, str):
            detail_str = detail
        else:
            detail_str = None
        with self._lock, self._connect() as conn:
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,:detail)"),
                {"task_id": task_id, "ts": ts, "agent": agent,
                 "event_type": event_type, "detail": detail_str},
            )

    # ── feedback ──────────────────────────────────────────────────────────────

    def add_feedback(
        self,
        task_id: str,
        from_agent: str,
        verdict: str,
        confidence: float | None = None,
        notes: str = "",
    ) -> None:
        valid_verdicts = {"correct", "incorrect", "partial", "unverifiable"}
        if verdict not in valid_verdicts:
            raise ValueError(f"Invalid verdict {verdict!r}. Valid: {valid_verdicts}")
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO task_feedback
                        (task_id, from_agent, verdict, confidence, notes, created_at)
                    VALUES (:task_id, :from_agent, :verdict, :confidence, :notes, :ts)
                """),
                {
                    "task_id":    task_id,
                    "from_agent": from_agent,
                    "verdict":    verdict,
                    "confidence": confidence,
                    "notes":      notes,
                    "ts":         ts,
                },
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,:detail)"),
                {
                    "task_id":    task_id,
                    "ts":         ts,
                    "agent":      from_agent,
                    "event_type": "feedback_added",
                    "detail":     json.dumps({"verdict": verdict, "confidence": confidence}),
                },
            )

    # ── reads ─────────────────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                text("SELECT * FROM tasks WHERE id=:id"),
                {"id": task_id},
            ).fetchone()
            if not row:
                return None
            task = _row_to_dict(row)

            events = conn.execute(
                text("SELECT * FROM task_events WHERE task_id=:tid ORDER BY id"),
                {"tid": task_id},
            ).fetchall()
            task["events"] = [_row_to_dict(e) for e in events]

            feedback = conn.execute(
                text("SELECT * FROM task_feedback WHERE task_id=:tid ORDER BY id"),
                {"tid": task_id},
            ).fetchall()
            task["feedback"] = [_row_to_dict(f) for f in feedback]

            children = conn.execute(
                text("SELECT id, type, status, assigned_to, title "
                     "FROM tasks WHERE parent_id=:pid"),
                {"pid": task_id},
            ).fetchall()
            task["children"] = [_row_to_dict(c) for c in children]

        return task

    def list_tasks(
        self,
        assigned_to: str | None = None,
        status: str | None = None,
        type: str | None = None,
        alert_fingerprint: str | None = None,
        limit: int = 100,
        priority_filter: set[str] | None = None,
    ) -> list[dict]:
        clauses: list[str] = []
        params:  dict[str, Any] = {}

        if assigned_to:
            clauses.append("assigned_to = :assigned_to")
            params["assigned_to"] = assigned_to
        if status:
            clauses.append("status = :status")
            params["status"] = status
        if type:
            clauses.append("type = :type")
            params["type"] = type
        if alert_fingerprint:
            clauses.append("alert_fingerprint = :fp")
            params["fp"] = alert_fingerprint
        if priority_filter:
            placeholders = ", ".join(f":pf{i}" for i, _ in enumerate(priority_filter))
            clauses.append(f"priority IN ({placeholders})")
            for i, p in enumerate(sorted(priority_filter)):
                params[f"pf{i}"] = p

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params["limit"] = limit

        sql = text(
            f"SELECT * FROM tasks {where} "
            f"ORDER BY CASE priority "
            f"WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, "
            f"created_at DESC LIMIT :limit"
        )

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_approved_unexecuted_gates(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT t.* FROM tasks t
                    WHERE t.type = 'approval_gate'
                      AND t.status = 'complete'
                      AND EXISTS (
                          SELECT 1 FROM task_events e
                          WHERE e.task_id = t.id AND e.event_type = 'approved'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM task_events e
                          WHERE e.task_id = t.id AND e.event_type = 'execution_started'
                      )
                    ORDER BY t.created_at ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def retry_task(self, task_id: str, agent: str) -> bool:
        ts = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                text("SELECT retry_count FROM tasks WHERE id=:id AND status='failed'"),
                {"id": task_id},
            ).fetchone()
            if not row:
                return False
            if row[0] >= 2:
                return False
            new_count = row[0] + 1
            conn.execute(
                text("UPDATE tasks SET status='pending', retry_count=:cnt WHERE id=:id"),
                {"cnt": new_count, "id": task_id},
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,:detail)"),
                {
                    "task_id":    task_id,
                    "ts":         ts,
                    "agent":      agent,
                    "event_type": "retry_scheduled",
                    "detail":     json.dumps({"retry_count": new_count}),
                },
            )
        return True

    def get_active_rca_for_device(self, device: str, minutes: int = 15) -> dict | None:
        if not device:
            return None
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes))
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._connect() as conn:
            row = conn.execute(
                text("""
                    SELECT * FROM tasks
                    WHERE type='rca'
                      AND status NOT IN ('failed','rejected','complete')
                      AND (content LIKE :pat1 OR content LIKE :pat2)
                      AND created_at >= :cutoff
                    ORDER BY created_at DESC LIMIT 1
                """),
                {
                    "pat1":   f'%"device": "{device}"%',
                    "pat2":   f'%"device":"{device}"%',
                    "cutoff": cutoff_str,
                },
            ).fetchone()
        return _row_to_dict(row) if row else None

    def count_successful_executions(self, device: str, fix_type: str) -> int:
        """
        Count approval_gate tasks where the fix was successfully executed for
        this device+fix_type combination.  Uses Python-side JSON filtering for
        cross-dialect compatibility.
        """
        with self._connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT t.content, e.detail FROM task_events e
                    JOIN tasks t ON t.id = e.task_id AND t.type = 'approval_gate'
                    WHERE e.event_type = 'execution_complete'
                """),
            ).fetchall()

        count = 0
        for r in rows:
            try:
                detail = json.loads(r[1] or "{}")
                if detail.get("status") != "success":
                    continue
                content = json.loads(r[0] or "{}")
                dev = (content.get("device")
                       or content.get("fix_proposal", {}).get("device", ""))
                ft  = (content.get("fix_type")
                       or content.get("fix_proposal", {}).get("fix_type", ""))
                if dev == device and ft == fix_type:
                    count += 1
            except Exception:
                pass
        return count

    def get_resolution_history(
        self, alertname: str, device: str, limit: int = 5
    ) -> list[dict]:
        """
        Return recent approval_gate tasks for this device where execution was
        verified.  Uses Python-side JSON filtering for cross-dialect compat.
        """
        with self._connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT t.id, t.title, t.created_at, t.alert_fingerprint, t.content,
                           ev.detail AS exec_detail,
                           vf.detail AS verify_detail
                    FROM tasks t
                    JOIN task_events ev ON ev.task_id = t.id
                        AND ev.event_type = 'execution_complete'
                    LEFT JOIN task_events vf ON vf.task_id = t.id
                        AND vf.event_type = 'execution_verified'
                    WHERE t.type = 'approval_gate'
                    ORDER BY t.created_at DESC
                    LIMIT :lim
                """),
                {"lim": limit * 10},  # over-fetch before Python-side device filter
            ).fetchall()

        results = []
        for r in rows:
            try:
                content = json.loads(r[4] or "{}")
                dev = (content.get("device")
                       or content.get("fix_proposal", {}).get("device", ""))
                if dev != device:
                    continue
                exec_d   = json.loads(r[5] or "{}")
                verify_d = json.loads(r[6] or "{}") if r[6] else {}
                results.append({
                    "id":              r[0],
                    "title":           r[1],
                    "created_at":      r[2],
                    "fingerprint":     r[3],
                    "exec_status":     exec_d.get("status", "unknown"),
                    "changes_applied": exec_d.get("changes_applied", ""),
                    "alert_resolved":  verify_d.get("alert_resolved"),
                    "ttr_seconds":     verify_d.get("ttr_seconds"),
                })
                if len(results) >= limit:
                    break
            except Exception:
                pass
        return results

    def get_active_task_for_fingerprint(self, fingerprint: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                text("""
                    SELECT * FROM tasks
                    WHERE alert_fingerprint = :fp
                      AND status NOT IN ('failed','rejected')
                    ORDER BY created_at DESC LIMIT 1
                """),
                {"fp": fingerprint},
            ).fetchone()
        return _row_to_dict(row) if row else None

    def get_task_chain(self, task_id: str) -> list[dict]:
        chain: list[dict] = []
        current_id: str | None = task_id

        with self._connect() as conn:
            while current_id:
                row = conn.execute(
                    text("SELECT * FROM tasks WHERE id=:id"),
                    {"id": current_id},
                ).fetchone()
                if not row:
                    break
                task = _row_to_dict(row)
                events = conn.execute(
                    text("SELECT * FROM task_events WHERE task_id=:tid ORDER BY id"),
                    {"tid": current_id},
                ).fetchall()
                task["events"] = [_row_to_dict(e) for e in events]
                chain.insert(0, task)
                current_id = task.get("parent_id")

        return chain

    # ── incident helpers ──────────────────────────────────────────────────────

    def create_incident(
        self,
        *,
        severity: str,
        impact: str,
        alert_fingerprint: str | None = None,
        device: str = "",
        alertname: str = "",
    ) -> dict:
        """
        Create a top-level incident task that groups correlated alert pipelines.

        Severity maps to priority: P1 → critical, P2 → high, P3 → normal, P4 → low.
        """
        sev_to_priority = {"P1": "critical", "P2": "high", "P3": "normal", "P4": "low"}
        priority = sev_to_priority.get(severity, "normal")
        return self.create_task(
            type="incident",
            created_by="system",
            assigned_to=None,
            title=f"{severity} Incident: {alertname or device or 'Network Event'}",
            alert_fingerprint=alert_fingerprint,
            priority=priority,
            content={
                "severity":  severity,
                "impact":    impact,
                "device":    device,
                "alertname": alertname,
                "affected_devices": [device] if device else [],
            },
        )

    def get_open_incident_for_device(
        self, device: str, minutes: int = 30
    ) -> dict | None:
        """
        Return an open incident task covering the given device created within
        the last `minutes` minutes, or None.
        """
        if not device:
            return None
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes))
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._connect() as conn:
            row = conn.execute(
                text("""
                    SELECT * FROM tasks
                    WHERE type = 'incident'
                      AND status NOT IN ('complete', 'rejected')
                      AND (content LIKE :pat1 OR content LIKE :pat2)
                      AND created_at >= :cutoff
                    ORDER BY created_at DESC LIMIT 1
                """),
                {
                    "pat1":   f'%"device": "{device}"%',
                    "pat2":   f'%{device}%',
                    "cutoff": cutoff_str,
                },
            ).fetchone()
        return _row_to_dict(row) if row else None

    def link_task_to_incident(self, task_id: str, incident_id: str) -> None:
        """Associate a task with an incident."""
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE tasks SET incident_id=:inc WHERE id=:id"),
                {"inc": incident_id, "id": task_id},
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,:detail)"),
                {
                    "task_id":    incident_id,
                    "ts":         ts,
                    "agent":      "system",
                    "event_type": "task_linked",
                    "detail":     json.dumps({"linked_task_id": task_id}),
                },
            )

    def add_device_to_incident(self, incident_id: str, device: str) -> None:
        """Append a device to the incident's affected_devices list."""
        if not device:
            return
        with self._lock, self._connect() as conn:
            row = conn.execute(
                text("SELECT content FROM tasks WHERE id=:id"),
                {"id": incident_id},
            ).fetchone()
            if not row:
                return
            try:
                content = json.loads(row[0] or "{}")
                devices: list[str] = content.get("affected_devices", [])
                if device not in devices:
                    devices.append(device)
                    content["affected_devices"] = devices
                    conn.execute(
                        text("UPDATE tasks SET content=:c WHERE id=:id"),
                        {"c": json.dumps(content), "id": incident_id},
                    )
            except Exception:
                pass

    def list_incidents(self, open_only: bool = True, limit: int = 50) -> list[dict]:
        """Return incident tasks, newest first."""
        where = "WHERE type='incident'"
        if open_only:
            where += " AND status NOT IN ('complete','rejected')"
        with self._connect() as conn:
            rows = conn.execute(
                text(f"SELECT * FROM tasks {where} "
                     f"ORDER BY CASE priority "
                     f"WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, "
                     f"created_at DESC LIMIT :lim"),
                {"lim": limit},
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_incident_pipelines(self, incident_id: str) -> list[dict]:
        """Return all RCA tasks linked to this incident, each with their chain."""
        with self._connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM tasks WHERE incident_id=:inc AND type='rca' "
                     "ORDER BY created_at DESC"),
                {"inc": incident_id},
            ).fetchall()
        result = []
        for r in rows:
            t = _row_to_dict(r)
            fp = t.get("alert_fingerprint", "")
            if fp:
                t["pipeline"] = self.list_tasks(alert_fingerprint=fp, limit=20)
            result.append(t)
        return result

    def close_incident(self, incident_id: str, resolution: str = "") -> None:
        """Mark an incident as resolved."""
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE tasks SET status='complete', completed_at=:ts WHERE id=:id"),
                {"ts": ts, "id": incident_id},
            )
            conn.execute(
                text("INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) "
                     "VALUES (:task_id,:ts,:agent,:event_type,:detail)"),
                {
                    "task_id":    incident_id,
                    "ts":         ts,
                    "agent":      "system",
                    "event_type": "incident_resolved",
                    "detail":     json.dumps({"resolution": resolution}) if resolution else None,
                },
            )

    def clear_all_tasks(self) -> int:
        with self._lock, self._connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM tasks")).fetchone()[0]
            conn.execute(text("DELETE FROM task_feedback"))
            conn.execute(text("DELETE FROM task_events"))
            conn.execute(text("DELETE FROM tasks"))
        return count

    def get_kpis(self) -> dict:
        """Compute KPI metrics from tasks created today."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn:
            total = conn.execute(
                text("SELECT COUNT(*) FROM tasks WHERE created_at LIKE :pat"),
                {"pat": f"{day}%"},
            ).fetchone()[0]
            complete = conn.execute(
                text("SELECT COUNT(*) FROM tasks WHERE created_at LIKE :pat AND status='complete'"),
                {"pat": f"{day}%"},
            ).fetchone()[0]
            failed = conn.execute(
                text("SELECT COUNT(*) FROM tasks WHERE created_at LIKE :pat AND status='failed'"),
                {"pat": f"{day}%"},
            ).fetchone()[0]
            awaiting = conn.execute(
                text("SELECT COUNT(*) FROM tasks WHERE status='awaiting_approval'"),
            ).fetchone()[0]
            fb_total = conn.execute(
                text("SELECT COUNT(*) FROM task_feedback"),
            ).fetchone()[0]
            fb_correct = conn.execute(
                text("SELECT COUNT(*) FROM task_feedback WHERE verdict='correct'"),
            ).fetchone()[0]
            escalated = conn.execute(
                text("SELECT COUNT(*) FROM tasks "
                     "WHERE type='approval_gate' AND created_at LIKE :pat"),
                {"pat": f"{day}%"},
            ).fetchone()[0]

            # MTTR from execution_verified events on today's gates — Python-side JSON parse
            mttr_rows = conn.execute(
                text("""
                    SELECT e.detail FROM task_events e
                    JOIN tasks gate ON gate.id = e.task_id AND gate.type = 'approval_gate'
                    WHERE e.event_type = 'execution_verified'
                      AND gate.created_at LIKE :pat
                """),
                {"pat": f"{day}%"},
            ).fetchall()

        auto_resolved  = complete - escalated if complete > escalated else complete
        validation_rate  = round(fb_correct / fb_total * 100, 1) if fb_total else 0.0
        escalation_rate  = round(escalated / complete * 100, 1)  if complete else 0.0

        ttr_minutes: list[float] = []
        for (detail_str,) in mttr_rows:
            try:
                d = json.loads(detail_str or "{}")
                if d.get("alert_resolved") and d.get("ttr_seconds", 0) > 0:
                    ttr_minutes.append(d["ttr_seconds"] / 60.0)
            except Exception:
                pass

        sorted_ttr = sorted(ttr_minutes)
        avg_ttr = round(sum(sorted_ttr) / len(sorted_ttr), 1) if sorted_ttr else 0.0
        p50_ttr = round(sorted_ttr[len(sorted_ttr) // 2], 1)  if sorted_ttr else 0.0

        return {
            "today": {
                "total_tasks":        total,
                "complete":           complete,
                "failed":             failed,
                "awaiting_approval":  awaiting,
                "auto_resolved":      auto_resolved,
                "escalated":          escalated,
            },
            "rates": {
                "auto_resolved_pct":    round(auto_resolved / complete * 100, 1) if complete else 0.0,
                "validation_rate_pct":  validation_rate,
                "escalation_rate_pct":  escalation_rate,
            },
            "feedback": {
                "total":   fb_total,
                "correct": fb_correct,
            },
            "mttr": {
                "avg_minutes":    avg_ttr,
                "p50_minutes":    p50_ttr,
                "resolved_today": len(sorted_ttr),
            },
        }
