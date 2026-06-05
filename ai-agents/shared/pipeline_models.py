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
"""
from __future__ import annotations

from typing import Literal

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
