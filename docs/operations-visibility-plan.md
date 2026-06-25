# Operations Visibility Redesign — Implementation Plan

*Plan date: 2026-06-12. Targets the Operations page ("Pipeline" tab) and the
alert-handling backend.*

> **Status (2026-06-12):** Phases A, B and the core of C are implemented —
> `shared/alert_journal.py` + 10 recording call sites + retention prune;
> funnel strip + action stream replace the old dual left pane on Operations;
> decision-journal banner in the inspector (renders even when no pipeline
> ran); fast_path_resolved events now carry full condition definitions.
> Remaining from C: per-condition observed-value traces and per-stage cost
> chips. Tests: tests/test_alert_journal.py, tests/test_action_stream.py.

## 1. Problem statement

The operator's complaint — "some tasks are just skipped and I'm not sure
what's going on" — is accurate, and it is a **data problem before it is a UI
problem**. The system makes many correct decisions that leave **no queryable
record**, only a container log line. The UI cannot show what was never stored.

### 1.1 Silent paths (alert handled, nothing visible in any UI)

| # | Path | Where | What the operator sees |
|---|------|-------|------------------------|
| 1 | Severity not in allow-list | `alert_poller._classify_event` | nothing |
| 2 | Deduplicated (fingerprint already seen) | `_classify_event` (`_seen` map) | nothing |
| 3 | "No longer firing in Prometheus" double-check fails | `_classify_event` | nothing |
| 4 | Alert resolved → incident closed, stale gates auto-rejected | `_try_close_incident` / `_auto_reject_stale_gates` | gate *disappears* from Approvals with no trace of why |
| 5 | LLM budget exceeded → deferred to next cycle | `alert_poller._investigate` | nothing (alert looks stuck) |
| 6 | Active task already exists for fingerprint | `_investigate` | nothing new |
| 7 | **Suppressed by standing intent** | `workflow._node_check_intents` → `no_action` ends the graph **before any task is created** | nothing — the single worst offender |
| 8 | Webhook `push_alert` returns False (dedup/filter) | `main.py /webhook/alert` | counted in an HTTP response nobody reads |

### 1.2 Visible-but-confusing paths

| # | Path | Why it confuses |
|---|------|-----------------|
| 9 | Alert correlated onto an existing device task (`alert_correlated` event) | the new alert's fingerprint has **no pipeline of its own**; searching for it finds nothing |
| 10 | Downstream alert folded into upstream RCA (`downstream_alert`) | same — the link lives on the *other* task |
| 11 | Fast path + L4 → auto-approved → executed in seconds | the whole lifecycle happens between two poll refreshes; operator never sees it move |
| 12 | `no_ai_skipped` gates (AI-optional mode) | gate appears with no investigation and no explanation of *why* there's no diagnosis |
| 13 | Autonomy decision (who gated / who auto-approved and why) | `AutonomyDecision.reason` is stored in events but buried in raw JSON |
| 14 | Fast-path condition evaluation | only *matched* conditions are recorded; when conditions fail and the AI takes over, there is no trace of what was checked |

## 2. Design principles

1. **No silent drops.** Every alert ingress produces exactly one durable
   *decision record*, whatever the outcome. If Clano decides to do nothing,
   "nothing" is itself a recorded, explained decision.
2. **Every state shows its "why".** Each transition displays the intent /
   policy / config flag / correlation that caused it, by name, with a link.
3. **One page to follow everything.** The Operations page becomes:
   funnel summary (top) → action stream (left) → decision inspector (right).

## 3. Component A — Alert Journal (backend keystone)

New module `shared/alert_journal.py`, same pattern as `eval_engine.EvalStore`
(reuses the TaskStore engine/lock; SQLite + Postgres dialects).

```sql
CREATE TABLE IF NOT EXISTS alert_journal (
    id             {serial},
    tenant_id      TEXT NOT NULL DEFAULT 'default',
    fingerprint    TEXT NOT NULL,
    alertname      TEXT NOT NULL DEFAULT '',
    device         TEXT NOT NULL DEFAULT '',
    severity       TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',   -- webhook | poller | intent_monitor
    decision       TEXT NOT NULL,              -- enum below
    reason         TEXT NOT NULL DEFAULT '',   -- human sentence, e.g. "Suppressed by intent 'maintenance leaf1'"
    ref_task_id    TEXT NOT NULL DEFAULT '',   -- task investigated / correlated into
    ref_id         TEXT NOT NULL DEFAULT '',   -- intent_id / policy_id when applicable
    received_at    TEXT NOT NULL
);
CREATE INDEX idx_journal_fp   ON alert_journal(fingerprint, received_at);
CREATE INDEX idx_journal_time ON alert_journal(tenant_id, received_at);
```

