# Closed-Loop Automation Pipeline

The closed-loop pipeline is the autonomous incident-response system built into the AI agent stack. When Prometheus fires an alert, the pipeline coordinates the three agents — Ops, Engineering, and Chaos — to investigate, propose, validate, and gate a remediation, with a mandatory human approval step before any configuration change is executed on the network.

---

## Overview

```
  Prometheus alert fires
         │
         ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  AlertPoller  (ops_agent)                                            │
  │  · Critical alerts polled every 15 s, normal alerts every 60 s      │
  │  · Validates alert still firing in live Prometheus                   │
  │  · Checks device maintenance status (if enabled)                    │
  │  · Correlates with existing open RCAs for the same device           │
  │  · Creates or links an Incident grouping entity                     │
  └──────────────────────────┬───────────────────────────────────────────┘
                             │ creates RCA task
                             ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Stage 1 — RCA  (ops_agent)                                          │
  │  Correlates alerts, metrics, and syslogs into a root cause summary  │
  └──────────────────────────┬───────────────────────────────────────────┘
                             │ creates fix_proposal task
                             ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Stage 2 — Fix Proposal  (eng_agent)                                 │
  │  Checks runbook library first, then generates specific remediation   │
  │  Produces a config diff showing before/after                         │
  └────────┬──────────────────────────────────────────────┬──────────────┘
           │ risk = low / medium                          │ risk = high
           │ creates validation task                      │ or FIX_TYPE = escalate_human
           ▼                                              │
  ┌─────────────────────────────────┐                    │
  │  Stage 3 — Validation           │                    │
  │  (chaos_agent)                  │                    │
  │  Blast-radius check, topology   │                    │
  │  analysis, read-only device     │                    │
  │  inspection                     │                    │
  └────────┬────────────────────────┘                    │
           │ verdict = correct / partial                  │
           │ creates approval_gate task                   │
           ▼                                              ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Stage 4 — Approval Gate  (human)                                    │
  │  · Human reviews commands, config diff, and validation verdict       │
  │  · Optional: approval webhook fires for out-of-band notification     │
  │  · Optional: fix applied to lab device first (LAB_VALIDATION_ENABLED)│
  │  · Execution with check_mode=False on approval                       │
  │  · Post-execution: config applied on device verified (non-LLM)      │
  │  · Post-execution: Prometheus checked for alert resolution           │
  └──────────────────────────────────────────────────────────────────────┘
```

