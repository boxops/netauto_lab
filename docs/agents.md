# AI Agents

The stack includes a **unified AI agent** (`ai-agent` service) built with LangGraph (ReAct loop) that handles the full closed-loop pipeline — root cause analysis, fix generation, validation, and execution — within a single state machine on **port 8000**. All background tasks (AlertPoller, IntentEvaluator, APScheduler, policy-promotion sweep) run inside this one service.

See [`docs/closed-loop-pipeline.md`](closed-loop-pipeline.md) for the full pipeline reference, [`docs/policy-autonomy.md`](policy-autonomy.md) for the autonomy level system, and [`docs/intent-layer.md`](intent-layer.md) for standing intents.

## Accessing the Agent

| Interface | URL |
|---|---|
| Clano Web UI (pipeline dashboard, incidents, chat, config, system) | http://localhost:7860 |
| AI Agent REST API | http://localhost:8000 |

---

## Unified Agent Capabilities

The unified agent combines the capabilities of the three legacy roles into one LangGraph ReAct loop. It has access to all tool tiers and is invoked sequentially through the pipeline stages.

### Reactive capabilities (alert-driven)

| Capability | Description |
|---|---|
| Alert investigation | Queries active Prometheus alerts, correlates with metrics and logs |
| Root cause analysis | Synthesises a structured DIAGNOSIS / AFFECTED / ACTION / CONFIDENCE summary |
| Topology correlation | Uses `TopologyCorrelator` to detect cascading failures and infer blast radius from Nautobot cable data |
| Runbook-first fix generation | Calls `get_runbook(alertname)` before re-deriving fixes from scratch |
| Config diff generation | Produces a unified diff of current vs proposed running-config |
| Blast-radius assessment | Maps which devices depend on a target interface or device |
| Fix validation | Cross-checks fix proposals for correctness and downstream impact |
| Execution | Applies approved fixes with `check_mode=False` after human or policy sign-off |
| Post-execution verification | Non-LLM config check + Prometheus resolution check after execution |

### Proactive capabilities (intent-driven)

| Capability | Description |
|---|---|
| Metric monitoring | Polls Prometheus on a schedule and opens an RCA when a threshold is breached — before Alertmanager fires |
| Alert suppression | Skips pipeline investigation for known-flapping or maintenance-window alerts |
| Forced escalation | Forces the approval gate regardless of policy autonomy level |
| Chaos scheduling | Runs chaos scenarios on a cron expression for resilience testing |

### Interactive capabilities (chat-driven)

| Capability | Description |
|---|---|
| Device lookup | Queries Nautobot for device info, interfaces, neighbors |
| Config review | Reviews configs against best practices |
| IP and VLAN planning | Finds available IPs/VLANs from Nautobot IPAM |
| Playbook authoring | Writes Ansible playbooks from natural-language descriptions |
| Health reporting | Generates fleet-wide or per-device health reports |

---

## Tool Tiers

The agent follows a four-tier tool hierarchy enforced by the system prompt:

| Tier | Tools | Purpose |
|---|---|---|
| 0 — Runbook | `get_runbook(alertname)` | **Check first** for known alert types before reasoning from scratch |
| 1 — Discovery | Nautobot tools (`get_device_info`, `get_device_interfaces`, `get_topology`, …) | Ground answers in actual inventory data |
| 2 — Metrics/State | Prometheus tools (`get_device_metrics`, `get_active_alerts`, …) | Validate current device state |
| 3 — Logs | Loki tools (`get_interface_events`, `get_bgp_events`, `get_syslog_events`, …) | Correlate events with alert timeline |
| 4 — Actions | `run_show_commands`, `run_config_commands`, chaos tools | Read or apply config (`check_mode=True` by default) |

See [`docs/agent-tools-framework.md`](agent-tools-framework.md) for adding new tools and docstring conventions.

---

## Safety Rules

- All `run_config_commands` and chaos action calls default to `check_mode=True` (dry run).
- In the automated pipeline, the agent never sets `check_mode=False` — execution is always gated by the autonomy policy (human approval at L0–L3, or policy threshold at L4).
- L5 (fully autonomous) is never set automatically — it requires explicit operator action in the Config UI.
- Devices tagged `maintenance` or in a configured Nautobot status receive `do_not_auto_execute=true`, blocking automated execution at the gate regardless of policy level.

---

## Closed-Loop Pipeline

When Prometheus fires an alert, the pipeline runs automatically through four stages, tracked as linked tasks in the shared TaskStore:

