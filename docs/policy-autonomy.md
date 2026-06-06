# Autonomy Policies and the Learning Engine

This document describes the **PolicyRegistry**, the **L0–L5 autonomy level system**, and the **LearningEngine** — the three components that together determine how much independence the AI agent has for each type of network fix, and how that independence changes over time based on outcomes.

---

## The Autonomy Level Scale

Inspired by SAE automation levels for vehicles, the system uses six levels to express how much human oversight is required for a given action class:

| Level | Name | What happens |
| --- | --- | --- |
| **L0** | Manual | Telemetry and diagnosis only. The agent reports findings; humans take all action. |
| **L1** | Advisory | The agent surfaces a diagnosis and recommended fix. The human interprets and decides. |
| **L2** | Supervised | The agent stages a complete fix (config diff, exact commands) and waits at an approval gate. The human reviews and clicks Approve. **This is the safe default.** |
| **L3** | Human gate | The approval gate is always presented. Once approved, execution proceeds immediately without additional confirmation. |
| **L4** | Auto-approve | The gate is auto-approved when policy thresholds are met (confidence, risk, and prior success count). Human reviews outcomes, not individual changes. |
| **L5** | Autonomous | The agent executes, records the outcome, and notifies. Requires explicit operator configuration — never set automatically by the Learning Engine. |

The system-wide default for unmatched actions is **L2 — Supervised**. L5 must be set manually and is intended for well-understood, fully reversible actions in test environments only.

---

## PolicyRegistry

`shared/policy_registry.py` — Called at the approval gate stage of every pipeline run.

### How matching works

When a fix proposal reaches the approval gate, the workflow calls `PolicyRegistry.query()` with:

| Parameter | Source |
| --- | --- |
| `fix_type` | Structured output from fix proposal (`config_change`, `runbook`, `escalate_human`) |
| `alertname` | Prometheus alert label |
| `device_role` | Nautobot device role for the target device |
| `environment` | Nautobot site/environment tag for the target device |
| `confidence` | Confidence level from fix proposal (`high`, `medium`, `low`) |
| `risk` | Risk assessment from fix proposal or validation (`low`, `medium`, `high`) |
| `prior_success_count` | Number of prior successful executions for this `(fix_type, device_role)` combination |

The registry scans all enabled policies and selects the **most specific matching policy** using a specificity score:

| Field matched | Score |
| --- | --- |
| `alertname` | +8 |
| `fix_type` | +4 |
| `device_role` | +2 |
| `environment` | +1 |

An empty value in any policy field acts as a wildcard (matches any value). Hard filters — `min_confidence`, `max_risk`, `min_prior_successes` — must pass before a policy is considered. Ties are broken by `created_at` (newer policy wins).

If no policy matches, the default `L2 — Supervised` decision is returned.

### Default seed policies

Seven policies are loaded on first startup (if the `action_policies` table is empty):

| Policy | Alertname | Fix type | Device role | Environment | Min confidence | Max risk | Min successes | Level |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BGP peer reset — lab leaf | BGPPeerDown | runbook | leaf | lab | high | low | 2 | **L4** |
| BGP peer reset — lab spine | BGPPeerDown | runbook | spine | lab | high | medium | 0 | **L3** |
| BGP peer reset — production | BGPPeerDown | runbook | (any) | production | low | high | 0 | **L2** |
| Interface restore — lab leaf | InterfaceDown | config_change | leaf | lab | high | low | 2 | **L4** |
| Interface restore — production | InterfaceDown | config_change | (any) | production | high | medium | 0 | **L3** |
| Generic config change | (any) | config_change | (any) | (any) | low | high | 0 | **L2** |
| Escalate to human | (any) | escalate_human | (any) | (any) | low | high | 0 | **L1** |

### Managing policies

Policies are managed in the **⚙️ Config → Autonomy Policies** section of the Clano UI at [http://localhost:7860/config](http://localhost:7860/config). You can:

- View all active policies with their current level and match criteria.
- Add a new policy with the inline form (name, fix type, device role, environment, autonomy level).
- View policy performance history in the **Policy Performance & Learning** section.

Policies can also be managed directly in the `action_policies` database table.

---

## LearningEngine

`shared/learning_engine.py` — Called from `workflow._verify_resolution()` after every `execution_verified` event.

### Promotion

After each successful execution (`alert_resolved=True`), the LearningEngine counts the number of **consecutive** successful outcomes for the matched policy. If the count reaches the promotion threshold (default **3**), the policy is promoted one level:

```
L2 → L3 → L4  (maximum automatic promotion)
```

L4 is the automatic promotion cap. Reaching L5 requires an operator to explicitly set it via the Config UI.

### Demotion

After any failed execution (`alert_resolved=False`), the matched policy is **immediately demoted** one level regardless of prior successes:

```
L4 → L3 → L2 → L1  (minimum automatic demotion)
```

Demotion never goes below L1.

### Audit trail

Every promotion and demotion is recorded as a task event (`task_events` table) and as a row in `policy_performance`, so the full history of how a policy's level changed over time is queryable.

### Policy Performance panel

The **Policy Performance & Learning** section in the Config tab shows, per policy:
- Total executions and success rate (last 30 days)
- Average TTR (time-to-resolution)
- Trend — whether the policy has been promoted or demoted recently

---

## Approval gate flow

When the pipeline reaches the approval gate, the workflow:

1. Calls `PolicyRegistry.query()` with the fix context.
2. If `autonomy_level >= L4` and `allow_execution=True`:
   - Records an `auto_approved` event with `autonomy_level` and `policy_id`.
   - Proceeds directly to execution.
3. If `autonomy_level <= L3` and `requires_approval=True`:
   - Sets the task to `awaiting_approval`.
   - Sends an approval webhook notification (if configured).
   - Waits for a human to click Approve or Reject in the **Operations** tab Chronicle.
   - On Approve: records an `approval_policy` event with `autonomy_level` and `policy_id`, then executes.
   - On Reject: records the rejection reason; LearningEngine may demote the policy.

### Overrides

A **Standing intent** of type `escalate` forces L2 behaviour (human gate always required) regardless of the matched policy level. This is the safest way to protect production devices during risky change windows without permanently modifying policies.

---

## Adding or modifying policies via the database

```sql
-- Add a new policy (PostgreSQL example)
INSERT INTO action_policies (
  id, tenant_id, name,
  alertname, fix_type, device_role, environment,
  min_confidence, max_risk, min_prior_successes,
  autonomy_level, enabled, created_at
) VALUES (
  'pol-' || gen_random_uuid(),
  'default',
  'High CPU — lab leaf → L4',
  'HighCPUUtilization', 'runbook', 'leaf', 'lab',
  'high', 'low', 3,
  'L4', 1, NOW()
);

-- Disable a policy without deleting it
UPDATE action_policies SET enabled = 0 WHERE name = 'BGP peer reset — lab leaf → L4';

-- Manually promote a policy
UPDATE action_policies SET autonomy_level = 'L4' WHERE id = 'pol-abc123';
```