Decision enum (one per row): `investigating`, `fast_path`,
`suppressed_by_intent`, `escalated_by_intent`, `deduplicated`,
`not_firing`, `resolved_cleared`, `severity_filtered`, `budget_deferred`,
`already_active`, `correlated_into`, `downstream_of`, `maintenance_flagged`.

API: `journal.record(decision, event, reason, ref_task_id="", ref_id="")`,
`journal.entries(filters…, limit)`, `journal.funnel(hours=24)` (grouped
counts), `journal.for_fingerprint(fp)`, `journal.prune(days)` (config
`JOURNAL_RETENTION_DAYS`, default 14; called from the hourly sweep).

**Recording call sites** (each is a 2–5 line insertion; the journal must be
best-effort/never-raise like `chaos_tools._record_injection`):

| Call site | Decision |
|---|---|
| `_classify_event` severity branch | `severity_filtered` (record once per fingerprint, not per poll — guard with the `_seen` key) |
| `_classify_event` dedup branch | `deduplicated` (same once-per-transition guard) |
| `_classify_event` not-firing branch | `not_firing` |
| `_classify_event` resolved branch | `resolved_cleared` (+ list auto-rejected gate IDs in reason) |
| `_investigate` budget branch | `budget_deferred` |
| `_investigate` existing-task branch | `already_active` (ref_task_id) |
| `_investigate` correlation branches | `correlated_into` / `downstream_of` (ref_task_id) |
| `_investigate` success → workflow start | `investigating` (ref_task_id once known) |
| `_node_check_intents` suppress/escalate | `suppressed_by_intent` / `escalated_by_intent` (ref_id=intent) |
| `_node_policy_fast_path` resolution | `fast_path` (ref_task_id, ref_id=policy) |

Dedup/severity records use the existing `_seen` transition guard so a
flapping alert writes one row per state change, not one per 60 s poll.

