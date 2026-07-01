# AI Agent Reference

The **unified AI agent** (`ai-agent` service, port 8000) is a LangGraph ReAct loop that handles interactive chat, closed-loop incident response, and proactive monitoring. All background tasks (AlertPoller, IntentEvaluator, APScheduler, policy-promotion sweep) run inside this one service.

| Interface | URL |
|---|---|
| Clano Web UI (pipeline, incidents, chat, config, system) | http://localhost:7860 |
| AI Agent REST API | http://localhost:8000 |

---

## Capabilities

| Mode | Capability |
|---|---|
| **Alert-driven** | Root cause analysis · Topology blast-radius correlation · Runbook-first fix generation · Config diff · Fix validation · Execution (check_mode=False after approval) · Post-execution verification |
| **Intent-driven** | Proactive metric polling (before Alertmanager fires) · Alert suppression · Forced escalation · Chaos scheduling |
| **Chat-driven** | Device lookup · Config review · IP/VLAN planning · Playbook authoring · Fleet health reports |

---

## Tool Tier Model

The agent works **top-to-bottom** through the tiers. Skipping Tier 1 (inventory) before acting is the primary cause of incorrect responses.

```
┌──────────────────────────────────────────────────────────────┐
│  Tier 0 – Runbook    get_runbook(alertname)                   │
│  Known fix? Check first — returns YAML procedure if exists    │
├──────────────────────────────────────────────────────────────┤
│  Tier 1 – Discovery  Nautobot REST API                        │
│  What exists?        devices, interfaces, topology, IPs       │
├──────────────────────────────────────────────────────────────┤
│  Tier 2 – Metrics    Prometheus + Alertmanager                │
│  What is happening?  reachability, counters, BGP, alerts      │
├──────────────────────────────────────────────────────────────┤
│  Tier 3 – Logs       Loki (syslog)                            │
│  What happened?      interface events, BGP events, errors     │
├──────────────────────────────────────────────────────────────┤
│  Tier 4 – Actions    Nautobot Jobs + chaos tools              │
│  Change something    run_show_commands, run_config_commands   │
│  (requires approval) chaos tools (CHAOS_TOOLS_ENABLED)        │
└──────────────────────────────────────────────────────────────┘
```

---

## Tool Reference

### Tier 0 — Runbook

| Tool | When to use |
|---|---|
| `get_runbook(alertname)` | **First call** for any alert investigation — returns Gitea YAML runbook if one exists. Reduces token usage 60–80% for known alert types. |

Built-in runbooks: `BGPPeerDown`, `InterfaceDown`, `InterfaceAdminDown`, `DeviceDown`, `HighInterfaceUtilization`, `InterfaceHighErrorRate`, `BGPPrefixCountDecreased`. Add custom runbooks as `{AlertName}.yaml` in the Gitea `runbooks` repo.

### Tier 1 — Nautobot Discovery

| Tool | When to use | Key arg |
|---|---|---|
| `get_all_devices()` | First step for any multi-device task | — |
| `get_device_info(device)` | Full detail on one device | exact hostname |
| `get_device_interfaces(device)` | Interface list with neighbors and IPs | exact hostname |
| `get_topology()` | Full physical topology / blast-radius analysis | — |
| `get_connected_devices(device)` | Quick neighbor lookup | exact hostname |
| `get_vlans()` | VLAN inventory | — |
| `get_prefixes()` | Prefix/subnet inventory | — |
| `get_ip_addresses(device, prefix)` | IPs by device or within a prefix | optional filters |
| `get_available_ips(prefix, count)` | Find free IPs for allocation | prefix string |
| `search_nautobot(query)` | Keyword search across all object types | search term |
| `get_devices_by_location(location)` | All devices at one site | location name |

> **IP resolution:** Prometheus uses IPs, Nautobot uses hostnames. Tools auto-resolve `primary_ip4` from Nautobot before querying Prometheus.

### Tier 2 — Prometheus Metrics

| Tool | When to use |
|---|---|
| `get_active_alerts()` | Start of every incident investigation |
| `get_recent_alert_events(limit)` | Alert history including resolved |
| `get_device_metrics(device)` | Reachability, RTT, packet loss per device |
| `get_interface_metrics(device, interface)` | Interface traffic and error counters |
| `query_prometheus(promql, minutes)` | Custom PromQL for advanced queries |

