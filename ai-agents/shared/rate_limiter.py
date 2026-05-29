"""
Token budget enforcement and usage tracking for all AI agents.

Writes to the token_usage table in activity.db (shared SQLite volume).
Called from StatusCallbackHandler on every LLM response.

Usage:
    limiter = RateLimiter()
    limiter.check_budget("ops_agent")          # raises BudgetExceededError if over limit
    limiter.record_usage("ops_agent", ...)      # called after each LLM completion
    limiter.get_summary()                       # returns current spend and headroom
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from shared.config import settings

_DEFAULT_DB = os.environ.get("ACTIVITY_DB_PATH", "./activity.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL,
    agent                 TEXT    NOT NULL,
    session_id            TEXT    NOT NULL,
    task_id               TEXT,
    prompt_tokens         INTEGER NOT NULL,
    completion_tokens     INTEGER NOT NULL,
    model                 TEXT    NOT NULL,
    estimated_cost_usd    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_usage_agent_ts
    ON token_usage (agent, timestamp);
"""


class BudgetExceededError(Exception):
    """Raised when an agent would exceed its configured token or dollar budget."""
    def __init__(self, reason: str, remaining_usd: float = 0.0):
        super().__init__(reason)
        self.remaining_usd = remaining_usd


class RateLimiter:
    """Thread-safe token budget tracker backed by the shared activity.db."""

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

    # ── budget checks ─────────────────────────────────────────────────────────

    def check_budget(self, agent: str) -> None:
        """
        Raise BudgetExceededError if the agent is over any configured limit.
        Call this BEFORE invoking the LLM.
        """
        now = datetime.now(timezone.utc)
        hour_start = now.strftime("%Y-%m-%d %H:")
        day_start  = now.strftime("%Y-%m-%d")

        with self._connect() as conn:
            # Per-agent hourly token limit
            row = conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) "
                "FROM token_usage WHERE agent=? AND timestamp LIKE ?",
                (agent, f"{hour_start}%"),
            ).fetchone()
            tokens_this_hour = row[0]

            # Per-agent daily token limit
            row = conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) "
                "FROM token_usage WHERE agent=? AND timestamp LIKE ?",
                (agent, f"{day_start}%"),
            ).fetchone()
            tokens_today = row[0]

            # Global daily dollar spend
            row = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0.0) "
                "FROM token_usage WHERE timestamp LIKE ?",
                (f"{day_start}%",),
            ).fetchone()
            spend_today = row[0]

        remaining_usd = max(0.0, settings.daily_budget_usd - spend_today)

        if tokens_this_hour >= settings.max_tokens_per_agent_per_hour:
            raise BudgetExceededError(
                f"{agent}: hourly token limit reached "
                f"({tokens_this_hour:,} / {settings.max_tokens_per_agent_per_hour:,}). "
                "Try again next hour.",
                remaining_usd=remaining_usd,
            )
        if tokens_today >= settings.max_tokens_per_agent_per_day:
            raise BudgetExceededError(
                f"{agent}: daily token limit reached "
                f"({tokens_today:,} / {settings.max_tokens_per_agent_per_day:,}).",
                remaining_usd=remaining_usd,
            )
        if spend_today >= settings.daily_budget_usd:
            raise BudgetExceededError(
                f"Daily dollar budget exhausted "
                f"(${spend_today:.4f} / ${settings.daily_budget_usd:.2f}).",
                remaining_usd=0.0,
            )

    # ── usage recording ───────────────────────────────────────────────────────

    def record_usage(
        self,
        *,
        agent: str,
        session_id: str,
        task_id: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
    ) -> float:
        """
        Persist a token usage record and return the estimated cost in USD.
        Call this AFTER each LLM completion.
        """
        cost = (
            prompt_tokens     / 1000 * settings.openai_input_cost_per_1k
            + completion_tokens / 1000 * settings.openai_output_cost_per_1k
        )
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO token_usage
                   (timestamp, agent, session_id, task_id,
                    prompt_tokens, completion_tokens, model, estimated_cost_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, agent, session_id, task_id,
                 prompt_tokens, completion_tokens, model, cost),
            )
        return cost

    # ── summaries ─────────────────────────────────────────────────────────────

    def get_summary(self, agent: str | None = None) -> dict:
        """
        Return current-period usage stats.
        If agent is given, scopes to that agent; otherwise returns global totals.
        """
        now = datetime.now(timezone.utc)
        hour_prefix = now.strftime("%Y-%m-%d %H:")
        day_prefix  = now.strftime("%Y-%m-%d")

        agent_filter = "AND agent=?" if agent else ""
        params_hour = [f"{hour_prefix}%"] + ([agent] if agent else [])
        params_day  = [f"{day_prefix}%"]  + ([agent] if agent else [])

        with self._connect() as conn:
            def _fetch(prefix: str, extra: str, params: list):
                return conn.execute(
                    f"SELECT "
                    f"COALESCE(SUM(prompt_tokens),0), "
                    f"COALESCE(SUM(completion_tokens),0), "
                    f"COALESCE(SUM(estimated_cost_usd),0.0), "
                    f"COUNT(*) "
                    f"FROM token_usage WHERE timestamp LIKE ? {extra}",
                    params,
                ).fetchone()

            h = _fetch(hour_prefix, agent_filter, params_hour)
            d = _fetch(day_prefix,  agent_filter, params_day)

            # per-agent breakdown for the day
            breakdown_rows = conn.execute(
                "SELECT agent, "
                "SUM(prompt_tokens+completion_tokens) AS tokens, "
                "SUM(estimated_cost_usd) AS cost "
                "FROM token_usage WHERE timestamp LIKE ? "
                "GROUP BY agent ORDER BY cost DESC",
                (f"{day_prefix}%",),
            ).fetchall()

        return {
            "agent": agent or "all",
            "this_hour": {
                "prompt_tokens":     h[0],
                "completion_tokens": h[1],
                "total_tokens":      h[0] + h[1],
                "cost_usd":          round(h[2], 6),
                "calls":             h[3],
            },
            "today": {
                "prompt_tokens":     d[0],
                "completion_tokens": d[1],
                "total_tokens":      d[0] + d[1],
                "cost_usd":          round(d[2], 6),
                "calls":             d[3],
            },
            "budget": {
                "daily_limit_usd":          settings.daily_budget_usd,
                "remaining_usd":            round(max(0.0, settings.daily_budget_usd - d[2]), 6),
                "pct_used":                 round(min(100.0, d[2] / settings.daily_budget_usd * 100), 1),
                "hourly_token_limit":       settings.max_tokens_per_agent_per_hour,
                "daily_token_limit":        settings.max_tokens_per_agent_per_day,
            },
            "by_agent": [
                {
                    "agent": r["agent"],
                    "tokens_today": r["tokens"],
                    "cost_usd":     round(r["cost"], 6),
                }
                for r in breakdown_rows
            ],
        }
