# AI Agent Tools Framework

This document describes the tool architecture for the network automation AI agent, the data model
behind each tier, and the workflow patterns the agent is expected to follow.

---

## Overview

The unified AI agent is a LangGraph ReAct agent backed by an LLM. It decides which tools to call
and in what order based on the user's request and the guidance in its system prompt. Tools are the
only mechanism through which the agent can access live data — the LLM itself has no network access.

### The Five-Tier Model

```
┌────────────────────────────────────────────────────────────┐
│  Tier 0 – Runbook      Gitea runbook library               │
│  Known fix?            get_runbook(alertname)              │
│  Check first.          Returns YAML procedure if exists    │
├────────────────────────────────────────────────────────────┤
│  Tier 1 – Discovery    Nautobot REST API                   │
│  What exists?          devices, interfaces, topology,      │
│                        VLANs, prefixes, IP addresses       │
├────────────────────────────────────────────────────────────┤
│  Tier 2 – Metrics      Prometheus + Alertmanager           │
│  What is happening     reachability, interface counters,   │
│  right now?            BGP state, active alerts            │
├────────────────────────────────────────────────────────────┤
│  Tier 3 – Logs         Loki (syslog aggregation)           │
│  What happened?        interface events, BGP events,       │
│                        error/warning messages              │
├────────────────────────────────────────────────────────────┤
│  Tier 4 – Actions      Nautobot Jobs + chaos tools         │
│  Change something      run_show_commands (Commands Runner) │
│  (requires approval)   run_config_commands (Deploy Config) │
│                        chaos tools (CHAOS_TOOLS_ENABLED)   │
└────────────────────────────────────────────────────────────┘
```

The agent works top-to-bottom through the tiers: check for a known runbook, then discover what
exists, measure current state, investigate event history, and finally act. Skipping Tier 1 —
reaching for metrics or actions without first grounding in inventory data — is the primary cause
of poor agent responses.

---

## Data Sources

### Nautobot (Source of Truth)

- **Endpoint base**: `http://nautobot:8080/api/`
- **Auth**: Token in `Authorization` header
- **Key models**: `dcim/devices/`, `dcim/interfaces/`, `dcim/cables/`, `ipam/prefixes/`,
  `ipam/vlans/`, `ipam/ip-addresses/`
- **Depth parameter**: Use `depth=1` for single-level expansion of foreign keys. Higher depths
  return large payloads and are avoided.
- **Connected endpoints**: At `depth=1`, `connected_endpoint` on an interface contains a
  `natural_slug` field in the format `device-name__location-slug__interface-name_shortid`.
  The device name is extracted as `natural_slug.split("__")[0]`.

### Prometheus

- **Endpoint**: `http://prometheus:9090/api/v1/`
- **Key metric families**:
  | Metric prefix | Source | Key labels |
  |---|---|---|
  | `ping_*` | Telegraf ICMP probes | `url` (device primary IP) |
  | `interface_if*` | Telegraf SNMP polling | `agent_host` (device IP), `ifDescr` |
  | `bgp_peer_bgpPeer*` | Telegraf SNMP polling | `agent_host`, `bgpPeerRemoteAddr` |
  | `up` | Prometheus scrape health | `instance`, `job` |

- **IP resolution**: Device primary IPs are retrieved from Nautobot before querying
  Prometheus, since Prometheus labels use IPs not hostnames.
- **SNMP metrics** (`interface_if*`, `bgp_peer_*`) require active SNMP polling via Telegraf.
  When the lab network is down or Telegraf is not polling, these series return 0 results —
  tools handle this gracefully.

### Loki

- **Endpoint**: `http://loki:3100/loki/api/v1/`
- **Log stream**: `{job="syslog"}`, optionally with `device="hostname"` label
- **Query language**: LogQL — tools use `|~` for case-insensitive regex matching
- **Retention**: Logs are available for the configured retention period (typically 30 days)

### Nautobot Jobs (Execution Engine)

Actions are executed by submitting jobs to the Nautobot Celery queue. The two jobs used by agents:

| Job display name               | Class                  | Purpose                                                                                |
| ------------------------------ | ---------------------- | -------------------------------------------------------------------------------------- |
| `Commands Runner`              | `CommandRunner`        | Read-only show commands (`is_config=False`) or config-mode commands (`is_config=True`) |
| `Deploy Device Configurations` | `DeployConfigurations` | Push arbitrary configuration blocks to devices                                         |

