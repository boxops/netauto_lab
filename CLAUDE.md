# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Commands

```bash
# Start / stop all services
make start
make stop
make restart SVC=<service-name>   # e.g. SVC=ai-ops-agent

# Rebuild after code changes
make rebuild SVC=agent-ui         # single service
make rebuild                      # all images

# Logs
make logs SVC=ai-ops-agent

# Tests
make test                                         # all tests
python3 -m pytest tests/test_agents.py -v        # single file
python3 -m pytest tests/ -m unit -v              # unit tests only (no services needed)
python3 -m pytest tests/ -m integration -v       # integration (requires running stack)

# Data management
make plan-data                    # dry-run Nautobot reconciliation
make apply-data                   # apply nautobot/data_loader/data.yml
make lint-data                    # validate data.yml YAML

# Lab topology
make deploy-lab                   # deploy Containerlab spine-leaf
make sync-inventory               # register lab devices in Nautobot
make destroy-lab

# Ansible lint
make lint                         # ansible-lint + YAML validation

# CLI chat with Ops Agent
make agent-chat
```

Tests run from the repo root using the `.venv-host` virtualenv. Unit tests mock LLM and Nautobot calls; integration tests require the full stack running.

---

## Architecture

### Service map

All services run as Docker containers defined in `docker-compose.yml`. The four internal Docker networks (`mgmt-network`, `monitoring-network`, `syslog-network`, `clab`) segment traffic. The `clab` network is external and created by Containerlab.

| Category | Service | Port |
|---|---|---|
| Source of truth | Nautobot (DCIM/IPAM) | 8080 |
| Observability | Prometheus, Alertmanager, Grafana, Loki | 9090, 9093, 3000, 3100 |
| Metrics collection | Telegraf (SNMP + ICMP) | — |
| Log ingestion | Promtail → Loki | — |
| AI agents | Ops Agent, Eng Agent, Chaos Agent | 8000, 8001, 8002 |
| Agent UI | FastAPI + Jinja2 + HTMX | 7860 |
| Git | Gitea | 3001 |
| Lab | Containerlab cEOS spine-leaf | 172.20.20.0/24 |

### AI agent internals (`ai-agents/`)

Each agent is a **LangGraph ReAct** agent running inside a FastAPI server (uvicorn). The three agents (`ops_agent`, `eng_agent`, `chaos_agent`) share a single Docker image built from `ai-agents/Dockerfile`.

**Shared layer (`ai-agents/shared/`):**
- `tools.py` — 24 LangChain `@tool` functions organised in four tiers: Nautobot discovery → Prometheus metrics → Loki logs → Ansible actions. All agent tool sets are drawn from this one file.
- `task_store.py` — SQLite-backed task queue (`activity.db`) shared across all three agent containers via a named Docker volume. This is the backbone of the closed-loop pipeline; all pipeline state lives here.
- `activity_store.py` — Separate SQLite table logging every chat interaction (message, response, latency, tool calls) for the Activity tab.
- `config.py` — `pydantic-settings` `Settings` class; reads from `.env`. LLM selection falls back to Ollama if `OPENAI_API_KEY` is not set.
- `status_tracker.py` — `AgentStatus` dataclass + `StatusCallbackHandler` (LangChain callback). Updated in real-time during every ReAct loop iteration; polled by the UI `/status` endpoint every 2 s.
- `rate_limiter.py` — Token + cost budgets per agent per hour/day. Raises `BudgetExceededError` on breach; the agent's `/chat` endpoint returns HTTP 429.

**Per-agent structure:** each agent directory (`ops_agent/`, `engineering_agent/`, `chaos_agent/`) contains:
- `agent.py` — `create_react_agent` call, system prompt, module-level singletons (`task_store`, `rate_limiter`, `agent_status`, `status_handler`).
- `main.py` — FastAPI app, `/chat`, `/status`, `/usage`, `/health` endpoints.
- `task_runner.py` — Background thread that polls `task_store` for `pending` tasks assigned to this agent (polling intervals: Ops 60 s, Eng 90 s, Chaos 120 s).

