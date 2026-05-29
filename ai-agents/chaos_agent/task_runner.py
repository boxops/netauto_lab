"""
Chaos Agent task runner — validates fix_proposals by processing validation tasks.

Picks up validation tasks created by the Engineering Agent, uses the Chaos Agent's
topology and observability tools to cross-check whether the proposed fix correctly
addresses the root cause, and writes structured feedback back to both the validation
task and its parent fix_proposal task.

Task lifecycle this runner owns:
  validation: pending → claimed → running → complete | failed
  (writes feedback to parent fix_proposal task)
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone

from shared.rate_limiter import BudgetExceededError

logger = logging.getLogger(__name__)

AGENT_NAME     = "chaos_agent"
POLL_INTERVAL  = 120    # seconds — runs after eng (90s) so fixes are ready
MAX_PER_CYCLE  = 1
INTER_TASK_DELAY = 10

_VALIDATION_KEYS = {"VERDICT", "CONFIDENCE", "RISK_CONFIRMED", "NOTES"}


def _parse_tail(text: str, keys: set) -> dict:
    result = {}
    for line in text.split("\n"):
        m = re.match(r"^([A-Z][A-Z_]+):\s*(.+)$", line.strip())
        if m and m.group(1) in keys:
            result[m.group(1)] = m.group(2).strip()
    return result


class ChaosTaskRunner:
    """Polls TaskStore for validation tasks and drives the Chaos Agent."""

    def __init__(self, agent, task_store, rate_limiter) -> None:
        self._agent        = agent
        self._task_store   = task_store
        self._rate_limiter = rate_limiter
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="ChaosTaskRunner", daemon=True
        )
        self._thread.start()
        logger.info("ChaosTaskRunner started (interval=%ds)", POLL_INTERVAL)

    def stop(self) -> None:
        self._stop.set()

    # ── loop ──────────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.wait(POLL_INTERVAL):
            try:
                self._poll_once()
            except Exception:
                logger.exception("ChaosTaskRunner: unhandled error in poll cycle")

    def _poll_once(self) -> None:
        pending = self._task_store.list_tasks(
            assigned_to=AGENT_NAME,
            status="pending",
            type="validation",
            limit=MAX_PER_CYCLE,
        )
        for i, task in enumerate(pending):
            if self._stop.is_set():
                break
            if i > 0:
                self._stop.wait(INTER_TASK_DELAY)
            self._process_task(task)

    # ── task processing ───────────────────────────────────────────────────────

    def _process_task(self, task: dict) -> None:
        task_id = task["id"]

        try:
            self._rate_limiter.check_budget(AGENT_NAME)
        except BudgetExceededError as exc:
            logger.warning("ChaosTaskRunner: budget exceeded for task=%s: %s", task_id, exc)
            return

        if not self._task_store.claim_task(task_id, AGENT_NAME):
            return

        content = {}
        try:
            content = json.loads(task.get("content") or "{}")
        except json.JSONDecodeError:
            pass

        fix_proposal    = content.get("fix_proposal", {})
        rca             = content.get("rca", {})
        parent_task_id  = content.get("parent_task_id", task.get("parent_id", ""))

        fix_type    = fix_proposal.get("fix_type", "unknown")
        device      = fix_proposal.get("device", "unknown")
        commands    = fix_proposal.get("commands", "none")
        risk        = fix_proposal.get("risk", "unknown")
        fix_reason  = fix_proposal.get("reason", "")
        diagnosis   = rca.get("diagnosis", "")

        prompt = (
            f"AUTOMATED FIX VALIDATION REQUEST\n\n"
            f"The Engineering Agent has proposed a fix for a network alert. "
            f"Your job is to validate whether this fix is correct and safe.\n\n"
            f"Context:\n"
            f"  Root cause (from Ops Agent):  {diagnosis}\n"
            f"  Proposed fix type:            {fix_type}\n"
            f"  Target device:                {device}\n"
            f"  Configuration commands:\n"
            f"    {commands}\n"
            f"  Assessed risk:                {risk}\n"
            f"  Engineering Agent reasoning:  {fix_reason}\n\n"
            f"Your validation steps:\n"
            f"1. get_topology() — check if the fix could affect other devices (blast radius)\n"
            f"2. get_device_metrics('{device}') — confirm current device state\n"
            f"3. get_connected_devices('{device}') — identify what depends on this device\n"
            f"4. get_active_alerts() — see if the original alert is still firing\n"
            f"5. run_show_commands('{device}', 'show running-config') — read current config "
            f"(read-only, do NOT apply any config)\n\n"
            f"Answer these questions:\n"
            f"- Does the proposed fix address the stated root cause?\n"
            f"- Is the risk assessment accurate?\n"
            f"- Are there blast-radius concerns for connected devices or services?\n"
            f"- Is the fix safe to apply in a lab environment?\n\n"
            f"End your response with exactly these lines:\n"
            f"VERDICT: correct | incorrect | partial | unverifiable\n"
            f"CONFIDENCE: high | medium | low\n"
            f"RISK_CONFIRMED: low | medium | high\n"
            f"NOTES: <one sentence summarising your validation finding>"
        )

        self._task_store.start_task(task_id, AGENT_NAME)

        try:
            response, tool_calls = self._agent.chat_with_trace(
                prompt,
                session_id=f"val-{task_id}",
                task_id=task_id,
                task_type="validation",
            )
            parsed  = _parse_tail(response, _VALIDATION_KEYS)
            verdict = parsed.get("VERDICT", "unverifiable").lower()
            conf    = parsed.get("CONFIDENCE", "medium").lower()
            notes   = parsed.get("NOTES", "")
            risk_c  = parsed.get("RISK_CONFIRMED", risk).lower()

            # Write result to the validation task
            self._task_store.complete_task(task_id, AGENT_NAME, {
                "verdict":         verdict,
                "confidence":      conf,
                "risk_confirmed":  risk_c,
                "notes":           notes,
                "tool_calls":      len(tool_calls),
                "full_response":   response[-3000:],
                "completed_at":    datetime.now(timezone.utc).isoformat(),
            })

            # Write feedback to the parent fix_proposal task
            verdict_map = {
                "correct":       "correct",
                "incorrect":     "incorrect",
                "partial":       "partial",
                "unverifiable":  "unverifiable",
            }
            if parent_task_id:
                self._task_store.add_feedback(
                    task_id=parent_task_id,
                    from_agent=AGENT_NAME,
                    verdict=verdict_map.get(verdict, "unverifiable"),
                    confidence={"high": 0.9, "medium": 0.6, "low": 0.3}.get(conf, 0.5),
                    notes=f"[{risk_c} risk] {notes}",
                )

            # Also write feedback to the grandparent rca task if traceable
            parent_task = self._task_store.get_task(parent_task_id) if parent_task_id else None
            if parent_task and parent_task.get("parent_id"):
                self._task_store.add_feedback(
                    task_id=parent_task["parent_id"],
                    from_agent=AGENT_NAME,
                    verdict=verdict_map.get(verdict, "unverifiable"),
                    confidence={"high": 0.9, "medium": 0.6, "low": 0.3}.get(conf, 0.5),
                    notes=f"Chaos validation of eng fix: {notes}",
                )

            logger.info(
                "ChaosTaskRunner: completed validation task=%s verdict=%s confidence=%s",
                task_id, verdict, conf,
            )

        except BudgetExceededError as exc:
            self._task_store.fail_task(task_id, AGENT_NAME, f"Budget exceeded: {exc}")
        except Exception as exc:
            self._task_store.fail_task(task_id, AGENT_NAME, str(exc)[:500])
            logger.exception("ChaosTaskRunner: task=%s failed", task_id)
