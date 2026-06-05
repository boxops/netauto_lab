"""
Unified AI Agent — single FastAPI application entry point.

Combines all capabilities (ops, engineering, validation, chaos) into one service on port 8000.

Background tasks started in lifespan:
  - AlertPoller: polls Alertmanager for new firing alerts, dispatches to workflow
  - IncidentWorkflow: LangGraph pipeline (investigate→fix→validate→approval gate on single rca task)
  - OpsScheduler: APScheduler for repeating lab experiments / scheduled scenarios
  - ActiveTasksRefresher: Prometheus gauge for active task counts

Active endpoints: /health, /metrics, /status, /chat, /chat/stream, /usage,
  /tasks (CRUD + feedback), /webhook/alert, /workflow/resume/{id},
  /poller/reset, /schedule, /schedules, /schedule/{job_id}
"""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from shared.task_store import TaskStore
from shared.rate_limiter import RateLimiter, BudgetExceededError
from shared.status_tracker import AgentStatus, StatusCallbackHandler
from shared.auth import require_api_key, warn_if_no_api_key
from shared.config import settings
from shared.metrics import ActiveTasksRefresher, metrics_response
from shared.unified_agent import UnifiedAgent, AGENT_NAME

from ops_agent.alert_poller import AlertPoller
from ops_agent.workflow import IncidentWorkflow
from ops_agent.scheduler import OpsScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Singletons ─────────────────────────────────────────────────────────────────

task_store     = TaskStore()
rate_limiter   = RateLimiter(engine=task_store._engine)
agent_status   = AgentStatus(agent_name=AGENT_NAME)
status_handler = StatusCallbackHandler(
    status=agent_status,
    agent_name=AGENT_NAME,
    task_store=task_store,
    rate_limiter=rate_limiter,
)

agent        = UnifiedAgent(rate_limiter=rate_limiter, status_handler=status_handler)
_metrics     = ActiveTasksRefresher(task_store, agent_name=None)  # None = all tasks
_workflow    = IncidentWorkflow(task_store, rate_limiter, status_handler)
poller       = AlertPoller(agent, task_store, rate_limiter, workflow=_workflow)
scheduler: OpsScheduler | None = None


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    warn_if_no_api_key(AGENT_NAME)
    scheduler = OpsScheduler(agent)
    poller.start()
    _metrics.start()
    yield
    poller.stop()
    _metrics.stop()
    if scheduler:
        scheduler.shutdown()


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Network AI Agent",
    description="Unified AI agent for network operations, engineering, validation, and lab experiments",
    version="3.0.0",
    lifespan=lifespan,
    dependencies=[Depends(require_api_key)],
)


# ── Request / response models ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message:    str
    session_id: str = ""
    task_id:    str = ""
    task_type:  str = ""


class ChatResponse(BaseModel):
    response:   str
    session_id: str
    tool_calls: list[dict] = []


class ScheduleRequest(BaseModel):
    scenario:         str = Field(..., description="Chaos experiment prompt to run on schedule")
    interval_minutes: int = Field(..., ge=1, le=1440, description="Repeat interval in minutes")


class TaskCreateRequest(BaseModel):
    type:              str
    created_by:        str
    content:           dict
    title:             str = ""
    assigned_to:       str | None = None
    parent_id:         str | None = None
    alert_fingerprint: str | None = None
    priority:          str = "normal"


class TaskPatchRequest(BaseModel):
    action: str
    agent:  str = ""
    result: dict | None = None
    error:  str = ""
    reason: str = ""


class FeedbackRequest(BaseModel):
    from_agent:  str
    verdict:     str
    confidence:  float | None = None
    notes:       str = ""


# ── Health & Metrics ───────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    from shared.unified_agent import ALL_TOOLS
    return {"status": "healthy", "agent": AGENT_NAME, "tool_count": len(ALL_TOOLS)}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    content, media_type = metrics_response()
    return Response(content=content, media_type=media_type)


# ── Status ─────────────────────────────────────────────────────────────────────

@app.get("/status")
async def status():
    return agent_status.to_dict()


# ── Chat ───────────────────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    try:
        response, tool_calls = agent.chat_with_trace(
            request.message,
            session_id=session_id,
            task_id=request.task_id or None,
            task_type=request.task_type or None,
        )
        return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)
    except BudgetExceededError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        logger.exception("Agent error")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())

    async def generate() -> AsyncGenerator[str, None]:
        async for chunk in agent.astream(request.message, session_id=session_id):
            yield chunk

    return StreamingResponse(generate(), media_type="text/plain")


# ── Usage / cost ───────────────────────────────────────────────────────────────

@app.get("/usage")
async def usage():
    return rate_limiter.get_summary(agent=AGENT_NAME)


# ── Task endpoints ─────────────────────────────────────────────────────────────

