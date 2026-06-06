"""
Learning engine for autonomous policy promotion and demotion.

Implements the graduated autonomy feedback loop from docs/autonomous-agent-framework.md
(Section 3: Autonomy Levels — Promotion requires evidence; Demotion is automatic).

Rules:
  - Promotion: N consecutive successful executions (alert_resolved=True) for a given
    policy_id promote it by one autonomy level (capped at L4 — L5 requires manual action).
  - Demotion: A failed execution (alert_resolved=False) immediately demotes by one level.
  - Both actions are recorded as events in task_events for audit visibility.
"""
from __future__ import annotations

import logging

from shared.policy_registry import PolicyRegistry
import shared.metrics as _metrics

logger = logging.getLogger(__name__)

_PROMOTION_THRESHOLD = 3   # consecutive successes required to promote


class LearningEngine:
    """
    Receives execution outcomes from the pipeline verification step and adjusts
    autonomy policy levels accordingly.
    """

    def __init__(
        self,
        policy_registry: PolicyRegistry,
        task_store,
        promotion_threshold: int = _PROMOTION_THRESHOLD,
        demotion_on_failure: bool = True,
    ) -> None:
        self._registry   = policy_registry
        self._store      = task_store
        self._threshold  = promotion_threshold
        self._demote     = demotion_on_failure

    def record_outcome(
        self,
        *,
        policy_id: str | None,
        fix_type: str,
        device_role: str = "",
        tenant_id: str = "default",
        alert_resolved: bool | None,
        ttr_seconds: int | None = None,
    ) -> None:
        """
        Record an execution outcome to policy_performance and trigger
        promotion/demotion if thresholds are met.

        Called from workflow._verify_resolution() after execution_verified event.
        """
        self._store.record_policy_outcome(
            policy_id=policy_id,
            fix_type=fix_type,
            device_role=device_role,
            tenant_id=tenant_id,
            alert_resolved=alert_resolved,
            ttr_seconds=ttr_seconds,
        )

        if ttr_seconds:
            _metrics.record_policy_ttr(policy_id or "default", ttr_seconds)

        if policy_id is None:
            return  # default L2 applied — no policy to adjust

        if alert_resolved is False and self._demote:
            new_level = self._registry.demote(policy_id)
            if new_level:
                logger.warning(
                    "LearningEngine: policy %s demoted to %s after execution failure",
                    policy_id, new_level,
                )
                _metrics.record_policy_decision(policy_id, new_level, "demoted")
            return

        if alert_resolved is True:
            self._evaluate_promotion(policy_id)

    def evaluate_promotions(self, tenant_id: str = "default") -> list[str]:
        """
        Scan all policies and promote any that have reached the threshold.
        Called on a periodic basis (e.g. daily maintenance sweep).
        Returns list of promoted policy_ids.
        """
        promoted = []
        policies = self._store.list_policies(tenant_id=tenant_id)
        for p in policies:
            pid = p["id"]
            if self._consecutive_successes(pid) >= self._threshold:
                new_level = self._registry.promote(pid)
                if new_level and new_level != p["autonomy_level"]:
                    promoted.append(pid)
                    logger.info(
                        "LearningEngine: policy %s promoted to %s "
                        "(%d consecutive successes)",
                        pid, new_level, self._threshold,
                    )
        return promoted

    def evaluate_demotions(self, tenant_id: str = "default") -> list[str]:
        """
        Scan all policies and demote any that had a recent failure.
        Returns list of demoted policy_ids.
        """
        demoted = []
        policies = self._store.list_policies(tenant_id=tenant_id)
        for p in policies:
            pid       = p["id"]
            outcomes  = self._store.get_policy_performance(pid, limit=1)
            if outcomes and outcomes[0].get("alert_resolved") == 0:
                new_level = self._registry.demote(pid)
                if new_level and new_level != p["autonomy_level"]:
                    demoted.append(pid)
        return demoted

    # ── private helpers ───────────────────────────────────────────────────────

    def _evaluate_promotion(self, policy_id: str) -> None:
        consec = self._consecutive_successes(policy_id)
        if consec >= self._threshold:
            new_level = self._registry.promote(policy_id)
            if new_level:
                logger.info(
                    "LearningEngine: policy %s promoted to %s "
                    "(%d consecutive successes)",
                    policy_id, new_level, consec,
                )

    def _consecutive_successes(self, policy_id: str) -> int:
        """Count the number of consecutive successful outcomes at the end of history."""
        outcomes = self._store.get_policy_performance(policy_id, limit=self._threshold + 5)
        count = 0
        for o in outcomes:
            if o.get("alert_resolved") == 1:
                count += 1
            else:
                break  # chain broken
        return count
