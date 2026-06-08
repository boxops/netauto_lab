"""
Pydantic models for structured agent pipeline outputs.

These models are used with LangChain's with_structured_output() to enforce
response schemas at the API level, replacing the hand-rolled regex _parse_tail
parsers that silently produced empty strings when the LLM deviated from format.

Each model maps to one pipeline stage:
  RcaResult        — ops_agent root cause analysis
  FixProposalResult — eng_agent remediation plan
  ValidationResult  — chaos/validation agent fix review
  ExecutionResult   — eng_agent post-approval execution

Policy/intent models (internal, not LLM-structured-output):
  AutonomyDecision  — result of policy_registry.query()
  ActionPolicy      — API request/response for /policies CRUD
  StandingIntentMatch — result of intent_registry.matching()
  StandingIntent    — API request/response for /intents CRUD
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field


class RcaResult(BaseModel):
    """Structured output from the ops agent RCA stage."""

    diagnosis: str = Field(
        description="One-sentence root cause of the alert."
    )
    affected: str = Field(
        description="The device hostname affected, or 'unknown'."
    )
    action: str = Field(
        description="Recommended next step (e.g. 'bring up Ethernet1', 'no action required')."
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="How confident the agent is in the diagnosis."
    )
    affected_devices: list[str] = Field(
        default_factory=list,
        description="All devices impacted by this incident (including upstream/downstream)."
    )
    upstream_cause: str = Field(
        default="",
        description="Root cause device if different from the alerting device, else empty."
    )
    is_leaf_symptom: bool = Field(
        default=False,
        description="True when this alert is a downstream symptom of an upstream failure."
    )


class FixProposalResult(BaseModel):
    """Structured output from the eng agent fix proposal stage."""

    fix_type: Literal["config_change", "runbook", "no_action", "escalate_human"] = Field(
        description="Category of the proposed fix."
    )
    device: str = Field(
        description="Exact device hostname the fix targets."
    )
    commands: str = Field(
        description="Configuration lines to apply, exactly as they would be entered on the device. "
                    "Use 'none' if no configuration change is needed."
    )
    risk: Literal["low", "medium", "high"] = Field(
        description="Risk level of applying the fix."
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="Confidence that the fix will resolve the issue."
    )
    reason: str = Field(
        description="One sentence explaining the fix and why it addresses the root cause."
    )


class ValidationResult(BaseModel):
    """Structured output from the validation agent fix review stage."""

    verdict: Literal["correct", "incorrect", "partial", "unverifiable"] = Field(
        description="Whether the proposed fix correctly addresses the root cause."
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="Confidence in the validation verdict."
    )
    risk_confirmed: Literal["low", "medium", "high"] = Field(
        description="Validated risk level after checking blast radius."
    )
    notes: str = Field(
        description="One sentence summarising the validation finding."
    )


class ExecutionResult(BaseModel):
    """Structured output from the eng agent post-approval execution stage."""

    execution_status: Literal["success", "failed"] = Field(
        description="Whether the configuration was successfully applied."
    )
    device: str = Field(
        description="The hostname of the device that was configured."
    )
    changes_applied: str = Field(
        description="Brief description of what was applied, or why it failed."
    )


# ── Policy / Intent models (internal — not used with with_structured_output) ─────

@dataclass
class AutonomyDecision:
    """Result of PolicyRegistry.query(). Determines approval routing for a pipeline action."""
    autonomy_level: str          # "L0" | "L1" | "L2" | "L3" | "L4" | "L5"
    requires_approval: bool      # True for L0–L3: must go to approval gate
    allow_execution: bool        # True for L4–L5: can auto-execute without human gate
    policy_id: Optional[str]     # which DB row matched, or None = default L2 applied
    reason: str                  # one sentence for the audit event


_LEVEL_ORDER = ["L0", "L1", "L2", "L3", "L4", "L5"]

_LEVEL_DESCRIPTIONS = {
    "L0": "Manual — advisory only, human does everything",
    "L1": "Advisory — surfaces diagnosis, no suggestions acted on",
    "L2": "Supervised — stages fix, human must approve and execute",
    "L3": "Conditional — human approves, system executes automatically",
    "L4": "Automated — auto-approves within policy limits, notifies",
    "L5": "Autonomous — executes, logs, notifies; exceptions only",
}


def autonomy_level_index(level: str) -> int:
    try:
        return _LEVEL_ORDER.index(level)
    except ValueError:
        return 2  # default to L2 on unknown input


class ActionPolicy(BaseModel):
    """API request/response model for /policies CRUD endpoints."""
    id: str = ""
    tenant_id: str = "default"
    name: str
    alertname: str = ""          # empty = match any
    fix_type: str = ""           # empty = match any
    device_role: str = ""        # empty = match any
    environment: str = ""        # empty = match any
    min_confidence: Literal["low", "medium", "high"] = "low"
    max_risk: Literal["low", "medium", "high"] = "high"
    min_prior_successes: int = 0
    autonomy_level: Literal["L0", "L1", "L2", "L3", "L4", "L5"] = "L2"
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    description: str = ""
    conditions:   str | None = None   # JSON array of condition objects
    rca_template: str | None = None   # JSON object
    fix_template: str | None = None   # JSON object


@dataclass
class StandingIntentMatch:
    """Result of IntentRegistry.matching(). Controls pipeline routing before investigation."""
    intent_id: str
    intent_type: str   # "suppress" | "escalate" | "monitor" | "chaos_schedule"
    action: str        # routing instruction passed to the pipeline
    reason: str        # one sentence for the audit event


class StandingIntent(BaseModel):
    """API request/response model for /intents CRUD endpoints."""
    id: str = ""
    tenant_id: str = "default"
    name: str
    description: str = ""
    intent_type: Literal["suppress", "escalate", "monitor", "chaos_schedule"]
    device: str = ""             # empty = all devices
    device_role: str = ""        # empty = all roles
    alertname: str = ""          # empty = all alerts
    metric_query: str = ""       # PromQL for "monitor" type
    threshold: str = ""          # e.g. "< 1" evaluated against metric_query result
    action: str = ""             # routing instruction
    schedule: str = ""           # cron expression for "chaos_schedule" type
    enabled: bool = True
    created_at: str = ""
    last_triggered_at: str = ""
