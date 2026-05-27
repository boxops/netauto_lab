"""Network Chaos Monkey Agent - FastAPI server entry point."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from chaos_agent.agent import ChaosAgent
from chaos_agent.scheduler import ChaosScheduler
from shared.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

agent = ChaosAgent()
scheduler: ChaosScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    scheduler = ChaosScheduler(agent)
    yield
    if scheduler:
        scheduler.shutdown()


app = FastAPI(
    title="Network Chaos Monkey AI Agent",
    description="AI-powered controlled chaos experiment assistant with scheduling",
    version="1.1.0",
    lifespan=lifespan,
)


# ── Request / response models ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = ""


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict] = []


class ScheduleRequest(BaseModel):
    scenario: str = Field(..., description="Chaos experiment prompt to run on schedule")
    interval_minutes: int = Field(..., ge=1, le=1440, description="Repeat interval in minutes")


# ── Chat endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "agent": "chaos"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a message and get a response from the chaos agent."""
    session_id = request.session_id or str(uuid.uuid4())
    try:
        response, tool_calls = agent.chat_with_trace(request.message, session_id=session_id)
        return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)
    except Exception as exc:
        logger.exception("Agent error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Stream the chaos agent response."""
    session_id = request.session_id or str(uuid.uuid4())

    async def generate() -> AsyncGenerator[str, None]:
        async for chunk in agent.astream(request.message, session_id=session_id):
            yield chunk

    return StreamingResponse(generate(), media_type="text/plain")


# ── Schedule endpoints ─────────────────────────────────────────────────────────

@app.post("/schedule", status_code=201)
async def create_schedule(request: ScheduleRequest):
    """Schedule a chaos experiment to run on a repeating interval."""
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")
    return scheduler.add_job(request.scenario, request.interval_minutes)


@app.get("/schedules")
async def list_schedules():
    """List all active scheduled chaos experiments."""
    if scheduler is None:
        return []
    return scheduler.list_jobs()


@app.delete("/schedule/{job_id}")
async def delete_schedule(job_id: str):
    """Cancel a scheduled chaos experiment."""
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")
    removed = scheduler.remove_job(job_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return {"deleted": True, "job_id": job_id}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.chaos_agent_port, log_level="info")