**UI (`ai-agents/ui/`):**
- `main.py` — FastAPI app serving the web UI on port 7860. Mounts `static/` and `templates/`. Uses `httpx.AsyncClient` (not blocking) for all outbound agent calls.
- `templates/` — Jinja2 templates. Page templates (`pipeline.html`, `chat.html`, `activity.html`) extend `base.html`. Partial templates in `templates/partials/` return HTML fragments consumed by HTMX polling (`hx-trigger="every Ns"`).
- `static/htmx.min.js` — HTMX served locally (no CDN dependency).
- `static/style.css` — Dark-theme CSS; no external CSS frameworks.
- Polling intervals: agent status (2 s), pipeline/task queue (3 s), activity (5 s), status bar/cost KPIs (30 s). All polling is plain HTTP GET returning HTML fragments via `hx-trigger="every Ns"`.
- The `from_json` Jinja2 filter is registered in `main.py` to parse JSON task result/content strings inside templates.

**Ops Agent extras:**
- `alert_poller.py` — Polls Alertmanager every 60 s. For each new alert fingerprint not already in the task queue, creates an `rca` task. Deduplicates via fingerprint to avoid re-investigating the same alert.

**Chaos Agent extras:**
- `chaos_tools.py` — Ansible-backed tools for `shutdown_interface`, `restore_interface`, `flap_bgp_neighbor`. These are the only write-action tools in the system; all others are read-only.
- `scheduler.py` — `APScheduler` `BackgroundScheduler`; exposes `/schedule` and `/schedules` REST endpoints for repeating chaos runs.

### Closed-loop pipeline

When Prometheus fires an alert, the pipeline proceeds automatically through four stages tracked in `task_store`:

```
rca (ops_agent) → fix_proposal (eng_agent) → validation (chaos_agent) → approval_gate (human)
```

All tasks share an `alert_fingerprint` field so the full chain is traceable. The pipeline only requires human input at the `approval_gate` stage (Approve/Reject in the UI's Pipeline tab).

### Tool tier model

Agents are instructed to work top-to-bottom through the tiers:
1. **Discovery** — Nautobot (what exists?)
2. **Metrics** — Prometheus (what is its current state?)
3. **Logs** — Loki (what events happened?)
4. **Actions** — Nautobot Jobs via Ansible (run_ansible_playbook, `check_mode=True` by default)

Action tools require explicit user approval (`"approved"`, `"execute"`, or `"apply"` in the message) before `check_mode=False` is used.

### Adding a new tool

1. Add a `@tool`-decorated function to `ai-agents/shared/tools.py`.
2. Add it to the appropriate `*_TOOLS` list at the bottom of `tools.py`.
3. Update the system prompt(s) in `agent.py` for every agent that should have access.
4. Follow the docstring convention in `docs/agent-tools-framework.md`.

### Nautobot data management

Inventory and IPAM data is declared in `nautobot/data_loader/data.yml`. The loader (`load_data.py`) runs inside the Nautobot container and performs full CRUD reconciliation (`make apply-data`). `make plan-data` is a safe dry-run. The loader uses a `state_store.py` to track previously managed objects so it can detect and delete removals.

### Environment and secrets

All configuration is in `.env` (gitignored). `.env.example` documents every variable. Agent configuration is loaded via `shared/config.py`'s `Settings` class. Services pick up variables via `env_file: .env` in `docker-compose.yml`.

---

## Key constraints

- **`check_mode=True` is the default for all Ansible action tools.** Never change this default — the chaos agent and approval gate exist precisely to gate `check_mode=False` execution.
- **`activity.db` is a shared volume.** All three agent containers and the UI container read and write it concurrently; the `TaskStore` uses WAL mode and a threading lock for safe access.
- **LLM selection is automatic.** `shared/llm.py` returns an OpenAI client if `OPENAI_API_KEY` is set, otherwise Ollama. Don't hardcode model clients in agent code.
