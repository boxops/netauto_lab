"""
Alert Journal — the decision ledger behind the Operations visibility redesign
(docs/operations-visibility-plan.md).

Every alert ingress produces exactly one durable decision record, whatever the
outcome. Before this ledger existed, eight handling paths (dedup, severity
filter, suppress intents, budget deferral, correlation folding, …) left only a
container log line, which is why alerts appeared to be "just skipped".

Writes are best-effort: journaling must never break alert handling. Callers
that fire repeatedly for the same alert state (poll-cycle dedup) must guard
with a once-per-transition key — see AlertPoller._journal_once.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

_TS_FMT = "%Y-%m-%d %H:%M:%S UTC"

# One row per decision. Keep in sync with docs/operations-visibility-plan.md §3.
DECISIONS = frozenset({
    "investigating",          # pipeline opened (AI, no-AI gate, or escalation)
    "fast_path",              # resolved programmatically by a policy
    "suppressed_by_intent",   # standing intent ended the pipeline pre-task
    "escalated_by_intent",    # standing intent forced a human gate
    "deduplicated",           # fingerprint already seen in this state
    "not_firing",             # Prometheus double-check says no longer firing
    "resolved_cleared",       # resolved event → incident closed / gates rejected
    "severity_filtered",      # severity not in the investigation allow-list
    "budget_deferred",        # LLM budget exhausted — retried next cycle
    "already_active",         # an active task already covers this fingerprint
    "correlated_into",        # folded onto an existing same-device task
    "downstream_of",          # folded onto an upstream root-cause task
})

# Stream filter categories (UI chips). "needs_me" is task-state driven and is
# resolved by the UI layer, not the journal.
DROPPED_DECISIONS = frozenset({
    "suppressed_by_intent", "deduplicated", "not_firing", "resolved_cleared",
    "severity_filtered", "budget_deferred", "already_active",
})
LINKED_DECISIONS = frozenset({"correlated_into", "downstream_of"})
ACTIVE_DECISIONS = frozenset({"investigating", "fast_path", "escalated_by_intent"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AlertJournal:
    """Decision ledger. Reuses the TaskStore engine/lock (SQLite + Postgres)."""

    def __init__(self, task_store) -> None:
        self._lock    = task_store._lock
        self._connect = task_store._connect
        serial = (
            "SERIAL PRIMARY KEY"
            if task_store._dialect == "postgresql"
            else "INTEGER PRIMARY KEY AUTOINCREMENT"
        )
        ddl = [
            f"""
            CREATE TABLE IF NOT EXISTS alert_journal (
                id           {serial},
                tenant_id    TEXT NOT NULL DEFAULT 'default',
                fingerprint  TEXT NOT NULL,
                alertname    TEXT NOT NULL DEFAULT '',
                device       TEXT NOT NULL DEFAULT '',
                severity     TEXT NOT NULL DEFAULT '',
                source       TEXT NOT NULL DEFAULT 'poller',
                decision     TEXT NOT NULL,
                reason       TEXT NOT NULL DEFAULT '',
                ref_task_id  TEXT NOT NULL DEFAULT '',
                ref_id       TEXT NOT NULL DEFAULT '',
                received_at  TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_journal_fp   ON alert_journal(fingerprint, id)",
            "CREATE INDEX IF NOT EXISTS idx_journal_time ON alert_journal(tenant_id, received_at)",
        ]
        with self._lock, self._connect() as conn:
            for stmt in ddl:
                conn.execute(text(stmt))

    # ── write ──────────────────────────────────────────────────────────────────

    def record(
        self,
        decision: str,
        event: dict,
        reason: str = "",
        ref_task_id: str = "",
        ref_id: str = "",
        tenant_id: str = "default",
    ) -> None:
        """Best-effort insert — never raises into alert handling."""
        if decision not in DECISIONS:
            logger.warning("AlertJournal: unknown decision %r — recording anyway", decision)
        try:
            labels = event.get("labels", {}) or {}
            row = {
                "tenant_id":   tenant_id,
                "fingerprint": event.get("fingerprint", ""),
                "alertname":   event.get("alertname", ""),
                "device": (
                    event.get("device")
                    or labels.get("sysName")
                    or labels.get("agent_host")
                    or ""
                ),
                "severity":    str(event.get("severity", "")),
                "source":      event.get("_source", "poller"),
                "decision":    decision,
                "reason":      reason[:500],
                "ref_task_id": ref_task_id or "",
                "ref_id":      ref_id or "",
                "received_at": _now().strftime(_TS_FMT),
            }
            with self._lock, self._connect() as conn:
                conn.execute(text(
                    "INSERT INTO alert_journal "
                    "(tenant_id, fingerprint, alertname, device, severity, source, "
                    " decision, reason, ref_task_id, ref_id, received_at) "
                    "VALUES (:tenant_id, :fingerprint, :alertname, :device, :severity, "
                    " :source, :decision, :reason, :ref_task_id, :ref_id, :received_at)"
                ), row)
        except Exception as exc:
            logger.warning("AlertJournal: record(%s) failed: %s", decision, exc)

    # ── read ───────────────────────────────────────────────────────────────────

    def for_fingerprint(self, fingerprint: str, limit: int = 50) -> list[dict]:
        """All decisions for one alert, oldest first (inspector banner)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM alert_journal WHERE fingerprint = :fp "
                "ORDER BY id DESC LIMIT :limit"
            ), {"fp": fingerprint, "limit": limit}).fetchall()
        return [dict(r._mapping) for r in reversed(rows)]

    def latest_per_fingerprint(
        self,
        limit: int = 60,
        tenant_id: str = "default",
        category: str = "",
    ) -> list[dict]:
        """
        Newest decision per fingerprint plus the record count, newest first —
        the backbone of the action stream. category filters by decision group:
        "" (all) | "active" | "dropped" | "linked".
        """
        sql = (
            "SELECT j.*, c.cnt AS record_count FROM alert_journal j "
            "JOIN (SELECT fingerprint, MAX(id) AS mid, COUNT(*) AS cnt "
            "      FROM alert_journal WHERE tenant_id = :t GROUP BY fingerprint) c "
            "  ON j.id = c.mid "
        )
        params: dict = {"t": tenant_id, "limit": limit}
        group = {
            "active":  ACTIVE_DECISIONS,
            "dropped": DROPPED_DECISIONS,
            "linked":  LINKED_DECISIONS,
        }.get(category)
        if group:
            placeholders = ", ".join(f":d{i}" for i, _ in enumerate(sorted(group)))
            sql += f"WHERE j.decision IN ({placeholders}) "
            for i, d in enumerate(sorted(group)):
                params[f"d{i}"] = d
        sql += "ORDER BY j.id DESC LIMIT :limit"
        with self._lock, self._connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [dict(r._mapping) for r in rows]

    def funnel(self, hours: int = 24, tenant_id: str = "default") -> dict:
        """Decision counts + distinct alerts in the window (funnel strip)."""
        cutoff = (_now() - timedelta(hours=hours)).strftime(_TS_FMT)
        with self._lock, self._connect() as conn:
            rows = conn.execute(text(
                "SELECT decision, COUNT(*) AS n FROM alert_journal "
                "WHERE tenant_id = :t AND received_at >= :cutoff GROUP BY decision"
            ), {"t": tenant_id, "cutoff": cutoff}).fetchall()
            distinct = conn.execute(text(
                "SELECT COUNT(DISTINCT fingerprint) FROM alert_journal "
                "WHERE tenant_id = :t AND received_at >= :cutoff"
            ), {"t": tenant_id, "cutoff": cutoff}).scalar() or 0
        by_decision = {r._mapping["decision"]: r._mapping["n"] for r in rows}
        return {
            "alerts":       distinct,
            "by_decision":  by_decision,
            "investigated": by_decision.get("investigating", 0),
            "fast_path":    by_decision.get("fast_path", 0),
            "dropped":      sum(by_decision.get(d, 0) for d in DROPPED_DECISIONS),
            "linked":       sum(by_decision.get(d, 0) for d in LINKED_DECISIONS),
        }

    def prune(self, days: int = 14) -> int:
        cutoff = (_now() - timedelta(days=days)).strftime(_TS_FMT)
        with self._lock, self._connect() as conn:
            result = conn.execute(text(
                "DELETE FROM alert_journal WHERE received_at < :cutoff"
            ), {"cutoff": cutoff})
        n = result.rowcount or 0
        if n:
            logger.info("AlertJournal: pruned %d records older than %d days", n, days)
        return n
