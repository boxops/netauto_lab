# Network Automation Stack

A production-ready, containerized network automation and observability platform for network engineers and NetOps teams.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker Compose Stack                        │
├────────────────┬───────────────────┬────────────────────────────┤
│   Source of    │  Monitoring &     │     AI Agents              │
│   Truth        │  Observability    │                            │
│                │                   │  ┌─────────────────────┐   │
│  ┌──────────┐  │  ┌─────────────┐  │  │  Ops Agent          │   │
│  │Nautobot  │  │  │ Prometheus  │  │  │  (LangGraph+GPT-4)  │   │
│  │(DCIM/IPAM│  │  │+ Alertmgr   │  │  └─────────────────────┘   │
│  │ Golden   │  │  └─────────────┘  │  ┌─────────────────────┐   │
│  │ Config)  │  │  ┌─────────────┐  │  │  Engineering Agent  │   │
│  └──────────┘  │  │   Grafana   │  │  │  (LangGraph+GPT-4)  │   │
│                │  │ (Dashboards)│  │  └─────────────────────┘   │
│  ┌──────────┐  │  └─────────────┘  │  ┌─────────────────────┐   │
│  │  Gitea   │  │  ┌─────────────┐  │  │  Gradio Web UI      │   │
│  │  (Git)   │  │  │  Telegraf   │  │  └─────────────────────┘   │
│  └──────────┘  │  │  (Metrics)  │  │                            │
│                │  └─────────────┘  │                            │
├────────────────┼───────────────────┼────────────────────────────┤
│   Logging      │   Automation      │   Lab Environment          │
│                │                   │                            │
│  ┌──────────┐  │  ┌─────────────┐  │  ┌─────────────────────┐   │
│  │   Loki   │  │  │   Ansible   │  │  │   Containerlab      │   │
│  │+Promtail │  │  │  Container  │  │  │  Spine-Leaf (cEOS)  │   │
│  │(Syslog)  │  │  └─────────────┘  │  │  2 Spines, 3 Leaves │   │
│  └──────────┘  │  ┌─────────────┐  │  └─────────────────────┘   │
│                │  │  RabbitMQ   │  │                            │
│                │  │(Event Bus)  │  │                            │
│                │  └─────────────┘  │                            │
└────────────────┴───────────────────┴────────────────────────────┘
```

## Services & Default Ports

| Service          | URL                        | Default Credentials |
|------------------|----------------------------|---------------------|
| Nautobot         | http://localhost:8080      | admin / see .env    |
| Grafana          | http://localhost:3000      | admin / see .env    |
| Prometheus       | http://localhost:9090      | N/A                 |
| Alertmanager     | http://localhost:9093      | N/A                 |
| Loki             | http://localhost:3100      | N/A                 |
| Gitea            | http://localhost:3001      | gitadmin / see .env |
| AI Agents UI     | http://localhost:7860      | N/A                 |

## Prerequisites

- **OS**: Ubuntu 22.04 LTS (recommended)
- **Docker**: 24.x+ with Docker Compose v2.20+
- **RAM**: 32 GB minimum (8 GB for dev/test)
- **Disk**: 100 GB SSD minimum
- **Containerlab**: 0.45+ (for lab topology)
- **OpenAI API key** (optional – falls back to local Ollama)

## Quick Start (< 10 minutes)

```bash
# 1. Clone the repository
git clone <repo-url> netauto_lab
cd netauto_lab

# 2. Run setup (creates .env, generates secrets, starts all services)
bash setup.sh

# 3. Check services are healthy
make health-check

# 4. (Optional) Deploy the Containerlab spine-leaf topology
make deploy-lab

# 5. (Optional) Sync lab devices to Nautobot
make sync-inventory
```

## Daily Operations

```bash
make start           # Start all services
make stop            # Stop all services
make status          # Show container status
make logs            # Tail all logs
make logs SVC=nautobot   # Tail specific service logs
make health-check    # Run health checks

# Ansible
make ansible-shell   # Open Ansible shell
make run-playbook    # Run a playbook interactively

# AI Agents
make agent-chat      # CLI chat with Ops Agent
# Web UI: http://localhost:7860

# Backups
make backup-data     # Backup all data
```

## Repository Structure

```
.
├── docker-compose.yml          # All services defined here
├── .env.example                # Environment template
├── Makefile                    # All operational commands
├── setup.sh                    # Automated first-run setup
├── nautobot/                   # Nautobot configuration & init scripts
├── prometheus/                 # Prometheus config, alerts, recording rules
├── grafana/                    # Dashboards & provisioning
├── telegraf/                   # Telegraf metrics collection config
├── loki/                       # Loki log storage config
├── promtail/                   # Promtail syslog ingest config
├── ansible/                    # Ansible playbooks, roles, inventory
├── ai-agents/                  # AI Ops & Engineering agents (LangGraph)
├── containerlab/               # Spine-leaf lab topology
├── scripts/                    # setup, health_check, backup, sync
└── tests/                      # Infrastructure & agent tests
```

## Ansible Playbooks

| Playbook             | Purpose                                    |
|----------------------|--------------------------------------------|
| `health_check.yml`   | Multi-vendor health check                  |
| `backup_config.yml`  | Backup running configs to Nautobot/Git     |
| `deploy_config.yml`  | Push intended configs from Nautobot        |
| `compliance_check.yml` | Run Golden Config compliance checks     |
| `provision_device.yml` | Zero-touch device provisioning          |

## AI Agent Capabilities

**Ops Agent** (`http://localhost:8000`):
- Investigate Prometheus alerts
- Correlate metrics + logs for root cause analysis
- Run Ansible playbooks (check mode by default, live with approval)

**Engineering Agent** (`http://localhost:8001`):
- Generate device configurations (EOS, IOS, JunOS)
- IP address and VLAN planning from Nautobot
- Generate Ansible playbooks from natural language
- Configuration review and best-practices guidance

## Troubleshooting

```bash
# View logs for a failing service
make logs SVC=nautobot

# Restart a single service
make restart SVC=prometheus

# Full teardown and re-setup
make clean   # WARNING: destroys all data
bash setup.sh
```

See [docs/](docs/) for detailed documentation on each component.
