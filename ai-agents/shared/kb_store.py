"""
Persistent SQLite-backed knowledge base for past incidents and resolutions.

Entries are created two ways:
  - Automatically when an approval_gate pipeline completes successfully
    (source='pipeline', linked via task_id)
  - Manually by an operator or the agent (source='manual')

The search() method does a simple LIKE match across symptom and root_cause
columns — no external dependencies, works offline.  Upgrade to FTS5 if
search quality becomes a concern.

Database path defaults to ./activity.db (same file as the activity store,
different table) and can be overridden via ACTIVITY_DB_PATH.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

_DEFAULT_DB = os.environ.get("ACTIVITY_DB_PATH", "./activity.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    alert_type  TEXT,
    device_type TEXT,
    symptom     TEXT NOT NULL,
    root_cause  TEXT NOT NULL,
    resolution  TEXT NOT NULL,
    tags        TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',
    task_id     TEXT
);

CREATE INDEX IF NOT EXISTS idx_kb_alert_type ON kb_entries(alert_type);
CREATE INDEX IF NOT EXISTS idx_kb_source     ON kb_entries(source);
"""


class KBStore:
    """Thread-safe SQLite knowledge base store."""

    def __init__(self, db_path: str = _DEFAULT_DB) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def save(
        self,
        symptom: str,
        root_cause: str,
        resolution: str,
        alert_type: str | None = None,
        device_type: str | None = None,
        tags: list[str] | None = None,
        source: str = "manual",
        task_id: str | None = None,
    ) -> dict:
        tags_json = json.dumps(tags) if tags else None
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO kb_entries
                  (created_at, alert_type, device_type, symptom, root_cause, resolution, tags, source, task_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (self._now(), alert_type, device_type, symptom, root_cause, resolution,
                 tags_json, source, task_id),
            )
            entry_id = cur.lastrowid
        return self.get(entry_id)

    def get(self, entry_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM kb_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Case-insensitive search across symptom and root_cause columns."""
        if not query or not query.strip():
            return self.get_all(limit=limit)
        pattern = f"%{query.strip()}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM kb_entries
                WHERE symptom LIKE ? OR root_cause LIKE ? OR resolution LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (pattern, pattern, pattern, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_all(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM kb_entries ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete(self, entry_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM kb_entries WHERE id = ?", (entry_id,))
        return cur.rowcount > 0

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM kb_entries").fetchone()
        return row[0] if row else 0

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        if d.get("tags"):
            try:
                d["tags"] = json.loads(d["tags"])
            except Exception:
                d["tags"] = []
        else:
            d["tags"] = []
        return d


# Module-level singleton — same pattern as activity_store.py
kb_store = KBStore()
