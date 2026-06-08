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
from datetime import datetime, timedelta, timezone
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

def _build_ddl(dialect: str) -> list[str]:
    """Return CREATE TABLE statements appropriate for the given SQL dialect."""
    # SQLite uses INTEGER PRIMARY KEY AUTOINCREMENT; PostgreSQL uses SERIAL.
    serial = "SERIAL PRIMARY KEY" if dialect == "postgresql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return [
        f"""
        CREATE TABLE IF NOT EXISTS token_usage (
            id                    {serial},
            timestamp             TEXT    NOT NULL,
            agent                 TEXT    NOT NULL,
            session_id            TEXT    NOT NULL,
            task_id               TEXT,
            prompt_tokens         INTEGER NOT NULL,
            completion_tokens     INTEGER NOT NULL,
            model                 TEXT    NOT NULL,
            estimated_cost_usd    REAL    NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_token_usage_agent_ts ON token_usage (agent, timestamp)",
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id                  TEXT PRIMARY KEY,
            tenant_id           TEXT NOT NULL DEFAULT 'default',
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
        f"""
        CREATE TABLE IF NOT EXISTS task_events (
            id          {serial},
            task_id     TEXT NOT NULL REFERENCES tasks(id),
            timestamp   TEXT NOT NULL,
            agent       TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            detail      TEXT
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS task_feedback (
            id          {serial},
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
    "CREATE INDEX IF NOT EXISTS idx_tasks_tenant    ON tasks(tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_task_events_tid ON task_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_tid    ON task_feedback(task_id)",
    """
    CREATE TABLE IF NOT EXISTS action_policies (
        id                   TEXT PRIMARY KEY,
        tenant_id            TEXT NOT NULL DEFAULT 'default',
        name                 TEXT NOT NULL,
        description          TEXT NOT NULL DEFAULT '',
        alertname            TEXT NOT NULL DEFAULT '',
        fix_type             TEXT NOT NULL DEFAULT '',
        device_role          TEXT NOT NULL DEFAULT '',
        environment          TEXT NOT NULL DEFAULT '',
        min_confidence       TEXT NOT NULL DEFAULT 'low',
        max_risk             TEXT NOT NULL DEFAULT 'high',
        min_prior_successes  INTEGER NOT NULL DEFAULT 0,
        autonomy_level       TEXT NOT NULL DEFAULT 'L2',
        enabled              INTEGER NOT NULL DEFAULT 1,
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_policies_tenant ON action_policies(tenant_id, enabled)",
    """
    CREATE TABLE IF NOT EXISTS standing_intents (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL DEFAULT 'default',
        name              TEXT NOT NULL,
        description       TEXT NOT NULL DEFAULT '',
        intent_type       TEXT NOT NULL,
        device            TEXT NOT NULL DEFAULT '',
        device_role       TEXT NOT NULL DEFAULT '',
        alertname         TEXT NOT NULL DEFAULT '',
        metric_query      TEXT NOT NULL DEFAULT '',
        threshold         TEXT NOT NULL DEFAULT '',
        action            TEXT NOT NULL DEFAULT '',
        schedule          TEXT NOT NULL DEFAULT '',
        enabled           INTEGER NOT NULL DEFAULT 1,
        created_at        TEXT NOT NULL,
        last_triggered_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intents_tenant ON standing_intents(tenant_id, enabled)",
    f"""
    CREATE TABLE IF NOT EXISTS policy_performance (
        id             {serial},
        policy_id      TEXT,
        fix_type       TEXT NOT NULL,
        device_role    TEXT NOT NULL DEFAULT '',
        tenant_id      TEXT NOT NULL DEFAULT 'default',
        alert_resolved INTEGER,
        ttr_seconds    INTEGER,
        created_at     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_perf_policy ON policy_performance(policy_id, created_at)",
    ]

# Safe migrations — IGNORE errors so they are idempotent on existing DBs.
_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN maintenance_window INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN do_not_auto_execute INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN incident_id TEXT",
    "ALTER TABLE tasks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
    # Fast-path programmatic resolution fields (nullable — backward compatible)
    "ALTER TABLE action_policies ADD COLUMN conditions    TEXT",
    "ALTER TABLE action_policies ADD COLUMN rca_template  TEXT",
    "ALTER TABLE action_policies ADD COLUMN fix_template  TEXT",
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
        ddl = _build_ddl(self._dialect)
        with self._connect() as conn:
            for stmt in ddl:
                conn.execute(text(stmt))
        # Each migration runs in its own transaction.  In PostgreSQL a failed
        # statement (e.g. "column already exists") aborts the current
        # transaction, so mixing migrations with the DDL block would cause
        # PostgreSQL to roll back the freshly created tables on commit.
        for migration in _MIGRATIONS:
            try:
                with self._connect() as conn:
                    conn.execute(text(migration))
            except Exception:
                pass  # column already exists — idempotent

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
        tenant_id: str = "default",
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
            "tenant_id":           tenant_id,
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
                        id, tenant_id, parent_id, incident_id, alert_fingerprint, type, status, priority,
                        created_by, assigned_to, title, content, result,
                        created_at, claimed_at, completed_at,
                        retry_count, maintenance_window, do_not_auto_execute
                    ) VALUES (
                        :id, :tenant_id, :parent_id, :incident_id, :alert_fingerprint, :type, :status, :priority,
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

    def update_task_content(self, task_id: str, content_dict: dict) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE tasks SET content=:content WHERE id=:id"),
                {"content": json.dumps(content_dict), "id": task_id},
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
                     "VALUES (:task_id,:ts,:agent,:event_type,:detail)"),
                {"task_id": task_id, "ts": ts, "agent": agent,
                 "event_type": "approval_requested",
                 "detail": json.dumps({"requested_by": agent, "timestamp": ts})},
            )
        # Dispatch notifications outside the lock so a slow webhook never
        # blocks other writers. Import lazily to avoid circular imports.
        try:
            task = self.get_task(task_id)
            if task:
                from shared.notifications import notify_approval_required
                notify_approval_required(task)
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "TaskStore: notification failed for task=%s: %s", task_id, exc
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

    def get_task_events(self, task_id: str) -> list[dict]:
        """Return all events for a task ordered chronologically."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM task_events WHERE task_id=:tid ORDER BY id"),
                {"tid": task_id},
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

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
        tenant_id: str | None = None,
        exclude_statuses: list[str] | None = None,
        created_after_minutes: int | None = None,
    ) -> list[dict]:
        clauses: list[str] = []
        params:  dict[str, Any] = {}

        if tenant_id:
            clauses.append("tenant_id = :tenant_id")
            params["tenant_id"] = tenant_id
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
        if exclude_statuses:
            placeholders = ", ".join(f":ex{i}" for i, _ in enumerate(exclude_statuses))
            clauses.append(f"status NOT IN ({placeholders})")
            for i, s in enumerate(exclude_statuses):
                params[f"ex{i}"] = s
        if created_after_minutes is not None:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=created_after_minutes))
            clauses.append("created_at >= :created_after")
            params["created_after"] = cutoff.strftime("%Y-%m-%d %H:%M:%S UTC")

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
        """Return a task that is ACTIVELY being investigated for this fingerprint.
        Only returns pending/claimed/running tasks — awaiting_approval and complete
        tasks are intentionally excluded so that an alert that resolved and re-fired
        gets a fresh investigation rather than being silently deduplicated against a
        stale gate task from a previous alert cycle."""
        with self._connect() as conn:
            row = conn.execute(
                text("""
                    SELECT * FROM tasks
                    WHERE alert_fingerprint = :fp
                      AND status IN ('pending','claimed','running')
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

    def get_open_incident_for_fingerprint(self, fingerprint: str) -> dict | None:
        """Return an open incident task whose primary alert_fingerprint matches."""
        if not fingerprint:
            return None
        with self._connect() as conn:
            row = conn.execute(
                text("""
                    SELECT * FROM tasks
                    WHERE type = 'incident'
                      AND alert_fingerprint = :fp
                      AND status NOT IN ('complete', 'rejected')
                    ORDER BY created_at DESC LIMIT 1
                """),
                {"fp": fingerprint},
            ).fetchone()
        return _row_to_dict(row) if row else None

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

    # ── action_policies ───────────────────────────────────────────────────────

    def list_policies(self, tenant_id: str = "default") -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM action_policies WHERE tenant_id=:t ORDER BY created_at DESC"),
                {"t": tenant_id},
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def create_policy(self, data: dict) -> dict:
        ts = _now()
        # Normalise empty strings to None for nullable JSON fields
        def _json_or_none(key: str) -> str | None:
            v = data.get(key)
            return v if v else None

        row = {
            "id":                  _short_id("pol"),
            "tenant_id":           data.get("tenant_id", "default"),
            "name":                data["name"],
            "description":         data.get("description", ""),
            "alertname":           data.get("alertname", ""),
            "fix_type":            data.get("fix_type", ""),
            "device_role":         data.get("device_role", ""),
            "environment":         data.get("environment", ""),
            "min_confidence":      data.get("min_confidence", "low"),
            "max_risk":            data.get("max_risk", "high"),
            "min_prior_successes": data.get("min_prior_successes", 0),
            "autonomy_level":      data.get("autonomy_level", "L2"),
            "enabled":             int(data.get("enabled", True)),
            "conditions":          _json_or_none("conditions"),
            "rca_template":        _json_or_none("rca_template"),
            "fix_template":        _json_or_none("fix_template"),
            "created_at":          ts,
            "updated_at":          ts,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                text("""INSERT INTO action_policies
                    (id,tenant_id,name,description,alertname,fix_type,device_role,environment,
                     min_confidence,max_risk,min_prior_successes,autonomy_level,enabled,
                     conditions,rca_template,fix_template,created_at,updated_at)
                    VALUES
                    (:id,:tenant_id,:name,:description,:alertname,:fix_type,:device_role,:environment,
                     :min_confidence,:max_risk,:min_prior_successes,:autonomy_level,:enabled,
                     :conditions,:rca_template,:fix_template,:created_at,:updated_at)"""),
                row,
            )
        return row

    def update_policy(self, policy_id: str, data: dict) -> dict | None:
        ts = _now()
        allowed = {
            "name", "description", "alertname", "fix_type", "device_role", "environment",
            "min_confidence", "max_risk", "min_prior_successes", "autonomy_level", "enabled",
            "conditions", "rca_template", "fix_template",
        }
        sets = ", ".join(f"{k}=:{k}" for k in data if k in allowed)
        if not sets:
            return self.get_policy(policy_id)
        params = {k: v for k, v in data.items() if k in allowed}
        params["updated_at"] = ts
        params["id"] = policy_id
        with self._lock, self._connect() as conn:
            conn.execute(
                text(f"UPDATE action_policies SET {sets}, updated_at=:updated_at WHERE id=:id"),
                params,
            )
        return self.get_policy(policy_id)

    def get_policy(self, policy_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                text("SELECT * FROM action_policies WHERE id=:id"), {"id": policy_id}
            ).fetchone()
        return _row_to_dict(row) if row else None

    def delete_policy(self, policy_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(text("DELETE FROM action_policies WHERE id=:id"), {"id": policy_id})

    # ── standing_intents ──────────────────────────────────────────────────────

    def list_intents(self, tenant_id: str = "default") -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM standing_intents WHERE tenant_id=:t ORDER BY created_at DESC"),
                {"t": tenant_id},
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def create_intent(self, data: dict) -> dict:
        ts = _now()
        row = {
            "id":               _short_id("int"),
            "tenant_id":        data.get("tenant_id", "default"),
            "name":             data["name"],
            "description":      data.get("description", ""),
            "intent_type":      data["intent_type"],
            "device":           data.get("device", ""),
            "device_role":      data.get("device_role", ""),
            "alertname":        data.get("alertname", ""),
            "metric_query":     data.get("metric_query", ""),
            "threshold":        data.get("threshold", ""),
            "action":           data.get("action", ""),
            "schedule":         data.get("schedule", ""),
            "enabled":          int(data.get("enabled", True)),
            "created_at":       ts,
            "last_triggered_at": None,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                text("""INSERT INTO standing_intents
                    (id,tenant_id,name,description,intent_type,device,device_role,alertname,
                     metric_query,threshold,action,schedule,enabled,created_at,last_triggered_at)
                    VALUES
                    (:id,:tenant_id,:name,:description,:intent_type,:device,:device_role,:alertname,
                     :metric_query,:threshold,:action,:schedule,:enabled,:created_at,:last_triggered_at)"""),
                row,
            )
        return row

    def update_intent(self, intent_id: str, data: dict) -> dict | None:
        allowed = {
            "name", "description", "intent_type", "device", "device_role", "alertname",
            "metric_query", "threshold", "action", "schedule", "enabled",
        }
        sets = ", ".join(f"{k}=:{k}" for k in data if k in allowed)
        if not sets:
            return self.get_intent(intent_id)
        params = {k: v for k, v in data.items() if k in allowed}
        params["id"] = intent_id
        with self._lock, self._connect() as conn:
            conn.execute(
                text(f"UPDATE standing_intents SET {sets} WHERE id=:id"), params
            )
        return self.get_intent(intent_id)

    def get_intent(self, intent_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                text("SELECT * FROM standing_intents WHERE id=:id"), {"id": intent_id}
            ).fetchone()
        return _row_to_dict(row) if row else None

    def delete_intent(self, intent_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(text("DELETE FROM standing_intents WHERE id=:id"), {"id": intent_id})

    def get_matching_intents(
        self,
        device: str = "",
        alertname: str = "",
        tenant_id: str = "default",
    ) -> list[dict]:
        """Return enabled intents that match device and/or alertname."""
        with self._connect() as conn:
            rows = conn.execute(
                text("""SELECT * FROM standing_intents
                    WHERE tenant_id=:t AND enabled=1
                      AND (device='' OR device=:device)
                      AND (alertname='' OR alertname=:alertname)
                    ORDER BY created_at ASC"""),
                {"t": tenant_id, "device": device, "alertname": alertname},
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def touch_intent(self, intent_id: str) -> None:
        """Update last_triggered_at to now."""
        with self._lock, self._connect() as conn:
            conn.execute(
                text("UPDATE standing_intents SET last_triggered_at=:ts WHERE id=:id"),
                {"ts": _now(), "id": intent_id},
            )

    # ── policy_performance ────────────────────────────────────────────────────

    def record_policy_outcome(
        self,
        *,
        policy_id: str | None,
        fix_type: str,
        device_role: str = "",
        tenant_id: str = "default",
        alert_resolved: bool | None,
        ttr_seconds: int | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                text("""INSERT INTO policy_performance
                    (policy_id,fix_type,device_role,tenant_id,alert_resolved,ttr_seconds,created_at)
                    VALUES (:policy_id,:fix_type,:device_role,:tenant_id,:alert_resolved,:ttr,:ts)"""),
                {
                    "policy_id":      policy_id,
                    "fix_type":       fix_type,
                    "device_role":    device_role,
                    "tenant_id":      tenant_id,
                    "alert_resolved": int(alert_resolved) if alert_resolved is not None else None,
                    "ttr":            ttr_seconds,
                    "ts":             _now(),
                },
            )

    def get_policy_performance(self, policy_id: str, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                text("""SELECT * FROM policy_performance WHERE policy_id=:id
                    ORDER BY created_at DESC LIMIT :lim"""),
                {"id": policy_id, "lim": limit},
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_policy_stats(self, tenant_id: str = "default") -> list[dict]:
        """Aggregate accuracy and avg TTR per policy for the last 30 days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT policy_id,
                           COUNT(*) as total,
                           SUM(alert_resolved) as resolved,
                           AVG(ttr_seconds) as avg_ttr
                    FROM policy_performance
                    WHERE tenant_id=:t AND created_at >= :cutoff
                    GROUP BY policy_id
                """),
                {"t": tenant_id, "cutoff": cutoff},
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

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