**API flow for every action tool:**

```
1. Resolve device hostname → UUID
   GET /api/dcim/devices/?name=leaf1 → results[0].id

2. Resolve job name → UUID (cached after first call)
   GET /api/extras/jobs/?limit=200 → find matching name → id

3. Trigger the job
   POST /api/extras/jobs/{job_id}/run/
   {"data": {"device": ["<device_uuid>"], "commands": "...", "is_config": false}}
   → response.job_result.id

4. Poll until terminal state (SUCCESS / FAILURE / ERRORED)
   GET /api/extras/job-results/{result_id}/
   → status.value  (poll every 3 s, default timeout 90–120 s)

5. Fetch log entries (command output lives here)
   GET /api/extras/job-results/{result_id}/logs/
   → list of {log_level, message, grouping, created}
```

**check_mode semantics:**  
`run_config_commands(check_mode=True)` never submits a job — it returns a `SIMULATION` JSON describing what would be sent. Call `run_show_commands()` separately to capture current state. Only `check_mode=False` submits the `Deploy Device Configurations` job and requires explicit user approval.

---

## Tool Reference

### Tier 0 — Runbook Library

| Tool                       | When to use                                             | Key argument      |
| -------------------------- | ------------------------------------------------------- | ----------------- |
| `get_runbook(alertname)`   | **First tool call** for any pipeline alert investigation | exact alert name  |

Returns the YAML runbook from the Gitea `runbooks` repository if one exists for the alert name.
When a runbook is found, the agent follows its prescribed steps rather than re-deriving the fix
from first principles. This reduces token usage by 60–80% for known alert types. Custom runbooks
can be added to the Gitea `runbooks` repository as `{AlertName}.yaml` files.

### Tier 1 — Nautobot Discovery

| Tool                                     | When to use                                    | Key argument     |
| ---------------------------------------- | ---------------------------------------------- | ---------------- |
| `get_all_devices()`                      | First step for any multi-device task           | —                |
| `get_device_info(device_name)`           | Full detail on one known device                | exact hostname   |
| `get_device_interfaces(device_name)`     | Interface list with neighbors and IPs          | exact hostname   |
| `get_topology()`                         | Full physical topology / blast-radius analysis | —                |
| `get_connected_devices(device_name)`     | Quick neighbor lookup                          | exact hostname   |
| `get_vlans()`                            | VLAN inventory                                 | —                |
| `get_prefixes()`                         | Prefix/subnet inventory                        | —                |
| `get_ip_addresses(device_name, prefix)`  | IPs by device or within a prefix               | optional filters |
| `get_available_ips(prefix, count)`       | Find free IPs for allocation                   | prefix string    |
| `search_nautobot(query)`                 | Keyword search across all object types         | search term      |
| `get_devices_by_location(location_name)` | All devices at one site                        | location name    |

### Tier 2 — Prometheus Metrics

| Tool                                                 | When to use                               |
| ---------------------------------------------------- | ----------------------------------------- |
| `get_active_alerts()`                                | Start of every incident investigation     |
| `get_recent_alert_events(limit)`                     | Alert history including resolved          |
| `get_device_metrics(device_name)`                    | Reachability, RTT, packet loss per device |
| `get_interface_metrics(device_name, interface_name)` | Interface traffic and error counters      |
| `query_prometheus(promql, minutes)`                  | Custom PromQL for advanced queries        |

### Tier 3 — Loki Logs

| Tool                                         | Searches for                            |
| -------------------------------------------- | --------------------------------------- |
| `get_interface_events(device_name, minutes)` | Link up/down, protocol changes          |
| `get_bgp_events(device_name, minutes)`       | BGP state transitions, Established/Idle |
| `get_recent_errors(device_name, minutes)`    | ERROR/WARNING/CRITICAL log lines        |
| `query_logs(device, pattern, minutes)`       | Arbitrary log pattern (LogQL)           |

### Tier 4 — Actions (Nautobot Jobs + Chaos Tools)

**Standard action tools** (always available):

All standard action tools submit jobs to the Nautobot Celery queue and poll for completion.
Output is read from `GET /api/extras/job-results/{id}/logs/`.

| Tool                                                         | Nautobot Job                        | Notes                               |
| ------------------------------------------------------------ | ----------------------------------- | ----------------------------------- |
| `run_show_commands(device_name, commands)`                   | Commands Runner (`is_config=False`) | Read-only; any show command         |
| `run_config_commands(device_name, config_lines, check_mode)` | Deploy Device Configurations        | check_mode=True (default) = dry run |

