# Closed-Loop Pipeline

When Prometheus fires an alert, the unified agent runs a LangGraph state machine that investigates, proposes, validates, and gates a remediation — all tracked on one `rca` task linked by `alert_fingerprint`. The autonomy policy determines whether execution is human-gated or auto-approved.

```
Alert arrives (webhook or poller)
         │
         ▼
  AlertPoller — dedup by fingerprint · maintenance check · topology correlation
  · creates or links an Incident grouping entity · creates rca task
         │
         ▼
  ⚡ Policy Fast Path (zero LLM)
  Checks programmatic conditions (metric / show_command / nautobot).
  If ALL pass → generates RCA + fix from templates → Approval Gate.
  If any fail → AI investigation below.
         │
         ├─ AI path (AI_ENABLED=true)
         │
  Stage 1 — RCA: alerts + metrics + logs → structured diagnosis
         │
  Stage 2 — Fix Proposal: get_runbook() first → commands + config diff
         │       risk=high or escalate_human → skip to Approval Gate
         │
  Stage 3 — Validation: blast-radius + topology + read-only inspection
         │
         ▼
  Stage 4 — Approval Gate
  PolicyRegistry → L0–L3: human reviews → Approve/Reject
                → L4: auto-approved when thresholds met
                → L5: executes and notifies (explicit operator config only)
  On approval: execute (check_mode=False) → config verify → alert resolution check
```

---

## Task Lifecycle

| Type | Purpose |
|---|---|
| `incident` | Groups correlated alerts from the same device (P1–P4 priority) |
| `rca` | Root cause analysis |
| `fix_proposal` | Remediation commands + config diff (check_mode) |
| `validation` | Blast-radius and correctness check |
| `approval_gate` | Human sign-off before live execution |

**Status flow:** `pending → claimed → running → complete` / `awaiting_approval` / `failed` (auto-retried up to 2×) / `rejected`

Alert severity drives priority: `critical` → `high` (polled every 15 s); `warning` → `normal` (every 60 s). Maintenance-window devices always get `priority=low` + `do_not_auto_execute=true`.

---

## Autonomy Levels

| Level | Name | Behaviour |
|---|---|---|
| **L0** | Manual | Telemetry and diagnosis only; humans take all action |
| **L1** | Advisory | Agent surfaces diagnosis; human decides |
| **L2** | Supervised | Agent stages fix and waits at gate (default) |
| **L3** | Human gate | Gate always presented; executes immediately on click |
| **L4** | Auto-approve | Gate auto-approved when confidence/risk/prior-success thresholds met |
| **L5** | Autonomous | Executes and notifies; requires explicit operator configuration — never set by the Learning Engine |

Autonomy is **per action class** (`fix_type` × `alertname` × `device_role` × `environment`), not system-wide. The system-wide default for unmatched actions is **L2 — Supervised**.

---

## Programmatic Fast Path

`shared/policy_resolver.py` resolves known alert patterns with **zero LLM calls** in 1–3 seconds.

**Three condition types:**

```json
// metric — Prometheus instant query
{"type": "metric", "query": "interface_ifAdminStatus{sysName=\"{device}\",ifDescr=\"{interface}\"}", "expect": "2"}

// expect_ne — assert value is NOT equal (useful for BGP state checks)
{"type": "metric", "query": "bgp_peer_bgpPeerState{sysName=\"{device}\"}", "expect_ne": "6"}

// show_command — CLI output substring
{"type": "show_command", "command": "show interfaces {interface} status", "expect_contains": "disabled"}

// nautobot — API field value
{"type": "nautobot", "path": "/api/dcim/interfaces/?name={interface}&device={device}", "field": "results[0].enabled", "expect": "false"}
```

**Template variables:** `{device}`, `{interface}`, `{alertname}`, `{severity}`, `{instance}`, `{device_ip}`

**RCA template schema:** `{"diagnosis": "...", "confidence": "certain", "affected_device": "{device}", "upstream_cause": "", "is_leaf_symptom": false, "action": "..."}`

**Fix template schema:** `{"fix_type": "config_change|runbook|escalate_human", "commands": "...", "risk": "low", "confidence": "certain", "reason": "..."}`

**Built-in fast-path policies:**

