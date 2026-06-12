# Clano Platform Review — Full Audit, Product Evaluation, and Refactoring Roadmap

*Review date: 2026-06-12. Reviewed at commit `c6218ea` ("intents rework").*

This document covers Phases 1–6 of the platform review. Phase 7 (implemented
refactors) is tracked in the accompanying commits.

---

## Phase 1 — Codebase Audit

### 1.1 Backend / AI-agent architecture

**Current implementation.** A single unified LangGraph ReAct agent
(`shared/unified_agent.py`) served by `ai-agents/main.py` (FastAPI, port 8000).
The closed-loop pipeline is a LangGraph `StateGraph`
(`ops_agent/workflow.py`, 1 838 lines) with nodes:
`check_intents → policy_fast_path → [no_ai_gate | investigate → propose_fix →
validate] → create_approval_gate`, resumed after human approval by
`resume_execution()`. Alert intake is dual-path: Alertmanager webhook (instant)
plus a 60 s poller fallback (`alert_poller.py`). Layered on top: autonomy
policies (L0–L5 with promotion/TTL), a programmatic fast path that resolves
known alert patterns with **zero LLM calls**, standing intents
(suppress/escalate/monitor/chaos_schedule), topology correlation,
a learning engine, and a knowledge base.

**Strengths.**
- The autonomy-policy + fast-path design is genuinely good: deterministic
  resolution for known patterns, LLM only for novel incidents, human gates by
  default, earned autonomy with TTL'd promotion and non-promotable wildcards.
  This is the platform's core IP.
- `ai_enabled=False` default ("AI-optional mode") is the right safety posture.
- Structured outputs (`pipeline_models.py` + `parse_structured`) instead of
  free-text parsing between stages.
- Graceful degradation everywhere: RabbitMQ optional → polling fallback;
  Postgres optional → SQLite fallback; OpenAI optional → Ollama fallback.

**Weaknesses / risks.**
- **Architecture drift and dead code (severity 7).** CLAUDE.md, `docs/architecture.md`
  and the test suite still describe the retired 3-agent design.
  `ops_agent/main.py` (554 lines) is a dead duplicate of the live
  `ai-agents/main.py` — nothing runs it, but it is still edited in commits
  (wasted, divergent maintenance: the live `/chat` lacks the audit-task logic
  the dead one has). `engineering_agent/` and `chaos_agent/` are empty shells.
  Two system prompts exist (`unified_agent.py` and `ops_agent/agent.py`).
- **Blocking event loop (severity 8).** `POST /chat` in `ai-agents/main.py` is
  `async def` but invokes the synchronous ReAct loop inline. Every chat
  (10–60 s) freezes `/health`, `/status`, and `/webhook/alert` — the UI's 2 s
  status polls stall and Docker healthchecks can flap during chats.
  *(Fixed in Phase 7.)*
- **Untestable inline I/O (severity 6).** `_node_policy_fast_path` does a raw
  `httpx.get` to Nautobot for device-role lookup. The `strict_role=True`
  safety change (commit `6aef361`) silently broke two unit tests that can't
  mock the lookup. The tests have been red since. *(Fixed in Phase 7.)*
- `workflow.py` (1 838 lines) and `task_store.py` (1 517 lines) are god
  modules. `task_store` owns six unrelated table families (tasks, events,
  feedback, policies, intents, token usage, policy performance).
- Background work is `threading.Thread(daemon=True)` everywhere — no
  supervision, no restart on crash, no graceful drain on shutdown.

**Refactor priority: P0** (event loop, dead code, red tests), **P2** (module splits).

### 1.2 Frontend architecture

**Current implementation.** Server-rendered FastAPI + Jinja2 + HTMX
(`ai-agents/ui/main.py`, 3 533 lines, ~100 routes), dark-theme custom CSS, no
build step, HTMX polling at 2–30 s intervals plus one SSE endpoint
(`/stream/tasks`).

**Strengths.** Zero-build, zero-CDN stack is perfect for an air-gapped lab.
`run_in_threadpool` is used consistently for DB access; outbound agent calls
use `httpx.AsyncClient`. The partial-template pattern is coherent.

**Weaknesses.** A 3 533-line single module mixing routing, presentation
helpers (SVG chart generation in Python!), YAML codecs for policies/intents,
and business logic. Polling-heavy design (the SSE endpoint exists but most
panels still poll). No pagination on task/activity lists.

