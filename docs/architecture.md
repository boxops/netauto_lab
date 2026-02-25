# Architecture

## Overview

The stack is organized into four functional planes: **Source of Truth**, **Monitoring & Observability**, **Automation**, and **AI Assistance**. All services run as Docker containers orchestrated by Docker Compose.

## Network Layout

Four isolated Docker networks are created:

| Network | Subnet | Purpose |
|---------|--------|---------|
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
    :8080 Nautobot   :3000 Grafana    :7860 Agent UI
          │                │                  │
    ┌─────┴──────┐   ┌─────┴──────┐   ┌──────┴──────┐
    │ PostgreSQL │   │ Prometheus │   │ Ops Agent   │
    │ Redis      │   │ Loki       │   │ Eng Agent   │
    └────────────┘   │ Alertmgr  │   └─────────────┘
                     └─────┬──────┘
                           │ scrapes
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        Telegraf    Node Exporter   Blackbox
              │
      ┌───────┴───────┐
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
- **Database**: PostgreSQL 15 (persistent volume `nautobot_db`)
- **Cache/Queue**: Redis 7 (single instance, two DBs: 0 = cache, 1 = Celery)
- **Workers**: `nautobot-worker` (Celery) + `nautobot-scheduler` (beat scheduler)
- **Plugins**: Golden Config, Device Lifecycle, BGP Models, Data Validation Engine
- **Initial Data**: Regions, sites, device roles, platforms (EOS/IOS/JunOS), VLANs, IP prefixes loaded via `nautobot/initializers/load_initial_data.py`

### Gitea

- Self-hosted Git server for all generated configs, Ansible playbooks, and Golden Config diffs.
- Reachable at port 3001; backed by separate PostgreSQL database.

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

| Dashboard | UID | Description |
|-----------|-----|-------------|
| Network Overview | `network-overview` | Fleet-wide health summary |
| Device Detail | `device-detail` | Per-device drill-down |
| Interface Analytics | `interface-analytics` | Traffic and error rates |
| BGP Monitoring | `bgp-monitoring` | Peer state and prefix counts |

### Loki + Promtail

- Promtail listens for syslog on UDP/TCP port 1514.
- Pipeline stages extract structured labels: `device`, `facility`, `severity`, `interface`, `bgp_neighbor`.
- Loki stores logs with 90-day retention.

## Automation Plane

### Ansible Container

- Python 3.11 + ansible-core 2.17 with 9 vendor collections installed at build time.
- Mounts the `ansible/` directory for live playbook development.
- Nautobot dynamic inventory via the `nautobot.nautobot.nb_inventory` plugin.

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

All eBGP. Leaves advertise loopbacks + host routes to both spines.

## AI Assistance Plane

### Ops Agent

- **Purpose**: Reactive NOC assistance — investigate alerts, correlate metrics and logs.
- **Model**: GPT-4o (falls back to local Ollama `llama3.1`).
- **Safety**: All Ansible actions default to `check_mode=True`; live execution requires explicit user approval.
- **API**: FastAPI on port 8000; also accessible via Gradio UI.

### Engineering Agent

- **Purpose**: Proactive engineering — generate configs, plan IPs/VLANs, write playbooks.
- **Model**: GPT-4o (same fallback).
- **API**: FastAPI on port 8001.

### Gradio UI

- Tabbed web interface for both agents.
- Displays per-session conversation history.
- Provides example prompts.
- Service status dashboard embedded.

## Data Persistence

| Volume | Contents |
|--------|----------|
| `nautobot_db` | Nautobot PostgreSQL data |
| `gitea_db` | Gitea PostgreSQL data |
| `nautobot_media` | Nautobot uploaded files |
| `gitea_data` | Gitea repositories |
| `prometheus_data` | Prometheus TSDB |
| `grafana_data` | Grafana dashboards & users |
| `loki_data` | Loki log chunks |
