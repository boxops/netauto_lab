"""Network Operations Agent – FastAPI server entry point."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import threading
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ops_agent.agent import OpsAgent, agent_status, task_store, rate_limiter, status_handler, AGENT_NAME
from ops_agent.alert_poller import AlertPoller
from ops_agent.scheduler import OpsScheduler
from shared.auth import require_api_key, warn_if_no_api_key
from shared.config import settings
from shared.metrics import ActiveTasksRefresher, metrics_response
from shared.rate_limiter import BudgetExceededError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

agent    = OpsAgent()
_metrics = ActiveTasksRefresher(task_store, AGENT_NAME)
_scheduler: OpsScheduler | None = None

# Unified LangGraph pipeline — instantiated here to share the same task_store,
# rate_limiter, and status_handler singletons as the rest of the ops-agent.
# Passed to AlertPoller so it can dispatch directly without a circular import.
from ops_agent.workflow import IncidentWorkflow
_workflow = IncidentWorkflow(task_store, rate_limiter, status_handler)

poller   = AlertPoller(agent, task_store, rate_limiter, workflow=_workflow)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    warn_if_no_api_key(AGENT_NAME)
    _scheduler = OpsScheduler(agent)
    poller.start()
    _metrics.start()
    yield
    poller.stop()
    _metrics.stop()
    if _scheduler:
        _scheduler.shutdown()


app = FastAPI(
    title="Network Operations AI Agent",
    description="AI-powered network operations assistant",
    version="2.0.0",
    lifespan=lifespan,
    dependencies=[Depends(require_api_key)],
)


# ── Request / response models ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = ""
    task_id: str = ""
    task_type: str = ""


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict] = []


class TaskCreateRequest(BaseModel):
    type: str
    created_by: str
    content: dict
    title: str = ""
    assigned_to: str | None = None
    parent_id: str | None = None
    alert_fingerprint: str | None = None
    priority: str = "normal"


class TaskPatchRequest(BaseModel):
    action: str          # claim | start | complete | fail | request_approval | approve | reject
    agent: str = ""
    result: dict | None = None
    error: str = ""
    reason: str = ""


class FeedbackRequest(BaseModel):
    from_agent: str
    verdict: str
    confidence: float | None = None
    notes: str = ""


class ScheduleRequest(BaseModel):
    scenario: str
    interval_minutes: int


# ── Health & Metrics ───────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "agent": "ops"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    from fastapi.responses import Response
    content, media_type = metrics_response()
    return Response(content=content, media_type=media_type)


# ── Status (live agent state for dashboard) ────────────────────────────────────

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

        # Audit trail: create a task record for any chat-triggered config change
        # so it appears in the pipeline view alongside pipeline-generated tasks.
        config_applied = [
            tc for tc in tool_calls
            if tc.get("tool_name") == "run_config_commands"
            and "'check_mode': False" in tc.get("input_summary", "")
        ]
        if config_applied:
            try:
                audit_task = task_store.create_task(
                    type="rca",
                    created_by="chat",
                    assigned_to=AGENT_NAME,
                    title=f"[CHAT] Config change via interactive session {session_id[:8]}",
                    priority="normal",
                    content={
                        "source":      "chat",
                        "session_id":  session_id,
                        "message":     request.message[:500],
                        "tool_calls":  len(config_applied),
                    },
                )
                task_store.claim_task(audit_task["id"], AGENT_NAME)
                task_store.complete_task(
                    audit_task["id"], AGENT_NAME,
                    result={"response": response[:500], "tool_calls": len(tool_calls)},
                )
                logger.info("Chat audit task created: %s for session %s", audit_task["id"], session_id)
            except Exception as audit_exc:
                logger.warning("Failed to create chat audit task: %s", audit_exc)

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


# ── Alertmanager webhook ──────────────────────────────────────────────────────

@app.post("/webhook/alert", status_code=200)
async def alertmanager_webhook(request: Request):
    """
    Direct Alertmanager webhook receiver — zero polling latency.

    Alertmanager sends its standard v4 payload:
      { "status": "firing|resolved", "alerts": [{...}], ... }

    Each alert is processed immediately in a background thread, bypassing
    the 60-second polling loop. The polling loop continues as a fallback
    for alerts that arrive before the webhook is reachable.
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
        alert_status = alert.get("status", batch_status)

        event = {
            "alertname":    labels.get("alertname", ""),
            "severity":     labels.get("severity", "warning"),
            "instance":     labels.get("instance", ""),
            "device":       labels.get("sysName") or labels.get("agent_host") or "",
            "fingerprint":  alert.get("fingerprint", ""),
            "alert_status": alert_status,
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
        "Alertmanager webhook: %d alert(s) accepted, %d skipped (dedup/filter)",
        accepted, skipped,
    )
    return {"ok": True, "accepted": accepted, "skipped": skipped}


