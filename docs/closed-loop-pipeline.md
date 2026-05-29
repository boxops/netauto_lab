# Closed-Loop Automation Pipeline

The closed-loop pipeline is the autonomous incident-response system built into the AI agent stack. When Prometheus fires an alert, the pipeline coordinates the three agents — Ops, Engineering, and Chaos — to investigate, propose, validate, and gate a remediation, with a mandatory human approval step before any configuration change is executed on the network.

---

## Overview

```
  Prometheus alert fires
         │
         ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  AlertPoller  (ops_agent · every 60 s)                               │
  │  Validates alert is still firing in live Prometheus before acting    │
  └──────────────────────────┬───────────────────────────────────────────┘
                             │ creates RCA task
                             ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Stage 1 — RCA  (ops_agent)                                          │
  │  Correlates alerts, metrics, and syslogs into a root cause summary   │
  └──────────────────────────┬───────────────────────────────────────────┘
                             │ creates fix_proposal task
                             ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Stage 2 — Fix Proposal  (eng_agent · every 90 s)                    │
  │  Generates the most specific, actionable remediation in check mode   │
  └────────┬──────────────────────────────────────────────┬──────────────┘
           │ risk = low / medium                          │ risk = high
           │ creates validation task                      │ or FIX_TYPE = escalate_human
           ▼                                              │
  ┌─────────────────────────────────┐                    │
  │  Stage 3 — Validation           │                    │
  │  (chaos_agent · every 120 s)    │                    │
  │  Blast-radius check, topology   │                    │
  │  analysis, read-only device     │                    │
  │  inspection                     │                    │
  └────────┬────────────────────────┘                    │
           │ verdict = correct / partial                  │
           │ creates approval_gate task                   │
           ▼                                              ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  Stage 4 — Approval Gate  (human)                                    │
  │  Human reviews commands and authorises check_mode=False execution    │
  └──────────────────────────────────────────────────────────────────────┘
```

