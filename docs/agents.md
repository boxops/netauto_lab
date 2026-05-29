# AI Agents

The stack includes three AI agents built with LangGraph (ReAct loop):

- **Ops Agent** — Incident investigation: correlates alerts, metrics, and syslogs into root-cause summaries
- **Engineering Agent** — Config design: generates vendor-specific configurations, plans IP space, writes Ansible playbooks
- **Chaos Agent** — Controlled experiments: blast-radius assessment, simulation-first chaos tests, fix validation

All three agents are wired into an **autonomous closed-loop pipeline** that triggers automatically when Prometheus fires an alert. See [`docs/closed-loop-pipeline.md`](closed-loop-pipeline.md) for the full pipeline reference.

## Accessing the Agents

| Interface | URL |
|-----------|-----|
| Web UI (all agents + pipeline dashboard) | http://localhost:7860 |
| Ops Agent REST API | http://localhost:8000 |
| Engineering Agent REST API | http://localhost:8001 |
| Chaos Agent REST API | http://localhost:8002 |

---

## Ops Agent

### Capabilities

| Capability | Description |
|------------|-------------|
| Alert investigation | Queries active Prometheus alerts, correlates with metrics and logs |
| Log correlation | Searches Loki for syslog events matching an alert timeframe |
| Device lookup | Queries Nautobot for device info, interfaces, neighbors |
| Root cause analysis | Synthesises a DIAGNOSIS / AFFECTED / ACTION / CONFIDENCE summary |
| Playbook execution | Runs Ansible playbooks (check mode by default) |

### Safety rules

- All Ansible executions default to `check_mode: true` (dry run).
- Destructive operations require explicit "yes, execute" confirmation in the prompt.

### Example prompts

```
"What alerts are currently firing?"
"Investigate the BGP peer down alert on spine2."
"Why is leaf1 showing high packet loss? Check logs and metrics."
"Generate a health report for all lab devices."
```

### REST API

```bash
# Chat
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Check BGP status on leaf1", "session_id": "ops-1"}'

# Health check
curl http://localhost:8000/health

# Live agent status
curl http://localhost:8000/status

# Token usage and cost
curl http://localhost:8000/usage

# Reset alert poller deduplication state
curl -X POST http://localhost:8000/poller/reset
```

---

## Engineering Agent

### Capabilities

| Capability | Description |
|------------|-------------|
| Config generation | Generates EOS/IOS/JunOS/SR-Linux device configs from requirements |
| IP planning | Finds available IPs from Nautobot IPAM prefixes |
| VLAN design | Plans VLAN allocations with full Nautobot context |
| Playbook authoring | Writes Ansible playbooks from natural-language descriptions |
| Config review | Reviews configs against best practices |
| Fix generation | Produces specific remediation commands in check mode (automated pipeline) |

### Safety rules

- `run_config_commands` always defaults to `check_mode=True`.
- IP or VLAN allocation that modifies Nautobot requires explicit confirmation.
- In the automated pipeline, the agent is instructed never to set `check_mode=False` — execution requires human approval via the approval gate.

### Example prompts

```
"Design a BGP configuration for a new leaf router with AS 65104."
"What IPs are available in the 10.10.0.0/16 prefix?"
"Generate an Ansible playbook that sets SNMPv3 credentials on all EOS devices."
"Find all devices and generate interface description standards for every uplink."
"Compare spine1's running config to its intended state in Nautobot."
```

### REST API

```bash
curl -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Design a leaf config for AS 65104", "session_id": "eng-1"}'

curl http://localhost:8001/health
curl http://localhost:8001/status
curl http://localhost:8001/usage
```

---

## Chaos Agent

### Capabilities

| Capability | Description |
|------------|-------------|
| Blast-radius assessment | Maps which devices and services depend on a target interface or device |
| Simulation-first experiments | Runs chaos actions with `check_mode=True` by default |
| Fix validation | Cross-checks Engineering Agent proposals for correctness and blast radius |
| Scheduled chaos | Runs chaos scenarios on a repeating interval via APScheduler |
| Rollback planning | Generates structured rollback procedures before any experiment |

### Safety rules

- `shutdown_interface`, `restore_interface`, `flap_bgp_neighbor` all default to `check_mode=True`.
- In the automated pipeline, the Chaos Agent only performs read-only validation (no config changes).
- Live execution of chaos actions requires the user to explicitly set `check_mode=False` in the prompt.

### Example prompts

```
"What is the blast radius if I shut down Ethernet1 on spine1?"
"Design a 15-minute game day for testing BGP reconvergence."
"Simulate a leaf uplink failure on leaf2 in check mode."
"Validate this fix: restore interface Ethernet2 on spine1."
"Create a rollback-first runbook for a dual-uplink failure test."
```

### REST API

```bash
curl -X POST http://localhost:8002/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Assess blast radius for shutting Ethernet1 on leaf1", "session_id": "chaos-1"}'

curl http://localhost:8002/health
curl http://localhost:8002/status
curl http://localhost:8002/usage

# Schedule management
curl http://localhost:8002/schedules
curl -X POST http://localhost:8002/schedule \
  -H "Content-Type: application/json" \
  -d '{"scenario": "Shut Ethernet1 on leaf1 in check mode", "interval_minutes": 30}'
curl -X DELETE http://localhost:8002/schedule/<job_id>
```

---

## Closed-Loop Automation Pipeline

When Prometheus fires an alert, the three agents automatically coordinate a four-stage response without any human intervention until the approval gate:

```
Alert → Ops Agent (RCA) → Eng Agent (Fix Proposal) → Chaos Agent (Validation) → Human (Approval Gate)
```

The full pipeline is described in [`docs/closed-loop-pipeline.md`](closed-loop-pipeline.md), including:

- How the AlertPoller detects and deduplicates alerts
- The structured output format each agent is expected to produce
- The routing logic that determines whether a fix goes to validation or straight to a human gate
- How to approve or reject a fix in the Pipeline Dashboard
- The task data model and SQLite schema
- How to extend the pipeline with new stages or task types

The **📊 Pipeline** tab in the web UI is the primary interface for monitoring and acting on the pipeline. It opens by default when you load [http://localhost:7860](http://localhost:7860).

---

## Configuration

Agent behaviour is controlled via environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (none) | GPT-4o key — required for OpenAI |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model name |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama endpoint (local LLM fallback) |
| `OLLAMA_MODEL` | `llama3` | Ollama model name |
| `DAILY_BUDGET_USD` | `5.00` | Hard daily spend limit across all agents |
| `MAX_TOKENS_PER_AGENT_PER_HOUR` | `2,000,000` | Hourly token cap per agent |
| `LANGSMITH_API_KEY` | (none) | LangSmith tracing key (optional) |
| `LANGSMITH_TRACING` | `false` | Enable LangSmith trace export |

## Local LLM Fallback (Ollama)

If no `OPENAI_API_KEY` is set, agents fall back to a locally running Ollama instance. Pull the model first:

```bash
docker compose exec ollama ollama pull llama3
```

Local models are significantly slower and less capable for complex multi-step reasoning tasks like RCA or fix generation. For reliable pipeline operation, an OpenAI API key is recommended.