# ── Workflow resume (Phase 2 — triggered after human approval) ────────────────

class ResumeRequest(BaseModel):
    operator_commands: str = ""


@app.post("/workflow/resume/{task_id}")
async def workflow_resume(task_id: str, req: ResumeRequest = None):
    """
    Trigger Phase 2 of the incident workflow: execute the approved fix with
    check_mode=False and then verify alert resolution in Prometheus.

    Called by the UI's approve endpoint when WORKFLOW_ENABLED=true.
    The gate task must already be in status='complete' with an 'approved' event.

    Optional body: {"operator_commands": "interface Eth1\\n no shutdown"}
    When provided these commands override the agent-generated ones.  Useful
    when fix_type=escalate_human left commands="none".
    """
    if req is None:
        req = ResumeRequest()

    if not settings.workflow_enabled:
        raise HTTPException(status_code=400, detail="WORKFLOW_ENABLED is false — use legacy execution path")

    gate = task_store.get_task(task_id)
    if not gate:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    if gate["status"] not in ("complete", "awaiting_approval"):
        raise HTTPException(
            status_code=409,
            detail=f"Task {task_id!r} is in status {gate['status']!r}, expected 'complete' or 'awaiting_approval'",
        )

    operator_commands = req.operator_commands.strip()

    # Add the approved event before starting execution so resume_execution
    # can find it when checking for human authorisation.
    task_store.approve_task(task_id, "human")

    def _run():
        _workflow.resume_execution(task_id, "human", operator_commands=operator_commands)

    threading.Thread(target=_run, daemon=True, name=f"resume-{task_id}").start()
    return {"ok": True, "task_id": task_id, "status": "execution_started"}


# ── Poller control ────────────────────────────────────────────────────────────

@app.post("/poller/reset")
async def reset_poller():
    """
    Clear the poller's deduplication state and re-seed from the TaskStore.
    Call this after clearing the task queue so the poller re-investigates
    any alerts that are still firing in Prometheus.
    """
    remaining = poller.reset_seen()
    return {"ok": True, "seeded_fingerprints": remaining}


# ── Usage / cost ───────────────────────────────────────────────────────────────

@app.get("/usage")
async def usage():
    return rate_limiter.get_summary(agent=AGENT_NAME)


# ── Task endpoints ─────────────────────────────────────────────────────────────

@app.get("/tasks")
async def list_tasks(status: str = "", type: str = "", limit: int = 100):
    return task_store.list_tasks(
        assigned_to=AGENT_NAME,
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

    action = req.action
    agent_name = req.agent or AGENT_NAME

    if action == "claim":
        ok = task_store.claim_task(task_id, agent_name)
        if not ok:
            raise HTTPException(status_code=409, detail="Task already claimed")
    elif action == "start":
        task_store.start_task(task_id, agent_name)
    elif action == "complete":
        task_store.complete_task(task_id, agent_name, req.result or {})
    elif action == "fail":
        task_store.fail_task(task_id, agent_name, req.error)
    elif action == "request_approval":
        task_store.request_approval(task_id, agent_name)
    elif action == "approve":
        task_store.approve_task(task_id, agent_name)
    elif action == "reject":
        task_store.reject_task(task_id, agent_name, req.reason)
    else:
        raise HTTPException(status_code=422, detail=f"Unknown action {action!r}")

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


# ── Schedule endpoints ────────────────────────────────────────────────────────

@app.post("/schedule", status_code=201)
async def create_schedule(request: ScheduleRequest):
    if _scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")
    return _scheduler.add_job(request.scenario, request.interval_minutes)


@app.get("/schedules")
async def list_schedules():
    if _scheduler is None:
        return []
    return _scheduler.list_jobs()


@app.delete("/schedule/{job_id}")
async def delete_schedule(job_id: str):
    if _scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")
    removed = _scheduler.remove_job(job_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return {"deleted": True, "job_id": job_id}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.ops_agent_port, log_level="info")
