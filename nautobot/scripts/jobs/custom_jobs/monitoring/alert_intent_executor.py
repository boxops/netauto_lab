"""Purpose: Process pending alert-response intents with approval controls."""

from __future__ import annotations

import json
import subprocess

from django.conf import settings
from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, StringVar, register_jobs
from nautobot.extras.choices import JobResultStatusChoices
from nautobot.extras.models import JobResult


name = "Monitoring"


INTENT_PREFIX = "[PENDING_APPROVAL]"
DEFAULT_ALLOWED_PLAYBOOKS = "health_check.yml,validate_bgp.yml,validate_routing.yml"


class AlertIntentExecutor(Job):
    """Approve/reject pending alert intents and optionally run check-mode playbooks."""

    action = StringVar(
        description="Action to apply: approve or reject",
        default="approve",
    )
    execute_check_mode = BooleanVar(
        description=(
            "When approving, execute the recommended playbook in --check mode "
            "(never live mode)"
        ),
        default=False,
        required=False,
    )
    allowed_playbooks = StringVar(
        description="Comma-separated allowlist for executable playbooks",
        default=DEFAULT_ALLOWED_PLAYBOOKS,
        required=False,
    )
    intent_name_filter = StringVar(
        description="Optional substring filter on intent name",
        default="",
        required=False,
    )
    intent_limit = IntegerVar(
        description="Maximum number of pending intents to process",
        default=20,
        min_value=1,
        max_value=200,
    )
    dry_run = BooleanVar(
        description="Preview decisions and execution plan without modifying intent records",
        default=True,
        required=False,
    )

    class Meta:
        name = "Alert Intent Executor"
        description = (
            "Process pending closed-loop alert intents. "
            "Supports approve/reject and optional check-mode playbook execution."
        )
        has_sensitive_variables = False
        soft_time_limit = 600
        time_limit = 1200
        task_queues = [
            settings.CELERY_TASK_DEFAULT_QUEUE,
            "priority",
        ]

    def _safe_create_file(self, filename: str, content: str) -> None:
        try:
            self.create_file(filename, content)
        except Exception:
            self.logger.info(f"Artifact not attached (non-interactive run): {filename}")

    def _collect_intents(self, intent_name_filter: str, intent_limit: int) -> list[JobResult]:
        candidates = JobResult.objects.filter(
            status=JobResultStatusChoices.STATUS_PENDING,
            name__startswith=INTENT_PREFIX,
        ).order_by("date_created")

        intents = []
        for jr in candidates:
            task_kwargs = jr.task_kwargs or {}
            if task_kwargs.get("intent_type") != "closed_loop_alert_response":
                continue
            if intent_name_filter and intent_name_filter.lower() not in jr.name.lower():
                continue
            intents.append(jr)
            if len(intents) >= intent_limit:
                break
        return intents

    def _build_ansible_cmd(self, playbook: str, devices: list[str]) -> list[str]:
        cmd = [
            "docker",
            "exec",
            "netauto-ansible-1",
            "ansible-playbook",
            f"/ansible/playbooks/{playbook}",
            "-i",
            "/ansible/inventory/lab.yml",
            "--check",
        ]
        if devices:
            cmd.extend(["--limit", ",".join(devices)])
        return cmd

    def _execute_check_mode(self, playbook: str, devices: list[str]) -> dict:
        cmd = self._build_ansible_cmd(playbook, devices)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
            )
            return {
                "command": cmd,
                "return_code": proc.returncode,
                "success": proc.returncode == 0,
                "stdout": proc.stdout[-3000:] if len(proc.stdout) > 3000 else proc.stdout,
                "stderr": proc.stderr[-1000:] if len(proc.stderr) > 1000 else proc.stderr,
            }
        except Exception as exc:
            return {
                "command": cmd,
                "return_code": -1,
                "success": False,
                "stdout": "",
                "stderr": str(exc),
            }

    def run(
        self,
        action="approve",
        execute_check_mode=False,
        allowed_playbooks=DEFAULT_ALLOWED_PLAYBOOKS,
        intent_name_filter="",
        intent_limit=20,
        dry_run=True,
    ):
        normalized_action = action.strip().lower()
        if normalized_action not in {"approve", "reject"}:
            self.logger.error("Invalid action value. Use 'approve' or 'reject'.")
            return

        allowed = {p.strip() for p in allowed_playbooks.split(",") if p.strip()}
        intents = self._collect_intents(intent_name_filter, intent_limit)

        processed = 0
        approved = 0
        rejected = 0
        executed = 0
        failed_execution = 0
        skipped_not_allowlisted = 0

        decisions = []

        for intent in intents:
            task_kwargs = intent.task_kwargs or {}
            proposal = task_kwargs.get("proposal", {})
            playbook = proposal.get("recommended_playbook", "health_check.yml")
            scope = proposal.get("recommended_scope", [])

            decision = {
                "intent_id": str(intent.pk),
                "intent_name": intent.name,
                "action": normalized_action,
                "playbook": playbook,
                "scope": scope,
                "status_before": intent.status,
            }

            if normalized_action == "reject":
                rejected += 1
                decision["state"] = "REJECTED"
                decision["status_after"] = JobResultStatusChoices.STATUS_REVOKED
                if not dry_run:
                    result = intent.result or {}
                    result.update({
                        "state": "REJECTED",
                        "executor_action": "reject",
                    })
                    intent.result = result
                    intent.status = JobResultStatusChoices.STATUS_REVOKED
                    intent.save(update_fields=["result", "status"])
                processed += 1
                decisions.append(decision)
                continue

            approved += 1

            if execute_check_mode:
                if playbook not in allowed:
                    skipped_not_allowlisted += 1
                    decision["state"] = "APPROVED_BLOCKED_NOT_ALLOWLISTED"
                    decision["status_after"] = JobResultStatusChoices.STATUS_FAILURE
                    if not dry_run:
                        result = intent.result or {}
                        result.update({
                            "state": "APPROVED_BLOCKED_NOT_ALLOWLISTED",
                            "executor_action": "approve",
                            "blocked_playbook": playbook,
                        })
                        intent.result = result
                        intent.status = JobResultStatusChoices.STATUS_FAILURE
                        intent.save(update_fields=["result", "status"])
                    processed += 1
                    decisions.append(decision)
                    continue

                exec_result = self._execute_check_mode(playbook, scope)
                decision["execution"] = exec_result
                executed += 1
                if exec_result.get("success"):
                    decision["state"] = "APPROVED_EXECUTED_CHECK_MODE"
                    decision["status_after"] = JobResultStatusChoices.STATUS_SUCCESS
                    if not dry_run:
                        result = intent.result or {}
                        result.update({
                            "state": "APPROVED_EXECUTED_CHECK_MODE",
                            "executor_action": "approve",
                            "execution": exec_result,
                        })
                        intent.result = result
                        intent.status = JobResultStatusChoices.STATUS_SUCCESS
                        intent.save(update_fields=["result", "status"])
                else:
                    failed_execution += 1
                    decision["state"] = "APPROVED_EXECUTION_FAILED"
                    decision["status_after"] = JobResultStatusChoices.STATUS_FAILURE
                    if not dry_run:
                        result = intent.result or {}
                        result.update({
                            "state": "APPROVED_EXECUTION_FAILED",
                            "executor_action": "approve",
                            "execution": exec_result,
                        })
                        intent.result = result
                        intent.status = JobResultStatusChoices.STATUS_FAILURE
                        intent.save(update_fields=["result", "status"])
            else:
                decision["state"] = "APPROVED_NO_EXECUTION"
                decision["status_after"] = JobResultStatusChoices.STATUS_SUCCESS
                if not dry_run:
                    result = intent.result or {}
                    result.update({
                        "state": "APPROVED_NO_EXECUTION",
                        "executor_action": "approve",
                    })
                    intent.result = result
                    intent.status = JobResultStatusChoices.STATUS_SUCCESS
                    intent.save(update_fields=["result", "status"])

            processed += 1
            decisions.append(decision)

        summary = {
            "job": "Alert Intent Executor",
            "dry_run": dry_run,
            "action": normalized_action,
            "intent_count": len(intents),
            "processed": processed,
            "approved": approved,
            "rejected": rejected,
            "executed_check_mode": executed,
            "failed_execution": failed_execution,
            "blocked_not_allowlisted": skipped_not_allowlisted,
            "decisions": decisions,
        }

        self.logger.info(
            f"Intent executor complete: intents={len(intents)} processed={processed} "
            f"approved={approved} rejected={rejected} dry_run={dry_run}."
        )

        if dry_run:
            self.logger.info("DRY RUN mode enabled. No pending intent records were modified.")

        self._safe_create_file("alert_intent_execution_results.json", json.dumps(summary, indent=2, sort_keys=True))


register_jobs(AlertIntentExecutor)