**Severity 5. Priority P2** — split into APIRouter modules
(`routes_pipeline.py`, `routes_policies.py`, `routes_intents.py`,
`routes_kb.py`, `yaml_codec.py`, `viz.py`); migrate the 2–3 s polls to the
existing SSE channel.

### 1.3 Database design

**Current implementation.** SQLAlchemy Core with hand-written dialect-switched
DDL and idempotent `ALTER TABLE` migrations; SQLite (WAL) on a shared Docker
volume by default, Postgres via `TASK_DB_URL` (compose default already points
at `agent-postgres`). Separate `activity_store` for chat history; `kb_store`
for the knowledge base.

**Strengths.** Backend-swappable behind one interface; WAL + lock discipline;
sensible indexes including `(tenant_id, status)`.

**Weaknesses.** Hand-rolled migration list will not scale (no ordering,
no version stamps — use Alembic when the schema stabilises). Timestamps stored
as TEXT. `tenant_id` is threaded through every query but **nothing
authenticates a tenant** — it's a column, not a boundary. JSON blobs in TEXT
columns (`content`, `result`, `conditions`) are unqueryable in SQLite.

**Severity 4. Priority P2–P3** (Alembic, JSONB on Postgres).

### 1.4 API design

Reasonably RESTful agent API (`/tasks`, `/policies`, `/intents` CRUD;
`/workflow/resume/{id}`). Weak spots: no API versioning (`/v1/`), mixed
casing conventions in responses, `PATCH /tasks/{id}` accepts free-form status
transitions without a state machine guard, and the UI partials double as an
unofficial API. **Severity 4, P2.**

### 1.5 Authentication & authorization

- Agent API: single shared API key (`X-API-Key` or — until Phase 7 — the
  `?api_key=` query param, which leaks into access logs and browser history).
  Dev-mode bypass with startup warning when unset. No roles, no per-user
  identity, no audit attribution (`approved_by="human"` literally).
- **The UI on port 7860 had no authentication at all** (severity 9): anyone
  who can reach the port can approve remediations, toggle AI mode, edit
  policies that grant L4 auto-execution, and clear the task queue. The UI holds
  the agent API key, so the agent-side key is moot for anyone who can reach
  the UI — a classic confused deputy. *(Session login added in Phase 7.)*
- `/webhook/alert` is intentionally public; a forger can inject alerts and
  burn LLM budget / trigger pipelines. Mitigate with a shared-secret header
  Alertmanager can send (`http_config.authorization`), P1.
- No CSRF protection; mitigated in Phase 7 by `SameSite=Strict` session cookie.

**Severity 9 → addressed to ~5. Remaining priority P1** (webhook secret,
named users + audit attribution).

### 1.6 State management

Pipeline state lives in the DB as task events (good — restart-safe,
auditable). In-process state that does **not** survive restarts: LangGraph
`MemorySaver` checkpoints (chat history), APScheduler jobs (in-memory store),
alert-poller dedup state (has `/poller/reset` as an escape hatch), rate-limiter
counters (recomputed from `token_usage` — fine). **Severity 5, P2:** persist
scheduler jobs (SQLAlchemyJobStore) and chat threads (SqliteSaver).

### 1.7 AI architecture

**Strengths.** Tiered tool model (runbooks → discovery → metrics → logs →
actions) with per-alert focus hints; structured stage outputs; budget
enforcement (429 on breach); KB auto-save of successful fixes; post-execution
verification against Prometheus; lab-validation option before prod execution.

**Weaknesses.**
- `ENG_TOOLS = OPS_TOOLS` alias and tool-count comments in docstrings that
  drift from reality.
- The 1 600-line `tools.py` returns formatted strings; per-tool truncation
  policies are inconsistent — token-bloat risk on large inventories.
- No evaluation harness: nothing scores RCA accuracy against the chaos
  agent's known fault injections. **This is the single biggest missed
  opportunity** — the platform injects faults with ground truth and never uses
  it to grade the LLM (see Phase 3/4).
- Prompt text duplicated across `unified_agent.py` / `ops_agent/agent.py`.

**Severity 6, P1** (eval harness), **P2** (tool output budgets).

### 1.8 Infrastructure & deployment

Docker Compose, four segmented networks, healthchecks, log rotation, optional
prod overlay (`docker-compose.prod.yml`). Suitable for a lab; no HA story, no
backup/restore automation for the agent DB, secrets via `.env` plaintext.
**Severity 4 for the lab's stated purpose, P3** (K8s/Helm only if
productising).

### 1.9 Observability (of Clano itself)

