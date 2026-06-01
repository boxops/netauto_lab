"""
Engineering Agent task runner — processes fix_proposal tasks from the TaskStore.

Picks up tasks created by the Ops Agent after RCA completion, generates a
specific remediation fix using the Engineering Agent's tools, and creates a
downstream validation task for the Chaos Agent.

Task lifecycle this runner owns:
  fix_proposal: pending → claimed → running → complete | failed
  (creates)  →  validation: pending       (if risk low/medium)
             →  approval_gate: pending    (if risk high or fix_type escalate_human)

  approval_gate: complete + approved event → execution_started event → execution_complete/failed event
  (runs approved fixes with check_mode=False)
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

from shared.rate_limiter import BudgetExceededError

logger = logging.getLogger(__name__)

AGENT_NAME     = "eng_agent"
POLL_INTERVAL  = 90     # seconds — slightly offset from ops (60s) to avoid burst
MAX_PER_CYCLE  = 1      # process one fix at a time; fixes are expensive
INTER_TASK_DELAY = 10   # seconds between consecutive tasks in one cycle

# Structured keys the engineering agent is prompted to emit
_FIX_KEYS       = {"FIX_TYPE", "DEVICE", "COMMANDS", "RISK", "CONFIDENCE", "REASON"}
_EXECUTION_KEYS = {"EXECUTION_STATUS", "DEVICE", "CHANGES_APPLIED"}


def _parse_tail(text: str, keys: set) -> dict:
    result = {}
    for line in text.split("\n"):
        m = re.match(r"^([A-Z][A-Z_]+):\s*(.+)$", line.strip())
        if m and m.group(1) in keys:
            result[m.group(1)] = m.group(2).strip()
    return result


class EngTaskRunner:
    """Polls TaskStore for fix_proposal tasks and drives the Engineering Agent."""

    def __init__(self, agent, task_store, rate_limiter) -> None:
        self._agent        = agent
        self._task_store   = task_store
        self._rate_limiter = rate_limiter
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="EngTaskRunner", daemon=True
        )
        self._thread.start()
        logger.info("EngTaskRunner started (interval=%ds)", POLL_INTERVAL)

    def stop(self) -> None:
        self._stop.set()

    # ── loop ──────────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.wait(POLL_INTERVAL):
            try:
                self._poll_once()
            except Exception:
                logger.exception("EngTaskRunner: unhandled error in fix_proposal cycle")
            try:
                self._poll_approved_gates()
            except Exception:
                logger.exception("EngTaskRunner: unhandled error in execution cycle")

    def _poll_once(self) -> None:
        pending = self._task_store.list_tasks(
            assigned_to=AGENT_NAME,
            status="pending",
            type="fix_proposal",
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

        # Budget guard — leave task pending so it's retried next cycle
        try:
            self._rate_limiter.check_budget(AGENT_NAME)
        except BudgetExceededError as exc:
            logger.warning("EngTaskRunner: budget exceeded for task=%s: %s", task_id, exc)
            return

        # Atomically claim the task
        if not self._task_store.claim_task(task_id, AGENT_NAME):
            return  # another runner instance claimed it first

        content = {}
        try:
            content = json.loads(task.get("content") or "{}")
        except json.JSONDecodeError:
            pass

        rca     = content.get("rca", {})
        alert   = content.get("alert", {})
        alertname       = content.get("alertname", alert.get("alertname", ""))
        affected_device = rca.get("affected_device", "") or alert.get("device", "unknown")
        diagnosis       = rca.get("diagnosis", "")
        action          = rca.get("recommended_action", "")
        confidence      = rca.get("confidence", "")
        rca_response    = rca.get("full_response", "")

        prompt = (
            f"AUTOMATED FIX GENERATION REQUEST\n\n"
            f"The Ops Agent completed a root cause analysis and has escalated to you "
            f"for remediation.\n\n"
            f"  Alert:      {alertname}\n"
            f"  Device:     {affected_device}\n"
            f"  Diagnosis:  {diagnosis}\n"
            f"  Action hint from Ops: {action}\n"
            f"  Ops confidence: {confidence}\n\n"
            f"Ops Agent full analysis (last 1500 chars):\n"
            f"---\n{rca_response[-1500:]}\n---\n\n"
            f"Your job:\n"
            f"1. get_device_info('{affected_device}') — confirm platform and current status\n"
            f"2. get_device_interfaces('{affected_device}') — check interface state\n"
            f"3. run_show_commands(device, cmds) — read current config if needed\n"
            f"4. run_config_commands(device, config_lines, check_mode=True) — "
            f"simulate the fix (never check_mode=False)\n\n"
            f"Generate the most specific, actionable fix possible.\n\n"
            f"End your response with exactly these lines:\n"
            f"FIX_TYPE: config_change | runbook | no_action | escalate_human\n"
            f"DEVICE: <exact device hostname>\n"
            f"COMMANDS: <config lines to apply, or 'none'>\n"
            f"RISK: low | medium | high\n"
            f"CONFIDENCE: high | medium | low\n"
            f"REASON: <one sentence explaining the fix>"
        )

        self._task_store.start_task(task_id, AGENT_NAME)

        try:
            response, tool_calls = self._agent.chat_with_trace(
                prompt,
                session_id=f"fix-{task_id}",
                task_id=task_id,
                task_type="fix_proposal",
            )
            parsed = _parse_tail(response, _FIX_KEYS)
            fix_type   = parsed.get("FIX_TYPE", "runbook").lower().replace(" ", "_")
            risk       = parsed.get("RISK", "medium").lower()
            device     = parsed.get("DEVICE", affected_device)
            commands   = parsed.get("COMMANDS", "none")
            confidence_fix = parsed.get("CONFIDENCE", "medium")

            result = {
                "fix_type":       fix_type,
                "device":         device,
                "commands":       commands,
                "risk":           risk,
                "confidence":     confidence_fix,
                "reason":         parsed.get("REASON", ""),
                "tool_calls":     len(tool_calls),
                "full_response":  response[-3000:],
                "completed_at":   datetime.now(timezone.utc).isoformat(),
            }
            self._task_store.complete_task(task_id, AGENT_NAME, result)
            logger.info(
                "EngTaskRunner: completed fix_proposal task=%s fix_type=%s risk=%s",
                task_id, fix_type, risk,
            )

            if fix_type == "no_action":
                logger.info("EngTaskRunner: no fix needed for task=%s", task_id)
            elif fix_type == "escalate_human" or risk == "high":
                self._create_approval_gate(task, result, rca)
            else:
                self._create_validation_task(task, result, rca)

        except BudgetExceededError as exc:
            self._task_store.fail_task(task_id, AGENT_NAME, f"Budget exceeded: {exc}")
        except Exception as exc:
            error_str = str(exc)
            if "rate_limit_exceeded" in error_str or "429" in error_str:
                # LangChain already retried (max_retries=4). If we're still here,
                # the rate limit persisted — fail so the task is visible in the
                # queue rather than stuck in 'claimed'.
                self._task_store.fail_task(
                    task_id, AGENT_NAME,
                    f"OpenAI rate limit exceeded after retries: {error_str[:200]}",
                )
                logger.warning("EngTaskRunner: rate limit exhausted for task=%s", task_id)
            else:
                self._task_store.fail_task(task_id, AGENT_NAME, error_str[:500])
                logger.exception("EngTaskRunner: task=%s failed", task_id)

    # ── child task creation ───────────────────────────────────────────────────

    def _create_validation_task(
        self, parent_task: dict, fix_result: dict, rca: dict
    ) -> None:
        fp       = parent_task.get("alert_fingerprint", "")
        priority = parent_task.get("priority", "normal")
        device   = fix_result.get("device", "unknown")

        try:
            child = self._task_store.create_task(
                type="validation",
                created_by=AGENT_NAME,
                assigned_to="chaos_agent",
                title=f"Validate fix: {fix_result.get('fix_type')} on {device}",
                parent_id=parent_task["id"],
                alert_fingerprint=fp,
                priority=priority,
                content={
                    "fix_proposal": fix_result,
                    "rca":          rca,
                    "parent_task_id": parent_task["id"],
                },
            )
            logger.info(
                "EngTaskRunner: created validation task=%s (parent fix=%s)",
                child["id"], parent_task["id"],
            )
        except Exception as exc:
            logger.error("EngTaskRunner: failed to create validation task: %s", exc)

    def _create_approval_gate(
        self, parent_task: dict, fix_result: dict, rca: dict
    ) -> None:
        fp     = parent_task.get("alert_fingerprint", "")
        device = fix_result.get("device", "unknown")

        try:
            child = self._task_store.create_task(
                type="approval_gate",
                created_by=AGENT_NAME,
                assigned_to="human",
                title=f"APPROVAL REQUIRED: {fix_result.get('fix_type')} on {device} "
                      f"[risk={fix_result.get('risk')}]",
                parent_id=parent_task["id"],
                alert_fingerprint=fp,
                priority="high",
                content={
                    "fix_proposal":   fix_result,
                    "rca":            rca,
                    "parent_task_id": parent_task["id"],
                    "reason":         "High-risk fix or explicit escalation — human approval required.",
                },
            )
            self._task_store.request_approval(child["id"], AGENT_NAME)
            logger.info(
                "EngTaskRunner: created approval_gate task=%s for fix=%s",
                child["id"], parent_task["id"],
            )
        except Exception as exc:
            logger.error("EngTaskRunner: failed to create approval_gate task: %s", exc)

    # ── post-approval execution ───────────────────────────────────────────────

    def _poll_approved_gates(self) -> None:
        """Execute fixes for approval_gate tasks approved by a human."""
        gates = self._task_store.list_approved_unexecuted_gates(limit=MAX_PER_CYCLE)
        for gate in gates:
            if self._stop.is_set():
                break
            self._execute_approved_gate(gate)

    def _execute_approved_gate(self, gate: dict) -> None:
        gate_id = gate["id"]

        try:
            self._rate_limiter.check_budget(AGENT_NAME)
        except BudgetExceededError as exc:
            logger.warning("EngTaskRunner: budget exceeded for gate=%s: %s", gate_id, exc)
            return

        content = {}
        try:
            content = json.loads(gate.get("content") or "{}")
        except json.JSONDecodeError:
            pass

        fix_proposal = content.get("fix_proposal", {})
        device   = fix_proposal.get("device") or content.get("device", "unknown")
        commands = fix_proposal.get("commands") or content.get("commands", "none")
        fix_type = fix_proposal.get("fix_type", "config_change")

        # Mark as started immediately so a restart cannot double-execute
        self._task_store.add_event(
            gate_id, AGENT_NAME, "execution_started",
            {"device": device, "commands": commands},
        )

        if commands == "none" or fix_type == "no_action":
            self._task_store.add_event(
                gate_id, AGENT_NAME, "execution_complete",
                {"result": "No configuration commands to apply."},
            )
            logger.info("EngTaskRunner: gate=%s has no commands — skipping execution", gate_id)
            return

        logger.info(
            "EngTaskRunner: executing approved gate=%s device=%s", gate_id, device
        )

        prompt = (
            f"APPROVED FIX EXECUTION REQUEST\n\n"
            f"A human has reviewed and approved this configuration change.\n"
            f"Execute it now with check_mode=False.\n\n"
            f"  Approval gate ID: {gate_id}\n"
            f"  Device:           {device}\n"
            f"  Configuration commands:\n"
            f"    {commands}\n\n"
            f"Steps:\n"
            f"1. get_device_info('{device}') — confirm the device is reachable\n"
            f"2. run_config_commands('{device}', config_lines, check_mode=False) "
            f"— apply the approved fix (this is the only time check_mode=False is allowed)\n"
            f"3. run_show_commands('{device}', 'show running-config') — verify the change\n\n"
            f"End your response with exactly these lines:\n"
            f"EXECUTION_STATUS: success | failed\n"
            f"DEVICE: <hostname>\n"
            f"CHANGES_APPLIED: <brief description of what was applied or why it failed>"
        )

        try:
            response, tool_calls = self._agent.chat_with_trace(
                prompt,
                session_id=f"exec-{gate_id}",
                task_id=gate_id,
                task_type="approval_gate",
            )
            parsed      = _parse_tail(response, _EXECUTION_KEYS)
            exec_status = parsed.get("EXECUTION_STATUS", "unknown").lower()
            changes     = parsed.get("CHANGES_APPLIED", "")

            self._task_store.add_event(
                gate_id, AGENT_NAME, "execution_complete",
                {
                    "status":          exec_status,
                    "device":          device,
                    "changes_applied": changes,
                    "tool_calls":      len(tool_calls),
                },
            )
            logger.info(
                "EngTaskRunner: executed gate=%s status=%s device=%s",
                gate_id, exec_status, device,
            )
        except BudgetExceededError as exc:
            self._task_store.add_event(
                gate_id, AGENT_NAME, "execution_failed",
                {"error": f"Budget exceeded: {exc}"},
            )
            logger.warning("EngTaskRunner: budget exceeded executing gate=%s", gate_id)
        except Exception as exc:
            self._task_store.add_event(
                gate_id, AGENT_NAME, "execution_failed",
                {"error": str(exc)[:500]},
            )
            logger.exception("EngTaskRunner: failed to execute gate=%s", gate_id)
