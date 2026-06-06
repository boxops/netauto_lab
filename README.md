# 📡 Clano — Closed-Loop Autonomous Network Ops

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![LangGraph](https://img.shields.io/badge/LangGraph-ReAct-FF6B35?logo=langchain&logoColor=white)](https://github.com/langchain-ai/langgraph)
[![Nautobot](https://img.shields.io/badge/Nautobot-3.x-00C389?logo=data:image/svg+xml;base64,)](https://nautobot.readthedocs.io/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A fully containerised, closed-loop autonomous network operations platform. When Prometheus fires an alert, a unified LangGraph AI agent automatically investigates, proposes a fix, validates it, and routes to a human-reviewed or policy-approved execution gate — all without manual intervention until you want it.

---

## ✨ What's Inside

| Source of Truth | Observability | AI / Automation |
| --- | --- | --- |
| Nautobot DCIM/IPAM | Prometheus + Alertmanager | Unified LangGraph agent (L0–L5 autonomy) |
| Gitea (runbook library) | Grafana (4 dashboards) | PolicyRegistry + LearningEngine |
| Containerlab cEOS lab | Loki + Promtail (syslog) | Intent layer (suppress / escalate / monitor / chaos) |
| Ansible automation | Telegraf (SNMP + ICMP) | Clano UI — FastAPI + HTMX (port 7860) |

---

## 🏗️ Architecture

```
                        ┌──────────────────────────────────┐
                        │          Browser / API           │
                        └──────┬───────────────┬───────────┘
                               │               │
                  ┌────────────▼──┐   ┌────────▼─────────────────────┐
                  │  Nautobot     │   │  Clano UI  :7860             │
                  │  :8080        │   │  ┌───────────────────────┐   │
                  │  DCIM · IPAM  │   │  │ Unified AI Agent :8000│   │
                  │  Golden Cfg   │   │  │  · PolicyRegistry     │   │
                  └────────┬──────┘   │  │  · LearningEngine     │   │
                           │          │  │  · IntentRegistry     │   │
                  ┌────────▼──────┐   │  │  · TopologyCorrelator │   │
                  │  PostgreSQL   │   │  └───────────────────────┘   │
                  │  Redis        │   └──────────────┬───────────────┘
                  └───────────────┘                  │ queries
                                       ┌─────────────▼─────────────┐
                  ┌────────────────┐   │   Prometheus  :9090        │
                  │  Gitea  :3001  │   │   Alertmanager :9093       │
                  │  Runbook lib   │   │   Grafana      :3000       │
                  └────────────────┘   └─────────────┬─────────────┘
                                                     │ scrapes
                  ┌────────────────┐   ┌─────────────▼─────────────┐
                  │  Ansible       │   │  Telegraf (SNMP + ICMP)   │
                  │  RabbitMQ      │   │  Loki + Promtail (syslog) │
                  └────────────────┘   └─────────────┬─────────────┘
                                                     │
                                       ┌─────────────▼─────────────┐
                                       │   Containerlab :172.20.20  │
                                       │   spine1, spine2           │
                                       │   leaf1, leaf2, leaf3      │
                                       │   client1, client2 (cEOS)  │
                                       └────────────────────────────┘
```

---

## 🚀 Quick Start

```bash
# 1. Clone
git clone https://github.com/boxops/netauto_lab && cd netauto_lab

# 2. One-command setup (installs Docker if needed, generates .env, starts all services)
bash setup.sh

# 3. Verify all services are healthy
make health-check

# 4. (Optional) Deploy the virtual spine-leaf network
make deploy-lab

# 5. (Optional) Register lab devices in Nautobot
make sync-inventory
```

Open the Clano UI → **http://localhost:7860**

---

## 🖥️ Services

| Service | URL | Default Credentials |
| --- | --- | --- |
| **Clano UI** | http://localhost:7860 | — |
| Nautobot | http://localhost:8080 | admin / see `.env` |
| Grafana | http://localhost:3000 | admin / see `.env` |
| Prometheus | http://localhost:9090 | — |
| Alertmanager | http://localhost:9093 | — |
| Loki | http://localhost:3100 | — |
| Gitea | http://localhost:3001 | gitadmin / see `.env` |
| AI Agent API | http://localhost:8000 | — |

---

## 🤖 Unified AI Agent

A single **LangGraph ReAct agent** (`ai-agent` service, port 8000) handles the full incident-response cycle — RCA, fix generation, validation, and execution — within one LangGraph state machine. All background tasks (AlertPoller, IntentEvaluator, APScheduler, policy-promotion sweep) run inside this single service.

### Four-Tier Tool Model

```
Tier 0 — Runbook      Gitea        get_runbook(alertname)  ← check first for known alerts

Tier 1 — Discovery    Nautobot     get_all_devices · get_device_interfaces · get_topology
                                   get_vlans · get_prefixes · get_ip_addresses · …

Tier 2 — Metrics      Prometheus   get_device_metrics · get_interface_metrics
                                   get_active_alerts · query_prometheus · …

Tier 3 — Logs         Loki         get_interface_events · get_bgp_events
                                   get_recent_errors · query_logs

Tier 4 — Actions      Config       run_show_commands · run_config_commands (check_mode=True)
          (gated)     Chaos        shutdown_interface · restore_interface · flap_bgp_neighbor
```

The agent works top-to-bottom: discover what exists → measure current state → investigate event history → act. See [`docs/agent-tools-framework.md`](docs/agent-tools-framework.md).

### Shared Subsystems

| Module | Purpose |
| --- | --- |
| `PolicyRegistry` | Matches pipeline actions to L0–L5 autonomy policies; determines whether a fix needs human approval or can auto-execute |
| `LearningEngine` | Auto-promotes policies after N consecutive successful resolutions; demotes after failure |
| `IntentRegistry` + `IntentEvaluator` | Standing policies for proactive monitoring, alert suppression, forced escalation, and chaos scheduling |
| `TopologyCorrelator` | Builds an adjacency graph from Nautobot cable data for blast-radius calculation and cascading-failure root-cause inference |

---

## 🔄 Closed-Loop Pipeline

When Prometheus fires an alert, the pipeline runs automatically:

```
Prometheus alert (or IntentEvaluator threshold breach)
        │
        ▼
  AlertPoller
  · Deduplicates by fingerprint
  · Checks maintenance status (Nautobot)
  · TopologyCorrelator: is this a cascade from an upstream device?
  · Creates / links an Incident entity
        │  creates rca task
        ▼
  ┌─── LangGraph IncidentWorkflow ──────────────────────────────────────┐
  │                                                                      │
  │  Stage 1 · RCA         Stage 2 · Fix Proposal  Stage 3 · Validate   │
  │  alerts + metrics      get_runbook() first      blast-radius check   │
  │  logs + topology       config diff generated    read-only inspection │
  │                                                                      │
  │  Stage 4 · Approval Gate                                             │
  │  PolicyRegistry → L0–L5 decision                                     │
  │  L0–L3: human reviews and approves in Clano UI                       │
  │  L4: auto-approved when thresholds met (confidence, risk, successes) │
  │  Post-approval: execute → config verify → alert resolution check     │
  └──────────────────────────────────────────────────────────────────────┘
```

See [`docs/closed-loop-pipeline.md`](docs/closed-loop-pipeline.md) for the full reference.

---

## 🎚️ Graduated Autonomy (L0–L5)

Every fix type can be assigned a different autonomy level based on risk, device role, and environment. The level determines what happens at the approval gate:

| Level | Name | What happens |
| --- | --- | --- |
| L0 | Manual | Agent reports only — humans take all action |
| L1 | Advisory | Agent surfaces diagnosis; human decides |
| L2 | Supervised | Agent stages fix and waits at gate **(default)** |
| L3 | Human gate | Gate always shown; executes immediately on approval |
| L4 | Auto-approve | Gate auto-approved when policy thresholds are met |
| L5 | Autonomous | Executes and notifies — requires explicit operator configuration |

The **LearningEngine** automatically promotes policies (after 3 consecutive successful resolutions) and demotes them (on failure), so the system earns autonomy incrementally rather than being granted it upfront.

Policies are managed in the **⚙️ Config** tab. See [`docs/policy-autonomy.md`](docs/policy-autonomy.md).

---

## 🎯 Standing Intents

Intents are persistent declarations about how the network should behave, evaluated independently of Prometheus alerts:

| Type | Effect |
| --- | --- |
| `suppress` | Skip investigation for matching alerts (known-flapping link, maintenance window) |
| `escalate` | Force human approval regardless of policy autonomy level |
| `monitor` | Proactively poll a Prometheus metric and open an RCA when a threshold is breached |
| `chaos_schedule` | Run a chaos scenario on a cron expression |

Intents are managed in the **⚙️ Config** tab. See [`docs/intent-layer.md`](docs/intent-layer.md).

---

## 🖥️ Clano UI

Five-tab web interface at [http://localhost:7860](http://localhost:7860):

| Tab | Contents |
| --- | --- |
| **📡 Operations** | Ops Health KPI bar · Alert Processing Pipeline (Visual + Chronicle) · Task Queue |
| **🚨 Incidents** | Incidents grouped by root cause, severity, and affected devices |
| **💬 Assist** | Interactive chat with the unified agent |
| **⚙️ Config** | Autonomy policies · L0–L5 gauge · Policy performance · Standing intents · UI preferences |
| **📊 System** | Cost Monitor (token usage, budget burn) · Activity Log (full audit trail) |

**Pipeline Chronicle**: each alert chain renders as a human-readable vertical timeline — Intent-triggered or Alert-triggered badge, stage chapters with autonomy level, blast-radius block, runbook provenance, rejection reason capture, and a Stage 5 Verify chapter showing config confirmation and alert resolution side-by-side.

Real-time updates via **Server-Sent Events** — a single `/stream/tasks` connection drives all live widgets with no polling lag.

---

## 📈 Observability

Four pre-built Grafana dashboards at [http://localhost:3000](http://localhost:3000):

| Dashboard | What it shows |
| --- | --- |
| **Network Overview** | Fleet health, interface utilisation, active alert count, BGP peer state |
| **Device Detail** | Per-device CPU, traffic, BGP peers, recent syslogs |
| **Interface Analytics** | Traffic rates, error counters, CRC errors, utilisation heatmap |
| **BGP Monitoring** | Per-peer session state, prefix counts, reconvergence events |

Prometheus scrapes **Telegraf** (SNMP polling + ICMP probes); **Loki** aggregates syslog from all Containerlab devices via **Promtail**.

---

## 🔬 Lab Topology

Virtual spine-leaf network in **Containerlab** using Arista cEOS:

```
      spine1 (AS 65001)     spine2 (AS 65002)
         │   │                   │   │
   ┌─────┘   └──────┐    ┌───────┘   └────┐
   ▼                ▼    ▼                ▼
 leaf1           leaf2            leaf3
 (AS 65101)     (AS 65102)      (AS 65103)
   │                │
 client1          client2
```

```bash
make deploy-lab       # Deploy the virtual topology
make sync-inventory   # Register devices in Nautobot
make destroy-lab      # Tear down the virtual topology
```

---

## 🛠️ Makefile Reference

```bash
# Lifecycle
make start                    # Start all services
make stop                     # Stop all services
make restart SVC=grafana      # Restart one service
make rebuild SVC=agent-ui     # Rebuild image + restart one service
make rebuild                  # Rebuild all images + restart
make status                   # Show container status
make logs SVC=agent-ui        # Tail logs for a service
make health-check             # Full health check

# Lab
make deploy-lab               # Deploy Containerlab topology
make destroy-lab              # Tear down topology
make sync-inventory           # Sync devices to Nautobot

# Nautobot data
make apply-data               # Apply data from nautobot/data_loader/data.yml
make plan-data                # Dry-run data reconciliation

# Ansible
make ansible-shell            # Open interactive Ansible container shell
make run-playbook             # Run a playbook interactively

# AI Agent
make agent-chat               # CLI chat with the AI agent

# Tests
make test                     # Run all tests
```

---

## 📁 Repository Structure

```
.
├── ai-agents/
│   ├── main.py                     # Unified FastAPI entry point (:8000) — all pipeline logic
│   ├── shared/
│   │   ├── unified_agent.py        # LangGraph ReAct agent (all pipeline roles)
│   │   ├── tools.py                # Full tool library (Tier 0–4)
│   │   ├── policy_registry.py      # L0–L5 autonomy policy matching
│   │   ├── learning_engine.py      # Auto-promotion/demotion of policies
│   │   ├── intent_registry.py      # Standing intents + IntentEvaluator
│   │   ├── topology_correlator.py  # Blast-radius + root-cause inference
│   │   ├── task_store.py           # Shared SQLite/PostgreSQL task queue (7 tables)
│   │   ├── pipeline_models.py      # Pydantic models for structured LLM outputs
│   │   ├── activity_store.py       # Chat interaction log
│   │   └── config.py               # Settings (pydantic-settings, reads .env)
│   ├── ops_agent/
│   │   ├── workflow.py             # LangGraph IncidentWorkflow state machine
│   │   ├── alert_poller.py         # Prometheus → TaskStore bridge
│   │   └── scheduler.py            # APScheduler for repeating chaos experiments
│   └── ui/                         # Clano UI — FastAPI + Jinja2 + HTMX (:7860)
├── ansible/                        # Playbooks, roles, inventory
├── containerlab/                   # Spine-leaf topology definition
├── docs/
│   ├── agents.md                   # Unified agent capabilities and REST API
│   ├── policy-autonomy.md          # L0–L5 system, PolicyRegistry, LearningEngine
│   ├── intent-layer.md             # Standing intents reference
│   ├── closed-loop-pipeline.md     # Full pipeline reference and data model
│   ├── agent-tools-framework.md    # Tool guide and workflow patterns
│   ├── architecture.md             # Service map, networks, storage
│   ├── autonomous-agent-framework.md  # Design principles and extensibility guide
│   └── …
├── grafana/                        # Dashboard JSON + provisioning
├── nautobot/                       # Data loader, jobs, scripts
├── prometheus/                     # Scrape configs, alert rules
├── docker-compose.yml
├── Makefile
└── setup.sh
```

---

## 📋 Prerequisites

| Requirement | Version |
| --- | --- |
| OS | Ubuntu 22.04 LTS (recommended) |
| Docker + Compose | 24.x+ / v2.20+ |
| RAM | 16 GB minimum (32 GB recommended) |
| Disk | 60 GB SSD minimum |
| Containerlab | 0.45+ (for lab topology) |
| OpenAI API key | Optional — falls back to Ollama |

---

## 📚 Documentation

| Document | Description |
| --- | --- |
| [`docs/closed-loop-pipeline.md`](docs/closed-loop-pipeline.md) | Pipeline stages, task data model, approval gate, post-execution verification |
| [`docs/policy-autonomy.md`](docs/policy-autonomy.md) | L0–L5 graduated autonomy, PolicyRegistry matching, LearningEngine |
| [`docs/intent-layer.md`](docs/intent-layer.md) | Standing intents: suppress, escalate, monitor, chaos_schedule |
| [`docs/agents.md`](docs/agents.md) | Agent capabilities, REST API, example prompts |
| [`docs/agent-tools-framework.md`](docs/agent-tools-framework.md) | Tool tier model, adding new tools, docstring conventions |
| [`docs/architecture.md`](docs/architecture.md) | Docker networks, service map, database schema |
| [`docs/autonomous-agent-framework.md`](docs/autonomous-agent-framework.md) | Design principles, five-stage loop, governance guide |
| [`docs/monitoring.md`](docs/monitoring.md) | Grafana dashboards, Prometheus rules, Loki queries |
| [`docs/installation.md`](docs/installation.md) | Detailed installation and configuration guide |
| [`docs/data-loader.md`](docs/data-loader.md) | Nautobot data management with `data.yml` |

---

## 🤝 Contributing

Issues and pull requests are welcome. When adding or modifying AI agent tools, follow the conventions in [`docs/agent-tools-framework.md`](docs/agent-tools-framework.md) — particularly the docstring template and the requirement to update the system prompt for any agent that gains a new tool.

```bash
make test   # Run all tests before submitting a PR
```
