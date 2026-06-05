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
    :8080 Nautobot   :3000 Grafana    :7860 Agent UI
          │                │                  │
    ┌─────┴──────┐   ┌─────┴──────┐   ┌──────┴──────────┐
    │ PostgreSQL │   │ Prometheus │   │ Ops Agent       │
    │ Redis      │   │ Loki       │   │ Eng Agent       │
    └────────────┘   │ Alertmgr  │   │ Chaos Agent     │
                     └─────┬──────┘   └────────┬────────┘
                           │ scrapes            │ shared
              ┌────────────┼────────────┐  ┌───┴──────────┐
              ▼            ▼            ▼  │ Agent TaskDB │
        Telegraf    Node Exporter   Blackbox│ (SQLite or   │
              │                            │  PostgreSQL) │
      ┌───────┴───────┐                   └──────────────┘
      │  SNMP polling │
      │  cEOS devices │
      └───────┬───────┘
              │ syslog
              ▼
           Promtail → Loki
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
- **Runbook library**: The Engineering Agent reads `{AlertName}.yaml` files from the `netauto/runbooks` repository via the Gitea API to accelerate fix generation for known alert types.

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

### Ops Agent

- **Purpose**: Reactive NOC assistance — investigate alerts, correlate metrics and logs, run the closed-loop pipeline.
- **Model**: GPT-4o (falls back to local Ollama `llama3`).
- **Safety**: All Ansible actions default to `check_mode=True`; live execution requires explicit user approval.
- **API**: FastAPI on port 8000.
- **Background threads**: AlertPoller (priority-aware: 15 s for critical, 60 s for normal).

### Engineering Agent

- **Purpose**: Fix generation and config design — generates vendor-specific remediations, plans IP space, writes playbooks.
- **Model**: GPT-4o (same fallback).
- **API**: FastAPI on port 8001.
- **Background threads**: EngTaskRunner (15 s priority loop, 90 s normal loop), approved gate executor.
- **Runbook-first**: Calls `get_runbook(alertname)` before re-deriving fixes from scratch.

### Chaos Agent

- **Purpose**: Fix validation, blast-radius assessment, and controlled chaos experiments.
- **Model**: GPT-4o (same fallback).
- **API**: FastAPI on port 8002.
- **Background threads**: ChaosTaskRunner (15 s priority loop, 120 s normal loop), APScheduler for repeating chaos runs.

### Agent TaskStore

Shared SQLite database (or PostgreSQL in production) used by all four agent containers and the UI to store pipeline state.

| Mode       | Backend                | When to use                                    |
| ---------- | ---------------------- | ---------------------------------------------- |
| Default    | SQLite (`activity.db`) | Lab / development                              |
| Production | PostgreSQL 16          | Multi-replica, persistent, LISTEN/NOTIFY ready |

Set `TASK_DB_URL` in `.env` to switch to PostgreSQL. The `agent-postgres` service is pre-defined in `docker-compose.yml`.

### Agent UI

- FastAPI application serving the web UI on port 7860.
- Jinja2 server-side templates with HTMX for partial updates.
- **Real-time updates via SSE**: A single persistent connection to `/stream/tasks` drives all live widgets. When any task state changes, the server pushes a `tasks-changed` event; HTMX triggers targeted refreshes. This replaces polling entirely on the pipeline page.
- **Tabs**: Pipeline Dashboard · Ops Agent · Engineering Agent · Chaos Agent · 🚨 Incidents · Activity · Cost Monitor.
- **Pipeline views**: The Alert Processing Pipeline panel offers two views toggled by a button group:
  - **📊 Visual** — card-per-stage layout with status, key fields, and connecting arrows.
  - **📖 Chronicle** — vertical timeline narrative with stage chapters, gap timings, confidence/risk/verdict badges, collapsible detail panels, and a header summarising alert severity, device, and time-to-resolution.
- **Performance**: All agent HTTP calls inside async handlers use `asyncio.gather` for concurrency (the three `/partials/agent-status` calls, for example, run in parallel rather than sequentially). A single shared `httpx.AsyncClient` is created at startup and reused across all requests. All synchronous SQLite calls are dispatched to a thread pool via `run_in_threadpool` so the asyncio event loop is never blocked.
- Shares the `agent-activity-data` Docker volume with all three agent containers.

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