All stages are tracked in the shared **TaskStore** (SQLite by default, PostgreSQL optional), visible on the **Pipeline Dashboard** at [http://localhost:7860](http://localhost:7860), and linked by a common `alert_fingerprint` so the full chain is traceable. Correlated alerts from the same device are grouped under an **Incident** entity.

---

## Task Types and Lifecycle

### Task types

| Type            | Owner         | Created by                   | Purpose                                                     |
| --------------- | ------------- | ---------------------------- | ----------------------------------------------------------- |
| `incident`      | `system`      | `system` (AlertPoller)       | Groups correlated alerts from the same device into one P1–P4 incident |
| `rca`           | `ops_agent`   | `system` (AlertPoller)       | Root cause analysis of a firing alert                       |
| `fix_proposal`  | `eng_agent`   | `ops_agent`                  | Specific remediation commands in check mode, with config diff |
| `validation`    | `chaos_agent` | `eng_agent`                  | Blast-radius and correctness check of the fix               |
| `approval_gate` | `human`       | `eng_agent` or `chaos_agent` | Human sign-off required before live execution               |

### Status lifecycle

```
pending ──► claimed ──► running ──► complete
                                 ├─► awaiting_approval  (approval_gate tasks only)
                                 ├─► failed             (auto-retried up to 2×)
                                 └─► rejected
```

`awaiting_approval` persists until a human acts (Approve or Reject) in the UI or via the approval webhook.

### Priority mapping

Alert severity drives task priority and flows through the full chain:

| Prometheus severity | Task priority | Critical poll interval |
| ------------------- | ------------- | ---------------------- |
| `critical`          | `high`        | 15 s                   |
| `warning`           | `normal`      | 60 s                   |

Devices in a maintenance window always receive `priority=low` regardless of severity.

---

## AlertPoller Behaviour

The AlertPoller runs in a background thread inside the ops_agent container. Key behaviours:

### Priority-aware polling

Two nested poll loops run simultaneously:
- **Tight loop** (every 15 s): picks up `critical` and `high` priority tasks only
- **Normal loop** (every 60 s, every 4th tick of the tight loop): processes all severities

### Maintenance window check

If `MAINTENANCE_CHECK_ENABLED=true`, the poller calls Nautobot before creating an RCA task:
1. Checks the device's status field (configurable via `MAINTENANCE_STATUSES`)
2. Checks for a `maintenance` tag (configurable via `MAINTENANCE_TAG`)

If the device is in maintenance, the pipeline still runs but with `priority=low` and `do_not_auto_execute=true`. Automated execution is suppressed at the approval gate; a human can still approve and trigger execution manually.

### Alert correlation

Before creating a new RCA task, the poller checks for an active RCA for the same device created within the last 15 minutes. If one exists, the new alert is recorded as a `alert_correlated` event on the existing task rather than spawning a parallel investigation. This prevents duplicate work during alert storms.

### Incident grouping

Every new alert creates or links to an **Incident** task:
- If an open Incident for the same device exists within 30 minutes, the new RCA task is linked to it
- Otherwise a new Incident is created (P1 for critical, P2 for warning, P3 for info)

The Incident tracks the list of affected devices across correlated alerts and can be resolved via the **🚨 Incidents** tab.

### Pipeline retry

If an RCA, fix_proposal, or validation task fails, the runner schedules an automatic retry after 120 seconds. Tasks are retried at most **twice** (`retry_count` column). Failed tasks whose `retry_count` has reached 2 remain in `failed` state and require manual intervention.

---

## Stage 1 — Root Cause Analysis (Ops Agent)

**Triggered by:** AlertPoller detecting a new firing alert
**Task type:** `rca`
**Poll interval:** 15 s (critical), 60 s (normal)

### What the AlertPoller does

1. Fetches events from the alert-event-receiver service (`GET /events`)
2. Deduplicates by fingerprint — most recent event per fingerprint wins
3. Validates each firing event against live Prometheus (`GET /api/v1/alerts`)
4. Checks device maintenance status (if `MAINTENANCE_CHECK_ENABLED=true`)
5. Checks for correlated RCA on same device (last 15 min)
6. Creates or links to an Incident grouping entity
7. Creates an `rca` task and immediately runs the investigation

### What the Ops Agent investigates

```
1. get_active_alerts()               → confirm what is currently firing
2. get_device_metrics(device)        → reachability, interface oper state, RTT
3. get_interface_events(device)      → link up/down syslog events
4. get_bgp_events(device)            → BGP session state changes
5. get_topology()                    → blast-radius context (if relevant)
```

### Structured output

```
DIAGNOSIS:  <one-sentence root cause>
AFFECTED:   <device hostname or "unknown">
ACTION:     <recommended next step>
CONFIDENCE: high | medium | low
```

### Escalation decision

If `ACTION` contains any of `no action`, `no fix`, `already resolved`, `self-healed`, or `monitor only`, the pipeline stops. Otherwise a `fix_proposal` task is created for the Engineering Agent.

---

## Stage 2 — Fix Proposal (Engineering Agent)

**Triggered by:** RCA task completing with an actionable recommendation
**Task type:** `fix_proposal`
**Poll interval:** 15 s (critical/high), 90 s (normal)

### What the Engineering Agent does

```
0. get_runbook(alertname)                              → check runbook library FIRST
1. get_device_info(device)                             → confirm platform and current status
2. get_device_interfaces(device)                       → check interface state
3. run_show_commands(device, cmds)                     → read current config if needed
4. run_config_commands(device, lines, check_mode=True) → simulate the fix
```

**Runbook-first approach:** The agent is prompted to call `get_runbook(alertname)` before any other tool. If a matching runbook exists, the agent follows its prescribed steps rather than re-deriving the fix from first principles. This reduces token usage by 60–80% for known alert types and produces consistent, tested procedures.

Built-in runbooks are available for: `BGPPeerDown`, `InterfaceDown`, `InterfaceAdminDown`, `DeviceDown`, `HighInterfaceUtilization`, `InterfaceHighErrorRate`, `BGPPrefixCountDecreased`. Custom runbooks can be added to the Gitea `runbooks` repository as YAML files.

### Config diff

After generating the fix, the runner makes a direct (non-LLM) call to fetch the relevant section of the device's running config and computes a unified diff showing what will change. This diff is stored in the approval gate content and rendered in the task detail UI for human reviewers.

### Structured output

```
FIX_TYPE:   config_change | runbook | no_action | escalate_human
DEVICE:     <exact device hostname>
COMMANDS:   <config lines to apply, or "none">
RISK:       low | medium | high
CONFIDENCE: high | medium | low
REASON:     <one sentence explaining the fix>
```

`COMMANDS` may be provided inline or in a fenced code block — the parser handles both formats.

### Confidence-based auto-approval

If all three conditions are met, the approval gate is **auto-approved** without human intervention:
- `RISK = low`
- `CONFIDENCE = high`
- The same `(device, fix_type)` combination has been successfully executed at least **2 previous times** without a follow-up alert

Auto-approved gates show `assigned_to = system` and an `auto_approved` event in the timeline.

### Routing decision

| Condition                                                 | Next step                                            |
| --------------------------------------------------------- | ---------------------------------------------------- |
| `FIX_TYPE = no_action`                                    | Pipeline ends                                        |
| `RISK = high` or `FIX_TYPE = escalate_human`              | Approval gate created immediately (skips validation) |
| `RISK = low` or `medium` and `FIX_TYPE ≠ escalate_human`  | Validation task created for Chaos Agent              |
| `do_not_auto_execute = true` (maintenance window)         | Auto-approval blocked; human gate always created     |

---

## Stage 3 — Validation (Chaos Agent)

**Triggered by:** Fix proposal completing with low or medium risk
**Task type:** `validation`
**Poll interval:** 15 s (critical/high), 120 s (normal)

### What the Chaos Agent checks

```
1. get_topology()                    → blast radius: which other devices depend on this?
2. get_device_metrics(device)        → confirm current device state
3. get_connected_devices(device)     → direct neighbors that could be affected
4. get_active_alerts()               → is the original alert still firing?
5. run_show_commands(device, ...)    → read-only config inspection (no changes applied)
```

### Structured output

```
VERDICT:        correct | incorrect | partial | unverifiable
CONFIDENCE:     high | medium | low
RISK_CONFIRMED: low | medium | high
NOTES:          <one sentence summarising the validation finding>
```

### Feedback propagation

After completing, the Chaos Agent writes structured feedback to the parent `fix_proposal` task and the grandparent `rca` task for long-term accuracy tracking in the KPI dashboard.

### Routing decision

| Verdict                       | Next step                                      |
| ----------------------------- | ---------------------------------------------- |
| `correct` or `partial`        | Approval gate created — human review required  |
| `incorrect` or `unverifiable` | Pipeline ends — no approval gate created       |

---

## Stage 4 — Approval Gate (Human)

**Triggered by:** Engineering Agent (high-risk) or Chaos Agent (validated low/medium-risk fix)
**Task type:** `approval_gate`
**Status on creation:** `awaiting_approval`

### Approval gate content

The approval gate contains everything a human needs to make an informed decision:

| Field                | Description                                            |
| -------------------- | ------------------------------------------------------ |
| `device`             | Target device hostname                                 |
| `commands`           | Exact configuration lines to be applied                |
| `config_diff`        | Unified diff of current running-config vs proposed     |
| `fix_type`           | Classification of the change                           |
| `risk_confirmed`     | Risk level as assessed by the Chaos Agent              |
| `validation_verdict` | Chaos Agent verdict (`correct` / `partial` / etc.)     |
| `chaos_notes`        | One-sentence Chaos Agent validation summary            |
| `rca`                | Full RCA context from the Ops Agent                    |

### Approval webhook

If `APPROVAL_WEBHOOK_URL` is set, the UI server fires a POST to that URL when a task first enters `awaiting_approval`. The payload includes `task_id`, `device`, `commands`, `risk`, `approve_url`, and `reject_url`. The webhook body is HMAC-SHA256 signed if `APPROVAL_WEBHOOK_SECRET` is set. Point it at Slack, PagerDuty, or OpsGenie for instant out-of-band notification.

### Approving or rejecting in the UI

1. Open the **Pipeline Dashboard** tab at [http://localhost:7860](http://localhost:7860)
2. Click the approval gate row in the Task Queue table — the task ID auto-fills
3. Review the **Config Diff** and **Post-execution Verification** panels in Task Detail
4. Click **✅ Approve** or **❌ Reject**

The Incidents tab groups all related pipeline chains under a single incident entry.

### Lab validation (optional)

If `LAB_VALIDATION_ENABLED=true`, the execution step applies the fix to the Containerlab equivalent of the production device first (`clab-{device}` by default, configurable via `LAB_DEVICE_PREFIX`). After `LAB_VERIFY_DELAY` seconds (default 30), the runner checks Prometheus to see if the corresponding lab alert cleared. If it did not clear, production execution is aborted and an `execution_aborted` event is recorded.

### Post-execution verification

After executing the approved fix, the runner performs two independent checks and stores their results in the `execution_complete` event:

**Device config verification (immediate, non-LLM):**
- Calls `run_show_commands(device, "show running-config")` directly (bypassing the LLM)
- Checks whether each applied config line appears in the running-config output
- Records `config_applied: true/false/null`, `found_lines`, and `missing_lines`
- Null means the device was unreachable — execution is not retried on this basis

**Alert resolution check (background, after `EXECUTION_VERIFY_DELAY` seconds):**
- Queries Prometheus `/api/v1/alerts` for firing alerts
- Matches by `(alertname, sysName)` — the labels Prometheus actually carries
- Records `alert_resolved: true/false`, `ttr_seconds`, and `check_at` in an `execution_verified` event
- TTR (time-to-resolve) counts from the original RCA task `created_at` to the verification check

Both results are rendered in the **Post-execution Verification** panel on the approval gate's Task Detail page.

### Execution event sequence

```
execution_started      → device + commands recorded
execution_complete     → status, config_applied, found_lines, missing_lines
execution_verified     → alert_resolved, ttr_seconds, alertname, check_at
```

---

## Pipeline Dashboard

The **📊 Pipeline** tab is the default landing page of the web UI at [http://localhost:7860](http://localhost:7860).

### Real-time updates

The dashboard uses **Server-Sent Events** (SSE). A single persistent connection to `/stream/tasks` pushes a notification whenever any task state changes. The pipeline visual, task queue, approval badge, and incident list all update immediately on change — no polling lag.

### Alert Processing Pipeline — Visual and Chronicle views

The **Alert Processing Pipeline** section offers two views, toggled with the **📊 Visual / 📖 Chronicle** buttons in the top-right of the panel. Selecting a different alert fingerprint from the dropdown reloads whichever view is currently active.

#### 📊 Visual view

A card layout shows the four pipeline stages for a selected alert fingerprint:

```
[🔍 RCA · ops_agent] › [🔧 Fix Proposal · eng_agent] › [✅ Validation · chaos_agent] › [🔐 Approval Gate · human]
```

Each card shows status, key result fields, and age. Connecting arrows turn green when stages complete.

#### 📖 Chronicle view

The Chronicle is a human-readable **incident narrative** for the selected alert. It renders the same pipeline data as a vertical timeline where each stage is a "chapter" with:

- **Header**: severity badge (P1/P2/P3), alert name, device, overall pipeline status, and time-to-resolution if the alert has been resolved.
- **Chapter header**: timestamp, stage label (e.g. `ROOT CAUSE IDENTIFIED`), and a coloured confidence/risk/verdict badge.
- **Chapter body**: prose summary of the stage's findings — diagnosis, fix commands, validation verdict, or approval/execution status. Collapsible detail panels show the full agent response and config diff.
- **Gap dividers**: time elapsed between consecutive stages (e.g. `┄┄ 3m 12s ┄┄`), making queuing delays immediately visible.
- **Task ID links**: each chapter's task ID is a click-through to the full Task Detail panel.

The Chronicle auto-refreshes via SSE whenever the pipeline state changes.

**Badge semantics per stage:**

| Stage | Badge field | Values |
| ----- | ----------- | ------ |
| RCA | Confidence | High ✅ · Medium 🟡 · Low ⚠️ |
| Fix Proposal | Risk | Low ✅ · Medium 🟡 · High 🔴 |
| Validation | Verdict | Correct ✅ · Partial 🟡 · Incorrect ❌ · Unverifiable ❓ |
| Approval Gate | Execution outcome | Resolved ✅ · Executed ✅ · Failed ❌ · Awaiting 🟣 · Rejected |

### Incidents tab

The **🚨 Incidents** tab groups all pipelines that share the same root incident. Each incident shows:
- Severity (P1–P4) and impact statement
- All affected devices
- Links to each individual alert's RCA → fix → gate pipeline
- Close button to mark the incident resolved with a resolution summary

### Task Queue

A filterable table of all tasks. Click any row to expand its full event timeline, input, result, config diff, and post-execution verification in the Task Detail panel.

### Task Detail — verification panel

For `approval_gate` tasks, the Task Detail panel includes a **Post-execution Verification** section with two side-by-side cards:

| Card | Shows |
| ---- | ----- |
| Config on device | ✅ all lines confirmed / ❌ missing lines listed / ⚠️ unavailable |
| Alert in Prometheus | ✅ no longer firing / ⚠️ still firing on device / ⏳ check pending |

---

## Configuration Reference

All pipeline behaviour is controlled by environment variables in `.env`:

### Core agent settings

| Variable                        | Default                 | Description                                    |
| ------------------------------- | ----------------------- | ---------------------------------------------- |
| `OPENAI_API_KEY`                | —                       | Required for OpenAI (gpt-4o)                   |
| `OPENAI_MODEL`                  | `gpt-4o`                | Model used by all three agents                 |
| `DAILY_BUDGET_USD`              | `5.00`                  | Hard daily spend limit across all agents       |
| `MAX_TOKENS_PER_AGENT_PER_HOUR` | `2,000,000`             | Hourly token cap per agent                     |
| `ACTIVITY_DB_PATH`              | `/app/data/activity.db` | SQLite path (used when `TASK_DB_URL` is empty) |

### Task store backend

| Variable       | Default | Description                                                    |
| -------------- | ------- | -------------------------------------------------------------- |
| `TASK_DB_URL`  | (empty) | SQLAlchemy URL. Empty = SQLite (default). Set to a PostgreSQL URL for production use |
| `AGENT_DB_PASSWORD` | `netauto_agent_default` | Password for the `agent-postgres` container |

Example PostgreSQL URL:
```
TASK_DB_URL=postgresql+psycopg2://agent:password@agent-postgres:5432/agent_tasks
```

### RabbitMQ task bus (optional)

| Variable        | Default | Description                                          |
| --------------- | ------- | ---------------------------------------------------- |
| `RABBITMQ_URL`  | (empty) | AMQP URL. Empty = polling-only mode (default lab)    |

When set, `fix_proposal` and `validation` tasks are dispatched to agent consumers immediately on creation — eliminating polling latency. The polling loops remain as fallback.

```
RABBITMQ_URL=amqp://netauto:password@rabbitmq:5672/
```

### Approval webhook

| Variable                  | Default | Description                                                |
| ------------------------- | ------- | ---------------------------------------------------------- |
| `APPROVAL_WEBHOOK_URL`    | (empty) | POST target when a task enters `awaiting_approval`         |
| `APPROVAL_WEBHOOK_SECRET` | (empty) | HMAC-SHA256 signing secret; empty = unsigned               |
| `AGENT_UI_URL`            | `http://localhost:7860` | Base URL used to construct approve/reject links |

### Maintenance window

| Variable                 | Default                         | Description                                            |
| ------------------------ | ------------------------------- | ------------------------------------------------------ |
| `MAINTENANCE_CHECK_ENABLED` | `false`                      | Query Nautobot before creating RCA tasks               |
| `MAINTENANCE_STATUSES`   | `planned,staged,decommissioning` | Nautobot device status slugs that suppress auto-exec   |
| `MAINTENANCE_TAG`        | `maintenance`                   | Nautobot tag that marks a device as in maintenance     |

### Lab validation

| Variable                  | Default  | Description                                                        |
| ------------------------- | -------- | ------------------------------------------------------------------ |
| `LAB_VALIDATION_ENABLED`  | `false`  | Apply fix to Containerlab device before production execution       |
| `LAB_DEVICE_PREFIX`       | `clab-`  | Prefix that maps `leaf1` → `clab-leaf1` in the lab topology       |
| `LAB_VERIFY_DELAY`        | `30`     | Seconds to wait for lab alert to clear after applying the fix      |

### Runbook library

| Variable                  | Default       | Description                                             |
| ------------------------- | ------------- | ------------------------------------------------------- |
| `GITEA_TOKEN`             | (empty)       | API token for the Gitea runbooks repo (read access)     |
| `GITEA_RUNBOOK_OWNER`     | `netauto`     | Gitea organisation or username                          |
| `GITEA_RUNBOOK_REPO`      | `runbooks`    | Repository containing `{AlertName}.yaml` runbook files  |
| `GITEA_RUNBOOK_BRANCH`    | `main`        | Branch to read from                                     |

### Execution verification

| Variable                   | Default | Description                                                     |
| -------------------------- | ------- | --------------------------------------------------------------- |
| `EXECUTION_VERIFY_DELAY`   | `300`   | Seconds after execution before Prometheus alert check (5 min)   |

---

## Data Model

All pipeline state lives in three tables inside the task store (SQLite or PostgreSQL).

### `tasks`

| Column                | Type    | Description                                                                          |
| --------------------- | ------- | ------------------------------------------------------------------------------------ |
| `id`                  | TEXT    | Short unique ID with type prefix (e.g. `rca-a1b2c3d4`)                              |
| `parent_id`           | TEXT    | References the parent task (fix → rca, validation → fix, gate → validation or fix)  |
| `incident_id`         | TEXT    | References the parent Incident grouping task                                         |
| `alert_fingerprint`   | TEXT    | Links the entire pipeline chain                                                      |
| `type`                | TEXT    | `incident` / `rca` / `fix_proposal` / `validation` / `approval_gate`                |
| `status`              | TEXT    | See lifecycle diagram                                                                |
| `priority`            | TEXT    | `critical` / `high` / `normal` / `low`                                              |
| `created_by`          | TEXT    | `system`, `ops_agent`, `eng_agent`, `chaos_agent`                                   |
| `assigned_to`         | TEXT    | Agent or `human` responsible for processing                                          |
| `content`             | TEXT    | JSON input context passed to the processing agent                                    |
| `result`              | TEXT    | JSON structured output after completion                                              |
| `retry_count`         | INTEGER | Number of automatic retries attempted (max 2)                                        |
| `maintenance_window`  | INTEGER | 1 if device was in a maintenance window when the task was created                    |
| `do_not_auto_execute` | INTEGER | 1 to suppress automated execution at the approval gate                               |

### `task_events`

Append-only event log. Key event types:

| Event type             | Stage             | Detail fields                                               |
| ---------------------- | ----------------- | ----------------------------------------------------------- |
| `created`              | all               | `assigned_to`, `priority`                                   |
| `claimed`              | all               | —                                                           |
| `started`              | all               | —                                                           |
| `completed`            | all               | —                                                           |
| `failed`               | all               | `error`                                                     |
| `retry_scheduled`      | all               | `retry_count`                                               |
| `alert_correlated`     | rca               | `alertname`, `fingerprint`, `severity`                      |
| `approval_requested`   | approval_gate     | —                                                           |
| `approved`             | approval_gate     | —                                                           |
| `auto_approved`        | approval_gate     | `reason` (risk + confidence + prior executions)             |
| `execution_started`    | approval_gate     | `device`, `commands`                                        |
| `execution_suppressed` | approval_gate     | `reason` (maintenance window)                               |
| `execution_aborted`    | approval_gate     | `reason` (lab validation failed)                            |
| `lab_fix_applied`      | approval_gate     | `lab_device`, `commands`                                    |
| `lab_validated`        | approval_gate     | `lab_device`, `alert_cleared`                               |
| `lab_validation_failed`| approval_gate     | `reason`, `lab_device`                                      |
| `execution_complete`   | approval_gate     | `status`, `device`, `changes_applied`, `config_applied`, `found_lines`, `missing_lines` |
| `execution_verified`   | approval_gate     | `alert_resolved`, `ttr_seconds`, `alertname`, `device`, `check_at` |
| `feedback_added`       | fix_proposal, rca | `verdict`, `confidence`                                     |
| `task_linked`          | incident          | `linked_task_id`                                            |
| `incident_resolved`    | incident          | `resolution`                                                |

### `task_feedback`

Structured feedback written by the Chaos Agent: `verdict`, `confidence` (0.0–1.0), `notes`. Used to compute the KPI validation accuracy metric.

---

## Deduplication and Resilience

| Mechanism              | Description                                                                   |
| ---------------------- | ----------------------------------------------------------------------------- |
| Fingerprint dedup      | `_seen` dict seeded from TaskStore on startup; survives container restarts    |
| Prometheus validation  | Every candidate alert is checked against live `/api/v1/alerts` before acting |
| Alert correlation      | Same device + 15-min window → append note rather than spawn parallel pipeline |
| Maintenance suppression| `do_not_auto_execute` blocks automated gate execution for maintenance devices |
| Task claim atomicity   | `UPDATE … WHERE status='pending'` rowcount check prevents double-processing   |
| Pipeline retry         | Failed tasks auto-retry up to 2× after 120 s; failure fingerprints re-enter `_seen` |
| Rate-limit retry       | HTTP 429 from OpenAI triggers one 70 s wait-and-retry                         |
| Budget guard           | Token budget checked before claiming any task; task stays `pending` if exceeded |

---

## Extending the Pipeline

### Adding a new task type

1. Add the type string to `_VALID_TYPES` in `shared/task_store.py`
2. Add the migration `ALTER TABLE tasks ADD COLUMN …` to `_MIGRATIONS` if needed
3. Create a task runner class that polls `list_tasks(type="your_type", status="pending")`
4. Call `claim_task → start_task → complete_task` (or `fail_task`) around the agent invocation
5. Wire the runner into the agent's `main.py`

### Adding a new pipeline stage

1. Change the upstream stage's child-task creation to use your new type
2. Have your new runner create the downstream task on completion
3. Add the new type to `TYPE_ICONS` and relevant template sections in the UI

### Adding a runbook

Create a YAML file in the Gitea `runbooks` repository (default: `netauto/runbooks`):

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

The Engineering Agent calls `get_runbook(alertname)` as its first tool call for any automated fix request. If no Gitea token is configured, the agent falls back to built-in runbooks for the seven standard alert types.

### Migrating to PostgreSQL

1. Start the `agent-postgres` container (already in `docker-compose.yml`)
2. Run the migration script to copy existing SQLite data:
   ```bash
   python3 scripts/migrate_sqlite_to_postgres.py \
     --sqlite /path/to/activity.db \
     --postgres postgresql+psycopg2://agent:password@agent-postgres:5432/agent_tasks
   ```
3. Set `TASK_DB_URL=postgresql+psycopg2://agent:password@agent-postgres:5432/agent_tasks` in `.env`
4. Restart all agent containers: `make restart`

SQLite remains the default for lab/development use; no migration is required unless deploying to production.
