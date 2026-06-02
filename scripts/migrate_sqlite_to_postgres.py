#!/usr/bin/env python3
"""
One-shot migration: copy all rows from the SQLite activity.db into the
PostgreSQL agent_tasks database.

Usage:
    python3 scripts/migrate_sqlite_to_postgres.py \
        --sqlite /path/to/activity.db \
        --postgres postgresql+psycopg2://agent:pass@agent-postgres:5432/agent_tasks

Run once, after starting agent-postgres but before switching TASK_DB_URL in .env.
The script is idempotent: rows already present in PostgreSQL are skipped via
INSERT ... ON CONFLICT DO NOTHING.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# ------------------------------------------------------------------
# Bootstrap so we can import TaskStore even without the full venv
# ------------------------------------------------------------------
AI_AGENTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ai-agents")
sys.path.insert(0, AI_AGENTS)


def _migrate(sqlite_path: str, pg_url: str) -> None:
    from sqlalchemy import create_engine, text

    print(f"Source:      sqlite:///{sqlite_path}")
    print(f"Destination: {pg_url}")

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    dst = create_engine(pg_url, pool_pre_ping=True)

    # Ensure destination schema exists
    from shared.task_store import TaskStore
    TaskStore(db_url=pg_url)  # creates tables if missing
    print("Schema ready.")

    with dst.begin() as conn:
        # ── tasks ──────────────────────────────────────────────────────────
        tasks = src.execute("SELECT * FROM tasks").fetchall()
        inserted = 0
        for row in tasks:
            d = dict(row)
            # fill new columns that may not exist in old SQLite dbs
            d.setdefault("retry_count", 0)
            d.setdefault("maintenance_window", 0)
            d.setdefault("do_not_auto_execute", 0)
            try:
                conn.execute(
                    text("""
                        INSERT INTO tasks (
                            id, parent_id, alert_fingerprint, type, status, priority,
                            created_by, assigned_to, title, content, result,
                            created_at, claimed_at, completed_at,
                            retry_count, maintenance_window, do_not_auto_execute
                        ) VALUES (
                            :id, :parent_id, :alert_fingerprint, :type, :status, :priority,
                            :created_by, :assigned_to, :title, :content, :result,
                            :created_at, :claimed_at, :completed_at,
                            :retry_count, :maintenance_window, :do_not_auto_execute
                        ) ON CONFLICT (id) DO NOTHING
                    """),
                    d,
                )
                inserted += 1
            except Exception as exc:
                print(f"  SKIP task {d.get('id')}: {exc}")
        print(f"Tasks:       {inserted}/{len(tasks)} rows migrated")

        # ── task_events ────────────────────────────────────────────────────
        events = src.execute("SELECT * FROM task_events").fetchall()
        ev_inserted = 0
        for row in events:
            d = dict(row)
            try:
                conn.execute(
                    text("""
                        INSERT INTO task_events (id, task_id, timestamp, agent, event_type, detail)
                        VALUES (:id, :task_id, :timestamp, :agent, :event_type, :detail)
                        ON CONFLICT (id) DO NOTHING
                    """),
                    d,
                )
                ev_inserted += 1
            except Exception as exc:
                print(f"  SKIP event {d.get('id')}: {exc}")
        print(f"Events:      {ev_inserted}/{len(events)} rows migrated")

        # ── task_feedback ──────────────────────────────────────────────────
        feedbacks = src.execute("SELECT * FROM task_feedback").fetchall()
        fb_inserted = 0
        for row in feedbacks:
            d = dict(row)
            try:
                conn.execute(
                    text("""
                        INSERT INTO task_feedback
                            (id, task_id, from_agent, verdict, confidence, notes, created_at)
                        VALUES (:id, :task_id, :from_agent, :verdict, :confidence, :notes, :created_at)
                        ON CONFLICT (id) DO NOTHING
                    """),
                    d,
                )
                fb_inserted += 1
            except Exception as exc:
                print(f"  SKIP feedback {d.get('id')}: {exc}")
        print(f"Feedback:    {fb_inserted}/{len(feedbacks)} rows migrated")

    src.close()
    print("\nMigration complete.")
    print("Next: set TASK_DB_URL in .env and restart all agent containers.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate SQLite task store to PostgreSQL")
    parser.add_argument("--sqlite",   required=True, help="Path to activity.db")
    parser.add_argument("--postgres", required=True, help="PostgreSQL SQLAlchemy URL")
    args = parser.parse_args()
    _migrate(args.sqlite, args.postgres)