`/metrics` Prometheus endpoint, status tracker, activity log, LangSmith
opt-in. Missing: structured logging (currently free-text), trace correlation
between UI → agent → workflow stages, and dashboards for pipeline KPIs
(MTTR, fast-path hit rate, gate latency) that exist in the DB but aren't
exported. **Severity 5, P2.**

### 1.10 Developer experience & technical debt

Excellent: Makefile, mkdocs, 33-file test suite (162 unit tests, fast), marks
for unit/integration. The debt is concentrated and known: dead 3-agent
artifacts, two red tests, doc drift, god modules. **Severity 5, P0/P1 — cheap
to clear now, expensive in six months.**

---

## Phase 2 — Product Evaluation (enterprise-customer lens)

**Onboarding / FTUE.** `make init && make start` is good for engineers; there
is no in-product onboarding. First screen (Pipeline) is empty until an alert
fires — a new user can't tell if the system works. *Add a "fire test alert"
button and a setup checklist card (Nautobot synced? Prometheus targets up?
LLM reachable?).*

**Daily workflow.** The operator loop (see approval gate → review evidence →
approve) is genuinely strong: the chronicle view reconstructs the full
investigation. Friction points:
1. Approval requires navigating to Pipeline → fingerprint → gate task. The
   pending-approvals partial exists but isn't a first-class queue with
   keyboard actions.
2. No bulk operations (approve N similar gates from a flap storm).
3. Rejection asks for a reason but nothing learns from it (the learning
   engine only consumes successes).
4. Currency preference cookie but no timezone handling — all timestamps UTC.

**Navigation / IA.** Six tabs (Pipeline, Incidents, Assist, Knowledge, Config,
System) is the right altitude. "Pipeline" vs "Incidents" overlap confuses —
both show alert-driven work; merge into one Incidents view with a pipeline
detail pane (see Phase 5).

**Over-engineered / remove or merge.**
- Two parallel policy editors (form builder + YAML editor) and now two intent
  editors. Keep YAML as source of truth, generate the form from the schema, or
  drop the form.
- Per-agent chat routes (`/chat/{agent_name}`) when there is one agent.
- The currency-conversion cost display (USD/EUR/HUF) is charming but noise —
  cost belongs on the System page, not a nav-level concern.
- `ops_agent/main.py` (dead), `ENG_TOOLS` alias, empty agent packages.

**Missing for enterprise.** Named users/SSO, RBAC (operator vs approver vs
admin), real multi-tenancy (auth-bound, not a query param), report export
(weekly ops summary PDF/email), data retention policies, backup/restore,
change-management integration (ServiceNow ticket per executed fix).

---

## Phase 3 — Competitive Analysis

| Capability | ServiceNow | Datadog | Dynatrace | Splunk | PagerDuty | **Clano** |
|---|---|---|---|---|---|---|
| Alert→RCA automation | workflows, manual | correlations | Davis AI (causal) | ML anomaly | event intel | **LLM + topology + deterministic fast path** |
| Closed-loop remediation | runbook automation | webhooks | workflows | SOAR | runbook (Rundeck) | **native, human-gated, autonomy levels** |
| Earned autonomy (promote after N successes, TTL) | ✗ | ✗ | ✗ | ✗ | ✗ | **✓ unique** |
| Fault-injection validation of fixes | ✗ | ✗ | ✗ | ✗ | ✗ | **✓ unique (chaos tools + lab validation)** |
| Network source-of-truth grounding | CMDB (stale) | ✗ | topology (APM) | ✗ | ✗ | **Nautobot live intent** |
| Multi-tenant SaaS, RBAC, SSO | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| Reporting/analytics | ✓ | ✓ | ✓ | ✓ | ✓ | weak |

