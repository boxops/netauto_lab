# Closed-Loop Autonomous Network Operations Framework

This document defines the principles, architecture, and contract for building closed-loop
autonomous AI agent processes in this project. It is technology-neutral by design: it applies
whether the underlying LLM is GPT-4o, Claude, or a local model, and whether the network
vendors are Arista, Cisco, Juniper, or mixed.

> **Purpose** — Give every engineer and operator a shared mental model for how autonomous
> network agents should behave, how humans stay in control, and how to extend the system
> without breaking existing guarantees.

---

## Table of Contents

1. [Design Principles](#1-design-principles)
2. [The Five-Stage Loop](#2-the-five-stage-loop)
3. [Autonomy Levels](#3-autonomy-levels)
4. [Agent Roles](#4-agent-roles)
5. [Tool Tier Model](#5-tool-tier-model)
6. [Stage Data Contracts](#6-stage-data-contracts)
7. [Governance and Human Oversight](#7-governance-and-human-oversight)
8. [Extensibility Guide](#8-extensibility-guide)
9. [Stack Assessment](#9-stack-assessment)
10. [Recommended Roadmap](#10-recommended-roadmap)

---

## 1. Design Principles

These principles are the foundation. Individual implementation decisions should be evaluated
against them.

### 1.1 Intent over Instructions

Operators express what the network should do, not how. A policy like "all BGP peers must be
established" is an intent. The system figures out detection, diagnosis, and remediation.
Low-level CLI commands are a detail of execution, never the input.

*Competitive context: Cisco Catalyst Center calls this Intent-Based Networking (IBN). Aruba
NetConductor calls it "one-button connectivity". The pattern is universal.*

### 1.2 Observe → Diagnose → Remediate → Verify

Every autonomous action follows this sequence without shortcuts. An agent that acts without
first observing, or that declares success without verifying, is not closed-loop — it is
one-shot automation with AI branding. Verification closes the loop; without it there is
no feedback signal.

### 1.3 Humans at the Edge, Machines in the Middle

Humans define policy (what the network should be), approve changes above a configured risk
threshold, and review outcomes. Machines handle the investigation, fix generation, and
low-risk execution in between. Removing humans from the edge (policy) or from high-risk
approvals produces systems that are difficult to audit and unsafe to operate.

*Juniper Mist AI's self-driving network levels and Cisco's "trusted closed-loop execution
where machines handle day-to-day work while teams maintain control of outcomes" both
express this same principle.*

### 1.4 Explainability is Non-Negotiable

Every automated decision must produce a human-readable rationale. This is not a logging
requirement — it is a trust requirement. If an operator cannot understand why an agent
took an action, they cannot safely extend the autonomy level or debug a failure. One
sentence per decision is the minimum; it must cite the evidence (alert, metric, log line).

### 1.5 Defense in Depth via Stage Separation

No single agent approves and executes its own recommendation. Diagnosis, remediation
design, validation, and execution are owned by distinct stages. This mirrors how well-run
change management works: the engineer proposing a change is not the same person reviewing
it. It also limits blast radius — a faulty fix proposal is caught by the validation stage
before it reaches an approval gate.

### 1.6 Graduated Autonomy

Trust is earned by the system, not granted upfront. The autonomy level for any given
action class starts low and can be elevated only after sufficient evidence of correctness.
A system with a 95%+ accuracy record on a particular fix type can safely be moved to a
higher autonomy level for that type. The autonomy configuration is per-action class, not
system-wide.

### 1.7 Runbooks First, LLM Second

Known alert types and known fix procedures should be encoded in runbooks (human-readable
YAML). The LLM is the fallback for novel situations, not the default path. This reduces
token cost, produces more consistent results, and allows non-LLM review of the fix logic.
When an LLM deviates from a runbook, the deviation should be flagged.

### 1.8 Vendor Neutrality via the Tool Layer

All vendor-specific logic lives in the tool layer, not in agent prompts or pipeline code.
An agent asks "bring interface Ethernet1 on leaf1 into service"; the tool layer translates
that to EOS / IOS-XR / JunOS syntax and executes via the appropriate API. Swapping a vendor
means updating tool implementations, not agent logic.

---

## 2. The Five-Stage Loop

```
                         ┌─────────────────────────────────┐
                         │        Intent / Policy           │
                         │  (standing policies that drive   │
                         │   detection and auto-response)   │
                         └────────────────┬────────────────-┘
                                          │ policy violation detected
                                          ▼
  ┌────────────┐   signal   ┌─────────────────────────────────┐
  │  Telemetry │──────────► │  Stage 1 — SENSE & CORRELATE    │
  │  (metrics, │            │  Ingest events, deduplicate,     │
  │   alerts,  │            │  correlate into one Incident     │
  │   logs)    │            └────────────────┬────────────────-┘
  └────────────┘                             │ incident created
                                             ▼
                            ┌─────────────────────────────────┐
                            │  Stage 2 — DIAGNOSE             │
                            │  Root cause analysis with        │
                            │  structured, explainable output  │
                            └────────────────┬────────────────-┘
                                             │ actionable diagnosis
                                             ▼
                            ┌─────────────────────────────────┐
                            │  Stage 3 — REMEDIATE            │
                            │  Runbook lookup → fix generation │
                            │  Risk assessment + config diff   │
                            └────────────────┬────────────────-┘
                           risk=low/med       │         risk=high
                                ▼             │              ▼
                 ┌──────────────────────┐     │   ┌─────────────────┐
                 │  Stage 4 — VALIDATE  │     │   │  Approval Gate  │
                 │  Blast-radius check  │     │   │  (human review) │
                 │  Read-only device    │     │   └────────┬────────┘
                 │  inspection          │     │            │ approved
                 └──────────┬───────────┘     │            ▼
                            │ correct/partial  │   ┌─────────────────┐
                            ▼                  └──►│  EXECUTE        │
                 ┌──────────────────────┐          │  check_mode=F   │
                 │  Approval Gate       │          └────────┬────────┘
                 │  (human review or    │                   │
                 │   auto-approve)      │                   ▼
                 └──────────┬───────────┘      ┌─────────────────────┐
                            │ approved          │  Stage 5 — VERIFY   │
                            └──────────────────►│  Config confirmed   │
                                                │  Alert resolved?    │
                                                │  TTR recorded       │
                                                │  Feedback written   │
                                                └─────────────────────┘
```

### Stage Summaries

| Stage | Name | Owner | Key Output |
|---|---|---|---|
| 1 | Sense & Correlate | Sensor agent / poller | Incident with grouped alerts, priority |
| 2 | Diagnose | Diagnosis agent | RCA with confidence score and evidence |
| 3 | Remediate | Engineering agent | Commands, risk level, config diff |
| 4 | Validate | Validation agent | Blast-radius verdict and correctness check |
| — | Approval Gate | Human (or auto) | Approved / rejected decision |
| 5 | Verify | Executor / verifier | Config applied status, alert resolution, TTR |

The loop closes at Stage 5 when the system confirms the intent was restored. If the alert
persists after execution, the loop restarts at Stage 1 with the verification failure as
additional context.

---

## 3. Autonomy Levels

Inspired by the SAE levels for autonomous vehicles (Juniper Mist AI uses the same analogy),
these levels define how much a human is involved in the execution path for a given action.

```
Level │ Name           │ What the system does                │ Human role
──────┼────────────────┼─────────────────────────────────────┼──────────────────────
L0    │ Manual         │ Collects telemetry only             │ Does everything
L1    │ Advisory       │ Surfaces diagnosis, no suggestions  │ Interprets findings
L2    │ Assistive      │ Recommends a fix with config diff   │ Decides + executes
L3    │ Supervised     │ Stages fix + waits for approval     │ Reviews + approves
L4    │ Conditional    │ Auto-approves within policy limits  │ Reviews outcomes
L5    │ Autonomous     │ Executes, notifies, loops           │ Reviews exceptions only
```

**Key design decisions:**

- **Autonomy is per-action-class**, not system-wide. An action class is defined by
  `(fix_type, device_role, time_window)`. For example: BGP peer reset on a lab leaf
  during business hours could be L4; the same action on a production spine at midnight
  could be L2.

- **Promotion requires evidence.** Moving an action class from L3 to L4 requires a
  configurable number of successful executions (default: 3) with confirmed alert resolution.

- **Demotion is automatic.** A failed execution or a post-execution alert that does not
  resolve within the verification window drops the autonomy level for that action class
  by one.

- **L5 is opt-in and time-limited.** Full autonomy should be scoped to specific alert
  types in specific environments (e.g., lab only) and reviewed on a defined schedule.

### Autonomy Policy Schema

```yaml
# Conceptual YAML — stored in the policy registry, not in code
action_policies:
  - action_class: bgp_peer_reset
    device_roles: [leaf]
    environments: [lab]
    time_window: always
    autonomy_level: L4
    promotion_threshold: 3   # successful executions before auto-approve
    demotion_on_failure: true

  - action_class: bgp_peer_reset
    device_roles: [spine, core]
    environments: [production]
    time_window: business_hours
    autonomy_level: L3       # always require human approval
    demotion_on_failure: false
```

---

## 4. Agent Roles

Roles describe _what an agent is responsible for_. They are not tied to a specific
process or container. Multiple roles can be played by one agent (as in this project's
unified agent) or each role can run as a separate service.

| Role | Responsibility | Output |
|---|---|---|
| **Sensor** | Consume telemetry streams, deduplicate events, create Incidents | `incident` task |
| **Correlator** | Group alerts from the same device/blast-radius into one Incident | updated `incident` |
| **Diagnostician** | Run RCA: query metrics/logs, build evidence timeline, output structured diagnosis | `rca` result |
| **Remediator** | Fetch runbook or generate fix, compute config diff, assess risk | `fix_proposal` result |
| **Validator** | Check blast radius, inspect device state, confirm fix correctness without writing changes | `validation` result |
| **Executor** | Apply approved changes, record what was sent, trigger verification | `execution_complete` event |
| **Verifier** | Confirm config applied, check alert resolution, record TTR | `execution_verified` event |
| **Learner** | Aggregate feedback, update autonomy levels, identify runbook gaps | metrics + policy updates |

**Separation guideline:** The Remediator must never also be the Validator. The Validator
must never also be the Executor. These separations mirror standard change-management
practice and are the minimal safety net for autonomous operations.

---

## 5. Tool Tier Model

Tools are the only mechanism through which agents access live network state or take
actions. Tools are vendor-neutral at the agent interface and vendor-specific in their
implementation.

```
┌──────────────────────────────────────────────────────────────┐
│  Tier 1 — Intent / Source of Truth                           │
│  What should exist? What is the desired state?               │
│  Examples: Nautobot, NetBox, IPAM, CMDB                      │
├──────────────────────────────────────────────────────────────┤
│  Tier 2 — Observation (Metrics)                              │
│  What is happening right now?                                │
│  Examples: Prometheus, InfluxDB, SNMP collectors             │
├──────────────────────────────────────────────────────────────┤
│  Tier 3 — History (Logs and Events)                          │
│  What happened? What changed?                                │
│  Examples: Loki, Elasticsearch, Splunk, syslog               │
├──────────────────────────────────────────────────────────────┤
│  Tier 4 — Action (Read)                                      │
│  What does the device currently have? (show commands)        │
│  Examples: NETCONF get, SSH show, gNMI subscribe             │
├──────────────────────────────────────────────────────────────┤
│  Tier 5 — Action (Write)                                     │
│  Change network state. Requires explicit approval.           │
│  Examples: NETCONF edit-config, Ansible, CLI config push     │
└──────────────────────────────────────────────────────────────┘
```

**Rules for tools:**

1. Agents work top-down through the tiers. Skipping Tier 1 (inventory) before acting is
   the primary cause of incorrect fixes.
2. Tier 5 (write) tools must always have a `check_mode` parameter that defaults to `True`.
3. Every tool returns either a structured result or a structured error — never an exception
   to the agent. Errors must include suggestions for recovery (e.g., `available_devices`).
4. Tools return names, not IDs. The LLM reasons over human-readable values.
5. Tier 5 tools record a before-and-after snapshot in the task event log.

---

## 6. Stage Data Contracts

Each stage produces a structured output that the next stage consumes. Contracts are
defined here so that stages can be developed, tested, and swapped independently.

### Diagnosis Contract (Stage 2 Output)

```
DIAGNOSIS:  <one-sentence root cause, citing evidence>
AFFECTED:   <device hostname or "unknown">
ACTION:     <recommended next step>
CONFIDENCE: high | medium | low
EVIDENCE:
  - metric: <metric name and value that supports the diagnosis>
  - log:    <syslog event with timestamp>
  - alert:  <alert name and labels>
```

**Pipeline routing from diagnosis:**
- `ACTION` contains "no action", "monitor only", "self-healed" → pipeline ends
- Otherwise → create `fix_proposal` task for Remediator

### Remediation Contract (Stage 3 Output)

```
FIX_TYPE:   config_change | runbook | no_action | escalate_human
DEVICE:     <exact device hostname>
COMMANDS:   <config lines or "none">
RISK:       low | medium | high
CONFIDENCE: high | medium | low
REASON:     <one sentence citing the diagnosis>
RUNBOOK:    <runbook name if a known runbook was followed, else "none">
DIFF:       <unified diff of current running-config vs proposed change>
```

**Pipeline routing from remediation:**
- `FIX_TYPE = no_action` → pipeline ends
- `RISK = high` or `FIX_TYPE = escalate_human` → Approval Gate (skip validation)
- `RISK = low` or `medium` → Validation stage

### Validation Contract (Stage 4 Output)

```
VERDICT:        correct | incorrect | partial | unverifiable
CONFIDENCE:     high | medium | low
RISK_CONFIRMED: low | medium | high
NOTES:          <one sentence on the validation finding>
BLAST_RADIUS:   <list of devices that could be affected>
```

**Pipeline routing from validation:**
- `VERDICT = incorrect` or `unverifiable` → pipeline ends, no approval gate
- `VERDICT = correct` or `partial` → Approval Gate

### Verification Contract (Stage 5 Output)

```json
{
  "config_applied": true | false | null,
  "found_lines": ["..."],
  "missing_lines": ["..."],
  "alert_resolved": true | false,
  "ttr_seconds": 187,
  "check_at": "2026-06-05T14:22:00Z"
}
```

`null` for `config_applied` means the device was unreachable during verification.
The loop restarts if `alert_resolved = false` after a configurable delay.

---

## 7. Governance and Human Oversight

### The Approval Gate

The approval gate is the primary human interface. It must always present:

1. **What will change** — exact config lines to be applied
2. **Why** — the full diagnosis chain (alert → RCA → fix proposal → validation verdict)
3. **Risk** — confirmed risk level with blast-radius summary
4. **Config diff** — unified diff of current running-config vs proposed state
5. **Reject reason capture** — if rejected, the reason is recorded and feeds back to
   the Remediator's training signal

An approval gate must never present a change without the full context. "Trust me" is
not a valid UX pattern for network automation.

### Audit Trail Requirements

Every task must maintain an append-only event log with timestamps, actor (agent or human),
and event type. Minimum required events:

```
created → claimed → started → completed | failed | awaiting_approval
                                        → approved | rejected
                                        → execution_started
                                        → execution_complete
                                        → execution_verified
```

Events must never be deleted or modified. The audit trail is the paper trail for change
management and compliance review.

### Notification Contract

When a task enters `awaiting_approval`, an out-of-band notification must be sent if a
webhook is configured. The notification payload must include:

- Task ID, device, risk level, summary
- Direct links to approve and reject (authenticated)
- HMAC signature for payload integrity

Systems without out-of-band notification are invisible to on-call engineers who are not
watching the dashboard.

### Maintenance Window Integration

The pipeline must consult the source of truth for maintenance windows before creating tasks
and before executing approved changes. A device in a maintenance window always has
`do_not_auto_execute = true` regardless of the action class autonomy level. This is a
hard constraint, not a configuration option.

---

## 8. Extensibility Guide

### Adding a New Pipeline Stage

1. Define the stage's input (what the upstream stage puts in `content`) and output
   (the data contract above).
2. Add the task type to the valid types registry.
3. Implement a runner that follows `claim → start → complete | fail` around the
   agent invocation.
4. Wire the upstream stage to create your new task type on completion.
5. Wire your stage to create the downstream task type on completion.
6. Add the stage to the UI pipeline visual and chronicle views.

Stages should be stateless: all state lives in the task store, not in the runner process.
This means a runner can crash and restart without losing work.

### Adding a New Tool

See [agent-tools-framework.md](agent-tools-framework.md) for the full guide. The short
version:

1. Implement as a `@tool`-decorated function in `shared/tools.py`.
2. Return JSON always. Return names, not IDs. Return helpful errors, not exceptions.
3. Add a docstring with: what it returns, when to use it, args, and an example.
4. Add it to the appropriate tool tier set.
5. Update the system prompt for any agent that should use it.

### Adding a New Alert Type with a Runbook

Create a YAML file in the Gitea `runbooks` repository named `{AlertName}.yaml`:

```yaml
alertname: YourAlertName
description: What this alert means in plain English.
steps:
  - check: "show <relevant command>"
    expected: "<what healthy looks like>"
  - config: |
      <config lines to apply>
  - verify: "show <relevant command>"
    expected: "<what success looks like>"
expected_outcome: One sentence describing the desired end state.
rollback: |
  <config lines to undo the change>
risk: low | medium | high
automation_confidence: high | medium | low
```

The Remediator calls `get_runbook(alertname)` as its first action. Runbooks reduce LLM
token usage by 60–80% for known alert types and produce consistent, reviewable procedures.

### Adding a New Data Source (Tool Tier)

If you add a new observability backend (e.g., OpenTelemetry, Elastic), implement its
tools at the appropriate tier (Tier 2 for metrics, Tier 3 for logs) following the same
return format as existing tools. Agent prompts reference tiers conceptually, not specific
backends, so new tools in the right tier are picked up without prompt changes.

### Supporting a New Vendor

1. Implement the platform-specific config syntax in `run_config_commands` (Tier 5 write).
2. Ensure `run_show_commands` can parse the vendor's output format.
3. Add the platform to the agent system prompt's multi-vendor config section.
4. Test with a runbook for the most common alert type on that platform.

No changes to the pipeline, task store, or stage contracts are required when adding
vendor support.

---

## 9. Stack Assessment

This section gives an honest evaluation of the current implementation against the
framework above. The goal is to identify what to evolve versus what to replace.

### What the Current Stack Does Well

| Aspect | Verdict | Notes |
|---|---|---|
| Four-stage pipeline | **Strong** | RCA → Fix → Validation → Approval maps cleanly to the framework |
| Task store data model | **Strong** | Events, feedback, incidents, tenant_id already present |
| Tool tier model | **Strong** | Discovery → Metrics → Logs → Actions is textbook |
| Runbook-first approach | **Strong** | `get_runbook()` is the first tool call; Gitea-backed |
| Stage separation | **Strong** | Diagnosis, remediation, and validation are distinct agents |
| Approval gate | **Strong** | Config diff, risk, validation verdict all presented |
| Audit trail | **Strong** | Append-only event log with 20+ event types |
| Rate limiting & budget | **Good** | Per-agent hourly token cap + daily USD budget |
| Maintenance windows | **Good** | Nautobot-backed, blocks auto-execute |
| Auto-approval | **Good** | Track-record-based, foundation for graduated autonomy |
| Lab validation | **Good** | Optional pre-production test on Containerlab |
| SSE real-time UI | **Good** | Chronicle view is a good explainability surface |
| Webhook notifications | **Good** | HMAC-signed, approve/reject links |

### Where the Stack Has Gaps

| Gap | Severity | Description |
|---|---|---|
| No intent/policy layer | **High** | System is purely reactive. There is no standing policy like "all BGP peers must be up". Intent is implicit in Alertmanager rules, not declared in this system. |
| Binary autonomy model | **Medium** | Auto-approve is triggered by risk+confidence+execution count. There is no per-action-class autonomy level configuration. Hard to tune per environment. |
| No feedback loop to agent behavior | **Medium** | Feedback (TTR, verdict, resolved) is collected but does not update agent prompts, runbooks, or autonomy thresholds dynamically. Manual process. |
| Cross-domain correlation | **Medium** | Alert correlation is per-device within a 15-minute window. Cascading failures across devices with a shared root cause create separate incidents. |
| Polling latency | **Medium** | Background thread polling (15–120 s) is acceptable for a lab but limits time-to-diagnosis in production. RabbitMQ is already optional; making it default eliminates this. |
| Agent roles hardcoded | **Low** | Ops/Eng/Chaos split is in Python files. Adding a new specialized agent (e.g., a security agent) requires code changes, not configuration. |
| No proactive health checks | **Low** | The system responds to Prometheus alerts. It does not proactively query for degraded state that has not yet triggered an alert (e.g., prefix count dropping toward a threshold). |
| Single-tenant UI | **Low** | `tenant_id` exists in the task store schema but the UI does not filter by tenant. |

### Feasibility Verdict

**The current stack is the right foundation. A rewrite is not warranted.**

The core architecture — task store, pipeline stages, tool tier model, event log, and
approval gate — is sound and aligns with how Cisco, Juniper, and Aruba approach the
same problem. The gaps are in policy governance and proactive monitoring, which are
additive features, not architectural replacements.

A rewrite would discard several months of accumulated operational detail (runbooks,
the event schema, the Chronicle UI, maintenance window logic) without producing a
meaningfully better foundation.

The correct path is evolutionary: add the intent/policy layer on top of the reactive
pipeline, promote RabbitMQ from optional to default, and formalize autonomy levels
as configuration.

---

## 10. Recommended Roadmap

These are sequenced by impact-to-effort ratio, not by difficulty. Each item is
independent; they do not need to be done in order.

### Tier 1 — High Impact, Low Effort

**1.1 Formalize autonomy levels as configuration**

Add an `action_policies` table to the task store. The approval-gate runner reads it
before deciding whether to auto-approve or wait for a human. Start with three
action classes: `bgp_peer_reset`, `interface_restore`, `config_change_generic`.

Effort: ~1 day. Impact: Operators can tune autonomy per environment without code
changes.

**1.2 Make RabbitMQ the default dispatch path**

The RabbitMQ task bus already exists and is tested. Flip the default from polling to
message dispatch. Keep polling as the fallback for environments without RabbitMQ.

Effort: ~0.5 days. Impact: Eliminates 15–120 s polling latency; time-to-diagnosis
drops to near-real-time.

**1.3 Add rejection feedback to the runbook pipeline**

When a human rejects an approval gate, capture the rejection reason and write it as
a runbook annotation in Gitea. The next time the same alert type arrives, the
Remediator sees the annotation and avoids the same mistake.

Effort: ~1 day. Impact: System learns from human feedback without LLM retraining.

### Tier 2 — High Impact, Medium Effort

**2.1 Add a lightweight intent registry**

Define standing policies as YAML: `{metric: bgp_peers_established, threshold: < all,
device_role: leaf, severity: warning}`. A background poller evaluates each policy on
a configurable interval and creates incidents when a policy is violated, without waiting
for Alertmanager.

Effort: ~3 days. Impact: Proactive detection of degraded state that has not yet
triggered a Prometheus alert.

**2.2 Cross-device incident correlation**

When a new RCA is created, check for open incidents on topologically adjacent devices
(Tier 1 topology query). If a common upstream device is shared, link both RCAs to the
same incident. The Chronicle view would then show a multi-device incident narrative.

Effort: ~2 days. Impact: Alert storms during major failures are grouped into one
P1 incident rather than dozens of separate pipelines.

**2.3 Autonomy demotion on verification failure**

When `execution_verified` records `alert_resolved: false`, automatically lower the
autonomy level for that action class by one step and write an event to the audit trail.
Restore it after N subsequent successful executions.

Effort: ~1 day. Impact: System self-corrects when a fix type proves unreliable.

### Tier 3 — Medium Impact, Higher Effort

**3.1 Non-LLM deterministic validation rules**

Add a library of deterministic validation checks for known fix types (e.g., "for
`interface_restore`, verify the interface oper-state is `up` before creating an
approval gate"). These run before the validation agent and short-circuit to a
high-confidence verdict when they pass. The validation agent only runs when deterministic
rules cannot produce a verdict.

Effort: ~3 days. Impact: Faster, cheaper, more reliable validation for common cases.

**3.2 Proactive runbook gap detection**

After each LLM-generated fix (where no runbook existed), write the diagnosis + fix
as a candidate runbook to a `runbook-candidates` branch in Gitea. An operator can
review and promote to `main`. After promotion, the next occurrence uses the runbook
rather than the LLM.

Effort: ~2 days. Impact: Runbook library grows automatically from production incidents.

**3.3 Multi-tenant UI**

Filter the Pipeline Dashboard, Task Queue, and Incidents by `tenant_id`. The task store
already has the column; the gap is UI filtering and auth integration.

Effort: ~2 days. Impact: Multiple teams or environments can share one deployment.

---

## Appendix A: Competitive Landscape Summary

This framework draws on patterns from three products that represent the current
state of the art in autonomous network operations:

**Cisco Catalyst Center AI** — Intent-Based Networking translates business goals to
automated configurations. The AI Assistant brings AgenticOps to on-premises networks
with natural-language troubleshooting and closed-loop execution. Key pattern: multi-agent
orchestration across Meraki, Catalyst, SD-WAN, and ISE from a single intent surface.

**HPE Juniper Mist AI (Marvis)** — The "self-driving network" model. Domain-specific
agents (wired, wireless, WAN, client, app) collaborate under the Marvis AI engine.
The Large Experience Model (LEM) learns from billions of data points. The Marvis Actions
dashboard surfaces autonomous remediations with full IT oversight. Key pattern: graduated
autonomy levels modelled after automotive SAE levels.

**Aruba NetConductor** — Business-intent-to-config translation with closed-loop policy
enforcement. Dynamic Segmentation provides quarantine-on-compromise without human
intervention. Key pattern: policy as the primary input, with enforcement as the
automated output.

Common patterns extracted from all three:
- Intent abstraction above CLI
- Graduated autonomy (advisory → supervised → conditional → autonomous)
- Domain-specialized agents that collaborate
- Telemetry-first reasoning
- Human oversight at policy definition and exception review
- Feedback loops that improve the system over time

---

## Appendix B: Glossary

| Term | Definition |
|---|---|
| **Action class** | A category of network change defined by fix type and device role. Autonomy level is configured per action class. |
| **Approval gate** | A pipeline stage that requires a human decision (or an auto-approve policy) before execution. |
| **Autonomy level** | L0–L5 scale defining the degree of human involvement in execution for a given action class. |
| **Blast radius** | The set of devices and services that could be affected by a proposed change. |
| **Chronicle** | The human-readable incident narrative in the pipeline UI. One "chapter" per pipeline stage. |
| **Closed loop** | An automation process where the output of an action is observed and verified, and the result feeds back into future decisions. |
| **Config diff** | A unified diff (±) showing what configuration lines would change on a device after applying a fix. |
| **Intent** | A desired network state expressed as an outcome, not as CLI commands. |
| **Runbook** | A structured YAML document describing the canonical investigation and fix procedure for a known alert type. |
| **Sense** | The pipeline stage responsible for consuming telemetry, deduplicating events, and creating Incidents. |
| **Source of truth** | The authoritative inventory system (Nautobot, NetBox) for what devices exist and what their desired state is. |
| **TTR** | Time-to-Resolve — elapsed time from RCA task creation to confirmed alert resolution. |
| **Verification** | The pipeline stage that confirms the executed change produced the intended outcome. Closes the loop. |