### Tier 3 — Loki Logs

| Tool | Searches for |
|---|---|
| `get_interface_events(device, minutes)` | Link up/down, protocol changes |
| `get_bgp_events(device, minutes)` | BGP state transitions |
| `get_recent_errors(device, minutes)` | ERROR/WARNING/CRITICAL log lines |
| `query_logs(device, pattern, minutes)` | Arbitrary LogQL pattern |

### Tier 4 — Actions

| Tool | Notes |
|---|---|
| `run_show_commands(device, commands)` | Read-only; any show command |
| `run_config_commands(device, lines, check_mode)` | `check_mode=True` (default) = dry run only |
| `shutdown_interface(device, interface, check_mode)` | Chaos — requires `CHAOS_TOOLS_ENABLED=true` |
| `restore_interface(device, interface, check_mode)` | Chaos — requires `CHAOS_TOOLS_ENABLED=true` |
| `flap_bgp_neighbor(device, neighbor_ip, method, check_mode)` | Chaos — requires `CHAOS_TOOLS_ENABLED=true` |
| `verify_bgp_state(device, neighbor_ip)` | Read-only — always available |

**check_mode semantics:** `run_config_commands(check_mode=True)` never submits a job — it returns a `SIMULATION` JSON. Only `check_mode=False` submits the job and requires explicit user approval.

**Nautobot Jobs API flow:** resolve device → UUID → resolve job name → UUID → POST `/api/extras/jobs/{id}/run/` → poll `GET /api/extras/job-results/{id}/` (every 3 s, timeout 90–120 s) → fetch logs.

---

## Workflow Patterns

**Incident investigation**
```
get_active_alerts() → get_device_metrics(device) → get_interface_events(device, 60)
→ get_bgp_events(device, 60) → get_topology()
```

**Config design / new device**
```
get_all_devices() → get_topology() → get_device_interfaces(neighbor)
→ get_prefixes() + get_available_ips(prefix) → get_vlans()
```

**Inventory / documentation**
```
get_all_devices() → get_device_interfaces(device)  [for each]
→ get_topology()  [for full connection map]
```

**Chaos experiment**
```
# Before: get_topology() + get_device_metrics(target) + get_active_alerts()
# After:  get_active_alerts() + get_interface_events(device) + get_bgp_events(device) + get_device_metrics(device)
```

**Health report**
```
get_all_devices() → get_device_metrics(device)  [for each]
→ get_active_alerts() → get_recent_alert_events(50) → get_recent_errors(60)
```

---

## Adding a New Tool

1. Implement as `@tool`-decorated function in `ai-agents/shared/tools.py`.
2. Add to the appropriate tier list (`_NAUTOBOT_TOOLS`, `_PROMETHEUS_TOOLS`, `_LOKI_TOOLS`, `_ACTION_TOOLS`).
3. Update the tool guide in `shared/unified_agent.py` and, if pipeline-relevant, `ops_agent/agent.py`.
4. Rebuild: `make rebuild SVC=ai-agent`.

**Docstring template:**
```python
@tool
def my_new_tool(required_arg: str, optional_arg: str = "") -> str:
    """
    One sentence: what this tool returns.

    When to use: describe the scenario. Note what other tool to use instead
    when this one is not appropriate.

    Args:
        required_arg: What it is, valid values (e.g., 'leaf1', 'spine2').
        optional_arg: What it controls. Leave empty to <default behaviour>.

    Returns:
        JSON with <describe the structure>.
    """
```

**Tool design rules:** Return JSON always (errors as `{"error": "..."}`, never exceptions). Return names not IDs. Return helpful `{"note": "..."}` when data is absent. Limit list results to 50–200 items. Include `available_devices` in error responses so the agent can self-correct.

---

## Standing Intents

Standing intents control *when* the agent acts and *what it does* with specific alert types — independently of Prometheus. Managed in **⚙️ Config → Standing Intents** at http://localhost:7860/config.

| Type | Effect | When to use |
|---|---|---|
| `suppress` | Skip pipeline investigation for matching alerts | Known-flapping link, planned maintenance |
| `escalate` | Force the approval gate regardless of policy autonomy level | Production devices where auto-execution must never happen |
| `monitor` | Poll a Prometheus metric on a schedule; open an RCA when threshold is breached | Detect degraded state *before* Alertmanager fires |
| `chaos_schedule` | Run a chaos scenario on a cron expression | Regular resilience testing |