```
Prometheus alert
       │
       ▼
 AlertPoller (ai-agent / IntentEvaluator)
 · Deduplicates by fingerprint
 · Checks maintenance status
 · Runs TopologyCorrelator to detect cascading failures
 · Creates or links to an Incident entity
       │ creates rca task + immediately invokes
       ▼
 ┌─── LangGraph IncidentWorkflow ─────────────────────────────────────────┐
 │                                                                         │
 │   Stage 1 ─ RCA        Stage 2 ─ Fix Proposal   Stage 3 ─ Validation   │
 │   · Alerts / metrics   · get_runbook() first     · Blast-radius check   │
 │   · Logs / topology    · Config diff generated   · Read-only inspection │
 │   · Structured output  · Structured output        · Verdict output       │
 │                                                                         │
 │   Stage 4 ─ Approval Gate                                               │
 │   · PolicyRegistry determines autonomy level (L0–L5)                   │
 │   · L0–L3: human approval required                                      │
 │   · L4: auto-approved when policy thresholds met                        │
 │   · Post-approval: execution + config verify + alert resolution check   │
 └─────────────────────────────────────────────────────────────────────────┘
```

Full pipeline documentation: [`docs/closed-loop-pipeline.md`](closed-loop-pipeline.md)

---

## REST API

All endpoints are served by the single `ai-agent` service on port 8000. The `session_id` scopes chat history in the activity store.

```bash
# Chat
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Check BGP status on leaf1", "session_id": "clano-1"}'

# Streaming chat
curl -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "Investigate the BGP peer down alert on spine2", "session_id": "clano-1"}'

# Health / status / usage
curl http://localhost:8000/health
curl http://localhost:8000/status
curl http://localhost:8000/usage

# Reset alert poller deduplication state (after clearing the task queue)
curl -X POST http://localhost:8000/poller/reset

# Task CRUD
curl http://localhost:8000/tasks
curl http://localhost:8000/tasks/<task_id>

# Trigger Phase 2 execution after human approval
curl -X POST http://localhost:8000/workflow/resume/<task_id>

# Scheduled chaos experiments (APScheduler)
curl http://localhost:8000/schedules
curl -X POST http://localhost:8000/schedule \
  -H "Content-Type: application/json" \
  -d '{"scenario": "Shut Ethernet1 on leaf1 in check mode", "interval_minutes": 30}'
curl -X DELETE http://localhost:8000/schedule/<job_id>

# Autonomy policies
curl http://localhost:8000/policies
curl -X POST http://localhost:8000/policies -H "Content-Type: application/json" -d '{...}'

# Standing intents
curl http://localhost:8000/intents
curl -X POST http://localhost:8000/intents -H "Content-Type: application/json" -d '{...}'
```

---

## Example Prompts

### Investigation and diagnosis
```
"What alerts are currently firing?"
"Investigate the BGP peer down alert on spine2."
"Why is leaf1 showing high packet loss? Check logs and metrics."
"Generate a health report for all lab devices."
```

### Fix generation and config design
```
"Design a BGP configuration for a new leaf router with AS 65104."
"What IPs are available in the 10.10.0.0/16 prefix?"
"Generate an Ansible playbook that sets SNMPv3 credentials on all EOS devices."
"Compare spine1's running config to its intended state in Nautobot."
```

### Chaos and validation
```
"What is the blast radius if I shut down Ethernet1 on spine1?"
"Simulate a leaf uplink failure on leaf2 in check mode."
"Validate this fix: restore interface Ethernet2 on spine1."
"Design a 15-minute game day for testing BGP reconvergence."
```

---

## Configuration

Agent behaviour is controlled via environment variables in `.env`. See [`docs/closed-loop-pipeline.md`](closed-loop-pipeline.md) for the complete reference. Key variables:

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | (none) | GPT-4o key — required for OpenAI |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model name |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama endpoint (local LLM fallback) |
| `OLLAMA_MODEL` | `llama3` | Ollama model name |
| `DAILY_BUDGET_USD` | `5.00` | Hard daily spend limit across all agents |
| `MAX_TOKENS_PER_AGENT_PER_HOUR` | `2,000,000` | Hourly token cap per agent |
| `TASK_DB_URL` | (empty = SQLite) | PostgreSQL URL for production task store |
| `RABBITMQ_URL` | (empty = polling) | AMQP URL for near-zero latency task dispatch |
| `APPROVAL_WEBHOOK_URL` | (none) | Webhook fired when a task enters `awaiting_approval` |
| `MAINTENANCE_CHECK_ENABLED` | `false` | Query Nautobot before creating RCA tasks |
| `LAB_VALIDATION_ENABLED` | `false` | Apply fix to Containerlab device before production execution |
| `GITEA_TOKEN` | (none) | API token for the Gitea runbook library |
| `EXECUTION_VERIFY_DELAY` | `300` | Seconds before the post-execution Prometheus check |
| `CHAOS_TOOLS_ENABLED` | `false` | Enable chaos action tools (shutdown_interface, flap_bgp) |
| `LANGSMITH_API_KEY` | (none) | LangSmith tracing key (optional) |

## Local LLM Fallback (Ollama)

If no `OPENAI_API_KEY` is set, the agent falls back to a locally running Ollama instance:

```bash
docker compose exec ollama ollama pull llama3
```

Local models are significantly slower and less capable for complex multi-step reasoning. For reliable pipeline operation, an OpenAI API key is recommended.
