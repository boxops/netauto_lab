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

import json
import logging
from datetime import datetime, timedelta, timezone

from shared.config import settings
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
    # ── Fast-path entries (programmatic resolution — zero LLM calls) ──────────
    # InterfaceAdminDown: confirm via Prometheus ifAdminStatus==2, then restore.
    # Double-braces around PromQL label selectors so _fmt() only substitutes
    # {device} and {interface}, leaving {{sysName=...}} intact as valid PromQL.
    dict(
        name="InterfaceAdminDown fast path — any device",
        alertname="InterfaceAdminDown",
        fix_type="config_change",
        device_role="",       # wildcard: leaf and spine
        environment="",       # wildcard: lab and production
        min_confidence="high",
        max_risk="low",
        min_prior_successes=0,
        autonomy_level="L3",  # human gate on first deploy; promotable to L4
        conditions=json.dumps([
            {
                "type": "metric",
                "query": 'interface_ifAdminStatus{{ifDescr="{interface}",sysName="{device}"}}',
                "expect": "2",
            }
        ]),
        rca_template=json.dumps({
            "diagnosis": "Interface {interface} on {device} is administratively shut down (ifAdminStatus=2). No planned maintenance event found in task history.",
            "confidence": "high",
            "affected_device": "{device}",
            "action": "no shutdown",
            "upstream_cause": "",
            "is_leaf_symptom": False,
        }),
        fix_template=json.dumps({
            "fix_type": "config_change",
            "commands": "interface {interface}\n no shutdown",
            "risk": "low",
            "confidence": "high",
            "reason": "Programmatic fast path: interface {interface} on {device} is admin-down with no maintenance context. Restoring with 'no shutdown'.",
        }),
    ),
    # BGPPeerDown lab leaf: confirm session is not Established, soft-clear.
    # expect_ne="6" means "session must NOT be in state 6 (Established)".
    dict(
        name="BGPPeerDown fast path — lab leaf",
        alertname="BGPPeerDown",
        fix_type="runbook",
        device_role="leaf",
        environment="lab",
        min_confidence="high",
        max_risk="low",
        min_prior_successes=0,
        autonomy_level="L4",  # auto-execute in lab after conditions pass
        conditions=json.dumps([
            {
                "type": "metric",
                "query": 'bgp_peer_bgpPeerState{{sysName="{device}"}}',
                "expect_ne": "6",
            }
        ]),
        rca_template=json.dumps({
            "diagnosis": "BGP session on {device} is not in Established state (confirmed via Prometheus bgpPeerState). Session reset required.",
            "confidence": "high",
            "affected_device": "{device}",
            "action": "clear bgp neighbor",
            "upstream_cause": "",
            "is_leaf_symptom": False,
        }),
        fix_template=json.dumps({
            "fix_type": "runbook",
            "commands": "clear ip bgp * soft",
            "risk": "low",
            "confidence": "high",
            "reason": "Programmatic fast path: BGP session on {device} confirmed non-established. Soft-clear to re-establish.",
        }),
    ),
    # ── Post-hoc gate policies (autonomy decision after AI investigation) ─────
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
    # Generic config change — always L2 (supervised); non-promotable catch-all
    dict(name="Config change — default L2",
         alertname="", fix_type="config_change", device_role="",
         environment="", min_confidence="low", max_risk="high",
         min_prior_successes=0, autonomy_level="L2", promotable=False),
    # Escalate to human — never auto-execute; non-promotable catch-all
    dict(name="Escalate human — always L1",
         alertname="", fix_type="escalate_human", device_role="",
         environment="", min_confidence="low", max_risk="high",
         min_prior_successes=0, autonomy_level="L1", promotable=False),
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

        level = candidate["autonomy_level"]
        idx   = autonomy_level_index(level)

        # Fix 3: check promotion TTL — expired level treated as L2
        expires_at = candidate.get("autonomy_level_expires_at")
        if expires_at and idx >= 4:
            try:
                exp_dt = datetime.fromisoformat(expires_at)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp_dt:
                    logger.warning(
                        "Policy %s autonomy level %s has expired — treating as L2",
                        candidate["id"], level,
                    )
                    _metrics.record_policy_decision(candidate["id"], level, "expired")
                    return AutonomyDecision(
                        autonomy_level="L2",
                        requires_approval=True,
                        allow_execution=False,
                        policy_id=candidate["id"],
                        reason=f"Policy '{candidate['name']}' autonomy level expired — re-validation required.",
                    )
            except (ValueError, TypeError):
                pass

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
        """Increment the autonomy level by one step. Never promotes above L4 automatically.
        Non-promotable policies (catch-alls) are silently skipped."""
        policy = self._store.get_policy(policy_id)
        if not policy:
            return None
        # Fix 1: respect non-promotable flag on wildcard catch-all policies
        if not policy.get("promotable", 1):
            logger.debug("Policy %s is non-promotable — skipping promotion", policy_id)
            return policy["autonomy_level"]
        current_idx = autonomy_level_index(policy["autonomy_level"])
        if current_idx >= 4:  # cap at L4 — L5 requires explicit operator action
            return policy["autonomy_level"]
        new_level = _LEVEL_ORDER[current_idx + 1]
        # Fix 3: record promotion timestamp and TTL expiry
        now     = datetime.now(timezone.utc)
        expires = now + timedelta(days=getattr(settings, "policy_promotion_ttl_days", 90))
        self._store.update_policy(policy_id, {
            "autonomy_level":            new_level,
            "autonomy_level_promoted_at": now.isoformat(),
            "autonomy_level_expires_at":  expires.isoformat(),
        })
        logger.info("Policy %s promoted: %s → %s (expires %s)", policy_id, policy["autonomy_level"], new_level, expires.date())
        return new_level

    def demote(self, policy_id: str) -> str | None:
        """Decrement the autonomy level by one step. Never demotes below L1.
        Clears promotion timestamps — re-promotion re-starts the TTL clock."""
        policy = self._store.get_policy(policy_id)
        if not policy:
            return None
        current_idx = autonomy_level_index(policy["autonomy_level"])
        if current_idx <= 1:
            return policy["autonomy_level"]
        new_level = _LEVEL_ORDER[current_idx - 1]
        self._store.update_policy(policy_id, {
            "autonomy_level":             new_level,
            "autonomy_level_promoted_at": None,
            "autonomy_level_expires_at":  None,
        })
        logger.info("Policy %s demoted: %s → %s", policy_id, policy["autonomy_level"], new_level)
        return new_level

    def seed_defaults(self, tenant_id: str = "default") -> int:
        """
        Insert built-in default policies if the table is empty for this tenant.
        For existing deployments, backfills fast-path fields (conditions/templates)
        onto seed policies that were created before fast-path entries existed.
        Idempotent — safe to call on every startup.
        Returns number of policies inserted (0 if already seeded).
        """
        existing = self._store.list_policies(tenant_id=tenant_id)
        if not existing:
            count = 0
            for seed in _DEFAULT_SEED:
                self._store.create_policy({**seed, "tenant_id": tenant_id})
                count += 1
            logger.info("PolicyRegistry: seeded %d default policies for tenant=%s", count, tenant_id)
            return count
        self._backfill_fast_path_fields(existing, tenant_id)
        return 0

    def _backfill_fast_path_fields(self, existing: list[dict], tenant_id: str) -> None:
        """Populate conditions/templates on existing seed policies that lack them."""
        by_name = {p["name"]: p for p in existing}
        for seed in _DEFAULT_SEED:
            if not (seed.get("conditions") or seed.get("rca_template")):
                continue
            match = by_name.get(seed["name"])
            if match and not match.get("conditions"):
                self._store.update_policy(match["id"], {
                    "conditions":   seed["conditions"],
                    "rca_template": seed["rca_template"],
                    "fix_template": seed["fix_template"],
                })
                logger.info("PolicyRegistry: backfilled fast-path fields for '%s'", seed["name"])

    def get_fast_path_policies(
        self,
        alertname: str,
        tenant_id: str = "default",
        device_role: str = "",
    ) -> list[dict]:
        """
        Return enabled policies that have `conditions` defined and match the
        given alertname (or wildcard) and device_role (or wildcard).
        Ordered by specificity: most-specific (both alertname + device_role set) first.
        Used by _node_policy_fast_path before invoking the AI investigation.
        """
        all_policies = self._store.list_policies(tenant_id=tenant_id)
        fast_path = []
        for p in all_policies:
            if not (p.get("enabled") and p.get("conditions")):
                continue
            # alertname: empty = wildcard
            if p.get("alertname") and p["alertname"] != alertname:
                continue
            # device_role: empty = wildcard; if policy specifies a role it must match
            if p.get("device_role") and device_role and p["device_role"] != device_role:
                continue
            fast_path.append(p)
        # Higher specificity first: both set (score 2) > alertname only (1) > wildcard (0)
        fast_path.sort(key=lambda p: (
            -(int(bool(p.get("alertname"))) + int(bool(p.get("device_role"))))
        ))
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
