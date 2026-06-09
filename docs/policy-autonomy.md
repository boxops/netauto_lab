# Autonomy Policies and the Learning Engine

This document describes the **PolicyRegistry**, the **L0–L5 autonomy level system**, the **Programmatic Fast Path**, and the **LearningEngine** — the components that together determine how the pipeline handles each alert: whether it resolves instantly with deterministic logic, requires AI investigation, and how much human oversight is required before execution.

---

## Overview

Autonomy Policies serve two distinct roles in the pipeline:

| Role | When it runs | What it does |
| --- | --- | --- |
| **Programmatic fast path** | Before AI investigation | Runs live conditions (metrics, show commands, Nautobot) against the alert; if all pass, generates RCA + fix from templates — zero LLM calls |
| **Autonomy gate** | After RCA + fix proposal | Determines whether execution is auto-approved (L4+) or requires human sign-off (L0–L3) |

A policy can serve both roles simultaneously, or just the gate role (no conditions set). Policies without conditions behave exactly as before — they only affect the approval gate.

---

## The Autonomy Level Scale

| Level | Name | What happens |
| --- | --- | --- |
| **L0** | Manual | Telemetry and diagnosis only. The agent reports findings; humans take all action. |
| **L1** | Advisory | The agent surfaces a diagnosis and recommended fix. The human interprets and decides. |
| **L2** | Supervised | The agent stages a complete fix and waits at an approval gate. The human reviews and clicks Approve. **This is the safe default.** |
| **L3** | Human gate | The approval gate is always presented. Once approved, execution proceeds immediately. |
| **L4** | Auto-approve | The gate is auto-approved when policy thresholds are met (confidence, risk, prior success count). Human reviews outcomes, not individual changes. |
| **L5** | Autonomous | The agent executes, records the outcome, and notifies. Requires explicit operator configuration — never set automatically by the Learning Engine. |

The system-wide default for unmatched actions is **L2 — Supervised**.

---

## Programmatic Fast Path

`shared/policy_resolver.py` — `PolicyResolver`

### How it works

When an alert enters the pipeline the workflow's `policy_fast_path` node runs before any AI investigation:

1. `PolicyRegistry.get_fast_path_policies(alertname)` returns all enabled policies that have `conditions` set and match the alert name (or use an empty alertname wildcard).
2. For each candidate policy the `PolicyResolver.resolve(alert, policy)` method checks every condition in order against live data.
3. If **all conditions pass**, the resolver renders the `rca_template` and `fix_template` using context variables extracted from the alert (`{device}`, `{interface}`, `{alertname}`, `{severity}`, `{device_ip}`, `{instance}`). The result goes directly to the approval gate — no LLM calls, typically completing in 1–3 seconds.
4. If **any condition fails** (or raises an exception), the resolver returns `None` and the pipeline falls through to normal AI investigation transparently.

A `fast_path_resolved` event is recorded in `task_events` when the fast path fires, including the matched `policy_id` and the number of conditions checked.

### Condition types

Three condition types are supported:

#### `metric` — Prometheus instant query

```json
{
  "type": "metric",
  "query": "interface_ifAdminStatus{sysName=\"{device}\",ifDescr=\"{interface}\"}",
  "expect": "2"
}
```

Runs an instant PromQL query. The first scalar result value must equal `expect` (string comparison). Use `expect_ne` instead to assert the value does **not** equal a specific string — useful when the "bad" state is a known value (e.g. BGP peer state must not be `6` / Established):

```json
{
  "type": "metric",
  "query": "bgp_peer_bgpPeerState{sysName=\"{device}\"}",
  "expect_ne": "6"
}
```

`expect_ne` returns false when the metric value is empty (no data), so a missing metric never falsely triggers the fast path.

#### `show_command` — Device CLI output

```json
{
  "type": "show_command",
  "command": "show interfaces {interface} status",
  "expect_contains": "disabled"
}
```

Submits a read-only show command via the Nautobot Jobs API and checks that `expect_contains` appears in the output (case-insensitive).

#### `nautobot` — Nautobot API field check

```json
{
  "type": "nautobot",
  "path": "/api/dcim/interfaces/?name={interface}&device={device}",
  "field": "results[0].enabled",
  "expect": "false"
}
```

HTTP GETs a Nautobot API path and navigates to `field` using dot/bracket notation. The field value must equal `expect` (case-insensitive string comparison).

### Template variables

All condition values, RCA template fields, and fix template fields support these substitutions:

