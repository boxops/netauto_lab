"""
Persistent SQLite-backed activity store for agent interactions and tool calls.

The database path defaults to ./activity.db (suitable for local dev) and can be
overridden via the ACTIVITY_DB_PATH environment variable. In Docker the compose
file should set ACTIVITY_DB_PATH=/app/data/activity.db and mount a volume there.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

_DEFAULT_DB = os.environ.get("ACTIVITY_DB_PATH", "./activity.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    agent       TEXT    NOT NULL,
    session_id  TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    latency_ms  INTEGER NOT NULL,
    message     TEXT    NOT NULL,
    response    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT NOT NULL,
    agent          TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    tool_name      TEXT NOT NULL,
    input_summary  TEXT,
    output_summary TEXT
);
"""


def _truncate(text: str, max_len: int = 300) -> str:
    text = text or ""
    return text if len(text) <= max_len else f"{text[: max_len - 3]}..."


class ActivityStore:
    """Thread-safe SQLite activity store.

    Stores one row per agent interaction and one row per tool call made
    during the ReAct reasoning loop.
    """

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

    def record(
        self,
        *,
        agent: str,
        session_id: str,
        message: str,
        response: str,
        status: str,
        latency_ms: int,
    ) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO interactions
                   (timestamp, agent, session_id, status, latency_ms, message, response)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts, agent, session_id, status, latency_ms,
                 _truncate(message, 400), _truncate(response, 400)),
            )

    def record_tool_calls(
        self,
        *,
        agent: str,
        session_id: str,
        tool_calls: list[dict],
    ) -> None:
        if not tool_calls:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        rows = [
            (
                ts, agent, session_id,
                tc.get("tool_name", ""),
                _truncate(tc.get("input_summary", "") or "", 200),
                _truncate(tc.get("output_summary", "") or "", 300),
            )
            for tc in tool_calls
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT INTO tool_calls
                   (timestamp, agent, session_id, tool_name, input_summary, output_summary)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def get_recent(
        self,
        limit: int = 150,
        agent_filter: str | None = None,
    ) -> list[dict]:
        """Return recent interactions, newest first."""
        query = "SELECT * FROM interactions"
        params: list = []
        if agent_filter and agent_filter.lower() != "all":
            query += " WHERE agent = ?"
            params.append(agent_filter)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_tool_calls(self, session_id: str) -> list[dict]:
        """Return all tool calls for a specific session."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def summary(self) -> dict:
        """Return aggregate counts per agent and overall success/failure."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
            success = conn.execute(
                "SELECT COUNT(*) FROM interactions WHERE status = 'success'"
            ).fetchone()[0]
            by_agent = conn.execute(
                "SELECT agent, COUNT(*) as cnt FROM interactions GROUP BY agent"
            ).fetchall()
        return {
            "total": total,
            "success": success,
            "failed": total - success,
            "by_agent": {row["agent"]: row["cnt"] for row in by_agent},
        }
