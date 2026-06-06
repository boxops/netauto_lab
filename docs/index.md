# Intermediate Network Automation Stack

Welcome to the documentation for the **Intermediate Network Automation Stack** — a production-grade, containerized platform for network source of truth, automated ops, monitoring, and AI-assisted troubleshooting.

## What's Included

- **Nautobot** – Source of Truth (DCIM/IPAM + Golden Config, BGP Models, Device Lifecycle, DVE plugins)
- **Prometheus + Grafana** – Metrics collection, alerting, and visualization
- **Telegraf** – SNMP polling (IF-MIB, BGP4-MIB) from network devices
- **Loki + Promtail** – Syslog ingestion and structured log querying
- **Ansible Core 2.17** – Playbooks and roles for multi-vendor device management
- **Containerlab** – Arista cEOS spine-leaf virtual lab topology
- **AI Agents** – LangChain/LangGraph ReAct agents (OpenAI GPT-4 or local Ollama)
- **Gitea** – Self-hosted Git for config storage and versioning
- **RabbitMQ** – Event bus for automation pipelines

## Documentation Sections

| Section                                         | Description                                                                                       |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| [Installation](installation.md)                 | Prerequisites and step-by-step setup                                                              |
| [Architecture](architecture.md)                 | Service topology, network layout, storage backends, and design decisions                          |
| [Data Loader](data-loader.md)                   | Declarative Nautobot data reconciliation and CRUD workflow                                        |
| [AI Agents](agents.md)                          | Agent capabilities, runbook library, REST APIs, and example prompts                               |
| [Agent Tools Framework](agent-tools-framework.md) | Tool tier model, adding new tools, docstring conventions                                        |
| [Autonomous Agent Framework](autonomous-agent-framework.md) | Framework: design principles, the five-stage loop, autonomy levels, governance, stack assessment, and roadmap |
| [Closed-Loop Pipeline](closed-loop-pipeline.md) | Autonomous incident-response pipeline: stages, incidents, approval gate, post-execution verification, data model |
| [Ansible Playbooks](ansible.md)                 | Automation playbooks reference                                                                    |
| [Monitoring](monitoring.md)                     | Dashboards, alerts, and metrics reference                                                         |

## Quick Links

- [GitHub Repository](https://github.com/your-org/netauto_lab)
- Nautobot: [http://localhost:8080](http://localhost:8080)
- Grafana: [http://localhost:3000](http://localhost:3000)
- Prometheus: [http://localhost:9090](http://localhost:9090)
- Agent UI: [http://localhost:7860](http://localhost:7860)