| Variable | Source |
| --- | --- |
| `{device}` | `sysName` label from the alert |
| `{interface}` | `ifDescr` label from the alert |
| `{alertname}` | `alertname` label |
| `{severity}` | `severity` label |
| `{instance}` | `instance` label |
| `{device_ip}` | Primary IP resolved from Nautobot (empty if lookup fails) |

Unknown variables are left as-is rather than raising an error.

### RCA template schema

```json
{
  "diagnosis":      "Description of what is wrong and why",
  "confidence":     "certain",
  "affected_device": "{device}",
  "upstream_cause": "",
  "is_leaf_symptom": false,
  "action":         "What to do to fix it"
}
```

### Fix template schema

```json
{
  "fix_type":   "config_change",
  "commands":   "interface {interface}\n no shutdown",
  "risk":       "low",
  "confidence": "certain",
  "reason":     "One sentence explaining why this fix is correct"
}
```

`fix_type` must be one of `config_change`, `runbook`, or `escalate_human`.

### Built-in fast-path policies

Two policies are seeded by default and include programmatic conditions from first deployment:

| Policy | Alert | Condition | Autonomy |
| --- | --- | --- | --- |
| InterfaceAdminDown fast path — any device | `InterfaceAdminDown` | `interface_ifAdminStatus == 2` | L3 |
| BGPPeerDown fast path — lab leaf | `BGPPeerDown` (role=leaf, env=lab) | `bgp_peer_bgpPeerState ≠ 6` | L4 |

These run on every matching alert with zero LLM calls. The BGP policy uses `expect_ne` because the relevant condition is "session is confirmed not-Established", which is the opposite of the good state.

### When to use the fast path

The fast path is appropriate when:
- The root cause is deterministic given a live condition check (e.g. admin-down interface)
- The fix is a known, low-risk procedure with no meaningful alternatives
- The condition provides sufficient signal that AI investigation would add no new information

**Use `expect_ne` rather than `expect` when** the "bad" state is a specific known value (e.g. BGP `bgpPeerState=Idle/Active`) but you do not want to hard-code all possible bad values — asserting "not Established" is simpler and more robust.

**Avoid fast paths for:** device-unreachable scenarios where physical vs. reachability is ambiguous, or any case where the cause varies enough that AI diagnosis provides real value.

---

## PolicyRegistry

`shared/policy_registry.py`

### Autonomy gate matching

When a fix proposal reaches the approval gate, the workflow calls `PolicyRegistry.query()` with:

| Parameter | Source |
| --- | --- |
| `fix_type` | Structured output from fix proposal (`config_change`, `runbook`, `escalate_human`) |
| `alertname` | Prometheus alert label |
| `device_role` | Nautobot device role for the target device |
| `environment` | Nautobot site/environment tag |
| `confidence` | Confidence level from fix proposal (`high`, `medium`, `low`) |
| `risk` | Risk assessment from fix proposal or validation (`low`, `medium`, `high`) |
| `prior_success_count` | Consecutive prior successful executions for this `(fix_type, device_role)` pair |

The registry scans all enabled policies and selects the **most specific matching policy** using a specificity score:

| Field matched | Score |
| --- | --- |
| `alertname` | +8 |
| `fix_type` | +4 |
| `device_role` | +2 |
| `environment` | +1 |

An empty value in any policy field acts as a wildcard. Hard filters — `min_confidence`, `max_risk`, `min_prior_successes` — must pass before a policy is considered. Ties broken by `created_at` (newer wins).

If no policy matches, the default `L2 — Supervised` decision is returned.

### Managing policies

Policies are managed in the **⚙️ Config** tab of the Clano UI at `http://localhost:7860/config`.

The **Add Policy** form has two sections:

1. **Core fields** — name, fix type, device role, environment, autonomy level. These control the approval gate.
2. **⚡ Programmatic Fast Path** (collapsed by default) — JSON textareas for `conditions`, `rca_template`, and `fix_template`. Expand to add programmatic resolution to the policy.

Policies with conditions set show a **⚡** badge in the Active Policies list. Click the **Edit** button on any policy row to update its autonomy level, confidence/risk thresholds, description, or fast-path JSON fields inline — without deleting and recreating the policy.

### Policy Simulator

The **Policy Simulator** in the Config tab lets you dry-run a hypothetical alert scenario before deploying. Enter alert name, device role, environment, fix type, confidence, and risk; the simulator returns:

- The **autonomy gate decision** the PolicyRegistry would reach (level, requires_approval, allow_execution, reason).
- All **programmatic fast-path candidates** that match the alert, with their condition list displayed (type, query, expected value) — conditions are listed but never executed.

