"""
Self-grading evaluation loop: chaos injections as ground truth.

Clano is the rare system that *knows* what is wrong with the network when a
chaos tool injects a fault. This module closes that loop: every executed
chaos injection is recorded as ground truth, and a background sweep later
grades the pipeline's response against it:

  detected        — did the pipeline open an investigation for the device?
  correct_device  — did the RCA blame the injected device?
  correct_cause   — does the diagnosis/fix match the injected fault class?
  resolved        — did execution verification confirm the alert cleared?
  ttd / ttr       — time to detect (injection → task) and time to resolve

Grades aggregate into the accuracy ledger shown on the System page — a
published, per-fault-class accuracy record of the AI pipeline.

Cause-grading is deliberately a deterministic keyword heuristic (documented
per fault type below), not an LLM judge: the ledger must itself be auditable.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

_TS_FMT = "%Y-%m-%d %H:%M:%S UTC"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, _TS_FMT).replace(tzinfo=timezone.utc)
    except Exception:
        return None


# Keyword heuristics per fault type used by correct_cause grading.
# A diagnosis is "correct" when ANY group fully matches (all words of the
# group appear in the combined diagnosis + action + commands text).
_CAUSE_SIGNATURES: dict[str, list[list[str]]] = {
    "interface_down": [
        ["admin", "down"],
        ["admin", "shut"],
        ["no shutdown"],
        ["interface", "shut"],
    ],
    "bgp_flap": [
        ["bgp", "establish"],
        ["bgp", "session"],
        ["bgp", "clear"],
        ["bgp", "peer"],
    ],
}


class EvalStore:
    """
    Ground-truth injections + grades, stored alongside the task tables.
    Reuses the TaskStore engine/lock so SQLite and PostgreSQL both work.
    """

    def __init__(self, task_store) -> None:
        self._ts      = task_store
        self._lock    = task_store._lock
        self._connect = task_store._connect
        serial = (
            "SERIAL PRIMARY KEY"
            if task_store._dialect == "postgresql"
            else "INTEGER PRIMARY KEY AUTOINCREMENT"
        )
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS chaos_injections (
                id          TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL DEFAULT 'default',
                fault_type  TEXT NOT NULL,
                device      TEXT NOT NULL,
                target      TEXT NOT NULL DEFAULT '',
                source      TEXT NOT NULL DEFAULT '',
                injected_at TEXT NOT NULL,
                graded      INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_injections_graded ON chaos_injections(graded, injected_at)",
            f"""
            CREATE TABLE IF NOT EXISTS eval_results (
                id              {serial},
                injection_id    TEXT NOT NULL REFERENCES chaos_injections(id),
                tenant_id       TEXT NOT NULL DEFAULT 'default',
                fault_type      TEXT NOT NULL DEFAULT '',
                task_id         TEXT NOT NULL DEFAULT '',
                alertname       TEXT NOT NULL DEFAULT '',
                detected        INTEGER NOT NULL DEFAULT 0,
                correct_device  INTEGER NOT NULL DEFAULT 0,
                correct_cause   INTEGER NOT NULL DEFAULT 0,
                resolved        INTEGER NOT NULL DEFAULT 0,
                fast_path       INTEGER NOT NULL DEFAULT 0,
                ttd_seconds     INTEGER,
                ttr_seconds     INTEGER,
                graded_at       TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_eval_results_fault ON eval_results(tenant_id, fault_type)",
        ]
        with self._lock, self._connect() as conn:
            for stmt in ddl:
                conn.execute(text(stmt))

    # ── injections ─────────────────────────────────────────────────────────────

    def record_injection(
        self,
        fault_type: str,
        device: str,
        target: str = "",
        source: str = "",
        tenant_id: str = "default",
    ) -> dict:
        row = {
            "id":          str(uuid.uuid4()),
            "tenant_id":   tenant_id,
            "fault_type":  fault_type,
            "device":      device,
            "target":      target,
            "source":      source,
            "injected_at": _fmt(_now()),
        }
        with self._lock, self._connect() as conn:
            conn.execute(text(
                "INSERT INTO chaos_injections "
                "(id, tenant_id, fault_type, device, target, source, injected_at, graded) "
                "VALUES (:id, :tenant_id, :fault_type, :device, :target, :source, :injected_at, 0)"
            ), row)
        logger.info("EvalStore: recorded injection %s %s on %s/%s",
                    row["id"][:8], fault_type, device, target)
        return row

    def pending_injections(self, min_age_seconds: int = 0) -> list[dict]:
        """Ungraded injections at least min_age_seconds old (pipeline had time to run)."""
        cutoff = _fmt(_now() - timedelta(seconds=min_age_seconds))
        with self._lock, self._connect() as conn:
            rows = conn.execute(text(
                "SELECT * FROM chaos_injections "
                "WHERE graded = 0 AND injected_at <= :cutoff ORDER BY injected_at"
            ), {"cutoff": cutoff}).fetchall()
        return [dict(r._mapping) for r in rows]

    def save_grade(self, injection: dict, grade: dict) -> None:
        grade = {
            "injection_id":   injection["id"],
            "tenant_id":      injection.get("tenant_id", "default"),
            "fault_type":     injection.get("fault_type", ""),
            "graded_at":      _fmt(_now()),
            "task_id":        "",
            "alertname":      "",
            "detected":       0,
            "correct_device": 0,
            "correct_cause":  0,
            "resolved":       0,
            "fast_path":      0,
            "ttd_seconds":    None,
            "ttr_seconds":    None,
            **grade,
        }
        with self._lock, self._connect() as conn:
            conn.execute(text(
                "INSERT INTO eval_results "
                "(injection_id, tenant_id, fault_type, task_id, alertname, detected, "
                " correct_device, correct_cause, resolved, fast_path, ttd_seconds, "
                " ttr_seconds, graded_at) "
                "VALUES (:injection_id, :tenant_id, :fault_type, :task_id, :alertname, "
                " :detected, :correct_device, :correct_cause, :resolved, :fast_path, "
                " :ttd_seconds, :ttr_seconds, :graded_at)"
            ), grade)
            conn.execute(text(
                "UPDATE chaos_injections SET graded = 1 WHERE id = :id"
            ), {"id": injection["id"]})

    # ── ledger ─────────────────────────────────────────────────────────────────

    def summary(self, tenant_id: str = "default") -> list[dict]:
        """Per-fault-type accuracy aggregates for the System-page ledger."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(text(
                "SELECT fault_type, "
                "       COUNT(*)            AS injections, "
                "       SUM(detected)       AS detected, "
                "       SUM(correct_device) AS correct_device, "
                "       SUM(correct_cause)  AS correct_cause, "
                "       SUM(resolved)       AS resolved, "
                "       SUM(fast_path)      AS fast_path, "
                "       AVG(ttd_seconds)    AS avg_ttd, "
                "       AVG(ttr_seconds)    AS avg_ttr "
                "FROM eval_results WHERE tenant_id = :t "
                "GROUP BY fault_type ORDER BY fault_type"
            ), {"t": tenant_id}).fetchall()
        out = []
        for r in rows:
            m = dict(r._mapping)
            n = m["injections"] or 1
            out.append({
                "fault_type":         m["fault_type"],
                "injections":         m["injections"],
                "detected_pct":       round(100.0 * (m["detected"] or 0) / n),
                "correct_device_pct": round(100.0 * (m["correct_device"] or 0) / n),
                "correct_cause_pct":  round(100.0 * (m["correct_cause"] or 0) / n),
                "resolved_pct":       round(100.0 * (m["resolved"] or 0) / n),
                "fast_path":          m["fast_path"] or 0,
                "avg_ttd_seconds":    int(m["avg_ttd"]) if m["avg_ttd"] is not None else None,
                "avg_ttr_seconds":    int(m["avg_ttr"]) if m["avg_ttr"] is not None else None,
            })
        return out

    def recent_grades(self, limit: int = 20, tenant_id: str = "default") -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(text(
                "SELECT e.*, i.device, i.target, i.injected_at "
                "FROM eval_results e JOIN chaos_injections i ON e.injection_id = i.id "
                "WHERE e.tenant_id = :t ORDER BY e.id DESC LIMIT :limit"
            ), {"t": tenant_id, "limit": limit}).fetchall()
        return [dict(r._mapping) for r in rows]


class EvalGrader:
    """
    Matches pending injections against pipeline tasks and grades the response.

    Matching: the earliest rca task created after injection time (within
    match_window_seconds) whose content.device equals the injected device.
    If no task appears within the window, the injection is graded as missed.
    """

    def __init__(
        self,
        task_store,
        eval_store: EvalStore,
        min_age_seconds: int = 300,
        match_window_seconds: int = 1800,
    ) -> None:
        self._ts          = task_store
        self._es          = eval_store
        self.min_age      = min_age_seconds
        self.match_window = match_window_seconds

    # ── public ─────────────────────────────────────────────────────────────────

    def grade_pending(self) -> int:
        """Grade all due injections. Returns the number graded."""
        graded = 0
        for injection in self._es.pending_injections(self.min_age):
            try:
                if self._grade_one(injection):
                    graded += 1
            except Exception as exc:
                logger.warning("EvalGrader: grading %s failed: %s",
                               injection.get("id", "?")[:8], exc)
        if graded:
            logger.info("EvalGrader: graded %d injection(s)", graded)
        return graded

    # ── internals ──────────────────────────────────────────────────────────────

    def _grade_one(self, injection: dict) -> bool:
        injected_at = _parse(injection["injected_at"])
        if injected_at is None:
            self._es.save_grade(injection, {})  # unparseable — grade as missed
            return True

        task = self._match_task(injection, injected_at)
        if task is None:
            # No investigation yet: only grade as missed once the window closed.
            if _now() - injected_at < timedelta(seconds=self.match_window):
                return False
            self._es.save_grade(injection, {"detected": 0})
            logger.info("EvalGrader: injection %s on %s NOT detected within window",
                        injection["id"][:8], injection["device"])
            return True

        grade = self._grade_task(injection, injected_at, task)
        self._es.save_grade(injection, grade)
        return True

    def _match_task(self, injection: dict, injected_at: datetime) -> dict | None:
        window_end = injected_at + timedelta(seconds=self.match_window)
        candidates = self._ts.list_tasks(
            type="rca",
            tenant_id=injection.get("tenant_id") or None,
            limit=200,
        )
        matches = []
        for t in candidates:
            created = _parse(t.get("created_at"))
            if created is None or not (injected_at <= created <= window_end):
                continue
            content = self._content(t)
            if content.get("device", "") == injection["device"]:
                matches.append((created, t))
        if not matches:
            return None
        matches.sort(key=lambda pair: pair[0])
        return matches[0][1]

    def _grade_task(self, injection: dict, injected_at: datetime, task: dict) -> dict:
        content = self._content(task)
        events  = self._ts.get_task_events(task["id"])

        rca: dict       = content.get("rca") or {}
        fast_path       = False
        resolved        = False
        ttr: int | None = None

        for ev in events:
            etype  = ev.get("event_type", "")
            detail = ev.get("detail") or {}
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail) if detail else {}
                except Exception:
                    detail = {}
            if etype == "rca_complete" and detail:
                rca = detail
            elif etype == "fast_path_resolved":
                fast_path = True
            elif etype == "execution_verified":
                resolved = bool(detail.get("alert_resolved"))
                ttr      = detail.get("ttr_seconds")

        created = _parse(task.get("created_at"))
        ttd = int((created - injected_at).total_seconds()) if created else None

        affected = (rca.get("affected_device") or content.get("device") or "").lower()
        correct_device = affected == injection["device"].lower()

        fix  = content.get("fix_proposal") or {}
        blob = " ".join([
            str(rca.get("diagnosis", "")),
            str(rca.get("action", "")),
            str(fix.get("reason", "")),
            str(fix.get("commands", "")),
            str(content.get("commands", "")),
        ]).lower()
        signatures = _CAUSE_SIGNATURES.get(injection["fault_type"], [])
        correct_cause = any(all(word in blob for word in group) for group in signatures)

        return {
            "task_id":        task["id"],
            "alertname":      content.get("alertname", ""),
            "detected":       1,
            "correct_device": int(correct_device),
            "correct_cause":  int(correct_cause),
            "resolved":       int(resolved),
            "fast_path":      int(fast_path),
            "ttd_seconds":    ttd,
            "ttr_seconds":    ttr,
        }

    @staticmethod
    def _content(task: dict) -> dict:
        raw = task.get("content") or "{}"
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except Exception:
            return {}
