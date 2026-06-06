"""
Prometheus metrics for the AI agent pipeline.

Exports:
  agent_pipeline_tasks_total          — counter  (agent, task_type, status)
  agent_pipeline_duration_seconds     — histogram (agent, task_type)
  agent_llm_cost_usd_total            — counter  (agent, model)
  agent_llm_tokens_total              — counter  (agent, model, token_type)
  agent_pipeline_confidence_total     — counter  (agent, confidence_level)
  agent_active_tasks                  — gauge    (agent, task_type)
  agent_policy_decisions_total        — counter  (policy_id, autonomy_level, outcome)
  agent_policy_ttr_seconds            — histogram (policy_id)

Served on /metrics of each agent's FastAPI app.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import text

_METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").lower() not in ("false", "0", "no")

# ── optional prometheus_client import ─────────────────────────────────────────
try:
    from prometheus_client import (
        Counter, Gauge, Histogram,
        CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST,
        multiprocess, REGISTRY,
    )
    _HAS_PROM = True
except ImportError:
    _HAS_PROM = False


if _HAS_PROM and _METRICS_ENABLED:
    _tasks_total = Counter(
        "agent_pipeline_tasks_total",
        "Total pipeline tasks by agent, type, and terminal status",
        ["agent", "task_type", "status"],
    )
    _duration = Histogram(
        "agent_pipeline_duration_seconds",
        "Time from task claim to completion (seconds)",
        ["agent", "task_type"],
        buckets=[5, 15, 30, 60, 120, 300, 600, 1800],
    )
    _cost_total = Counter(
        "agent_llm_cost_usd_total",
        "Cumulative LLM cost in USD by agent and model",
        ["agent", "model"],
    )
    _tokens_total = Counter(
        "agent_llm_tokens_total",
        "Cumulative LLM tokens by agent, model, and token type (prompt/completion)",
        ["agent", "model", "token_type"],
    )
    _confidence_total = Counter(
        "agent_pipeline_confidence_total",
        "Count of RCA/fix outputs by confidence level",
        ["agent", "confidence_level"],
    )
    _active_tasks = Gauge(
        "agent_active_tasks",
        "Number of tasks currently in running/claimed state",
        ["agent", "task_type"],
    )
    _policy_decisions_total = Counter(
        "agent_policy_decisions_total",
        "Autonomy policy decisions by policy, level, and outcome",
        ["policy_id", "autonomy_level", "outcome"],
    )
    _policy_ttr_seconds = Histogram(
        "agent_policy_ttr_seconds",
        "Time-to-resolution per autonomy policy (seconds)",
        ["policy_id"],
        buckets=[30, 60, 120, 300, 600, 1800, 3600],
    )
    _METRICS_READY = True
else:
    _METRICS_READY = False


# ── public recording helpers ───────────────────────────────────────────────────

def record_task_completed(agent: str, task_type: str, status: str,
                          duration_seconds: float | None = None) -> None:
    if not _METRICS_READY:
        return
    _tasks_total.labels(agent=agent, task_type=task_type, status=status).inc()
    if duration_seconds is not None and duration_seconds > 0:
        _duration.labels(agent=agent, task_type=task_type).observe(duration_seconds)


def record_llm_usage(agent: str, model: str,
                     prompt_tokens: int, completion_tokens: int, cost_usd: float) -> None:
    if not _METRICS_READY:
        return
    _cost_total.labels(agent=agent, model=model).inc(cost_usd)
    _tokens_total.labels(agent=agent, model=model, token_type="prompt").inc(prompt_tokens)
    _tokens_total.labels(agent=agent, model=model, token_type="completion").inc(completion_tokens)


def record_confidence(agent: str, confidence_level: str) -> None:
    if not _METRICS_READY:
        return
    _confidence_total.labels(agent=agent, confidence_level=confidence_level).inc()


def set_active_tasks(agent: str, task_type: str, count: int) -> None:
    if not _METRICS_READY:
        return
    _active_tasks.labels(agent=agent, task_type=task_type).set(count)


def record_policy_decision(
    policy_id: str,
    autonomy_level: str,
    outcome: str,  # "auto_approved" | "approval_requested" | "suppressed" | "escalated"
) -> None:
    if not _METRICS_READY:
        return
    _policy_decisions_total.labels(
        policy_id=policy_id or "default",
        autonomy_level=autonomy_level,
        outcome=outcome,
    ).inc()


def record_policy_ttr(policy_id: str, ttr_seconds: int) -> None:
    if not _METRICS_READY:
        return
    _policy_ttr_seconds.labels(policy_id=policy_id or "default").observe(ttr_seconds)


# ── FastAPI /metrics endpoint factory ─────────────────────────────────────────

def metrics_response():
    """Return (content, media_type) for a /metrics endpoint."""
    if not _HAS_PROM or not _METRICS_ENABLED:
        return "# prometheus_client not installed or METRICS_ENABLED=false\n", "text/plain"
    return generate_latest(), CONTENT_TYPE_LATEST


# ── Background gauge refresher ────────────────────────────────────────────────

class ActiveTasksRefresher:
    """
    Polls TaskStore every 30 s and updates the active_tasks gauge.
    Start once per agent container in the FastAPI lifespan.
    """

    def __init__(self, task_store, agent_name: str | None) -> None:
        self._store      = task_store
        self._agent_name = agent_name   # None = count all tasks regardless of assigned_to
        self._stop       = threading.Event()

    def start(self) -> None:
        t = threading.Thread(target=self._loop, daemon=True, name="metrics-refresh")
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(30):
            self._refresh()

    def _refresh(self) -> None:
        if not _METRICS_READY:
            return
        gauge_label = self._agent_name or "all"
        try:
            for task_type in ("rca", "fix_proposal", "validation", "approval_gate"):
                active = len(self._store.list_tasks(
                    assigned_to=self._agent_name,  # None → TaskStore skips the filter
                    status="running",
                    type=task_type,
                    limit=500,
                ))
                set_active_tasks(gauge_label, task_type, active)
        except Exception:
            pass