@app.get("/tasks")
async def list_tasks(
    assigned_to: str = "",
    status:      str = "",
    type:        str = "",
    limit:       int = 100,
):
    return task_store.list_tasks(
        assigned_to=assigned_to or None,
        status=status or None,
        type=type or None,
        limit=limit,
    )


@app.post("/tasks", status_code=201)
async def create_task(req: TaskCreateRequest):
    try:
        return task_store.create_task(
            type=req.type,
            created_by=req.created_by,
            content=req.content,
            title=req.title,
            assigned_to=req.assigned_to,
            parent_id=req.parent_id,
            alert_fingerprint=req.alert_fingerprint,
            priority=req.priority,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    return task


@app.patch("/tasks/{task_id}")
async def patch_task(task_id: str, req: TaskPatchRequest):
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    actor = req.agent or AGENT_NAME

    if req.action == "claim":
        if not task_store.claim_task(task_id, actor):
            raise HTTPException(status_code=409, detail="Task already claimed")
    elif req.action == "start":
        task_store.start_task(task_id, actor)
    elif req.action == "complete":
        task_store.complete_task(task_id, actor, req.result or {})
    elif req.action == "fail":
        task_store.fail_task(task_id, actor, req.error)
    elif req.action == "request_approval":
        task_store.request_approval(task_id, actor)
    elif req.action == "approve":
        task_store.approve_task(task_id, actor)
    elif req.action == "reject":
        task_store.reject_task(task_id, actor, req.reason)
    else:
        raise HTTPException(status_code=422, detail=f"Unknown action {req.action!r}")

    return task_store.get_task(task_id)


@app.post("/tasks/{task_id}/feedback", status_code=201)
async def add_feedback(task_id: str, req: FeedbackRequest):
    task = task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    try:
        task_store.add_feedback(
            task_id=task_id,
            from_agent=req.from_agent,
            verdict=req.verdict,
            confidence=req.confidence,
            notes=req.notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True}


# ── Alertmanager webhook ──────────────────────────────────────────────────────

@app.post("/webhook/alert", status_code=200)
async def alertmanager_webhook(request: Request):
    """
    Direct Alertmanager webhook receiver — zero polling latency.
    Alertmanager sends its standard v4 payload.
    Each alert is processed immediately in a background thread.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    batch_status = payload.get("status", "firing")
    accepted = 0
    skipped  = 0

    for alert in payload.get("alerts", []):
        labels      = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        event = {
            "alertname":    labels.get("alertname", ""),
            "severity":     labels.get("severity", "warning"),
            "instance":     labels.get("instance", ""),
            "device":       labels.get("sysName") or labels.get("agent_host") or "",
            "fingerprint":  alert.get("fingerprint", ""),
            "alert_status": alert.get("status", batch_status),
            "batch_status": batch_status,
            "summary":      annotations.get("summary", ""),
            "description":  annotations.get("description", ""),
            "labels":       labels,
        }
        if poller.push_alert(event):
            accepted += 1
        else:
            skipped += 1

    logger.info(
        "Alertmanager webhook: %d alert(s) accepted, %d skipped",
        accepted, skipped,
    )
    return {"ok": True, "accepted": accepted, "skipped": skipped}


# ── Workflow resume (Phase 2 after human approval) ────────────────────────────

@app.post("/workflow/resume/{task_id}")
async def workflow_resume(task_id: str):
    """Trigger Phase 2 of the incident workflow: execute the approved fix."""
    if not settings.workflow_enabled:
        raise HTTPException(status_code=400, detail="WORKFLOW_ENABLED is false")

    gate = task_store.get_task(task_id)
    if not gate:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    if gate["status"] not in ("complete", "awaiting_approval"):
        raise HTTPException(
            status_code=409,
            detail=f"Task {task_id!r} is {gate['status']!r}, not approvable",
        )

    threading.Thread(
        target=_workflow.resume_execution,
        args=(task_id, "human"),
        daemon=True,
        name=f"resume-{task_id}",
    ).start()
    return {"ok": True, "task_id": task_id, "status": "execution_started"}


# ── Poller control ────────────────────────────────────────────────────────────

@app.post("/poller/reset")
async def reset_poller():
    """Clear alert poller deduplication state and re-seed from TaskStore."""
    remaining = poller.reset_seen()
    return {"ok": True, "seeded_fingerprints": remaining}


# ── Schedule endpoints (lab chaos experiments) ─────────────────────────────────

@app.post("/schedule", status_code=201)
async def create_schedule(request: ScheduleRequest):
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")
    return scheduler.add_job(request.scenario, request.interval_minutes)


@app.get("/schedules")
async def list_schedules():
    if scheduler is None:
        return []
    return scheduler.list_jobs()


@app.delete("/schedule/{job_id}")
async def delete_schedule(job_id: str):
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")
    removed = scheduler.remove_job(job_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return {"deleted": True, "job_id": job_id}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