| Policy | Alert | Condition | Level |
|---|---|---|---|
| InterfaceAdminDown fast path | `InterfaceAdminDown` | `ifAdminStatus == 2` | L3 |
| BGPPeerDown fast path (lab leaf) | `BGPPeerDown` (role=leaf, env=lab) | `bgpPeerState ≠ 6` | L4 |

**Use the fast path when** the root cause is deterministic given a live condition check, the fix is a known low-risk procedure, and AI investigation adds no value. **Avoid** for device-unreachable scenarios or cases where the cause varies.

---

## PolicyRegistry

`shared/policy_registry.py` — called at the Approval Gate with `fix_type`, `alertname`, `device_role`, `environment`, `confidence`, `risk`, and `prior_success_count`.

**Specificity scoring** (most specific matching policy wins):

| Field matched | Score |
|---|---|
| `alertname` | +8 |
| `fix_type` | +4 |
| `device_role` | +2 |
| `environment` | +1 |

Empty value in any field = wildcard. Hard filters (`min_confidence`, `max_risk`, `min_prior_successes`) must pass before a policy is considered.

**Policy Simulator** in the Config tab lets you dry-run a hypothetical alert scenario — returns the autonomy gate decision and fast-path candidates without making any API calls.

---

## LearningEngine

`shared/learning_engine.py` — runs after every `execution_verified` event.

- **Promotion:** After `N` consecutive successes (`alert_resolved=True`), the policy is promoted one level. Default `N=3`. Cap: **L4** (L5 requires explicit operator action).
- **Demotion:** After any failure (`alert_resolved=False`), the policy is immediately demoted one level. Minimum: L1.

All promotions and demotions are recorded in `task_events` and `policy_performance`.

---

## Approval Gate

The gate presents everything needed to make an informed decision: device, exact commands, unified config diff, fix type, confirmed risk level, validation verdict, and full RCA context.

**Approval webhook** (`APPROVAL_WEBHOOK_URL`): fires when a task enters `awaiting_approval`. Payload includes `task_id`, `device`, `commands`, `risk`, `approve_url`, `reject_url`. HMAC-SHA256 signed when `APPROVAL_WEBHOOK_SECRET` is set.

**Lab validation** (`LAB_VALIDATION_ENABLED=true`): applies the fix to `clab-{device}` first; aborts production execution if the lab alert does not clear within `LAB_VERIFY_DELAY` seconds.

**Post-execution verification:**
1. Immediate (non-LLM): `run_show_commands("show running-config")` → checks each applied config line is present → records `config_applied`, `found_lines`, `missing_lines`.
2. Background (after `EXECUTION_VERIFY_DELAY` seconds): queries Prometheus for alert resolution → records `alert_resolved`, `ttr_seconds`.

---

## Data Model

### `tasks` (key columns)

| Column | Description |
|---|---|
| `id` | Short unique ID with type prefix (e.g. `rca-a1b2c3d4`) |
| `parent_id` | Links fix → rca, validation → fix, gate → validation |
| `incident_id` | Parent incident grouping |
| `alert_fingerprint` | Links the entire pipeline chain |
| `type` | `incident` / `rca` / `fix_proposal` / `validation` / `approval_gate` |
| `status` | See lifecycle above |
| `do_not_auto_execute` | 1 = suppress automated execution (maintenance window, escalate intent) |

### `task_events` (key event types)

| Event | Stage | Key detail fields |
|---|---|---|
| `fast_path_resolved` | rca | `policy_id`, `conditions_matched` |
| `no_ai_skipped` | rca | `reason` — AI disabled, queued for human review |
| `auto_approved` | approval_gate | `autonomy_level`, `policy_id` |
| `execution_complete` | approval_gate | `config_applied`, `found_lines`, `missing_lines` |
| `execution_verified` | approval_gate | `alert_resolved`, `ttr_seconds` |
| `alert_correlated` | rca | `alertname`, `fingerprint` |

### Other tables

| Table | Purpose |
|---|---|
| `action_policies` | L0–L5 policies with match criteria + optional fast-path JSON |
| `policy_performance` | Execution outcomes per policy — feeds LearningEngine |
| `standing_intents` | suppress / escalate / monitor / chaos_schedule |
| `task_feedback` | Validation verdicts for KPI accuracy tracking |
| `token_usage` | Token counts + cost per agent session |

