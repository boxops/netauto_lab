# Platform Roadmap and Status

*Last reviewed: 2026-06-12 (commit `c6218ea`)*

---

## What's Implemented

### Core pipeline (all complete)
- Unified LangGraph ReAct agent replacing the retired 3-agent layout
- L0–L5 `PolicyRegistry` with match criteria (`alertname` × `fix_type` × `device_role` × `environment`)
- Programmatic fast path — zero LLM calls for known alert patterns
- `LearningEngine` — auto-promotes after 3 consecutive successes (cap L4), demotes on failure
- `IntentRegistry` + `IntentEvaluator` — suppress / escalate / monitor / chaos_schedule
- `TopologyCorrelator` — BFS on Nautobot cable graph; blast-radius + cascading-failure inference
- Post-execution verification: config diff check + Prometheus alert resolution + TTR
- HMAC-signed approval webhooks with approve/reject links
- SSE real-time UI — Chronicle view, stage confidence/risk badges, intent-trigger badge
- Session authentication (`UI_PASSWORD`), API key auth (`AGENT_API_KEY`)
- RabbitMQ optional dispatch bus; polling loop fallback

### Phase 7 additions (commit `13655da`)
- **Event loop fix** — `/chat` dispatched to threadpool so `/health`, `/webhook/alert` stay live during long chats
- **Webhook secret** — `ALERT_WEBHOOK_SECRET` rejects unauthenticated Alertmanager posts
- **Approvals tab** — dedicated queue sorted by risk×age; bulk approve; `j`/`k`/`a`/`r` keyboard navigation
- **Self-grading eval loop** (`shared/eval_engine.py`) — chaos injections recorded as ground truth; background sweep grades pipeline responses into `eval_results`; **AI Accuracy Ledger** on the System page
- **Policy synthesis** (`shared/policy_synthesizer.py`) — when the AI pipeline resolves the same pattern ≥ N times (default 3), compiles a DRAFT fast-path policy created disabled for operator review
- **Restart resilience** — interval jobs persisted to `scheduled_jobs` table; chat threads persist via `SqliteSaver` (or `MemorySaver` fallback)
- **UI refactor** — `ui/main.py` split: policy/intent YAML codec → `ui/yaml_codec.py`; route clusters → `ui/routers/`

### Operations visibility (partially complete)
- `shared/alert_journal.py` — `alert_journal` table with one decision record per alert ingress (investigating / fast_path / suppressed_by_intent / deduplicated / not_firing / etc.)
- 10 recording call sites across `alert_poller.py` and `workflow.py`
- Funnel strip + action stream replace old dual-pane on the Operations page
- Decision-journal banner in the inspector; `fast_path_resolved` events carry full condition definitions
- **Remaining:** per-condition observed-value traces; per-stage LLM cost chips

---

## Remaining Work

### P1 — High impact, moderate effort

| Item | Description | Effort |
|---|---|---|
| Per-condition evaluation traces | Record observed values for each fast-path condition check (pass AND fail); `fast_path_skipped` event when candidates existed but none matched | 1 d |
| Per-stage cost chips | Aggregate `token_usage` by `task_id` into stage-level cost chips in the chronicle inspector | 1 d |
| Named users + per-user audit | Replace `approved_by="human"` with real identity; RBAC (viewer / operator / approver / admin) | 2–3 wk |

### P2 — Quality and maintainability

| Item | Description | Effort |
|---|---|---|
| Split `task_store.py` into repositories | Module owns 6 unrelated table families; add Alembic migrations | 1 wk |
| SSE everywhere | Drop remaining 2–3 s polling panels; use existing SSE channel | 3 d |
| Non-LLM deterministic validation | Library of deterministic pre-validation checks for known fix types (e.g., "interface must be oper-up before approval gate") | 3 d |
| Proactive runbook gap detection | After each LLM-generated fix with no runbook, write a candidate to a `runbook-candidates` Gitea branch for operator review | 2 d |

### P3 — Enterprise / future

| Item | Description |
|---|---|
| OIDC SSO + real multi-tenancy | `tenant_id` exists in schema; gap is auth-bound filtering + UI |
| Helm chart | Compose is right for lab; Helm for multi-replica production |
| Report export | Weekly ops summary (MTTR, autonomy promotions, LLM spend) |
| Multi-tenant UI | Filter Operations, Task Queue, Incidents by `tenant_id` |

---

## Competitive Differentiators

Three capabilities no incumbent (Cisco Catalyst Center, Juniper Mist, Datadog, Dynatrace, Splunk) currently offers:

1. **Earned autonomy ladder** — the system must prove itself N times per fix-class per device-role before it acts alone, and the privilege expires (TTL re-validation). Fully auditable per-policy.
2. **Closed-loop ground truth** — Clano injects faults it knows (chaos schedule), watches its own pipeline diagnose them, and scores itself. A self-grading NOC agent with a published accuracy ledger.
3. **Deterministic fast path as a learning target** — when the LLM resolves the same pattern k times, the Policy Synthesizer drafts a fast-path policy (conditions + templates) for human review. Over time, marginal cost per incident approaches zero.

**What not to compete on:** dashboards, log search UX, alert dedup, on-call paging — integrate with Grafana and PagerDuty instead.
