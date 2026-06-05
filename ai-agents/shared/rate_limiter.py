"""
Token budget enforcement and usage tracking for all AI agents.

Writes to the token_usage table managed by TaskStore (shared SQLite or PostgreSQL).
Called from StatusCallbackHandler on every LLM response.

Usage:
    task_store = TaskStore()
    limiter = RateLimiter(engine=task_store._engine)
    limiter.check_budget("ops_agent")      # raises BudgetExceededError if over limit
    limiter.record_usage("ops_agent", ...) # called after each LLM completion
    limiter.get_summary()                  # returns current spend and headroom
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from sqlalchemy.engine import Engine
from sqlalchemy import text

from shared.config import settings


class BudgetExceededError(Exception):
    """Raised when an agent would exceed its configured token or dollar budget."""
    def __init__(self, reason: str, remaining_usd: float = 0.0):
        super().__init__(reason)
        self.remaining_usd = remaining_usd


class RateLimiter:
    """Thread-safe token budget tracker backed by the shared TaskStore database."""

    def __init__(self, engine: Engine | None = None) -> None:
        """
        Accept a SQLAlchemy Engine from TaskStore so both share the same backend
        (SQLite in dev, PostgreSQL in production). When engine is None a TaskStore
        is instantiated internally for backwards-compatibility with tests that
        construct RateLimiter() directly.
        """
        if engine is not None:
            self._engine = engine
        else:
            from shared.task_store import TaskStore as _TS
            self._engine = _TS()._engine
        self._lock = threading.Lock()

    # ── budget checks ─────────────────────────────────────────────────────────

    def check_budget(self, agent: str) -> None:
        """
        Raise BudgetExceededError if the agent is over any configured limit.
        Call this BEFORE invoking the LLM.
        """
        now = datetime.now(timezone.utc)
        hour_start = now.strftime("%Y-%m-%d %H:")
        day_start  = now.strftime("%Y-%m-%d")

        with self._engine.connect() as conn:
            tokens_this_hour = conn.execute(
                text("SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) "
                     "FROM token_usage WHERE agent=:agent AND timestamp LIKE :pat"),
                {"agent": agent, "pat": f"{hour_start}%"},
            ).scalar()

            tokens_today = conn.execute(
                text("SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) "
                     "FROM token_usage WHERE agent=:agent AND timestamp LIKE :pat"),
                {"agent": agent, "pat": f"{day_start}%"},
            ).scalar()

            spend_today = conn.execute(
                text("SELECT COALESCE(SUM(estimated_cost_usd), 0.0) "
                     "FROM token_usage WHERE timestamp LIKE :pat"),
                {"pat": f"{day_start}%"},
            ).scalar()

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
        with self._lock, self._engine.begin() as conn:
            conn.execute(
                text("""INSERT INTO token_usage
                        (timestamp, agent, session_id, task_id,
                         prompt_tokens, completion_tokens, model, estimated_cost_usd)
                        VALUES (:ts, :agent, :session_id, :task_id,
                                :prompt_tokens, :completion_tokens, :model, :cost)"""),
                {
                    "ts": ts, "agent": agent, "session_id": session_id,
                    "task_id": task_id, "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens, "model": model, "cost": cost,
                },
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

        agent_clause = "AND agent=:agent" if agent else ""
        params_h = {"pat": f"{hour_prefix}%"}
        params_d = {"pat": f"{day_prefix}%"}
        if agent:
            params_h["agent"] = agent
            params_d["agent"] = agent

        with self._engine.connect() as conn:
            h = conn.execute(
                text(f"SELECT COALESCE(SUM(prompt_tokens),0), "
                     f"COALESCE(SUM(completion_tokens),0), "
                     f"COALESCE(SUM(estimated_cost_usd),0.0), COUNT(*) "
                     f"FROM token_usage WHERE timestamp LIKE :pat {agent_clause}"),
                params_h,
            ).fetchone()
            d = conn.execute(
                text(f"SELECT COALESCE(SUM(prompt_tokens),0), "
                     f"COALESCE(SUM(completion_tokens),0), "
                     f"COALESCE(SUM(estimated_cost_usd),0.0), COUNT(*) "
                     f"FROM token_usage WHERE timestamp LIKE :pat {agent_clause}"),
                params_d,
            ).fetchone()
            breakdown_rows = conn.execute(
                text("SELECT agent, "
                     "SUM(prompt_tokens+completion_tokens) AS tokens, "
                     "SUM(estimated_cost_usd) AS cost "
                     "FROM token_usage WHERE timestamp LIKE :pat "
                     "GROUP BY agent ORDER BY cost DESC"),
                {"pat": f"{day_prefix}%"},
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
                "daily_limit_usd":      settings.daily_budget_usd,
                "remaining_usd":        round(max(0.0, settings.daily_budget_usd - d[2]), 6),
                "pct_used":             round(min(100.0, d[2] / settings.daily_budget_usd * 100), 1),
                "hourly_token_limit":   settings.max_tokens_per_agent_per_hour,
                "daily_token_limit":    settings.max_tokens_per_agent_per_day,
            },
            "by_agent": [
                {"agent": r[0], "tokens_today": r[1], "cost_usd": round(r[2], 6)}
                for r in breakdown_rows
            ],
        }