---

## Configuration Reference

### Core

| Variable | Default | Description |
|---|---|---|
| `AI_ENABLED` | `true` | `false` = only fast-path policies run |
| `DAILY_BUDGET_USD` | `5.00` | Hard daily spend limit |
| `MAX_TOKENS_PER_AGENT_PER_HOUR` | `2,000,000` | Hourly token cap |

### Task store backend

| Variable | Default | Description |
|---|---|---|
| `TASK_DB_URL` | (empty = SQLite) | PostgreSQL URL for production. Example: `postgresql+psycopg2://agent:pw@agent-postgres:5432/agent_tasks` |
| `AGENT_DB_PASSWORD` | `netauto_agent_default` | Password for `agent-postgres` container |

### RabbitMQ (optional)

`RABBITMQ_URL` (empty = polling): when set, `fix_proposal` and `validation` tasks are dispatched immediately rather than waiting for the next poll tick. Example: `amqp://netauto:password@rabbitmq:5672/`

### Approval webhook

| Variable | Default | Description |
|---|---|---|
| `APPROVAL_WEBHOOK_URL` | — | POST target when task enters `awaiting_approval` |
| `APPROVAL_WEBHOOK_SECRET` | — | HMAC-SHA256 signing secret |
| `AGENT_UI_URL` | `http://localhost:7860` | Used to construct approve/reject links |

### Maintenance window

| Variable | Default | Description |
|---|---|---|
| `MAINTENANCE_CHECK_ENABLED` | `false` | Query Nautobot before creating RCA tasks |
| `MAINTENANCE_STATUSES` | `planned,staged,decommissioning` | Nautobot device status slugs |
| `MAINTENANCE_TAG` | `maintenance` | Nautobot tag |

### Lab validation

| Variable | Default | Description |
|---|---|---|
| `LAB_VALIDATION_ENABLED` | `false` | Apply fix to Containerlab device before production |
| `LAB_DEVICE_PREFIX` | `clab-` | Maps `leaf1` → `clab-leaf1` |
| `LAB_VERIFY_DELAY` | `30` | Seconds to wait for lab alert to clear |

### Runbook library

| Variable | Default | Description |
|---|---|---|
| `GITEA_TOKEN` | — | API token for Gitea runbooks repo |
| `GITEA_RUNBOOK_OWNER` | `netauto` | Gitea org or username |
| `GITEA_RUNBOOK_REPO` | `runbooks` | Repo containing `{AlertName}.yaml` files |
| `EXECUTION_VERIFY_DELAY` | `300` | Seconds after execution before Prometheus alert check |

---

## Deduplication and Resilience

| Mechanism | Description |
|---|---|
| Fingerprint dedup | `_seen` dict seeded from TaskStore on startup; survives restarts |
| Prometheus validation | Every candidate alert checked against live `/api/v1/alerts` before acting |
| Alert correlation | Same device + 15-min window → append note rather than spawn parallel pipeline |
| Task claim atomicity | `UPDATE … WHERE status='pending'` rowcount check prevents double-processing |
| Pipeline retry | Failed tasks auto-retry up to 2× after 120 s |
| Budget guard | Token budget checked before claiming any task; stays `pending` if exceeded |

---

## Adding a Runbook

Create `{AlertName}.yaml` in the Gitea `runbooks` repository (`netauto/runbooks`):

```yaml
alertname: MyAlert
description: What this alert means
steps:
  - check: "show interface {interface} status"
    expected: "connected"
  - config: |
      interface {interface}
        no shutdown
  - verify: "show interface {interface} status"
    expected: "connected"
expected_outcome: Interface returns to connected state
rollback: |
  interface {interface}
    shutdown
risk: low
automation_confidence: high
```

## Migrating to PostgreSQL

```bash
# Already defined in docker-compose.yml — just set the URL and restart
python3 scripts/migrate_sqlite_to_postgres.py \
  --sqlite /path/to/activity.db \
  --postgres postgresql+psycopg2://agent:password@agent-postgres:5432/agent_tasks

# Then in .env:
TASK_DB_URL=postgresql+psycopg2://agent:password@agent-postgres:5432/agent_tasks
make restart
```