## 4. Component B — Operations page (3-zone layout)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 24h funnel:  37 received → 9 investigated → 6 fast-path → 4 gated →          │
│              3 auto-executed → 11 resolved   ·   18 suppressed/dropped ▾     │
│              (every number is a click-filter on the stream below)            │
├───────────────────────────┬──────────────────────────────────────────────────┤
│ ACTION STREAM             │ INSPECTOR (selected fingerprint)                 │
│ ◉ 10:42 BGPPeerDown leaf2 │ ┌ Decision banner ─────────────────────────────┐ │
│   ⚡ fast-path · policy   │ │ Received via webhook 10:41:58 · investigated │ │
│   "BGPPeerDown lab leaf"  │ │ Journal: 3 records for this fingerprint  ▸   │ │
│   → auto-executed (L4)    │ └──────────────────────────────────────────────┘ │
│ ○ 10:40 InterfaceDown sp1 │ 1 · Fast path  ✓ policy "InterfaceAdminDown…"   │
│   ⏸ awaiting approval     │     conditions: ifAdminStatus expected 2,        │
│ ◌ 10:38 IfaceDown leaf3   │     observed 2 ✓                       [why? ▾] │
│   🔇 suppressed by intent │ 2 · Approval gate  L4 auto-approved              │
│   "leaf3 maintenance"     │     why: policy …, 3 prior successes   [why? ▾] │
│ ◌ 10:31 BGPPeerDown leaf2 │ 3 · Execute  check_mode=False · diff ▾           │
│   ↳ deduplicated (seen    │ 4 · Verify  ⏳ checking Prometheus in 38 s       │
│     10:29, task #ab12)    │     cost: $0.000 (fast path — no LLM)            │
│ [All|Active|Auto|Dropped] │                                                  │
└───────────────────────────┴──────────────────────────────────────────────────┘
```

- **Funnel strip** (`/partials/ops-funnel`): `journal.funnel(24h)` + gate/exec
  counts from tasks. Replaces nothing — sits under the existing health bar
  (later: merge). Clicking a segment sets the stream filter.
- **Action stream** (`/partials/action-stream`): merges *journal entries* and
  *task state* into one reverse-chronological list grouped by fingerprint.
  Replaces both the "Active Incidents" list and the "Recent Events" live feed
  (one list instead of two half-views was the explicit confusion source).
  Filter chips: `All · Needs me · Handled automatically · Dropped/suppressed`;
  free-text device/alert filter. Each row: status icon, time, alert, device,
  decision chip, one-line reason, click → inspector.
- **Inspector** (`/partials/chronicle` upgraded — keep route): see Component C.
- SSE: journal writes piggyback the existing `tasks-changed` event (rename not
  needed); stream/funnel/inspector all subscribe via existing `sse-refresh`.

## 5. Component C — Inspector "why" surfaces

1. **Decision banner**: journal rows for this fingerprint rendered at top —
   including pre-pipeline drops ("deduplicated at 10:31") and cross-links for
   `correlated_into` / `downstream_of` ("folded into task ab12 →"). The
   reverse link renders on the host task too (events already exist:
   `alert_correlated`, `downstream_alert`, `downstream_consequence`).
2. **Why-expander per stage** (collapsed by default, one click):
   - check_intents → matched intent name + link to /intents.
   - fast path → **condition evaluation table**: each condition's query,
     expected, observed value, pass/fail. Requires `PolicyResolver` to also
     return/record *failed* evaluations: extend `ResolverResult` with
     `evaluations: list[{query, expect, observed, passed}]` and add a
     `fast_path_skipped` task event when candidates existed but none matched
     (currently a log line only).
   - approval gate → `AutonomyDecision.reason`, policy name + link, prior
     success count, promotable/TTL state.
   - no_ai gate → explicit banner: "AI investigation is disabled
     (`AI_ENABLED=false`) — manual review required" with link to Config.
3. **Timing + cost chips** per stage: gap between events (helpers exist:
   `_fmt_gap`/`_seconds_between`); LLM cost per stage from `token_usage`
   (has `task_id` — needs a `get_task_cost(task_id)` aggregate).
4. **"What happens next" hints** on non-terminal states: awaiting approval →
   "approve in Approvals tab (gate auto-rejects if the alert resolves)";
   executing → "verification queries Prometheus in {EXECUTION_VERIFY_DELAY}s".

## 6. Phases, estimates, tests

| Phase | Scope | Est. | Tests |
|---|---|---|---|
| **A** | `shared/alert_journal.py` + 10 recording call sites + retention prune in hourly sweep | 1 d | `tests/test_alert_journal.py`: every decision path writes exactly one row (drive `_classify_event` / `_investigate` with mocked store, real journal); once-per-transition guard; funnel aggregates; prune |
| **B** | funnel + action-stream partials; retire `active-pipelines` + `live-feed` panes from pipeline.html; filters; SSE wiring | 1–1.5 d | UI route tests (TestClient, mocked stores): stream renders all decision chips; filters; funnel counts; empty states |
| **C** | resolver evaluation traces, `fast_path_skipped` event, decision banner, why-expanders, timing/cost chips, next-step hints | 1–1.5 d | resolver unit tests (evaluations recorded on pass AND fail); chronicle context tests; cost aggregate test |
| **D** | polish: onboarding copy rewrite, keyboard (j/k from Approvals), CLAUDE.md + docs, drop dead partials | 0.5 d | full suite green; `make lint` |

Phase A ships value alone (journal queryable via DB/API) and B/C/D each ship
independently — no big-bang cutover.

## 7. Risks / mitigations

- **Journal volume** from flapping alerts → once-per-transition guard +
  `JOURNAL_RETENTION_DAYS` prune (default 14).
- **Two sources of truth** (journal vs tasks) → journal rows always carry
  `ref_task_id` when a task exists; the stream renders task state as primary
  and journal as the explanation layer.
- **Replacing the live feed breaks habits** → the action stream is a superset
  (same events + drops); keep `/partials/live-feed` route returning the new
  stream for one release.
- **Poller threads writing journal concurrently** → same engine/lock pattern
  already proven by EvalStore; journal writes are best-effort try/except.
- **Dialect drift** → identical DDL pattern to `eval_results` (serial vs
  autoincrement), tested on SQLite; Postgres covered by existing conventions.
