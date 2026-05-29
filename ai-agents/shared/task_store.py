"""
Shared task queue and feedback store for multi-agent closed-loop automation.

All three agent containers mount the same activity.db volume, so every agent
can read and write tasks directly without any inter-process messaging.

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
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

_DEFAULT_DB = os.environ.get("ACTIVITY_DB_PATH", "./activity.db")

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    parent_id         TEXT REFERENCES tasks(id),
    alert_fingerprint TEXT,
    type              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    priority          TEXT NOT NULL DEFAULT 'normal',
    created_by        TEXT NOT NULL,
    assigned_to       TEXT,
    title             TEXT,
    content           TEXT NOT NULL,
    result            TEXT,
    created_at        TEXT NOT NULL,
    claimed_at        TEXT,
    completed_at      TEXT
);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    timestamp   TEXT NOT NULL,
    agent       TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS task_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    from_agent  TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    confidence  REAL,
    notes       TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_assigned  ON tasks(assigned_to, status);
CREATE INDEX IF NOT EXISTS idx_tasks_type      ON tasks(type, status);
CREATE INDEX IF NOT EXISTS idx_tasks_alert     ON tasks(alert_fingerprint);
CREATE INDEX IF NOT EXISTS idx_task_events_tid ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_feedback_tid    ON task_feedback(task_id);
"""

