# Clano — Closed-Loop Autonomous Network Ops

Welcome to the documentation for **Clano** (Closed-Loop Autonomous Network Ops) — a production-grade, containerized platform for network source of truth, autonomous AI-driven incident response, monitoring, and configuration management.

## What's Included

- **Nautobot** – Source of Truth (DCIM/IPAM + Golden Config, BGP Models, Device Lifecycle, DVE plugins)
- **Prometheus + Grafana** – Metrics collection, alerting, and visualization
- **Telegraf** – SNMP polling (IF-MIB, BGP4-MIB) from network devices
- **Loki + Promtail** – Syslog ingestion and structured log querying
- **Ansible Core 2.17** – Playbooks and roles for multi-vendor device management
- **Containerlab** – Arista cEOS spine-leaf virtual lab topology
- **AI Agents** – Unified LangGraph ReAct agent (OpenAI GPT-4o or local Ollama) with closed-loop pipeline
- **Gitea** – Self-hosted Git for runbook library, config storage, and versioning
- **RabbitMQ** – Optional event bus for near-zero latency task dispatch

## Documentation Sections

| Section | Description |
| --- | --- |
| [Installation](installation.md) | Prerequisites and step-by-step setup |
| [Architecture](architecture.md) | Service topology, network layout, storage backends, and design decisions |
| [Data Loader](data-loader.md) | Declarative Nautobot data reconciliation and CRUD workflow |
| [AI Agents](agents.md) | Unified agent capabilities, REST APIs, and example prompts |
| [Agent Tools Framework](agent-tools-framework.md) | Tool tier model, adding new tools, docstring conventions |
| [Autonomy Policies](policy-autonomy.md) | L0–L5 graduated autonomy, PolicyRegistry, LearningEngine, managing policies |
| [Intent Layer](intent-layer.md) | Standing intents: proactive monitoring, suppression, escalation, chaos schedules |
| [Autonomous Agent Framework](autonomous-agent-framework.md) | Design principles, the five-stage loop, governance, and extensibility guide |
| [Closed-Loop Pipeline](closed-loop-pipeline.md) | Incident-response pipeline: stages, approval gate, post-execution verification, data model |
| [Ansible Playbooks](ansible.md) | Automation playbooks reference |
| [Monitoring](monitoring.md) | Dashboards, alerts, and metrics reference |

## Quick Links

- Nautobot: [http://localhost:8080](http://localhost:8080)
- Grafana: [http://localhost:3000](http://localhost:3000)
- Prometheus: [http://localhost:9090](http://localhost:9090)
- Clano UI: [http://localhost:7860](http://localhost:7860)