This makes it safe to test policy changes without triggering a real pipeline run. The simulator calls only the database — it makes no HTTP calls to Prometheus or Nautobot.

---

## LearningEngine

`shared/learning_engine.py` — Called from `workflow._verify_resolution()` after every `execution_verified` event.

### Promotion

After each successful execution (`alert_resolved=True`), the LearningEngine counts the number of **consecutive** successful outcomes for the matched policy. If the count reaches the promotion threshold (default **3**), the policy is promoted one level:

```
L2 → L3 → L4  (maximum automatic promotion)
```

L4 is the automatic cap. Reaching L5 requires an operator to set it explicitly.

### Demotion

After any failed execution (`alert_resolved=False`), the policy is **immediately demoted** one level:

```
L4 → L3 → L2 → L1  (minimum automatic demotion)
```

### Audit trail

Every promotion and demotion is recorded as a `task_events` row and as a row in `policy_performance`, so the full history of how a policy's level changed is queryable. The **Policy Performance & Learning** section in the Config tab shows per-policy execution counts, success rate, and average TTR.

---

## Approval gate flow

1. Calls `PolicyRegistry.query()` with the fix context.
2. If `autonomy_level >= L4` and thresholds met:
   - Records an `auto_approved` event with `autonomy_level` and `policy_id`.
   - Proceeds directly to execution.
3. If `autonomy_level <= L3`:
   - Sets the task to `awaiting_approval`.
   - Fires approval webhook (if `APPROVAL_WEBHOOK_URL` is set).
   - Waits for a human to click Approve or Reject in the Pipeline tab.
   - On Approve: records `approval_policy` event, then executes.
   - On Reject: records rejection; LearningEngine may demote the policy.

A **Standing Intent** of type `escalate` forces L2 behaviour regardless of the matched policy level — use this to protect devices during risky change windows without modifying policies.

---

## `action_policies` table schema

```sql
CREATE TABLE action_policies (
    id                   TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL DEFAULT 'default',
    name                 TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    alertname            TEXT NOT NULL DEFAULT '',   -- empty = any
    fix_type             TEXT NOT NULL DEFAULT '',   -- empty = any
    device_role          TEXT NOT NULL DEFAULT '',   -- empty = any
    environment          TEXT NOT NULL DEFAULT '',   -- empty = any
    min_confidence       TEXT NOT NULL DEFAULT 'low',
    max_risk             TEXT NOT NULL DEFAULT 'high',
    min_prior_successes  INTEGER NOT NULL DEFAULT 0,
    autonomy_level       TEXT NOT NULL DEFAULT 'L2',
    enabled              INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    -- Fast-path fields (nullable — backward compatible)
    conditions           TEXT,   -- JSON array of condition objects
    rca_template         TEXT,   -- JSON object
    fix_template         TEXT    -- JSON object
);
```

### Adding a policy with a fast path via SQL

```sql
INSERT INTO action_policies (
  id, tenant_id, name,
  alertname, fix_type, device_role, environment,
  min_confidence, max_risk, min_prior_successes,
  autonomy_level, enabled,
  created_at, updated_at,
  conditions, rca_template, fix_template
) VALUES (
  'pol-' || substr(md5(random()::text), 1, 8),
  'default',
  'InterfaceDown — lab spine → L3 (fast-path)',
  'InterfaceDown', 'config_change', 'spine', 'lab',
  'high', 'medium', 0,
  'L3', 1,
  NOW(), NOW(),
  '[{"type":"metric","query":"interface_ifAdminStatus{sysName=\"{device}\",ifDescr=\"{interface}\"}","expect":"2"}]',
  '{"diagnosis":"{interface} on {device} is administratively shut down","confidence":"certain","affected_device":"{device}","upstream_cause":""}',
  '{"fix_type":"config_change","commands":"interface {interface}\n no shutdown","risk":"low","confidence":"certain","reason":"Restore admin-down interface"}'
);
```

### Disabling or promoting a policy

```sql
-- Disable a policy without deleting it
UPDATE action_policies SET enabled = 0 WHERE name = 'InterfaceDown — lab leaf → L4 (fast-path)';

-- Manually promote a policy one level
UPDATE action_policies SET autonomy_level = 'L4'
WHERE id = 'pol-abc123' AND autonomy_level = 'L3';

-- Remove fast-path conditions from a policy (revert to AI-only gate)
UPDATE action_policies SET conditions = NULL, rca_template = NULL, fix_template = NULL
WHERE id = 'pol-abc123';
```
