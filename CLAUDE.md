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
| AI agent | Unified agent (`ai-agent` service) | 8000 |
| Agent UI | FastAPI + Jinja2 + HTMX | 7860 |
| Git | Gitea | 3001 |
| Lab | Containerlab cEOS spine-leaf | 172.20.20.0/24 |

### AI agent internals (`ai-agents/`)

There is **one unified agent service**. `ai-agents/main.py` is the single FastAPI entry point (see the `CMD` in `ai-agents/Dockerfile`); it serves the interactive chat agent (`shared/unified_agent.py`, a LangGraph ReAct agent with the full tool set) and starts all background loops in its lifespan: `AlertPoller`, `IncidentWorkflow`, `IntentEvaluator`, `OpsScheduler`, and the hourly policy-promotion sweep. The historical three-agent layout (ops/eng/chaos services on 8000/8001/8002) is retired — do not recreate per-agent `main.py` files.

**Shared layer (`ai-agents/shared/`):**
- `tools.py` — LangChain `@tool` functions organised in tiers: Nautobot discovery → Prometheus metrics → Loki logs → compliance → runbooks → KB → Ansible actions. `OPS_TOOLS` is the canonical list (`ENG_TOOLS` is a backward-compat alias).
- `unified_agent.py` — `UnifiedAgent` class: `create_react_agent` + combined system prompt; used by `/chat` and `/chat/stream`.
- `task_store.py` — Task queue + events + feedback + autonomy policies + standing intents + token usage. Backend chosen by `TASK_DB_URL`: Postgres (`agent-postgres`, the compose default) or SQLite (`activity.db`, WAL mode).
- `policy_registry.py` / `policy_resolver.py` — L0–L5 autonomy policies. Policies with `conditions` + templates form the **programmatic fast path** that resolves known alert patterns with zero LLM calls.
- `intent_registry.py` — Standing intents (suppress / escalate / monitor / chaos_schedule) plus the `IntentEvaluator` background thread.
- `activity_store.py` — Chat interaction log (message, response, latency, tool calls) for the Activity view.
- `config.py` — `pydantic-settings` `Settings`; reads `.env`. Notable flags: `ai_enabled` (AI-optional mode), `chaos_tools_enabled`, `environment` (lab/staging/production autonomy defaults).
- `status_tracker.py` — `AgentStatus` + `StatusCallbackHandler`; polled by the UI `/status` endpoint.
- `rate_limiter.py` — Token + cost budgets; `BudgetExceededError` → HTTP 429 on `/chat`.
- `auth.py` — `X-API-Key` auth for agent endpoints when `AGENT_API_KEY` is set (header only; no query-param auth).
- `task_bus.py` — Optional RabbitMQ publish/consume; no-op without `RABBITMQ_URL` (agents fall back to polling).
- Also: `kb_store.py` (knowledge base), `learning_engine.py`, `topology_correlator.py`, `notifications.py` (Slack/PagerDuty/webhook on approval gates), `metrics.py` (Prometheus `/metrics`), `pipeline_models.py` + `structured_output.py` (typed stage outputs).

**Workflow package (`ai-agents/ops_agent/` — name is historical):**
- `workflow.py` — `IncidentWorkflow`, a LangGraph `StateGraph`: `check_intents → policy_fast_path → [no_ai_gate | investigate → propose_fix → validate] → approval_gate`. All stages are events on a single `rca` task. Human approval triggers `resume_execution()` (check_mode=False + post-execution verification against Prometheus).
- `agent.py` — `OpsAgent` + the pipeline `SYSTEM_PROMPT` used by workflow nodes and the scheduler.
- `alert_poller.py` — Polls Alertmanager every 60 s as fallback; the `/webhook/alert` endpoint is the zero-latency primary path. Dedup by fingerprint; topology correlation for blast radius.
- `chaos_tools.py` — Ansible-backed `shutdown_interface`, `restore_interface`, `flap_bgp_neighbor`; only included when `CHAOS_TOOLS_ENABLED=true` (lab only).
- `scheduler.py` — APScheduler for repeating chaos/validation scenarios (`/schedule`, `/schedules`).

**UI (`ai-agents/ui/`):**
- `main.py` — FastAPI app on port 7860. Mounts `static/` and `templates/`. Uses `httpx.AsyncClient` for outbound agent calls and `run_in_threadpool` for store access.
- Session login: when `UI_PASSWORD` is set, all routes except `/login` and `/static` require the session cookie (HTMX requests get `HX-Redirect`). Unset = open UI with a startup warning (dev/lab only).
- `templates/` — Page templates extend `base.html`; partials in `templates/partials/` are HTML fragments consumed via HTMX polling and the `/stream/tasks` SSE channel.
- `static/htmx.min.js`, `static/style.css` — no CDN, no CSS framework.
- The `from_json` Jinja2 filter parses JSON task result/content strings inside templates.

### Closed-loop pipeline

When an alert fires (webhook or poller), the unified workflow runs all stages **as events on one `rca` task**, traceable by `alert_fingerprint`:

```
check_intents → policy_fast_path (no LLM if a policy matches)
             → investigate → propose_fix → validate → approval_gate (human)
approval (UI) → /workflow/resume/{task_id} → execute (check_mode=False) → verify_resolution
```

The autonomy policy decides whether the gate needs a human (L0–L3) or can auto-execute (L4–L5, earned via promotion after consecutive successes, with TTL re-validation). The UI's approve action must call `approve_task()` before resume — `resume_execution` guards on the approved event being present.

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
3. Update the tool guide in `shared/unified_agent.py` (interactive prompt) and, if pipeline-relevant, the `SYSTEM_PROMPT` in `ops_agent/agent.py`.
4. Follow the docstring convention in `docs/agent-tools-framework.md`.

### Nautobot data management

Inventory and IPAM data is declared in `nautobot/data_loader/data.yml`. The loader (`load_data.py`) runs inside the Nautobot container and performs full CRUD reconciliation (`make apply-data`). `make plan-data` is a safe dry-run. The loader uses a `state_store.py` to track previously managed objects so it can detect and delete removals.

### Environment and secrets

All configuration is in `.env` (gitignored). `.env.example` documents every variable. Agent configuration is loaded via `shared/config.py`'s `Settings` class. Services pick up variables via `env_file: .env` in `docker-compose.yml`.

---

## Key constraints

- **`check_mode=True` is the default for all Ansible action tools.** Never change this default — the validation stage and approval gate exist precisely to gate `check_mode=False` execution.
- **The task DB is shared.** The agent and UI containers read and write it concurrently. With the SQLite backend (`activity.db` volume) the `TaskStore` relies on WAL mode and a threading lock; the compose default is Postgres (`TASK_DB_URL`).
- **LLM selection is automatic.** `shared/llm.py` returns an OpenAI client if `OPENAI_API_KEY` is set, otherwise Ollama. Don't hardcode model clients in agent code.
- **Auth is opt-in but warn-by-default.** `AGENT_API_KEY` protects agent endpoints, `UI_PASSWORD` protects the web UI; leaving either unset logs a startup warning and disables that auth layer (lab convenience only).