**Commodity (don't compete):** dashboards, log search UX, alert dedup,
on-call paging. Integrate (Grafana, PagerDuty) instead of building.

**Category-defining differentiators to double down on:**
1. **Autonomy ladder with earned promotion** — no incumbent has "the system
   must prove itself N times per fix-class per device-role before it may act
   alone, and the privilege expires." This is the product. Make it visible,
   reportable, and auditable.
2. **Closed-loop with ground truth.** Clano can inject a fault it knows
   (chaos schedule), watch its own pipeline diagnose it, and **score itself**.
   A continuously self-grading NOC agent with a published accuracy ledger is a
   10x trust story no APM vendor can match — they have no ground truth.
3. **Deterministic fast path as a learning target:** when the LLM resolves the
   same alert class successfully k times, *synthesise a fast-path policy
   draft* (conditions + templates) for human review. LLM → compiled playbook.
   Today policies are hand-written; this closes the actual learning loop and
   drives marginal cost per incident toward zero.

---

## Phase 4 — Future Vision (greenfield design)

If rebuilt today, keep the shape, change the substrate:

- **System:** one control-plane service (API + workflow orchestrator) and one
  UI, exactly as now converged — the unified-agent decision was correct.
  Replace ad-hoc daemon threads with a supervised worker (e.g. `arq`/Celery or
  a single asyncio supervisor) so background loops restart and drain cleanly.
- **Data:** Postgres-only in anything beyond a laptop (the compose default
  already is); Alembic migrations; events as the source of truth (the current
  task_events design is already event-sourced in spirit — formalise it);
  JSONB for stage payloads.
- **Agent/AI:** one ReAct agent for interactive use + the StateGraph pipeline
  (as now), plus the missing third leg: an **evaluation loop** that replays
  chaos-injected ground truth through the pipeline nightly and publishes
  accuracy/MTTR/cost per alert class. Policy synthesis from repeated LLM
  successes (Phase 3 §3). Tool outputs get hard token budgets.
- **Security:** OIDC SSO, roles (viewer/operator/approver/admin), per-user
  audit on every gate decision, tenant = authenticated org not query param,
  webhook HMAC everywhere (the approval webhook already signs — apply the same
  pattern inbound).
- **Deployment:** Compose for lab (keep), Helm chart for enterprise; secrets
  via mounted store, not `.env`.

Why superior: each change removes a class of failure (thread death, schema
drift, identity-free audit trails) without discarding the validated core —
the autonomy/policy/fast-path engine.

---

## Phase 5 — UX Transformation

**Navigation:** collapse Pipeline + Incidents into **Incidents**; promote a
persistent **Approval queue** badge in the header (count + oldest-age); move
cost into System. Result: Incidents / Assist / Knowledge / Automation
(policies+intents+schedules) / System.

**Incident workspace (wireframe):** three-pane: left = incident list with
status/severity filters and SSE live updates; centre = chronicle timeline
(stages as chapters, evidence inline, diff viewer for config changes); right =
action rail (Approve / Edit commands / Reject-with-reason, blast-radius map,
autonomy decision explanation "why is this gated?"). Keyboard: `j/k` navigate,
`a` approve, `r` reject. Today this flow takes ~6 clicks across two tabs;
target ≤2 interactions from landing.

**Approval queue:** dedicated view sorted by risk×age, grouped by fingerprint
family so a flap storm is one card ("12 instances of InterfaceDown on leaf
pods — approve all / inspect"). Each card answers: what happened, what will be
run, on what device, what the agent verified, what happens if wrong (rollback).

**Executive view:** a single System-page strip: incidents auto-resolved vs
human-gated this week, MTTR trend sparkline, autonomy promotions/demotions,
LLM spend vs budget. All data already exists in `policy_performance` and
`token_usage` — it's an aggregation query, not a feature.

**Agent interaction:** keep chat, but anchor it to context — "Ask about this
incident" button on the chronicle pre-fills the session with the fingerprint
context instead of a cold chat.

---

## Phase 6 — Prioritised Roadmap

| # | Item | Pri | Bus. value | Tech value | Cmplx | Effort | Deps | Risk |
|---|---|---|---|---|---|---|---|---|
| 1 | Un-block `/chat` (threadpool) | **P0** | UI stays live during chats | event-loop hygiene | low | hours | — | none |
| 2 | Fix 2 red fast-path tests (extract role lookup) | **P0** | trust in CI | testable workflow | low | hours | — | none |
| 3 | UI session authentication | **P0** | closes critical exposure | — | low | 1 day | — | lockout (dev bypass kept) |
| 4 | Drop `?api_key=` query auth | **P0** | stops key leakage in logs | — | low | hours | — | breaks clients using it |
| 5 | Delete dead `ops_agent/main.py`, fix doc drift (CLAUDE.md) | **P0** | — | kills divergent maintenance | low | hours | — | none |
| 6 | Alertmanager webhook shared secret | P1 | alert-forgery defence | — | low | hours | alertmanager.yml | config rollout |
| 7 | Approval-queue UX (grouped cards, keyboard) | P1 | operator efficiency 3–5× | — | med | 1 wk | 3 | — |
| 8 | Self-grading eval loop (chaos ground truth → accuracy ledger) | P1 | the 10x trust story | regression net for prompts | med | 1–2 wk | — | — |
| 9 | Policy synthesis from repeated LLM successes | P1 | marginal cost → 0 | closes learning loop | high | 2 wk | 8 | bad auto-policies (keep human review) |
| 10 | Split `ui/main.py` into routers + yaml_codec | P2 | — | maintainability | med | 3 d | — | template regressions (tests cover) |
| 11 | Split `task_store.py` into repositories; Alembic | P2 | — | schema safety | med | 1 wk | — | migration bugs |
| 12 | Persist scheduler jobs + chat checkpoints | P2 | restart resilience | — | low | 2 d | — | — |
| 13 | SSE everywhere (drop 2–3 s polls) | P2 | snappier UI, less load | — | med | 3 d | 10 | — |
| 14 | Named users, RBAC, per-user audit | P2 | enterprise gate | — | high | 2–3 wk | 3 | — |
| 15 | Merge Pipeline+Incidents views | P2 | IA clarity | — | med | 1 wk | 7 | — |
| 16 | OIDC SSO, real multi-tenancy, Helm | P3 | enterprise sales | — | high | months | 14 | — |
| 17 | Report export (weekly ops summary) | P3 | exec visibility | — | low | 3 d | — | — |

**Phase 7 implements items 1–9** (all P0 and all P1). See commit history for
the diffs:

- Item 6: `ALERT_WEBHOOK_SECRET` — agent rejects unauthenticated `/webhook/alert`
  posts when set (`shared/auth.py::require_webhook_secret`); Alertmanager wiring
  documented inline in `prometheus/alertmanager.yml` (Bearer `credentials_file`).
- Item 7: new **Approvals** tab (`/approvals`) — gates grouped by alertname,
  highest-risk group first, oldest card first; per-card Approve & execute /
  Reject-with-reason; "Approve all (N)" per group; `j`/`k`/`a`/`r` keyboard
  navigation; live refresh via the existing SSE channel; deep link from each
  card back to its investigation chronicle (`/?fp=...`).
- Item 8: **self-grading eval loop** (`shared/eval_engine.py`). Every chaos
  injection executed with check_mode=False is recorded as ground truth
  (`chaos_injections`); a background sweep grades the pipeline's response
  (detected / correct device / correct cause via deterministic keyword
  signatures / resolved / TTD / TTR) into `eval_results`. The aggregate
  **AI Accuracy Ledger** renders on the System page. Config:
  `EVAL_GRADING_ENABLED`, `EVAL_GRADING_INTERVAL`, `EVAL_MIN_AGE_SECONDS`,
  `EVAL_MATCH_WINDOW_SECONDS`.
- Item 9: **policy synthesis** (`shared/policy_synthesizer.py`). When the AI
  pipeline resolves the same (alertname, fix_type, device_role) pattern with
  identical generalized commands ≥ N times (verified resolutions only,
  fast-path excluded), the hourly sweep compiles a DRAFT fast-path policy
  (conditions from per-alert recipes + rca/fix templates). Drafts are created
  **disabled** with provenance in the description; an operator reviews and
  enables. Config: `POLICY_SYNTHESIS_ENABLED`,
  `POLICY_SYNTHESIS_MIN_SUCCESSES`. Unknown alert types and inconsistent fix
  histories are never compiled.
- Item 12: **restart resilience**. Interval jobs from `POST /schedule` are
  persisted to a `scheduled_jobs` table and re-registered on startup
  (`ops_agent/scheduler.py` — metadata persistence, deliberately not
  APScheduler's pickle job store, since our jobs are closures). Chat threads
  persist via `shared/checkpoints.py`: SqliteSaver when the optional
  `langgraph-checkpoint-sqlite` package is installed (default path next to the
  activity DB, `CHAT_CHECKPOINT_DB` overrides), MemorySaver fallback otherwise.
- Item 10: `ui/main.py` split underway — 3 727 → 2 479 lines.
  (a) Policy/Intent ↔ YAML codec and blueprints (~500 lines of pure functions)
  extracted to `ui/yaml_codec.py` (old `ui.main._yaml_to_policy` names aliased).
  (b) Policy, intent, and knowledge-base route clusters moved to APIRouter
  modules under `ui/routers/`. Routers access shared singletons lazily via the
  `ui.main` module object (`M.task_store` etc.), so tests patching
  `ui.main.task_store` keep working; the routers are imported at the bottom of
  `ui/main.py`, making the circular import safe. Remaining: chat/activity,
  schedules, and pipeline/system clusters (the latter shares the chronicle
  helper stack — move helpers together with routes).