_VALID_STATUSES = frozenset({
    "pending", "claimed", "running",
    "awaiting_approval", "complete", "failed", "rejected",
})
_VALID_TYPES = frozenset({
    "rca", "fix_proposal", "validation", "approval_gate",
})
_VALID_PRIORITIES = frozenset({"critical", "high", "normal", "low"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _short_id(prefix: str = "") -> str:
    uid = str(uuid.uuid4())[:8]
    return f"{prefix}-{uid}" if prefix else uid


class TaskStore:
    """Thread-safe task queue backed by the shared activity.db."""

    def __init__(self, db_path: str = _DEFAULT_DB) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

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
    ) -> dict:
        if type not in _VALID_TYPES:
            raise ValueError(f"Invalid task type {type!r}. Valid: {_VALID_TYPES}")
        if priority not in _VALID_PRIORITIES:
            raise ValueError(f"Invalid priority {priority!r}. Valid: {_VALID_PRIORITIES}")

        task_id = _short_id(type[:3])
        content_str = json.dumps(content) if isinstance(content, dict) else content
        ts = _now()

        row = {
            "id": task_id,
            "parent_id": parent_id,
            "alert_fingerprint": alert_fingerprint,
            "type": type,
            "status": "pending",
            "priority": priority,
            "created_by": created_by,
            "assigned_to": assigned_to,
            "title": title,
            "content": content_str,
            "result": None,
            "created_at": ts,
            "claimed_at": None,
            "completed_at": None,
        }

        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO tasks
                   (id, parent_id, alert_fingerprint, type, status, priority,
                    created_by, assigned_to, title, content, result,
                    created_at, claimed_at, completed_at)
                   VALUES (:id,:parent_id,:alert_fingerprint,:type,:status,:priority,
                           :created_by,:assigned_to,:title,:content,:result,
                           :created_at,:claimed_at,:completed_at)""",
                row,
            )
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, created_by, "created",
                 json.dumps({"assigned_to": assigned_to, "priority": priority})),
            )
        return dict(row)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def claim_task(self, task_id: str, agent: str) -> bool:
        """Atomically move task from pending → claimed. Returns False if already claimed."""
        ts = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='claimed', claimed_at=? WHERE id=? AND status='pending'",
                (ts, task_id),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, agent, "claimed", None),
            )
        return True

    def start_task(self, task_id: str, agent: str) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='running' WHERE id=?", (task_id,)
            )
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, agent, "started", None),
            )

    def complete_task(self, task_id: str, agent: str, result: dict | str) -> None:
        result_str = json.dumps(result) if isinstance(result, dict) else result
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='complete', result=?, completed_at=? WHERE id=?",
                (result_str, ts, task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, agent, "completed", None),
            )

    def fail_task(self, task_id: str, agent: str, error: str) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', completed_at=? WHERE id=?",
                (ts, task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, agent, "failed", json.dumps({"error": error})),
            )

    def request_approval(self, task_id: str, agent: str) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='awaiting_approval' WHERE id=?", (task_id,)
            )
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, agent, "approval_requested", None),
            )

    def approve_task(self, task_id: str, approved_by: str) -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='complete', completed_at=? WHERE id=?",
                (ts, task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, approved_by, "approved", None),
            )

    def reject_task(self, task_id: str, rejected_by: str, reason: str = "") -> None:
        ts = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='rejected', completed_at=? WHERE id=?",
                (ts, task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, rejected_by, "rejected",
                 json.dumps({"reason": reason}) if reason else None),
            )

    # ── event logging (called from StatusCallbackHandler) ─────────────────────

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
        detail_str: str | None = None
        if isinstance(detail, dict):
            detail_str = json.dumps(detail)
        elif isinstance(detail, str):
            detail_str = detail
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, agent, event_type, detail_str),
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
                """INSERT INTO task_feedback
                   (task_id, from_agent, verdict, confidence, notes, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (task_id, from_agent, verdict, confidence, notes, ts),
            )
            conn.execute(
                "INSERT INTO task_events (task_id,timestamp,agent,event_type,detail) VALUES (?,?,?,?,?)",
                (task_id, ts, from_agent, "feedback_added",
                 json.dumps({"verdict": verdict, "confidence": confidence})),
            )

    # ── reads ─────────────────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                return None
            task = dict(row)

            events = conn.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
            task["events"] = [dict(e) for e in events]

            feedback = conn.execute(
                "SELECT * FROM task_feedback WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
            task["feedback"] = [dict(f) for f in feedback]

            children = conn.execute(
                "SELECT id, type, status, assigned_to, title FROM tasks WHERE parent_id=?",
                (task_id,),
            ).fetchall()
            task["children"] = [dict(c) for c in children]

        return task

    def list_tasks(
        self,
        assigned_to: str | None = None,
        status: str | None = None,
        type: str | None = None,
        alert_fingerprint: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if assigned_to:
            clauses.append("assigned_to=?")
            params.append(assigned_to)
        if status:
            clauses.append("status=?")
            params.append(status)
        if type:
            clauses.append("type=?")
            params.append(type)
        if alert_fingerprint:
            clauses.append("alert_fingerprint=?")
            params.append(alert_fingerprint)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY "
                f"CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                f"WHEN 'normal' THEN 2 ELSE 3 END, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_active_task_for_fingerprint(self, fingerprint: str) -> dict | None:
        """
        Return the most recent task for this alert fingerprint that is NOT
        in a terminal-failure state (failed or rejected).

        Used by the alert poller to avoid creating duplicate tasks when the
        container restarts and _seen is reset.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE alert_fingerprint=? "
                "AND status NOT IN ('failed','rejected') "
                "ORDER BY created_at DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
        return dict(row) if row else None

    def get_task_chain(self, task_id: str) -> list[dict]:
        """Return the ancestor chain from root down to this task, each with events."""
        chain: list[dict] = []
        current_id: str | None = task_id

        with self._connect() as conn:
            while current_id:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (current_id,)
                ).fetchone()
                if not row:
                    break
                task = dict(row)
                events = conn.execute(
                    "SELECT * FROM task_events WHERE task_id=? ORDER BY id",
                    (current_id,),
                ).fetchall()
                task["events"] = [dict(e) for e in events]
                chain.insert(0, task)
                current_id = task.get("parent_id")

        return chain

    def clear_all_tasks(self) -> int:
        """
        Delete every row from tasks, task_events, and task_feedback.
        Returns the number of tasks deleted.
        """
        with self._lock, self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            conn.execute("DELETE FROM task_feedback")
            conn.execute("DELETE FROM task_events")
            conn.execute("DELETE FROM tasks")
        return count

    def get_kpis(self) -> dict:
        """Compute KPI metrics from tasks created today."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE created_at LIKE ?",
                (f"{day}%",),
            ).fetchone()[0]
            complete = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE created_at LIKE ? AND status='complete'",
                (f"{day}%",),
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE created_at LIKE ? AND status='failed'",
                (f"{day}%",),
            ).fetchone()[0]
            awaiting = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='awaiting_approval'",
            ).fetchone()[0]

            # Chaos validation accuracy
            fb_total = conn.execute("SELECT COUNT(*) FROM task_feedback").fetchone()[0]
            fb_correct = conn.execute(
                "SELECT COUNT(*) FROM task_feedback WHERE verdict='correct'"
            ).fetchone()[0]

            # Tasks that required human approval (approval_gate type)
            escalated = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE type='approval_gate' AND created_at LIKE ?",
                (f"{day}%",),
            ).fetchone()[0]

        auto_resolved = complete - escalated if complete > escalated else complete
        validation_rate = round(fb_correct / fb_total * 100, 1) if fb_total else 0.0
        escalation_rate = round(escalated / complete * 100, 1) if complete else 0.0

        return {
            "today": {
                "total_tasks":      total,
                "complete":         complete,
                "failed":           failed,
                "awaiting_approval": awaiting,
                "auto_resolved":    auto_resolved,
                "escalated":        escalated,
            },
            "rates": {
                "auto_resolved_pct":  round(auto_resolved / complete * 100, 1) if complete else 0.0,
                "validation_rate_pct": validation_rate,
                "escalation_rate_pct": escalation_rate,
            },
            "feedback": {
                "total":   fb_total,
                "correct": fb_correct,
            },
        }