**Evaluation:** Alert-driven intents (`suppress`, `escalate`) are checked by the AlertPoller before any task is created. Both `device` and `alertname` fields must match (empty = wildcard). The `monitor` type runs via `IntentEvaluator` (5-min poll). Chaos schedules are handed to APScheduler.

**Threshold syntax** (for `monitor` intents): `<operator> <value>` — e.g. `< 1`, `>= 95`, `== 0`. Operators: `<`, `<=`, `>`, `>=`, `==`, `!=`. The PromQL query should return a single scalar.

**Deduplication:** Intent-triggered pipelines use `alert_fingerprint = "intent:<intent_id>"` — so a persisting threshold breach only creates one pipeline, not one per poll cycle.

```bash
# Examples via API
# Suppress all InterfaceDown alerts on leaf1
curl -X POST http://localhost:7860/partials/intent-create \
  -d "name=suppress+leaf1+flap&intent_type=suppress&device=leaf1&alertname=InterfaceDown"

# Monitor BGP prefix count — open RCA if drops below 5
curl -X POST http://localhost:7860/partials/intent-create \
  -d "name=BGP+prefix+monitor&intent_type=monitor&device=leaf1&metric_query=bgp_prefixes_received%7Bdevice%3D%27leaf1%27%7D&threshold=%3C+5"
```

---

## REST API

```bash
# Chat
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Check BGP status on leaf1", "session_id": "clano-1"}'

# Streaming chat
curl -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "Investigate BGP peer down on spine2", "session_id": "clano-1"}'

# Health / status / usage
curl http://localhost:8000/health
curl http://localhost:8000/status
curl http://localhost:8000/usage

# Tasks
curl http://localhost:8000/tasks
curl http://localhost:8000/tasks/<task_id>
curl -X POST http://localhost:8000/workflow/resume/<task_id>   # after human approval

# Policies and intents
curl http://localhost:8000/policies
curl -X POST http://localhost:8000/policies -H "Content-Type: application/json" -d '{...}'
curl http://localhost:8000/intents
curl -X POST http://localhost:8000/intents -H "Content-Type: application/json" -d '{...}'

# Chaos schedules
curl http://localhost:8000/schedules
curl -X POST http://localhost:8000/schedule \
  -H "Content-Type: application/json" \
  -d '{"scenario": "Shut Ethernet1 on leaf1 in check mode", "interval_minutes": 30}'
curl -X DELETE http://localhost:8000/schedule/<job_id>

# Reset alert poller dedup state
curl -X POST http://localhost:8000/poller/reset
```

---

## Example Prompts

**Investigation**
```
"What alerts are currently firing?"
"Investigate the BGP peer down alert on spine2."
"Why is leaf1 showing high packet loss? Check logs and metrics."
"Generate a health report for all lab devices."
```

**Design and config**
```
"Design a BGP configuration for a new leaf router with AS 65104."
"What IPs are available in the 10.10.0.0/16 prefix?"
"Generate an Ansible playbook that sets SNMPv3 credentials on all EOS devices."
"Compare spine1's running config to its intended state in Nautobot."
```

**Chaos and validation**
```
"What is the blast radius if I shut down Ethernet1 on spine1?"
"Simulate a leaf uplink failure on leaf2 in check mode."
"Validate this fix: restore interface Ethernet2 on spine1."
"Design a 15-minute game day for testing BGP reconvergence."
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | GPT-4o key; falls back to Ollama if unset |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model name |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama endpoint (local LLM fallback) |
| `OLLAMA_MODEL` | `llama3` | Ollama model name |
| `AI_ENABLED` | `true` | `false` = only fast-path policies run; unmatched alerts queue for human review |
| `DAILY_BUDGET_USD` | `5.00` | Hard daily spend limit |
| `MAX_TOKENS_PER_AGENT_PER_HOUR` | `2,000,000` | Hourly token cap |
| `CHAOS_TOOLS_ENABLED` | `false` | Enable chaos action tools (lab only) |
| `LANGSMITH_API_KEY` | — | LangSmith tracing (optional) |

If no `OPENAI_API_KEY` is set, pull an Ollama model first:
```bash
docker compose exec ollama ollama pull llama3
```
Local models are significantly slower and less capable for multi-step reasoning; an OpenAI key is recommended for reliable pipeline operation.