All four stages are tracked in the shared **TaskStore** (SQLite), visible in the **Pipeline Dashboard** at [http://localhost:7860](http://localhost:7860), and linked by a common `alert_fingerprint` so the full chain is always traceable.

---

## Task Types and Lifecycle

### Task types

| Type            | Owner         | Created by                   | Purpose                                       |
| --------------- | ------------- | ---------------------------- | --------------------------------------------- |
| `rca`           | `ops_agent`   | `system` (AlertPoller)       | Root cause analysis of a firing alert         |
| `fix_proposal`  | `eng_agent`   | `ops_agent`                  | Specific remediation commands in check mode   |
| `validation`    | `chaos_agent` | `eng_agent`                  | Blast-radius and correctness check of the fix |
| `approval_gate` | `human`       | `eng_agent` or `chaos_agent` | Human sign-off required before live execution |

### Status lifecycle

```
pending ──► claimed ──► running ──► complete
                                 ├─► awaiting_approval  (approval_gate tasks only)
                                 ├─► failed
                                 └─► rejected
```

`awaiting_approval` is the only non-terminal state that persists indefinitely — it waits for a human action (Approve or Reject) in the UI.

### Priority mapping

Alert severity drives task priority and flows through the chain:

| Prometheus severity | Task priority |
| ------------------- | ------------- |
| `critical`          | `high`        |
| `warning`           | `normal`      |

Approval gate tasks created for high-risk fixes are always `high` priority, regardless of the original alert severity.

---

## Stage 1 — Root Cause Analysis (Ops Agent)

**Triggered by:** AlertPoller detecting a new firing alert  
**Task type:** `rca`  
**Poll interval:** 60 s (startup grace period: 30 s)

### What the AlertPoller does

1. Fetches events from the alert-event-receiver service (`GET /events`)
2. Deduplicates by fingerprint — the most recent event per fingerprint wins
3. Validates each firing event against live Prometheus (`GET /api/v1/alerts`) to confirm the alert is still active before creating a task
4. Skips alerts already tracked in `_seen` (in-memory deduplication, seeded from TaskStore on startup to survive container restarts)
5. Skips alerts with a non-terminal task already in the TaskStore for that fingerprint
6. Creates an `rca` task and immediately runs the investigation

### What the Ops Agent investigates

```
1. get_active_alerts()               → confirm what is currently firing
2. get_device_metrics(device)        → reachability, interface oper state, RTT
3. get_interface_events(device)      → link up/down syslog events
4. get_bgp_events(device)            → BGP session state changes
5. get_topology()                    → blast-radius context (if relevant)
```

### Structured output

The agent is prompted to end its response with:

```
DIAGNOSIS:  <one-sentence root cause>
AFFECTED:   <device hostname or "unknown">
ACTION:     <recommended next step>
CONFIDENCE: high | medium | low
```

### Escalation decision

If `ACTION` contains any of the phrases `no action`, `no fix`, `already resolved`, `self-healed`, or `monitor only`, the pipeline stops here. Otherwise, a `fix_proposal` task is created for the Engineering Agent.

---

## Stage 2 — Fix Proposal (Engineering Agent)

**Triggered by:** RCA task completing with an actionable recommendation  
**Task type:** `fix_proposal`  
**Poll interval:** 90 s (offset from ops to avoid token bursts)

### What the Engineering Agent does

```
1. get_device_info(device)           → confirm platform and current status
2. get_device_interfaces(device)     → check interface state
3. run_show_commands(device, cmds)   → read current config if needed
4. run_config_commands(device, lines, check_mode=True)   → simulate the fix
```

> **Note:** `check_mode=True` is hardcoded in the agent prompt. The Engineering Agent never applies changes directly — it only simulates them.

### Structured output

```
FIX_TYPE:   config_change | runbook | no_action | escalate_human
DEVICE:     <exact device hostname>
COMMANDS:   <config lines to apply, or "none">
RISK:       low | medium | high
CONFIDENCE: high | medium | low
REASON:     <one sentence explaining the fix>
```

### Routing decision

| Condition                                                | Next step                                            |
| -------------------------------------------------------- | ---------------------------------------------------- |
| `FIX_TYPE = no_action`                                   | Pipeline ends — no further tasks created             |
| `RISK = high`                                            | Approval gate created immediately (skips validation) |
| `FIX_TYPE = escalate_human`                              | Approval gate created immediately                    |
| `RISK = low` or `medium` and `FIX_TYPE ≠ escalate_human` | Validation task created for Chaos Agent              |

---

## Stage 3 — Validation (Chaos Agent)

**Triggered by:** Fix proposal completing with low or medium risk  
**Task type:** `validation`  
**Poll interval:** 120 s (runs after Engineering Agent to ensure fixes are ready)

### What the Chaos Agent checks

```
1. get_topology()                    → blast radius: which other devices depend on this?
2. get_device_metrics(device)        → confirm current device state
3. get_connected_devices(device)     → direct neighbors that could be affected
4. get_active_alerts()               → is the original alert still firing?
5. run_show_commands(device, ...)    → read-only config inspection (no changes applied)
```

The Chaos Agent answers three questions:

- Does the proposed fix address the stated root cause?
- Is the risk assessment accurate?
- Are there blast-radius concerns for connected devices or services?

### Structured output

```
VERDICT:        correct | incorrect | partial | unverifiable
CONFIDENCE:     high | medium | low
RISK_CONFIRMED: low | medium | high
NOTES:          <one sentence summarising the validation finding>
```

### Feedback propagation

After completing, the Chaos Agent writes structured feedback to:

1. The parent `fix_proposal` task
2. The grandparent `rca` task (for long-term accuracy tracking in the KPI dashboard)

### Routing decision

| Verdict                       | Next step                                     |
| ----------------------------- | --------------------------------------------- |
| `correct` or `partial`        | Approval gate created — human review required |
| `incorrect` or `unverifiable` | Pipeline ends — no approval gate created      |

---

## Stage 4 — Approval Gate (Human)

**Triggered by:** Engineering Agent (high-risk fix) or Chaos Agent (validated low/medium-risk fix)  
**Task type:** `approval_gate`  
**Status on creation:** `awaiting_approval`

The approval gate is the only point in the pipeline where a human must act. It contains:

- The exact configuration commands the Engineering Agent proposed
- The target device
- The confirmed risk level (from the Chaos Agent, if available)
- The validation verdict
- The reason for the fix

### Approving or rejecting in the UI

1. Open the **Pipeline Dashboard** tab at [http://localhost:7860](http://localhost:7860)
2. In the **Alert Processing Pipeline** visualiser, select the alert fingerprint
3. The Approval Gate card will show **"Awaiting human approval"** in purple
4. Scroll down to the **Task Queue** table and click the approval gate row
5. The task ID auto-fills in the approval input
6. Click **✅ Approve** or **❌ Reject**

After approval, a human or automation must execute the commands with `check_mode=False` using:

```bash
curl -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Apply the fix from task app-<id>: run_config_commands on <device> with check_mode=False",
    "session_id": "human-approved-<id>"
  }'
```

Or directly via the Engineering Agent chat tab in the UI.

---

## Pipeline Dashboard

The **📊 Pipeline** tab is the default landing page of the web UI. It has four sections.

### Alert Processing Pipeline visualiser

At the top of the tab, a real-time card layout shows the four pipeline stages for a selected alert:

```
[🔍 RCA · ops_agent] › [🔧 Fix Proposal · eng_agent] › [✅ Validation · chaos_agent] › [🔐 Approval Gate · human]
```

Each card shows:

- Current status (pending / claimed / running / awaiting approval / complete / failed) with colour coding
- Key result fields: diagnosis, commands, verdict, risk level
- Duration and age
- A pulsing animation for in-progress stages

Connecting arrows between stages turn **green** when a stage completes. The overall pipeline badge shows `⟳ In Progress`, `🔐 Awaiting Approval`, or `✅ Pipeline Complete`.

**Controls:**

- **Alert fingerprint dropdown** — select any alert processed today; auto-selects the most recent on load
- **🔄 Refresh Alerts** — fetch new fingerprints (new alerts appear here within one poll cycle)
- Auto-updates every **3 seconds** without losing the selected alert

### Live Agent Status

Three agent cards show real-time state (`IDLE`, `THINKING`, `CALLING TOOL`, `WRITING`, `OFFLINE`) plus token/hour usage and daily cost. Updates every **2 seconds**.

### Task Queue

A filterable table of all tasks, sortable by status and type. Click any row to expand its full event timeline, input, result, and feedback in the **Task Detail** panel below. Clicking an `approval_gate` row auto-fills the task ID for the Approve/Reject buttons.

### Cost Monitor & KPIs

Daily spend, token usage, auto-resolution rate, validation accuracy (fraction of Chaos Agent verdicts that were `correct`), and escalation rate. Updates every **30 seconds**.

---

## Configuration

Pipeline behaviour is controlled by environment variables in `.env`:

| Variable                        | Default                 | Description                                 |
| ------------------------------- | ----------------------- | ------------------------------------------- |
| `OPENAI_API_KEY`                | —                       | Required for OpenAI (gpt-4o)                |
| `OPENAI_MODEL`                  | `gpt-4o`                | Model used by all three agents              |
| `DAILY_BUDGET_USD`              | `5.00`                  | Hard daily spend limit across all agents    |
| `MAX_TOKENS_PER_AGENT_PER_HOUR` | `2,000,000`             | Hourly token cap per agent                  |
| `ACTIVITY_DB_PATH`              | `/app/data/activity.db` | Shared SQLite database path (Docker volume) |

Poll intervals are hardcoded in each task runner:

| Component       | File                               | Interval |
| --------------- | ---------------------------------- | -------- |
| AlertPoller     | `ops_agent/alert_poller.py`        | 60 s     |
| EngTaskRunner   | `engineering_agent/task_runner.py` | 90 s     |
| ChaosTaskRunner | `chaos_agent/task_runner.py`       | 120 s    |

Maximum tasks processed per poll cycle (`MAX_PER_CYCLE`) is `1` for both Engineering and Chaos, preventing token bursts.

---

## Data Model

All pipeline state lives in three SQLite tables inside `activity.db`:

### `tasks`

| Column              | Description                                                                        |
| ------------------- | ---------------------------------------------------------------------------------- |
| `id`                | Short unique ID with type prefix (e.g. `rca-a1b2c3d4`)                             |
| `parent_id`         | References the parent task (fix → rca, validation → fix, gate → validation or fix) |
| `alert_fingerprint` | Prometheus fingerprint linking the entire chain                                    |
| `type`              | `rca` / `fix_proposal` / `validation` / `approval_gate`                            |
| `status`            | See lifecycle diagram above                                                        |
| `priority`          | `critical` / `high` / `normal` / `low`                                             |
| `created_by`        | Agent that created the task (`system`, `ops_agent`, `eng_agent`, `chaos_agent`)    |
| `assigned_to`       | Agent or `human` responsible for processing                                        |
| `content`           | JSON input context passed to the processing agent                                  |
| `result`            | JSON structured output after completion                                            |

### `task_events`

Append-only event log for each task: `created`, `claimed`, `started`, `completed`, `failed`, `approval_requested`, `approved`, `rejected`, `tool_call`, `tool_result`, `llm_end`, `feedback_added`. Used to render the event timeline in the Task Detail panel.

### `task_feedback`

Structured feedback written by the Chaos Agent after validation: `verdict` (`correct` / `incorrect` / `partial` / `unverifiable`), `confidence` (0.0–1.0), and freeform `notes`. Used to compute the KPI validation accuracy metric.

---

## Deduplication and Resilience

- **AlertPoller deduplication:** Each fingerprint is tracked in `_seen`. On container restart, `_seen` is pre-seeded from the TaskStore so already-investigated alerts are not re-processed.
- **Task claim atomicity:** `claim_task` uses an `UPDATE ... WHERE status='pending'` with row-count checking to prevent two runner instances from processing the same task.
- **Rate-limit retry:** If OpenAI returns HTTP 429, the Ops Agent retries once after 70 seconds. Engineering and Chaos agents fail the task and leave it visible in the queue.
- **Budget guard:** Each task runner checks the token budget before claiming a task. If the budget is exceeded, the task remains `pending` and is retried in the next poll cycle.
- **Duplicate alert guard:** Before creating a new RCA task, the AlertPoller checks the TaskStore for any non-terminal task with the same fingerprint.

---

## Extending the Pipeline

### Adding a new task type

1. Add the type string to `_VALID_TYPES` in `shared/task_store.py`
2. Create a task runner class that polls `list_tasks(type="your_type", status="pending")`
3. Call `claim_task → start_task → complete_task` (or `fail_task`) around the agent invocation
4. Wire the runner into the agent's `main.py`

### Adding a new pipeline stage

If you want to insert a stage between existing ones (e.g. a second validation layer):

1. Change the upstream stage's child-task creation to use your new type
2. Have your new runner create the downstream task on completion
3. Add the new type to `_TYPE_ICONS` and `_STATUS_COLORS` in `ui/app.py`
4. Add a new `STAGES` entry in `_alert_pipeline_html()` in `ui/app.py`

### Changing the approval trigger

The Engineering Agent creates an approval gate when `fix_type == "escalate_human"` or `risk == "high"` (`engineering_agent/task_runner.py`, `_process_task`). Adjust this condition to change which fixes bypass validation and go directly to human review.

The Chaos Agent creates an approval gate when `verdict in ("correct", "partial")` (`chaos_agent/task_runner.py`, `_create_approval_gate`). Adjust this to require approval only for specific verdicts or risk levels.
