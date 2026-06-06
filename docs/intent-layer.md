# Standing Intents and the Intent Layer

Standing intents are persistent declarations about how the network should behave. Unlike autonomy policies (which control *how much* independence the agent has when responding to a specific fix), intents control *when* the agent acts and *what it does* with specific alert types — independently of what Prometheus fires.

Intents are managed in the **⚙️ Config → Standing Intents** section of the Clano UI at [http://localhost:7860/config](http://localhost:7860/config#intents-section).

---

## Intent Types

| Type | Effect | When to use |
| --- | --- | --- |
| `suppress` | Skip pipeline investigation entirely for matching alerts | Known-flapping link, device under planned maintenance, or alert you are already tracking manually |
| `escalate` | Force the approval gate to appear regardless of the matched policy autonomy level | Production devices where you never want auto-execution, even if a L4 policy would normally apply |
| `monitor` | Proactively poll a Prometheus metric on a schedule and open an RCA task when a threshold is breached | Detect degraded state *before* Alertmanager fires — e.g. BGP prefix count declining, CPU trending up |
| `chaos_schedule` | Run a chaos scenario on a cron expression via APScheduler | Regular resilience testing — simulates failure and validates recovery without manual triggering |

---

## How Intents are Evaluated

### Alert-driven intents (`suppress`, `escalate`)

When the **AlertPoller** picks up a new alert, it calls `IntentRegistry.matching(device, alertname)` before creating any task. The matching logic:

- `device` field in the intent: exact hostname match, or empty string = any device.
- `alertname` field in the intent: exact alert name match, or empty string = any alert.
- Both fields must match (AND logic). An empty field is a wildcard.

If a `suppress` intent matches, no RCA task is created — the alert is silently skipped and the fingerprint is added to the deduplication set.

If an `escalate` intent matches, the RCA task is created normally but `do_not_auto_execute=true` is set on the task, which forces a human approval gate regardless of the PolicyRegistry decision.

### Proactive intents (`monitor`)

The **IntentEvaluator** runs as a background thread (default poll interval: 5 minutes, staggered 30 seconds after startup to avoid competing with AlertPoller's initial burst).

For each enabled `monitor` intent, it:

1. Queries Prometheus with the intent's `metric_query` (a PromQL expression).
2. Evaluates the returned value against the intent's `threshold` expression (e.g. `< 1`, `>= 95`, `== 0`).
3. If the threshold is breached and no active pipeline already exists for this intent's fingerprint (`intent:<intent_id>`), it creates an `rca` task with:
   - `alert_fingerprint = "intent:<intent_id>"`
   - `created_by = "intent_evaluator"`
   - `title = "[Intent] <intent_name>"`
4. Updates `last_triggered_at` on the intent row.

Intent-triggered pipelines are identified in the Chronicle by a purple **🎯 Intent-triggered** badge instead of the gray **🔔 Alert-triggered** badge.

### Scheduled intents (`chaos_schedule`)

Chaos schedule intents are handed off to the APScheduler instance running inside the `ai-agent` container. The `metric_query` field carries the cron expression; the `description` field carries the scenario to run.

---

## Creating Intents

### Via the Clano UI

1. Open [http://localhost:7860/config](http://localhost:7860/config) and scroll to **Standing Intents**.
2. Click **Add Intent** and fill in the form:
   - **Name**: human-readable label (e.g. "suppress leaf1 Ethernet1 flap").
   - **Intent type**: `suppress`, `escalate`, `monitor`, or `chaos_schedule`.
   - **Device**: exact hostname, or leave empty to match any device.
   - **Alert name**: exact Prometheus `alertname` label value, or leave empty to match any.
   - **Description**: optional free-text note.
   - For `monitor` type: also fill in **PromQL metric query** and **Threshold expression**.
3. Click **Add intent**. The intent is active immediately.

### Via the API

```bash
# Suppress all InterfaceDown alerts on leaf1
curl -X POST http://localhost:7860/partials/intent-create \
  -d "name=suppress+leaf1+flap&intent_type=suppress&device=leaf1&alertname=InterfaceDown&description=Known+flapping+uplink"

# Monitor BGP prefix count — create RCA if it drops below 5
curl -X POST http://localhost:7860/partials/intent-create \
  -d "name=BGP+prefix+monitor&intent_type=monitor&device=leaf1&metric_query=bgp_prefixes_received%7Bdevice%3D%27leaf1%27%7D&threshold=%3C+5&description=Early+warning+before+BGPPeerDown+fires"
```

### Directly in the database

```sql
INSERT INTO standing_intents (
  id, tenant_id, name, intent_type,
  device, alertname,
  metric_query, threshold,
  description, enabled, created_at
) VALUES (
  'int-' || gen_random_uuid(),
  'default',
  'Suppress spine2 maintenance',
  'suppress',
  'spine2', '',
  '', '',
  'spine2 is under maintenance until 2026-06-10',
  1, NOW()
);
```

---

## Threshold Expression Syntax

For `monitor` intents, the threshold expression is a simple two-token string: `<operator> <value>`.

| Operator | Meaning | Example |
| --- | --- | --- |
| `<` | Less than | `< 1` |
| `<=` | Less than or equal | `<= 0` |
| `>` | Greater than | `> 95` |
| `>=` | Greater than or equal | `>= 100` |
| `==` | Equal | `== 0` |
| `!=` | Not equal | `!= 1` |

The PromQL query should return a single scalar value. If it returns multiple series, only the first result's value is evaluated. If the query fails or returns no data, the threshold check is skipped silently.

---

## Interaction with PolicyRegistry

Standing intents and autonomy policies are independent layers:

```
Alert arrives
     │
     ├─ IntentRegistry: suppress? → skip (no task created)
     │
     ├─ IntentRegistry: escalate? → create task with do_not_auto_execute=true
     │                              (PolicyRegistry still runs but cannot auto-approve)
     │
     └─ No matching intent → create task normally
                              PolicyRegistry determines autonomy level
```

An `escalate` intent always wins over a L4 or L5 policy. It does not prevent the agent from investigating and proposing a fix — it only forces the approval gate to require a human click before execution.

---

## Deduplication

Intent-triggered pipelines use `alert_fingerprint = "intent:<intent_id>"` as their unique key. The IntentEvaluator checks for an active task with this fingerprint before creating a new one, so a threshold breach that persists across multiple poll cycles only creates one pipeline — not one per cycle.

Once the pipeline completes (or is rejected), the fingerprint is cleared from the active-task index and the intent will trigger again on the next threshold breach.