**Chaos tools** (available only when `CHAOS_TOOLS_ENABLED=true`):

Chaos tools are destructive by default and intended for lab environments only. When
`CHAOS_TOOLS_ENABLED=false` (the default), `verify_bgp_state` is the only chaos tool exposed.

| Tool                                                         | Nautobot Job                        | Notes                                                    |
| ------------------------------------------------------------ | ----------------------------------- | -------------------------------------------------------- |
| `shutdown_interface(device, interface, check_mode)`          | Deploy Device Configurations        | Admin-shut; check=True (default) shows current state     |
| `restore_interface(device, interface, check_mode)`           | Deploy Device Configurations        | No shutdown — restores interface to service              |
| `flap_bgp_neighbor(device, neighbor_ip, method, check_mode)` | Commands Runner (`is_config=True`)  | Clears BGP session; check=True does not disrupt session  |
| `verify_bgp_state(device, neighbor_ip)`                      | Commands Runner (`is_config=False`) | Read-only — confirms BGP peer is Established             |

---

## Workflow Patterns

### Pattern 1: Inventory + Interface Documentation

**Trigger**: "Find all devices", "List interfaces", "Generate interface descriptions"

```
get_all_devices()
  → for each device: get_device_interfaces(device_name)
  → get_topology()  [for a full connection map]
```

`get_device_interfaces` returns `connected_to.device` and `connected_to.interface`, which are
the primary inputs for generating standardised descriptions like `"Uplink to spine1:Ethernet2"`.

---

### Pattern 2: Incident Investigation

**Trigger**: "What alerts are firing?", "Why is X down?", "Investigate BGP peer down"

```
get_active_alerts()
  → identify affected device from alert labels
get_device_metrics(device_name)
  → confirm reachability, check interface oper status
get_interface_events(device_name, minutes=60)
  → look for link flap events preceding the alert
get_bgp_events(device_name, minutes=60)
  → look for BGP reconvergence or session loss
get_device_interfaces(device_name) + get_topology()
  → understand which services and peers are affected
```

Correlate timestamps across all three tiers to build a timeline before recommending action.

---

### Pattern 3: Configuration Design

**Trigger**: "Design BGP config", "Generate playbook", "Add a new leaf"

```
get_all_devices()
  → understand existing device names and AS numbers in use
get_topology()
  → understand which devices the new leaf will connect to
get_device_interfaces(neighbor_device)
  → find the right peer interfaces and IPs
get_prefixes() + get_available_ips(prefix)
  → allocate management and loopback IPs
get_vlans()
  → reference existing VLANs in the config
```

Ground every generated config in real Nautobot data. Never invent IP addresses or device
names — always query first.

---

### Pattern 4: Chaos Experiment Design

**Trigger**: "Design a chaos test", "What happens if spine1 fails?", "Blast radius of Ethernet1"

```
get_topology()
  → map ALL connections to/from the target device or link
get_device_interfaces(target_device)
  → get exact interface names for shutdown_interface calls
get_all_devices()
  → identify which devices depend on the target (leaves behind spines, etc.)
get_active_alerts()
  → document the pre-experiment baseline state
get_device_metrics(target_device)
  → confirm device is reachable before disruption
```

After the experiment:

```
get_active_alerts()             → verify expected alerts fired
get_interface_events(device)    → observe syslog event timeline
get_bgp_events(device)          → observe routing reconvergence
get_device_metrics(device)      → verify recovery after restore
```

---

### Pattern 5: Health Report

**Trigger**: "Network health report", "Status of all devices"

```
get_all_devices()
  → full device inventory with status field
  → for each device: get_device_metrics(device_name)
get_active_alerts()
  → list any firing alerts
get_recent_alert_events(limit=50)
  → recent alert history for trend context
get_recent_errors(minutes=60)
  → any error-level syslog entries
```

---

## Agent Tool Set

The unified AI agent has access to all tools from `OPS_TOOLS` (the full set defined in
`shared/tools.py`) plus the chaos tools from `ops_agent/chaos_tools.py` (gated by
`CHAOS_TOOLS_ENABLED`).

