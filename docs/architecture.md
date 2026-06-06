# Architecture

## Overview

The stack is organized into four functional planes: **Source of Truth**, **Monitoring & Observability**, **Automation**, and **AI Assistance**. All services run as Docker containers orchestrated by Docker Compose.

## Network Layout

Four isolated Docker networks are created:

| Network      | Subnet         | Purpose                        |
| ------------ | -------------- | ------------------------------ |
| `mgmt`       | 172.20.10.0/24 | Service-to-service API traffic |
| `monitoring` | 172.20.11.0/24 | Metrics scraping               |
| `syslog`     | 172.20.12.0/24 | Log forwarding from devices    |
| `clab`       | 172.20.20.0/24 | Containerlab device management |

## Service Map

```
                    ┌──────────────┐
                    │   Browser    │
                    └──────┬───────┘
          ┌────────────────┼──────────────────┐
          ▼                ▼                  ▼
    :8080 Nautobot   :3000 Grafana    :7860 Clano UI
          │                │                  │
    ┌─────┴──────┐   ┌─────┴──────┐   ┌──────┴──────────┐
    │ PostgreSQL │   │ Prometheus │   │ AI Agent  :8000  │
    │ Redis      │   │ Loki       │   │ (unified)        │
    └────────────┘   │ Alertmgr  │   └────────┬─────────┘
                     └─────┬──────┘            │ shared
                           │ scrapes      ┌────┴──────────┐
              ┌────────────┼──────────┐   │ Agent TaskDB  │
              ▼            ▼          ▼   │ (PostgreSQL   │
        Telegraf    Node Exporter Blackbox│  or SQLite)   │
              │                          └───────────────┘
      ┌───────┴───────┐
      │  SNMP polling │     ┌──────────────────┐
      │  cEOS devices │     │ topology-api :8765│
      └───────┬───────┘     │ (Nautobot graph) │
              │ syslog      └──────────────────┘
              ▼
           Promtail → Loki     ┌──────────────────────┐
                               │ alert-event-receiver  │
                               │ :8770 (webhook sink)  │
                               └──────────────────────┘
```

## Source of Truth Plane

### Nautobot

- **Version**: 3.x (community edition)
- **Database**: PostgreSQL 15 (persistent volume `nautobot-postgres-data`)
- **Cache/Queue**: Redis 7 (single instance, two DBs: 0 = cache, 1 = Celery)
- **Workers**: `nautobot-worker` (Celery) + `nautobot-scheduler` (beat scheduler)
- **Plugins**: Golden Config, Device Lifecycle, BGP Models, Data Validation Engine
- **Data Loader**: Regions, sites, device roles, platforms (EOS/IOS/JunOS), VLANs, IP prefixes loaded via `nautobot/data_loader/load_data.py`

### Gitea

- Self-hosted Git server for generated configs, Ansible playbooks, Golden Config diffs, and agent runbooks.
- Reachable at port 3001; backed by separate PostgreSQL database.
- **Runbook library**: The AI agent reads `{AlertName}.yaml` files from the `netauto/runbooks` repository via the Gitea API as the first step of every fix-proposal stage. Known alert types are handled by their runbook rather than re-derived by the LLM.

## Monitoring Plane

### Prometheus

- Scrapes all stack services, Containerlab nodes (via Telegraf SNMP), and blackbox targets.
- 30-day metric retention in persistent volume.
- Alerting rules in `prometheus/alerts/network.yml` (device availability, interface health, BGP, system resources).
- Recording rules in `prometheus/recording_rules/network.yml` for dashboard performance.

### Telegraf

- Polls all Containerlab nodes via SNMPv2c.
- Collects `IF-MIB` (interface counters) every 30 seconds.
- Collects `BGP4-MIB` (peer state, prefix counts) every 60 seconds.
- Exposes metrics on `:9273` for Prometheus scraping.

### Grafana

Four pre-provisioned dashboards:

| Dashboard           | UID                   | Description                  |
| ------------------- | --------------------- | ---------------------------- |
| Network Overview    | `network-overview`    | Fleet-wide health summary    |
| Device Detail       | `device-detail`       | Per-device drill-down        |
| Interface Analytics | `interface-analytics` | Traffic and error rates      |
| BGP Monitoring      | `bgp-monitoring`      | Peer state and prefix counts |

### Loki + Promtail

- Promtail listens for syslog on UDP/TCP port 1514.
- Pipeline stages extract structured labels: `device`, `facility`, `severity`, `interface`, `bgp_neighbor`.
- Loki stores logs with 90-day retention.

## Supporting Services

### topology-api

- Lightweight Python FastAPI server (port 8765, internal only).
- Exposes a single `/topology` endpoint that returns the full Nautobot device graph (devices + cables) as JSON.
- Used by `TopologyCorrelator` inside the AI agent for blast-radius calculation and cascading-failure root-cause inference without hitting the Nautobot REST API directly on every pipeline run.
- Runs inside the `mgmt-network` and `monitoring-network`; no external port binding.

### alert-event-receiver

- Lightweight Python HTTP server (port 8770, internal only).
- Receives Alertmanager webhook POSTs (`POST /alertmanager/webhook`) and stores them as an NDJSON event log.
- Exposes `GET /events?limit=N` for the AlertPoller to consume.
- Provides an auditable, deduplicated alert event stream independent of Prometheus's own `/api/v1/alerts` endpoint.
- Runs inside `monitoring-network` and `mgmt-network`; no external port binding.

## Automation Plane

### Ansible Container

- Python 3.11 + ansible-core 2.17 with 9 vendor collections installed at build time.
- Mounts the `ansible/` directory for live playbook development.
- Nautobot dynamic inventory via the `nautobot.nautobot.nb_inventory` plugin.

### RabbitMQ

