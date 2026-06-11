"""
Persistent SQLite-backed knowledge base for past incidents and resolutions.

Entries are created three ways:
  - Automatically when an approval_gate pipeline completes (source='pipeline')
  - By the AI agent after a successful manual diagnosis (source='agent')
  - By a human operator via the UI form (source='manual')

The search() method does a LIKE match across all text columns.
Usage is tracked per entry so operators can see what the agent actually consults.
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

# Columns added after initial schema; applied via ALTER TABLE with graceful skip
_MIGRATIONS = [
    ("usage_count",  "INTEGER NOT NULL DEFAULT 0"),
    ("last_used_at", "TEXT"),
    ("updated_at",   "TEXT"),
    ("fingerprint",  "TEXT"),
]


class KBStore:
    """Thread-safe SQLite knowledge base store."""

    def __init__(self, db_path: str = _DEFAULT_DB) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            for col, defn in _MIGRATIONS:
                try:
                    conn.execute(f"ALTER TABLE kb_entries ADD COLUMN {col} {defn}")
                except sqlite3.OperationalError:
                    pass  # column already exists

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

    # ── Write ──────────────────────────────────────────────────────────────────

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
        fingerprint: str | None = None,
    ) -> dict:
        tags_json = json.dumps(tags) if tags else None
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO kb_entries
                  (created_at, alert_type, device_type, symptom, root_cause, resolution,
                   tags, source, task_id, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (self._now(), alert_type, device_type, symptom, root_cause, resolution,
                 tags_json, source, task_id, fingerprint),
            )
            entry_id = cur.lastrowid
        return self.get(entry_id)

    def update(
        self,
        entry_id: int,
        *,
        symptom: str | None = None,
        root_cause: str | None = None,
        resolution: str | None = None,
        alert_type: str | None = None,
        device_type: str | None = None,
        tags: list[str] | None = None,
    ) -> dict | None:
        fields: dict[str, object] = {"updated_at": self._now()}
        if symptom     is not None: fields["symptom"]     = symptom
        if root_cause  is not None: fields["root_cause"]  = root_cause
        if resolution  is not None: fields["resolution"]  = resolution
        if alert_type  is not None: fields["alert_type"]  = alert_type or None
        if device_type is not None: fields["device_type"] = device_type or None
        if tags        is not None: fields["tags"]        = json.dumps(tags) if tags else None
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE kb_entries SET {set_clause} WHERE id = ?",
                list(fields.values()) + [entry_id],
            )
        return self.get(entry_id)

    def increment_usage(self, entry_ids: list[int]) -> None:
        if not entry_ids:
            return
        now = self._now()
        placeholders = ",".join("?" * len(entry_ids))
        with self._connect() as conn:
            conn.execute(
                f"UPDATE kb_entries SET usage_count = usage_count + 1, last_used_at = ? "
                f"WHERE id IN ({placeholders})",
                [now] + entry_ids,
            )

    def delete(self, entry_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM kb_entries WHERE id = ?", (entry_id,))
        return cur.rowcount > 0

    # ── Read ───────────────────────────────────────────────────────────────────

    def get(self, entry_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM kb_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def find_duplicate(self, alert_type: str, fingerprint: str) -> dict | None:
        """Return an existing pipeline entry for this alert+fingerprint, if any."""
        if not alert_type or not fingerprint:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM kb_entries WHERE alert_type = ? AND fingerprint = ? "
                "AND source = 'pipeline' ORDER BY id DESC LIMIT 1",
                (alert_type, fingerprint),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Case-insensitive search across all text columns, most-used results first."""
        if not query or not query.strip():
            return self.get_all(limit=limit)
        pattern = f"%{query.strip()}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM kb_entries
                WHERE  symptom     LIKE ?
                    OR root_cause  LIKE ?
                    OR resolution  LIKE ?
                    OR alert_type  LIKE ?
                    OR device_type LIKE ?
                ORDER BY usage_count DESC, created_at DESC
                LIMIT ?
                """,
                (pattern, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_all(self, limit: int = 100, source: str = "", alert_type: str = "") -> list[dict]:
        query = "SELECT * FROM kb_entries WHERE 1=1"
        params: list = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if alert_type:
            query += " AND alert_type = ?"
            params.append(alert_type)
        query += " ORDER BY usage_count DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM kb_entries").fetchone()
        return row[0] if row else 0

    def get_stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM kb_entries").fetchone()[0]
            by_source = conn.execute(
                "SELECT source, COUNT(*) AS cnt FROM kb_entries GROUP BY source"
            ).fetchall()
            top_used = conn.execute(
                "SELECT alert_type, SUM(usage_count) AS hits FROM kb_entries "
                "WHERE alert_type IS NOT NULL GROUP BY alert_type "
                "ORDER BY hits DESC LIMIT 3"
            ).fetchall()
            total_hits = conn.execute(
                "SELECT COALESCE(SUM(usage_count), 0) FROM kb_entries"
            ).fetchone()[0]
        return {
            "total":      total,
            "by_source":  {row["source"]: row["cnt"] for row in by_source},
            "top_used":   [{"alert_type": r["alert_type"], "hits": r["hits"]} for r in top_used],
            "total_hits": total_hits,
        }

    def get_distinct_alert_types(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT alert_type FROM kb_entries "
                "WHERE alert_type IS NOT NULL ORDER BY alert_type"
            ).fetchall()
        return [r["alert_type"] for r in rows]

    # ── Helpers ────────────────────────────────────────────────────────────────

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
        # Ensure new columns default gracefully on rows predating the migration
        d.setdefault("usage_count",  0)
        d.setdefault("last_used_at", None)
        d.setdefault("updated_at",   None)
        d.setdefault("fingerprint",  None)
        return d


# Module-level singleton
kb_store = KBStore()
