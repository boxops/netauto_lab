# Clano — Closed-Loop Autonomous Network Ops

Containerized platform for network source of truth, autonomous AI-driven incident response, monitoring, and configuration management.

**Stack:** Nautobot · Prometheus + Grafana · Telegraf · Loki + Promtail · Ansible · Containerlab · AI Agent (LangGraph) · Gitea · RabbitMQ (optional)

## Documentation

| Doc | What's inside |
|---|---|
| [Installation](installation.md) | Prerequisites, setup, service URLs, key env vars |
| [Architecture](architecture.md) | Service topology, network layout, storage, data persistence |
| [AI Agents](agents.md) | Capabilities, tool tiers, tool reference, workflow patterns, standing intents, REST API, example prompts |
| [Pipeline](pipeline.md) | Closed-loop pipeline stages, autonomy policies (L0–L5), fast path, LearningEngine, data model, config reference |
| [Nautobot](nautobot.md) | Data loader (declarative YAML reconciliation) and Jobs framework (creating jobs, parallel execution, existing jobs) |
| [Monitoring](monitoring.md) | Grafana dashboards, Prometheus metrics + alert rules, Alertmanager, Loki queries, Telegraf OIDs |
| [Ansible](ansible.md) | Playbook reference, inventory, roles |
| [Roadmap](roadmap.md) | What's implemented, remaining work, competitive differentiators |

## Quick Links

- Nautobot: http://localhost:8080
- Grafana: http://localhost:3000
- Prometheus: http://localhost:9090
- Clano UI: http://localhost:7860
- AI Agent API: http://localhost:8000
