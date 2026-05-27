"""Network Chaos Monkey Agent - FastAPI server entry point."""
from __future__ import annotations

import logging
import uuid
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from chaos_agent.agent import ChaosAgent
from shared.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Network Chaos Monkey AI Agent",
    description="AI-powered controlled chaos experiment assistant",
    version="1.0.0",
)

agent = ChaosAgent()


class ChatRequest(BaseModel):
    message: str
    session_id: str = ""


class ChatResponse(BaseModel):
    response: str
    session_id: str


@app.get("/health")
async def health():
    return {"status": "healthy", "agent": "chaos"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a message and get a response from the chaos agent."""
    session_id = request.session_id or str(uuid.uuid4())
    try:
        response = agent.chat(request.message, session_id=session_id)
        return ChatResponse(response=response, session_id=session_id)
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.chaos_agent_port, log_level="info")
