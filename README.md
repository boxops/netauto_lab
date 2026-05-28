# 🌐 Network Automation Lab

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![LangGraph](https://img.shields.io/badge/LangGraph-ReAct-FF6B35?logo=langchain&logoColor=white)](https://github.com/langchain-ai/langgraph)
[![Nautobot](https://img.shields.io/badge/Nautobot-3.x-00C389?logo=data:image/svg+xml;base64,)](https://nautobot.readthedocs.io/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A fully containerised network automation and observability platform built for **network engineers** who want to go beyond dashboards, with AI agents that investigate incidents, design configurations, and run controlled chaos experiments using real network state from Nautobot, Prometheus, and Loki.

---

## ✨ What's Inside

| Source of Truth | Observability | AI Agents |
|---|---|---|
| Nautobot DCIM/IPAM | Prometheus + Alertmanager | Ops Agent (incidents & alerts) |
| Gitea (config store) | Grafana (4 dashboards) | Engineering Agent (configs & planning) |
| Containerlab cEOS lab | Loki + Promtail (syslog) | Chaos Agent (controlled experiments) |
| Ansible automation | Telegraf (metrics) | Gradio Web UI (port 7860) |

---

## 🏗️ Architecture

```
                          ┌───────────────────────────────┐
                          │         Browser / API         │
                          └──────┬──────────┬─────────────┘
                                 │          │
                    ┌────────────▼──┐   ┌───▼──────────────────────┐
                    │  Nautobot     │   │  AI Agents  :7860 (UI)   │
                    │  :8080        │   │  ┌──────────────────────┐ │
                    │  DCIM · IPAM  │   │  │ 🚨 Ops Agent  :8000  │ │
                    │  Golden Cfg   │   │  │ 🔧 Eng Agent  :8001  │ │
                    └────────┬──────┘   │  │ 🔥 Chaos Agent :8002 │ │
                             │          │  └──────────────────────┘ │
                    ┌────────▼──────┐   └───────────┬───────────────┘
                    │  PostgreSQL   │               │ queries
                    │  Redis        │   ┌───────────▼───────────────┐
                    └───────────────┘   │   Prometheus  :9090       │
                                        │   Alertmanager :9093      │
                    ┌───────────────┐   │   Grafana      :3000      │
                    │  Gitea  :3001 │   └───────────┬───────────────┘
                    │  Git server   │               │ scrapes
                    └───────────────┘   ┌───────────▼───────────────┐
                                        │  Telegraf (SNMP + ICMP)   │
                    ┌───────────────┐   │  Loki + Promtail (syslog) │
                    │  Ansible      │   └───────────┬───────────────┘
                    │  RabbitMQ     │               │
                    └───────────────┘   ┌───────────▼───────────────┐
                                        │   Containerlab  :172.20.20│
                                        │   spine1, spine2          │
                                        │   leaf1, leaf2, leaf3     │
                                        │   client1, client2 (cEOS) │
                                        └───────────────────────────┘
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

Open the AI Agents web UI → **http://localhost:7860**

---

## 🖥️ Services

| Service | URL | Default Credentials |
|---|---|---|
| AI Agents UI | http://localhost:7860 | — |
| Nautobot | http://localhost:8080 | admin / see `.env` |
| Grafana | http://localhost:3000 | admin / see `.env` |
| Prometheus | http://localhost:9090 | — |
| Alertmanager | http://localhost:9093 | — |
| Loki | http://localhost:3100 | — |
| Gitea | http://localhost:3001 | gitadmin / see `.env` |
| Ops Agent API | http://localhost:8000 | — |
| Engineering Agent API | http://localhost:8001 | — |
| Chaos Agent API | http://localhost:8002 | — |

---

## 🤖 AI Agents

Three purpose-built LangGraph ReAct agents share a **four-tier tool framework** and a 24-tool library that spans every layer of the stack.

### The Four-Tier Tool Model

```
Tier 1 — Discovery     Nautobot   get_all_devices · get_device_interfaces · get_topology
                                  get_vlans · get_prefixes · get_ip_addresses · …

Tier 2 — Metrics       Prometheus get_device_metrics · get_interface_metrics
                                  get_active_alerts · query_prometheus · …

Tier 3 — Logs          Loki       get_interface_events · get_bgp_events
                                  get_recent_errors · query_logs

Tier 4 — Actions       Ansible    run_ansible_playbook  (check_mode=True by default)
          (with approval) Chaos   shutdown_interface · restore_interface · flap_bgp_neighbor
```

Agents are instructed to work top-to-bottom: **discover what exists, measure its current state, investigate event history, then act.** See [`docs/agent-tools-framework.md`](docs/agent-tools-framework.md) for the full workflow guide.

---

### 🚨 Ops Agent

Investigates network incidents by correlating Nautobot inventory, Prometheus metrics, and Loki syslogs.

**Example prompts**
```
"What alerts are currently firing?"
"Investigate the BGP peer down alert on spine2."
"Why is leaf1 showing high packet loss? Check logs and metrics."
"Generate a health report for all lab devices."
```

**Incident investigation workflow**
1. `get_active_alerts()` → identify what is firing
2. `get_device_metrics(device)` → confirm reachability
3. `get_interface_events(device)` → check for link flaps
4. `get_bgp_events(device)` → check routing changes
5. Correlates findings into a timeline with remediation recommendations

---

### 🔧 Engineering Agent

Designs configurations, plans IP space, and generates Ansible playbooks — always grounded in real Nautobot data before producing output.

**Example prompts**
```
"Find all devices and generate interface description standards for every link."
"Design a BGP configuration for a new leaf router with AS 65104."
"What IP addresses are available in 10.10.0.0/16?"
"Generate an Ansible playbook to configure VLANs 100–110 on all leaf switches."
```

**Config design workflow**
1. `get_all_devices()` + `get_topology()` → understand existing topology
2. `get_device_interfaces(device)` → get exact interface names and neighbors
3. `get_available_ips(prefix)` → allocate addresses from Nautobot IPAM
4. `get_vlans()` → reference existing VLANs
5. Generate vendor-specific config with consistent naming conventions

---

### 🔥 Chaos Agent

Plans and runs controlled chaos experiments with mandatory blast-radius assessment, simulation-first execution, and structured rollback procedures.

**Example prompts**
```
"What is the blast radius if I take down Ethernet1 on spine1?"
"Design a 15-minute game day for testing BGP reconvergence."
"Simulate a leaf uplink failure on leaf2 in check mode."
"Create a rollback-first runbook for a dual-uplink failure test."
```

**Chaos experiment workflow**
1. `get_topology()` → map redundant paths (are there any?)
2. `get_device_interfaces(device)` → get exact interface names
3. `get_active_alerts()` → document baseline before disruption
4. `shutdown_interface(device, interface, check_mode=True)` → dry-run first
5. [With approval] Execute, then observe `get_interface_events` + `get_bgp_events`
6. `restore_interface(device, interface)` + verify recovery

---

### 📊 Agent Activity

The **Agent Activity** tab in the web UI logs every interaction across all three agents with full message and response history, tool call details (inputs and outputs), latency, and per-agent statistics. Click any row to expand the full interaction.

---

## 📈 Observability

Four pre-built Grafana dashboards are automatically provisioned at startup:

| Dashboard | What it shows |
|---|---|
| **Network Overview** | Fleet health, interface utilisation, active alert count, BGP peer state |
| **Device Detail** | Per-device CPU, traffic, BGP peers, recent syslogs |
| **Interface Analytics** | Traffic rates, error counters, CRC errors, utilisation heatmap |
| **BGP Monitoring** | Per-peer session state, prefix counts, reconvergence events |

Prometheus scrapes **Telegraf** (SNMP polling + ICMP probes) and **Loki** aggregates syslog from all Containerlab devices via **Promtail**.

---

## 🔬 Lab Topology

A virtual spine-leaf network runs in **Containerlab** using Arista cEOS images:

```
        spine1 (AS 65001)     spine2 (AS 65002)
           │   │                  │   │
     ┌─────┘   └──────┐    ┌──────┘   └────┐
     ▼                ▼    ▼               ▼
  leaf1            leaf2            leaf3
  (AS 65101)      (AS 65102)      (AS 65103)
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
make logs SVC=ai-ops-agent    # Tail logs for a service
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

# AI Agents
make agent-chat               # CLI chat with Ops Agent

# Tests
make test                     # Run all tests
```

---

## 📁 Repository Structure

```
.
├── ai-agents/                  # LangGraph AI agents
│   ├── shared/
│   │   ├── tools.py            # 24-tool library (4 tiers)
│   │   └── activity_store.py   # SQLite interaction log
│   ├── ops_agent/              # Ops agent + system prompt
│   ├── engineering_agent/      # Engineering agent + system prompt
│   ├── chaos_agent/            # Chaos agent, chaos tools, scheduler
│   └── ui/                     # Gradio web UI
├── ansible/                    # Playbooks, roles, inventory
├── containerlab/               # Spine-leaf topology definition
├── docs/                       # Detailed documentation
│   ├── agent-tools-framework.md   # AI agent tool guide & workflow patterns
│   ├── architecture.md
│   ├── agents.md
│   ├── monitoring.md
│   └── …
├── grafana/                    # Dashboard JSON + provisioning
├── loki/                       # Log storage config
├── nautobot/                   # Data loader, jobs, scripts
├── prometheus/                 # Scrape configs, alert rules
├── promtail/                   # Syslog ingest config
├── telegraf/                   # SNMP + ICMP metrics config
├── docker-compose.yml          # All services
├── Makefile                    # Operational commands
└── setup.sh                    # First-run setup
```

---

## 📋 Prerequisites

| Requirement | Version |
|---|---|
| OS | Ubuntu 22.04 LTS (recommended) |
| Docker + Compose | 24.x+ / v2.20+ |
| RAM | 16 GB minimum (32 GB recommended) |
| Disk | 60 GB SSD minimum |
| Containerlab | 0.45+ (for lab topology) |
| OpenAI API key | Optional — falls back to Ollama |

---

## 📚 Documentation

| Document | Description |
|---|---|
| [`docs/agent-tools-framework.md`](docs/agent-tools-framework.md) | AI agent tool tiers, workflow patterns, adding new tools |
| [`docs/architecture.md`](docs/architecture.md) | Docker networks, service map, component details |
| [`docs/agents.md`](docs/agents.md) | Agent capabilities, REST API, example prompts |
| [`docs/monitoring.md`](docs/monitoring.md) | Grafana dashboards, Prometheus rules, Loki queries |
| [`docs/ansible.md`](docs/ansible.md) | Playbook inventory, roles, check-mode usage |
| [`docs/installation.md`](docs/installation.md) | Detailed installation and configuration guide |
| [`docs/data-loader.md`](docs/data-loader.md) | Nautobot data management with `data.yml` |

---

## 🤝 Contributing

Issues and pull requests are welcome. When adding or modifying AI agent tools, please follow the conventions in [`docs/agent-tools-framework.md`](docs/agent-tools-framework.md) — in particular the docstring template and the requirement to update the system prompts of any agent that gains access to a new tool.

```bash
make test   # Run all tests before submitting a PR
```
