"""
Autonomy policy registry for the closed-loop pipeline.

Implements the L0–L5 autonomy level framework defined in docs/autonomous-agent-framework.md.
The registry queries the action_policies table to determine whether a given pipeline action
requires human approval (L0–L3) or can proceed autonomously (L4–L5).

Matching priority (most specific wins):
  1. alertname + fix_type + device_role + environment
  2. fix_type + device_role + environment
  3. fix_type + environment
  4. fix_type
  5. default: L2 (supervised — human gate always required)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from shared.pipeline_models import AutonomyDecision, _LEVEL_ORDER, autonomy_level_index
import shared.metrics as _metrics

logger = logging.getLogger(__name__)

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_RISK_RANK       = {"low": 0, "medium": 1, "high": 2}

_DEFAULT_DECISION = AutonomyDecision(
    autonomy_level="L2",
    requires_approval=True,
    allow_execution=False,
    policy_id=None,
    reason="No matching policy — default L2 (supervised) applied.",
)

_DEFAULT_SEED: list[dict] = [
    # BGP peer reset — leaf nodes in lab → L4 (auto-approve after 2 successes)
    dict(name="BGP peer reset — lab leaf → L4",
         alertname="BGPPeerDown", fix_type="runbook", device_role="leaf",
         environment="lab", min_confidence="high", max_risk="low",
         min_prior_successes=2, autonomy_level="L4"),
    # BGP peer reset — spine in lab → L3 (human approves, auto-executes)
    dict(name="BGP peer reset — lab spine → L3",
         alertname="BGPPeerDown", fix_type="runbook", device_role="spine",
         environment="lab", min_confidence="high", max_risk="medium",
         min_prior_successes=0, autonomy_level="L3"),
    # BGP peer reset — any device in production → L2 (always human gate)
    dict(name="BGP peer reset — production → L2",
         alertname="BGPPeerDown", fix_type="runbook", device_role="",
         environment="production", min_confidence="low", max_risk="high",
         min_prior_successes=0, autonomy_level="L2"),
    # Interface restore — leaf in lab → L4
    dict(name="Interface restore — lab leaf → L4",
         alertname="InterfaceDown", fix_type="config_change", device_role="leaf",
         environment="lab", min_confidence="high", max_risk="low",
         min_prior_successes=2, autonomy_level="L4"),
    # Interface restore — any device in production → L3
    dict(name="Interface restore — production → L3",
         alertname="InterfaceDown", fix_type="config_change", device_role="",
         environment="production", min_confidence="high", max_risk="medium",
         min_prior_successes=0, autonomy_level="L3"),
    # Generic config change — always L2 (supervised)
    dict(name="Config change — default L2",
         alertname="", fix_type="config_change", device_role="",
         environment="", min_confidence="low", max_risk="high",
         min_prior_successes=0, autonomy_level="L2"),
    # Escalate to human — never auto-execute
    dict(name="Escalate human — always L1",
         alertname="", fix_type="escalate_human", device_role="",
         environment="", min_confidence="low", max_risk="high",
         min_prior_successes=0, autonomy_level="L1"),
]


class PolicyRegistry:
    """
    Query the action_policies table to determine the autonomy level for a given
    pipeline action. Instantiate with the TaskStore instance.
    """

    def __init__(self, task_store) -> None:
        self._store = task_store

    # ── public interface ──────────────────────────────────────────────────────

    def query(
        self,
        *,
        fix_type: str,
        device_role: str = "",
        environment: str = "",
        confidence: str = "low",
        risk: str = "high",
        prior_success_count: int = 0,
        alertname: str = "",
        tenant_id: str = "default",
    ) -> AutonomyDecision:
        """
        Return the highest-specificity policy that matches the given context.
        Falls back to L2 (supervised) if no policy matches.
        """
        policies = self._store.list_policies(tenant_id=tenant_id)
        enabled  = [p for p in policies if p.get("enabled", 1)]

        candidate = self._find_best_match(
            policies=enabled,
            fix_type=fix_type,
            device_role=device_role,
            environment=environment,
            confidence=confidence,
            risk=risk,
            prior_success_count=prior_success_count,
            alertname=alertname,
        )

        if candidate is None:
            _metrics.record_policy_decision("default", "L2", "approval_requested")
            return _DEFAULT_DECISION

        level   = candidate["autonomy_level"]
        idx     = autonomy_level_index(level)
        outcome = "auto_approved" if idx >= 4 else "approval_requested"
        _metrics.record_policy_decision(candidate["id"], level, outcome)
        return AutonomyDecision(
            autonomy_level=level,
            requires_approval=idx <= 3,   # L0–L3 require human approval
            allow_execution=idx >= 4,     # L4–L5 auto-execute
            policy_id=candidate["id"],
            reason=f"Policy '{candidate['name']}' matched ({level}).",
        )

    def promote(self, policy_id: str) -> str | None:
        """Increment the autonomy level by one step. Never promotes above L4 automatically."""
        policy = self._store.get_policy(policy_id)
        if not policy:
            return None
        current_idx = autonomy_level_index(policy["autonomy_level"])
        if current_idx >= 4:  # cap at L4 — L5 requires explicit operator action
            return policy["autonomy_level"]
        new_level = _LEVEL_ORDER[current_idx + 1]
        self._store.update_policy(policy_id, {"autonomy_level": new_level})
        logger.info("Policy %s promoted: %s → %s", policy_id, policy["autonomy_level"], new_level)
        return new_level

    def demote(self, policy_id: str) -> str | None:
        """Decrement the autonomy level by one step. Never demotes below L1."""
        policy = self._store.get_policy(policy_id)
        if not policy:
            return None
        current_idx = autonomy_level_index(policy["autonomy_level"])
        if current_idx <= 1:
            return policy["autonomy_level"]
        new_level = _LEVEL_ORDER[current_idx - 1]
        self._store.update_policy(policy_id, {"autonomy_level": new_level})
        logger.info("Policy %s demoted: %s → %s", policy_id, policy["autonomy_level"], new_level)
        return new_level

    def seed_defaults(self, tenant_id: str = "default") -> int:
        """
        Insert built-in default policies if the table is empty for this tenant.
        Idempotent — safe to call on every startup.
        Returns number of policies inserted (0 if already seeded).
        """
        existing = self._store.list_policies(tenant_id=tenant_id)
        if existing:
            return 0
        count = 0
        for seed in _DEFAULT_SEED:
            self._store.create_policy({**seed, "tenant_id": tenant_id})
            count += 1
        logger.info("PolicyRegistry: seeded %d default policies for tenant=%s", count, tenant_id)
        return count

    def get_fast_path_policies(
        self,
        alertname: str,
        tenant_id: str = "default",
    ) -> list[dict]:
        """
        Return enabled policies that have `conditions` defined and whose
        alertname filter matches the given alertname (or is a wildcard).
        Ordered by specificity: alertname-specific policies first, then wildcards.
        Used by _node_policy_fast_path before invoking the AI investigation.
        """
        all_policies = self._store.list_policies(tenant_id=tenant_id)
        fast_path = [
            p for p in all_policies
            if p.get("enabled") and p.get("conditions")
            and (not p.get("alertname") or p["alertname"] == alertname)
        ]
        # More-specific policies (alertname set) first
        fast_path.sort(key=lambda p: (0 if p.get("alertname") else 1))
        return fast_path

    # ── private helpers ───────────────────────────────────────────────────────

    def _find_best_match(
        self,
        policies: list[dict],
        fix_type: str,
        device_role: str,
        environment: str,
        confidence: str,
        risk: str,
        prior_success_count: int,
        alertname: str,
    ) -> dict | None:
        """
        Find the most specific matching policy. Specificity is the number of
        non-empty filter fields that matched; ties broken by created_at (newer wins).
        """
        best: dict | None = None
        best_score: int   = -1

        conf_rank = _CONFIDENCE_RANK.get(confidence, 0)
        risk_rank = _RISK_RANK.get(risk, 2)

        for p in policies:
            # Hard filter: confidence must meet minimum, risk must not exceed maximum
            min_conf = _CONFIDENCE_RANK.get(p.get("min_confidence", "low"), 0)
            max_risk = _RISK_RANK.get(p.get("max_risk", "high"), 2)
            if conf_rank < min_conf:
                continue
            if risk_rank > max_risk:
                continue
            if prior_success_count < p.get("min_prior_successes", 0):
                continue

            # Field matches — empty field in policy = wildcard (matches anything)
            score = 0
            if p.get("alertname"):
                if p["alertname"] != alertname:
                    continue
                score += 8
            if p.get("fix_type"):
                if p["fix_type"] != fix_type:
                    continue
                score += 4
            if p.get("device_role"):
                if p["device_role"] != device_role:
                    continue
                score += 2
            if p.get("environment"):
                if p["environment"] != environment:
                    continue
                score += 1

            if score > best_score:
                best_score = score
                best = p

        return best
