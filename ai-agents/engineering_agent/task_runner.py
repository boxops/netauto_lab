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

import difflib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

from shared.rate_limiter import BudgetExceededError

logger = logging.getLogger(__name__)

AGENT_NAME            = "eng_agent"
POLL_INTERVAL         = 90   # seconds — full sweep (normal priority)
CRITICAL_POLL_INTERVAL = 15  # seconds — tight loop for critical/high tasks
MAX_PER_CYCLE         = 1    # process one fix at a time; fixes are expensive
INTER_TASK_DELAY      = 10   # seconds between consecutive tasks in one cycle
RETRY_BACKOFF         = 120  # seconds before re-queuing a failed task
PROMETHEUS_URL        = __import__("os").getenv("PROMETHEUS_URL", "http://prometheus:9090")
VERIFY_DELAY          = int(__import__("os").getenv("EXECUTION_VERIFY_DELAY", "300"))

import os as _os
from shared.config import settings as _settings

# Structured keys the engineering agent is prompted to emit
_FIX_KEYS       = {"FIX_TYPE", "DEVICE", "COMMANDS", "RISK", "CONFIDENCE", "REASON"}
_EXECUTION_KEYS = {"EXECUTION_STATUS", "DEVICE", "CHANGES_APPLIED"}


def _parse_tail(text: str, keys: set) -> dict:
    """
    Extract KEY: value pairs from an agent response.

    Handles two formats the LLM may emit:
      COMMANDS: interface Ethernet1\n  no shutdown     (inline)
      COMMANDS:\n```\ninterface Ethernet1\n  no shutdown\n```  (fenced code block)
    """
    result = {}
    lines = text.split("\n")
    n = len(lines)
    i = 0
    while i < n:
        m = re.match(r"^([A-Z][A-Z_]+):\s*(.*)$", lines[i].strip())
        if m and m.group(1) in keys:
            key   = m.group(1)
            value = m.group(2).strip()
            # Empty inline value — look ahead for a fenced code block
            if not value:
                j = i + 1
                while j < n and not lines[j].strip():   # skip blank lines
                    j += 1
                if j < n and lines[j].strip().startswith("```"):
                    j += 1  # skip opening fence
                    code: list[str] = []
                    while j < n and not lines[j].strip().startswith("```"):
                        code.append(lines[j])
                        j += 1
                    value = "\n".join(code).strip()
                    i = j  # advance past the closing fence
            if value:
                result[key] = value
        i += 1
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

        # When RabbitMQ is configured, also consume fix_proposal and approval_gate
        # queues so tasks are processed immediately on arrival.  The polling loop
        # above stays running as a fallback for tasks created before the consumer
        # connected, or when RabbitMQ is unavailable.
        from shared.task_bus import start_consumer
        start_consumer("fix_proposal",  self._handle_mq_task)
        start_consumer("approval_gate", self._handle_mq_gate)

    def _handle_mq_task(self, task_id: str, _priority: str) -> None:
        """RabbitMQ consumer callback: process a fix_proposal task by ID."""
        task = self._task_store.get_task(task_id)
        if task and task.get("status") == "pending":
            self._process_task(task)

    def _handle_mq_gate(self, task_id: str, _priority: str) -> None:
        """RabbitMQ consumer callback: execute an approved approval_gate task."""
        gate = self._task_store.get_task(task_id)
        if gate:
            self._execute_approved_gate(gate)

    def stop(self) -> None:
        self._stop.set()

    # ── loop ──────────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        ticks = 0
        while not self._stop.wait(CRITICAL_POLL_INTERVAL):
            ticks += 1
            try:
                self._poll_priority()               # critical/high every 15 s
            except Exception:
                logger.exception("EngTaskRunner: error in priority sweep")
            try:
                self._poll_approved_gates()         # approved gates every 15 s
            except Exception:
                logger.exception("EngTaskRunner: error in execution cycle")
            if ticks % (POLL_INTERVAL // CRITICAL_POLL_INTERVAL) == 0:
                try:
                    self._poll_once()               # full normal sweep every 90 s
                except Exception:
                    logger.exception("EngTaskRunner: error in normal sweep")

    def _poll_priority(self) -> None:
        """Pick up critical/high priority fix_proposal tasks immediately."""
        pending = self._task_store.list_tasks(
            assigned_to=AGENT_NAME,
            status="pending",
            type="fix_proposal",
            limit=MAX_PER_CYCLE,
            priority_filter={"critical", "high"},
        )
        for task in pending:
            if self._stop.is_set():
                break
            self._process_task(task)

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
        # Carry maintenance flag forward so the approval gate can suppress auto-execution
        do_not_auto_execute = bool(task.get("do_not_auto_execute"))

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

            # Compute config diff now (best-effort, no LLM) so the approval
            # gate content includes a before/after diff for human reviewers.
            config_diff = ""
            if fix_type not in ("no_action",) and commands != "none":
                config_diff = self._fetch_config_diff(device, commands)

            result = {
                "fix_type":       fix_type,
                "device":         device,
                "commands":       commands,
                "risk":           risk,
                "confidence":     confidence_fix,
                "reason":         parsed.get("REASON", ""),
                "config_diff":    config_diff,
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
                self._create_approval_gate(task, result, rca, do_not_auto_execute)
            else:
                self._create_validation_task(task, result, rca, do_not_auto_execute)

        except BudgetExceededError as exc:
            self._task_store.fail_task(task_id, AGENT_NAME, f"Budget exceeded: {exc}")
        except Exception as exc:
            error_str = str(exc)
            if "rate_limit_exceeded" in error_str or "429" in error_str:
                self._task_store.fail_task(
                    task_id, AGENT_NAME,
                    f"OpenAI rate limit exceeded after retries: {error_str[:200]}",
                )
                logger.warning("EngTaskRunner: rate limit exhausted for task=%s", task_id)
            else:
                self._task_store.fail_task(task_id, AGENT_NAME, error_str[:500])
                logger.exception("EngTaskRunner: task=%s failed", task_id)
            self._schedule_retry(task_id)

    # ── config diff ───────────────────────────────────────────────────────────

    def _fetch_config_diff(self, device: str, commands: str) -> str:
        """
        Best-effort: fetch the device's running-config for the affected section,
        then produce a unified diff showing what the proposed commands will change.

        Falls back to a simple '+'-prefixed listing of the proposed commands if
        Nautobot/Ansible is unavailable or the call times out.  Never raises.
        """
        proposed = [l.strip() for l in commands.splitlines()
                    if l.strip() and not l.strip().startswith("!")]
        if not proposed:
            return ""

        simple = "\n".join(f"+ {l}" for l in proposed)

        try:
            from shared.tools import run_show_commands  # lazy import

            # Identify which config section to fetch
            section_hint = ""
            for line in proposed:
                words = line.split()
                if len(words) >= 2 and words[0].lower() in (
                    "interface", "router", "ip", "neighbor", "vlan"
                ):
                    section_hint = f"{words[0]} {words[1]}"
                    break

            show_cmd = (
                f"show running-config | section {section_hint}"
                if section_hint
                else "show running-config"
            )

            result = json.loads(
                run_show_commands.func(
                    device_name=device,
                    commands=show_cmd,
                    timeout=20,
                )
            )

            raw_output = result.get("output", "")
            if isinstance(raw_output, list):
                raw_output = "\n".join(raw_output)

            current = [l for l in raw_output.splitlines() if l.strip()]
            if not current:
                return simple

            # Build "after" by appending proposed lines not already present
            after = list(current)
            for pl in proposed:
                if pl not in after:
                    after.append(pl)

            diff_lines = list(difflib.unified_diff(
                current, after,
                fromfile="current config",
                tofile="proposed config",
                lineterm="",
            ))
            return "\n".join(diff_lines) if diff_lines else simple

        except Exception as exc:
            logger.debug("EngTaskRunner: config diff fetch failed for %s: %s", device, exc)
            return simple

    # ── child task creation ───────────────────────────────────────────────────

    def _create_validation_task(
        self, parent_task: dict, fix_result: dict, rca: dict,
        do_not_auto_execute: bool = False,
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
                do_not_auto_execute=do_not_auto_execute,
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
        self, parent_task: dict, fix_result: dict, rca: dict,
        do_not_auto_execute: bool = False,
    ) -> None:
        fp       = parent_task.get("alert_fingerprint", "")
        device   = fix_result.get("device", "unknown")
        fix_type = fix_result.get("fix_type", "config_change")
        risk     = fix_result.get("risk", "medium").lower()
        confidence = fix_result.get("confidence", "low").lower()

        # Auto-approve if the same fix has succeeded ≥ 2 times for this device,
        # and risk/confidence are favourable.  Maintenance window always forces
        # human approval regardless of confidence.
        auto = (
            not do_not_auto_execute
            and risk == "low"
            and confidence == "high"
            and self._task_store.count_successful_executions(device, fix_type) >= 2
        )

        try:
            child = self._task_store.create_task(
                type="approval_gate",
                created_by=AGENT_NAME,
                assigned_to="system" if auto else "human",
                title=f"{'AUTO-APPROVED' if auto else 'APPROVAL REQUIRED'}: "
                      f"{fix_type} on {device} [risk={risk}]",
                parent_id=parent_task["id"],
                alert_fingerprint=fp,
                priority="high",
                do_not_auto_execute=do_not_auto_execute,
                content={
                    "fix_proposal":      fix_result,
                    "rca":               rca,
                    "parent_task_id":    parent_task["id"],
                    "do_not_auto_execute": do_not_auto_execute,
                    "config_diff":       fix_result.get("config_diff", ""),
                    "reason":            (
                        "Device in maintenance window — auto-execution suppressed."
                        if do_not_auto_execute
                        else "High-risk fix or explicit escalation — human approval required."
                    ),
                },
            )
            if auto:
                self._task_store.add_event(
                    child["id"], AGENT_NAME, "auto_approved",
                    {"reason": f"risk={risk}, confidence={confidence}, 2+ prior successful executions"},
                )
                self._task_store.approve_task(child["id"], "system")
                logger.info(
                    "EngTaskRunner: AUTO-APPROVED gate=%s for fix=%s (risk=%s, confidence=%s)",
                    child["id"], parent_task["id"], risk, confidence,
                )
            else:
                self._task_store.request_approval(child["id"], AGENT_NAME)
                logger.info(
                    "EngTaskRunner: created approval_gate task=%s for fix=%s",
                    child["id"], parent_task["id"],
                )
        except Exception as exc:
            logger.error("EngTaskRunner: failed to create approval_gate task: %s", exc)

    # ── lab validation ────────────────────────────────────────────────────────

    def _validate_in_lab(
        self, gate_id: str, device: str, commands: str
    ) -> bool:
        """
        Apply the proposed fix to the Containerlab equivalent device, wait
        LAB_VERIFY_DELAY seconds, then check whether Prometheus shows the
        alert has cleared for the lab device.

        Returns True if lab validation passes (proceed to production).
        Returns False if lab validation fails (abort production execution).
        Always returns True when LAB_VALIDATION_ENABLED is false.
        Falls back to True on any error so a transient lab issue never blocks
        a production fix indefinitely.
        """
        if not _settings.lab_validation_enabled:
            return True

        lab_device = _settings.lab_device_prefix + device
        logger.info(
            "EngTaskRunner: lab validation for gate=%s prod=%s lab=%s",
            gate_id, device, lab_device,
        )

        try:
            from shared.tools import run_config_commands, run_show_commands

            # Apply fix to lab device with check_mode=False
            lab_result_str = run_config_commands.func(
                device_name=lab_device,
                config_lines=commands,
                check_mode=False,
                timeout=60,
            )
            lab_result = json.loads(lab_result_str)
            lab_status = (lab_result.get("status") or {})
            if isinstance(lab_status, dict):
                lab_status = lab_status.get("value", "")
            if str(lab_status).upper() not in ("COMPLETED", "SUCCESS", ""):
                logger.warning(
                    "EngTaskRunner: lab fix application failed for gate=%s: %s",
                    gate_id, lab_result.get("error", lab_result_str[:200]),
                )
                self._task_store.add_event(gate_id, AGENT_NAME, "lab_validation_failed",
                    {"reason": "lab fix application failed", "lab_device": lab_device})
                return False

            self._task_store.add_event(gate_id, AGENT_NAME, "lab_fix_applied",
                {"lab_device": lab_device, "commands": commands})

            # Wait for the lab alert to clear
            self._stop.wait(_settings.lab_verify_delay)
            if self._stop.is_set():
                return True  # shutting down — allow prod execution

            # Check Prometheus: is the alert still firing for the lab device?
            import httpx as _httpx
            try:
                r = _httpx.get(f"{PROMETHEUS_URL}/api/v1/alerts", timeout=8)
                r.raise_for_status()
                firing_devices = {
                    a["labels"].get("sysName", a["labels"].get("agent_host", ""))
                    for a in r.json().get("data", {}).get("alerts", [])
                    if a.get("state") == "firing"
                }
                lab_alert_cleared = lab_device not in firing_devices
            except Exception as exc:
                logger.warning(
                    "EngTaskRunner: Prometheus check failed during lab validation: %s — passing", exc
                )
                lab_alert_cleared = True  # be permissive on Prometheus error

            self._task_store.add_event(gate_id, AGENT_NAME,
                "lab_validated" if lab_alert_cleared else "lab_validation_failed",
                {"lab_device": lab_device, "alert_cleared": lab_alert_cleared})

            if not lab_alert_cleared:
                logger.warning(
                    "EngTaskRunner: lab validation FAILED for gate=%s — "
                    "alert still firing on %s after fix", gate_id, lab_device,
                )
            return lab_alert_cleared

        except Exception as exc:
            logger.warning(
                "EngTaskRunner: lab validation error for gate=%s: %s — proceeding to prod",
                gate_id, exc,
            )
            self._task_store.add_event(gate_id, AGENT_NAME, "lab_validation_error",
                {"error": str(exc)[:300]})
            return True  # fail-open: don't block prod on lab errors

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

        # Maintenance window guard: suppress automated execution but keep the gate
        # open for human review.  A human can still approve and execute manually.
        if content.get("do_not_auto_execute") or gate.get("do_not_auto_execute"):
            self._task_store.add_event(
                gate_id, AGENT_NAME, "execution_suppressed",
                {"reason": "device in maintenance window — auto-execution disabled"},
            )
            logger.info(
                "EngTaskRunner: gate=%s suppressed — device in maintenance window", gate_id
            )
            return

        logger.info(
            "EngTaskRunner: executing approved gate=%s device=%s", gate_id, device
        )

        # Lab validation: apply the fix to the Containerlab equivalent first.
        # Only runs when LAB_VALIDATION_ENABLED=true; fail-open on lab errors.
        if not self._validate_in_lab(gate_id, device, commands):
            self._task_store.add_event(
                gate_id, AGENT_NAME, "execution_aborted",
                {"reason": "lab validation failed — fix did not resolve alert in lab"},
            )
            logger.warning(
                "EngTaskRunner: aborting production execution for gate=%s "
                "— lab validation did not clear the alert", gate_id,
            )
            return

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

            # Immediately verify the config was applied on the device (non-LLM)
            config_check: dict = {}
            if exec_status == "success" and commands != "none":
                config_check = self._verify_config_applied(gate_id, device, commands)

            self._task_store.add_event(
                gate_id, AGENT_NAME, "execution_complete",
                {
                    "status":          exec_status,
                    "device":          device,
                    "changes_applied": changes,
                    "tool_calls":      len(tool_calls),
                    "config_applied":  config_check.get("config_applied"),
                    "found_lines":     config_check.get("found_lines", []),
                    "missing_lines":   config_check.get("missing_lines", []),
                },
            )
            logger.info(
                "EngTaskRunner: executed gate=%s status=%s config_applied=%s",
                gate_id, exec_status, config_check.get("config_applied"),
            )
        except BudgetExceededError as exc:
            self._task_store.add_event(
                gate_id, AGENT_NAME, "execution_failed",
                {"error": f"Budget exceeded: {exc}"},
            )
            logger.warning("EngTaskRunner: budget exceeded executing gate=%s", gate_id)
            return
        except Exception as exc:
            self._task_store.add_event(
                gate_id, AGENT_NAME, "execution_failed",
                {"error": str(exc)[:500]},
            )
            logger.exception("EngTaskRunner: failed to execute gate=%s", gate_id)
            return

        # Schedule Prometheus alert-resolution check in the background.
        # Pass alert labels discovered now so the check thread doesn't need
        # a second DB lookup after VERIFY_DELAY seconds.
        fp          = gate.get("alert_fingerprint", "")
        rca_info    = self._get_rca_info(fp)
        t = threading.Thread(
            target=self._verify_resolution,
            args=(gate_id, rca_info),
            daemon=True,
        )
        t.start()

    def _verify_config_applied(
        self, gate_id: str, device: str, commands: str
    ) -> dict:
        """
        Direct (non-LLM) post-execution check: fetch the device's running-config
        and verify each applied config line is present.

        Returns:
            config_applied: True  — all lines found
                            False — one or more lines missing
                            None  — could not reach device or parse output
        Never raises; always returns within ~30 s.
        """
        applied = [
            ln.strip() for ln in commands.splitlines()
            if ln.strip() and not ln.strip().startswith("!")
        ]
        if not applied:
            return {"config_applied": None, "note": "no verifiable config lines"}

        try:
            from shared.tools import run_show_commands
            raw = run_show_commands.func(
                device_name=device,
                commands="show running-config",
                timeout=30,
            )
            result  = json.loads(raw)
            output  = result.get("output", "")
            if isinstance(output, list):
                output = "\n".join(str(x) for x in output)
            # Some job wrappers bury text in log_entries
            if not output.strip():
                output = "\n".join(
                    str(e.get("message", ""))
                    for e in result.get("log_entries", [])
                )

            output_lower = output.lower()
            found   = [ln for ln in applied if ln.lower() in output_lower]
            missing = [ln for ln in applied if ln.lower() not in output_lower]

            applied_ok = len(missing) == 0 and len(found) > 0
            logger.info(
                "EngTaskRunner: config check gate=%s device=%s applied=%s "
                "found=%d missing=%d",
                gate_id, device, applied_ok, len(found), len(missing),
            )
            return {
                "config_applied": applied_ok,
                "found_lines":    found,
                "missing_lines":  missing,
                "checked_lines":  len(applied),
            }
        except Exception as exc:
            logger.warning(
                "EngTaskRunner: config verification failed gate=%s device=%s: %s",
                gate_id, device, exc,
            )
            return {"config_applied": None, "error": str(exc)[:200]}

    def _get_rca_info(self, fingerprint: str) -> dict:
        """
        Return alertname, device (sysName), instance, and created_at from the
        root RCA task for this fingerprint.  Used to initialise _verify_resolution.
        """
        if not fingerprint:
            return {}
        try:
            tasks = self._task_store.list_tasks(
                type="rca", alert_fingerprint=fingerprint, limit=1
            )
            if not tasks:
                return {}
            content = json.loads(tasks[0].get("content") or "{}")
            return {
                "alertname":  content.get("alertname", ""),
                "device":     content.get("device", ""),
                "instance":   content.get("instance", ""),
                "created_at": tasks[0].get("created_at"),
            }
        except Exception:
            return {}

    def _verify_resolution(self, gate_id: str, rca_info: dict) -> None:
        """
        Wait VERIFY_DELAY seconds then check Prometheus to see if the alert
        that triggered this pipeline is still firing.

        Matches by (alertname, sysName) — the labels Prometheus actually
        carries.  The old approach of matching by a 'fingerprint' label was
        broken: Prometheus metric labels never include that field.
        """
        if self._stop.wait(VERIFY_DELAY):
            return  # container shutting down

        alertname  = rca_info.get("alertname", "")
        sysname    = rca_info.get("device", "")
        rca_created = rca_info.get("created_at")

        import httpx as _httpx
        resolved = False
        try:
            r = _httpx.get(f"{PROMETHEUS_URL}/api/v1/alerts", timeout=8)
            r.raise_for_status()
            firing = [
                a for a in r.json().get("data", {}).get("alerts", [])
                if a.get("state") == "firing"
            ]
            if alertname:
                # Match on alertname + sysName (or agent_host as fallback)
                still_firing = any(
                    a["labels"].get("alertname") == alertname
                    and (
                        not sysname
                        or a["labels"].get("sysName", a["labels"].get("agent_host", "")) == sysname
                    )
                    for a in firing
                )
                resolved = not still_firing
            else:
                # No alertname — cannot determine; log and leave resolved=False
                logger.warning(
                    "EngTaskRunner: no alertname for gate=%s, cannot verify resolution",
                    gate_id,
                )
        except Exception as exc:
            logger.warning(
                "EngTaskRunner: Prometheus check failed for gate=%s: %s", gate_id, exc
            )

        ttr_s = 0
        if rca_created:
            try:
                t0 = datetime.strptime(rca_created, "%Y-%m-%d %H:%M:%S UTC")
                ttr_s = int((datetime.utcnow() - t0).total_seconds())
            except Exception:
                pass

        self._task_store.add_event(
            gate_id, AGENT_NAME, "execution_verified",
            {
                "alert_resolved": resolved,
                "ttr_seconds":    ttr_s,
                "alertname":      alertname,
                "device":         sysname,
                "check_at":       datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            },
        )
        logger.info(
            "EngTaskRunner: verification gate=%s alert_resolved=%s alertname=%s ttr=%ds",
            gate_id, resolved, alertname, ttr_s,
        )

    def _schedule_retry(self, task_id: str) -> None:
        """Retry a failed fix_proposal task after RETRY_BACKOFF seconds."""
        def _do_retry():
            self._stop.wait(RETRY_BACKOFF)
            if not self._stop.is_set():
                ok = self._task_store.retry_task(task_id, AGENT_NAME)
                if ok:
                    logger.info("EngTaskRunner: re-queued task=%s for retry", task_id)
        threading.Thread(target=_do_retry, daemon=True).start()
