# Architecture

All services run as Docker containers orchestrated by Docker Compose, organized into four planes: **Source of Truth**, **Monitoring**, **Automation**, and **AI**.

## Networks

| Network | Subnet | Purpose |
|---|---|---|
| `mgmt` | 172.20.10.0/24 | Service-to-service API traffic |
| `monitoring` | 172.20.11.0/24 | Metrics scraping |
| `syslog` | 172.20.12.0/24 | Log forwarding from devices |
| `clab` | 172.20.20.0/24 | Containerlab device management |

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
                     └─────┬──────┘            │
                           │ scrapes     ┌─────┴──────────┐
              ┌────────────┼──────────┐  │ Agent TaskDB   │
              ▼            ▼          ▼  │ (PostgreSQL or │
        Telegraf    Node Exporter Blackbox│  SQLite)       │
              │                          └────────────────┘
      ┌───────┴───────┐
      │  SNMP polling │     ┌───────────────────┐
      │  cEOS devices │     │ topology-api :8765 │
      └───────┬───────┘     │ (Nautobot graph)  │
              │ syslog      └───────────────────┘
              ▼
           Promtail → Loki     ┌──────────────────────┐
                               │ alert-event-receiver  │
                               │ :8770 (webhook sink)  │
                               └──────────────────────┘
```

## Key Services

### Source of Truth
**Nautobot** (port 8080) — Network SoT, IPAM, DCIM. PostgreSQL 15 backend, Redis cache/queue, Celery workers. Plugins: Golden Config, Device Lifecycle, BGP Models, Data Validation Engine.

**Gitea** (port 3001) — Self-hosted Git for generated configs, Ansible playbooks, Golden Config diffs, and agent runbooks. The AI agent reads `{AlertName}.yaml` runbooks from `netauto/runbooks` as the first step of every fix-proposal.

### Monitoring
**Prometheus** — 30-day metric retention. Alert rules in `prometheus/alerts/network.yml`. Recording rules in `prometheus/recording_rules/network.yml`.

**Telegraf** — Polls all Containerlab nodes via SNMPv2c: IF-MIB (30 s) and BGP4-MIB (60 s). Exposes metrics on `:9273`.

**Grafana** (port 3000) — Four pre-provisioned dashboards: `network-overview`, `device-detail`, `interface-analytics`, `bgp-monitoring`.

**Loki + Promtail** — Promtail listens for syslog on UDP/TCP port 1514. Pipeline stages extract labels: `device`, `facility`, `severity`, `interface`, `bgp_neighbor`. 90-day retention.

### Supporting Services
**topology-api** (port 8765, internal) — FastAPI serving the Nautobot device graph as JSON. Used by `TopologyCorrelator` for blast-radius calculation without hitting Nautobot on every pipeline run.

**alert-event-receiver** (port 8770, internal) — Receives Alertmanager webhook POSTs; stores NDJSON event log; exposes `GET /events?limit=N`. Provides an auditable alert stream the AlertPoller consumes.

### Automation
**Ansible container** — Python 3.11 + ansible-core 2.17, 9 vendor collections. Mounts `ansible/` for live playbook development. Nautobot dynamic inventory via `nautobot.nautobot.nb_inventory`.

**RabbitMQ** — Optional task dispatch bus. When `RABBITMQ_URL` is set, tasks are published to type-keyed exchanges for near-zero latency. Polling loop remains as fallback.

**Containerlab** — Five-node Arista cEOS spine-leaf fabric (Spine1 AS65001, Spine2 AS65002, Leaf1–3 AS65101–65103). All eBGP. When `LAB_VALIDATION_ENABLED=true`, fixes are applied to `clab-` prefixed devices before production.

### AI
**AI Agent** (port 8000) — Unified LangGraph ReAct agent. Handles the full pipeline: RCA → fix → validation → execution. See [agents.md](agents.md) and [pipeline.md](pipeline.md).

**Clano UI** (port 7860) — FastAPI + Jinja2 + HTMX. Five tabs: Operations, Incidents, Assist, Config, System. Real-time updates via SSE (`/stream/tasks`). No CDN, no CSS framework.

**Agent TaskStore** — SQLite (`activity.db`, WAL mode) by default; PostgreSQL via `TASK_DB_URL`. Shared between `ai-agent` and `agent-ui` containers via `agent-activity-data` volume.

## Data Persistence

| Volume | Contents |
|---|---|
| `nautobot-postgres-data` | Nautobot PostgreSQL |
| `gitea-postgres-data` | Gitea PostgreSQL |
| `agent-postgres-data` | Agent TaskStore PostgreSQL |
| `prometheus-data` | Prometheus TSDB |
| `grafana-data` | Grafana dashboards |
| `loki-data` | Loki log chunks |
| `gitea-data` | Gitea repos and runbooks |
| `agent-activity-data` | SQLite activity.db (default) |
| `rabbitmq-data` | RabbitMQ durable queues |