- Defined in `docker-compose.yml`; acts as the optional task dispatch bus for the agent pipeline.
- When `RABBITMQ_URL` is set in `.env`, newly created tasks are published to type-keyed exchanges (`task.fix_proposal`, `task.validation`, `task.approval_gate`).
- Agent task runners subscribe as consumers and process tasks immediately on arrival rather than waiting for the next poll tick.
- When `RABBITMQ_URL` is empty (the default), agents use their polling loops and RabbitMQ is unused.

### Containerlab Topology

Five-node Arista cEOS spine-leaf fabric:

```
      Spine1 (AS 65001)  Spine2 (AS 65002)
           │   ╲     ╱   │
           │    ╲   ╱    │
      Leaf1 (AS 65101) Leaf2 (AS 65102) Leaf3 (AS 65103)
           │                                    │
        Client1                             Client2
```

All eBGP. Leaves advertise loopbacks + host routes to both spines. When `LAB_VALIDATION_ENABLED=true`, the agent pipeline applies proposed fixes to the Containerlab equivalent (prefixed `clab-`) before production execution.

## AI Assistance Plane

### Unified Agent

A single **UnifiedAgent** (LangGraph ReAct) handles all pipeline stages — RCA, fix generation, validation, and execution — in one FastAPI service on **port 8000**.

- **Model**: GPT-4o (falls back to local Ollama `llama3`).
- **Safety**: All config actions default to `check_mode=True`; live execution is gated by the autonomy policy.
- **Shared modules**:
  - `PolicyRegistry` — queries `action_policies` table to determine the L0–L5 autonomy level for each pipeline action.
  - `LearningEngine` — auto-promotes policies after N consecutive successful resolutions; demotes on failure.
  - `IntentRegistry` + `IntentEvaluator` — standing policies for proactive monitoring, alert suppression, escalation, and chaos scheduling.
  - `TopologyCorrelator` — builds an adjacency graph from Nautobot cable data; provides blast-radius calculation and root-cause inference for cascading failures.
- **Background threads**: AlertPoller (15 s critical, 60 s normal), IntentEvaluator (5 min poll for monitor intents), APScheduler (chaos_schedule intents).

See [`docs/agents.md`](agents.md) for capabilities, [`docs/policy-autonomy.md`](policy-autonomy.md) for the autonomy system, and [`docs/intent-layer.md`](intent-layer.md) for standing intents.

### Agent TaskStore

Shared SQLite database (or PostgreSQL in production) used by all agent containers and the UI.

| Mode | Backend | When to use |
| --- | --- | --- |
| Default | SQLite (`activity.db`) | Lab / development |
| Production | PostgreSQL 16 | Multi-replica, persistent, LISTEN/NOTIFY ready |

Set `TASK_DB_URL` in `.env` to switch to PostgreSQL. The `agent-postgres` service is pre-defined in `docker-compose.yml`.

**Schema tables:**

| Table | Purpose |
| --- | --- |
| `tasks` | Main task queue (`rca`, `fix_proposal`, `validation`, `approval_gate`, `incident`) |
| `task_events` | Append-only event log per task (20+ event types) |
| `task_feedback` | Validation feedback for KPI accuracy tracking |
| `token_usage` | Token usage and cost per agent per session |
| `action_policies` | Autonomy policies (L0–L5) with match criteria (alertname, fix_type, device_role, environment) |
| `policy_performance` | Execution outcome history per policy — feeds the LearningEngine |
| `standing_intents` | Proactive monitoring rules, alert suppression, escalation overrides, chaos schedules |

### Clano UI

- FastAPI application serving the web UI on port 7860.
- Jinja2 server-side templates with HTMX for partial updates.
- **Real-time updates via SSE**: A single persistent connection to `/stream/tasks` drives all live widgets. When any task state changes, the server pushes a `tasks-changed` event; HTMX triggers targeted refreshes.
- **Navigation (5 tabs)**:
  - **📡 Operations** — Alert Processing Pipeline (Visual + Chronicle views), Ops Health KPI bar, Task Queue, Task Detail.
  - **🚨 Incidents** — Grouped incident view with severity, affected devices, and linked pipeline chains.
  - **💬 Assist** — Interactive chat with the unified agent.
  - **⚙️ Config** — Autonomy policies (L0–L5 gauge, policy list, Learning Engine performance), standing intents (add/manage), UI preferences.
  - **📊 System** — Cost Monitor (token usage, budget burn), Activity Log (full audit trail of agent conversations).
- **Chronicle view**: Each pipeline chain renders as a vertical narrative timeline (Intent-triggered or Alert-triggered badge, stage chapters with autonomy level badge, blast-radius block, runbook provenance, rejection reason capture, Stage 5 Verify chapter with config confirm + alert resolution cards).
- **Performance**: All agent HTTP calls use `asyncio.gather`; a single shared `httpx.AsyncClient` is reused across requests; synchronous DB calls are dispatched via `run_in_threadpool`.
- Shares the `agent-activity-data` Docker volume with all agent containers.

## Data Persistence

| Volume                   | Contents                            |
| ------------------------ | ----------------------------------- |
| `nautobot-postgres-data` | Nautobot PostgreSQL data            |
| `gitea-postgres-data`    | Gitea PostgreSQL data               |
| `agent-postgres-data`    | Agent task store PostgreSQL data    |
| `nautobot-media`         | Nautobot uploaded files             |
| `gitea-data`             | Gitea repositories and runbooks     |
| `prometheus-data`        | Prometheus TSDB                     |
| `grafana-data`           | Grafana dashboards and users        |
| `loki-data`              | Loki log chunks                     |
| `agent-activity-data`    | Shared SQLite activity.db (default) |
| `rabbitmq-data`          | RabbitMQ durable queues             |
