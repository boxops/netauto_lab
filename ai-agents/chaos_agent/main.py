"""Network Chaos Monkey Agent – FastAPI server entry point."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from chaos_agent.agent import ChaosAgent, agent_status, task_store, rate_limiter, AGENT_NAME
from chaos_agent.scheduler import ChaosScheduler
from chaos_agent.task_runner import ChaosTaskRunner
from shared.config import settings
from shared.rate_limiter import BudgetExceededError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

agent    = ChaosAgent()
runner   = ChaosTaskRunner(agent, task_store, rate_limiter)
scheduler: ChaosScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    scheduler = ChaosScheduler(agent)
    runner.start()
    yield
    runner.stop()
    if scheduler:
        scheduler.shutdown()


app = FastAPI(
    title="Network Chaos Monkey AI Agent",
    description="AI-powered controlled chaos experiment assistant with scheduling",
    version="2.0.0",
    lifespan=lifespan,
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


class ScheduleRequest(BaseModel):
    scenario: str = Field(..., description="Chaos experiment prompt to run on schedule")
    interval_minutes: int = Field(..., ge=1, le=1440, description="Repeat interval in minutes")


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
    action: str
    agent: str = ""
    result: dict | None = None
    error: str = ""
    reason: str = ""


class FeedbackRequest(BaseModel):
    from_agent: str
    verdict: str
    confidence: float | None = None
    notes: str = ""


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "agent": "chaos"}


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
    except Exception as exc:
        logger.exception("Agent error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())

    async def generate() -> AsyncGenerator[str, None]:
        async for chunk in agent.astream(request.message, session_id=session_id):
            yield chunk

    return StreamingResponse(generate(), media_type="text/plain")


# ── Usage ──────────────────────────────────────────────────────────────────────

@app.get("/usage")
async def usage():
    return rate_limiter.get_summary(agent=AGENT_NAME)


# ── Tasks ──────────────────────────────────────────────────────────────────────

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


# ── Schedule endpoints ─────────────────────────────────────────────────────────

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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.chaos_agent_port, log_level="info")