| Tool                    | Tier | Notes                                          |
| ----------------------- | :--: | ---------------------------------------------- |
| `get_runbook`           |  0   | Always called first for alert investigations   |
| `get_all_devices`       |  1   |                                                |
| `get_device_info`       |  1   |                                                |
| `get_device_interfaces` |  1   |                                                |
| `get_topology`          |  1   |                                                |
| `get_connected_devices` |  1   |                                                |
| `get_vlans`             |  1   |                                                |
| `get_prefixes`          |  1   |                                                |
| `get_ip_addresses`      |  1   |                                                |
| `get_available_ips`     |  1   |                                                |
| `search_nautobot`       |  1   |                                                |
| `get_devices_by_location` | 1  |                                                |
| `get_active_alerts`     |  2   |                                                |
| `get_recent_alert_events` | 2  |                                                |
| `get_device_metrics`    |  2   |                                                |
| `get_interface_metrics` |  2   |                                                |
| `query_prometheus`      |  2   |                                                |
| `get_interface_events`  |  3   |                                                |
| `get_bgp_events`        |  3   |                                                |
| `get_recent_errors`     |  3   |                                                |
| `query_logs`            |  3   |                                                |
| `run_show_commands`     |  4   |                                                |
| `run_config_commands`   |  4   | check_mode=True by default                     |
| `shutdown_interface`    |  4   | Chaos — requires `CHAOS_TOOLS_ENABLED=true`    |
| `restore_interface`     |  4   | Chaos — requires `CHAOS_TOOLS_ENABLED=true`    |
| `flap_bgp_neighbor`     |  4   | Chaos — requires `CHAOS_TOOLS_ENABLED=true`    |
| `verify_bgp_state`      |  4   | Read-only — always available                   |

---

## Adding New Tools

1. Implement the function in `ai-agents/shared/tools.py` decorated with `@tool`.
2. Write a docstring that explains **what the tool returns**, **when to use it vs. similar tools**,
   and **what each argument means** with an example value.
3. Add the tool to the appropriate tier list (`_NAUTOBOT_TOOLS`, `_PROMETHEUS_TOOLS`,
   `_LOKI_TOOLS`, `_ACTION_TOOLS`) — `OPS_TOOLS` is automatically assembled from these.
4. Update the system prompt in `ai-agents/ops_agent/agent.py` — add the tool to the Tool Guide
   section and to any relevant Workflow Patterns.
5. Update this document: add a row to the Tool Reference table and the Agent Tool Set table.
6. Rebuild the agent container: `make rebuild SVC=ai-agent`.

### Docstring Template

```python
@tool
def my_new_tool(required_arg: str, optional_arg: str = "") -> str:
    """
    One sentence: what this tool returns.

    When to use: describe the scenario. Note what other tool to use instead
    when this one is not appropriate.

    Args:
        required_arg: What it is, valid values or format (e.g., 'leaf1', 'spine2').
        optional_arg: What it controls. Leave empty to <default behaviour>.

    Returns:
        JSON with <describe the structure>.
    """
```

### Design Principles

- **Return JSON always.** Wrap errors as `{"error": "message"}` — never raise exceptions to the agent.
- **Return names, not IDs.** The LLM reads names; UUIDs add noise. Extract human-readable fields.
- **Handle empty gracefully.** When data is absent, return a helpful `{"note": "..."}` rather than an empty list the agent cannot reason about.
- **Keep responses compact.** Limit list results (50–200 items max). The LLM context window is finite.
- **Suggest next steps on failure.** When a device is not found, include `available_devices` in the error response so the agent can self-correct.

---

## Prometheus Label Mapping

Since Prometheus labels use IP addresses and Nautobot uses hostnames, tools resolve the primary IP
from Nautobot before querying Prometheus:

```python
dev = _nautobot_get("dcim/devices/", {"name": device_name})
primary_ip = dev["results"][0]["primary_ip4"]["address"].split("/")[0]
# → use primary_ip in Prometheus label filters: {url="172.20.20.11"}
```

If a device has no primary IP in Nautobot, the tool returns a clear note rather than silently
returning empty results.

---

## Nautobot Connected Endpoint Encoding

At `depth=1`, the `connected_endpoint` field on a Nautobot interface returns a partial object:

```json
{
  "display": "Ethernet1",
  "natural_slug": "spine1__site-lab_north-america__ethernet1_9fac"
}
```

The connected device name is extracted from the slug:

```python
device_name = natural_slug.split("__")[0]   # → "spine1"
interface_name = endpoint.get("display")     # → "Ethernet1"
```

This is handled in `_device_name_from_slug()` in `shared/tools.py`. If the slug format ever
changes, this helper is the single place to update.
