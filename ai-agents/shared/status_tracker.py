"""
In-process agent status tracking and LangChain callback integration.

AgentStatus is an in-memory singleton per agent process, updated in real-time
as the LangGraph ReAct loop runs.  The /status endpoint on each agent's FastAPI
server serialises this object so the dashboard can poll it every 2 seconds.

StatusCallbackHandler is a LangChain BaseCallbackHandler that:
  - Updates AgentStatus on every LLM start/end and tool start/end
  - Writes task_events rows to TaskStore (when a task_id is active)
  - Records token usage to RateLimiter on every LLM completion
"""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Status dataclass ──────────────────────────────────────────────────────────

@dataclass
class AgentStatus:
    """Mutable snapshot of what an agent process is doing right now."""

    agent_name: str = ""
    state: str = "idle"             # idle | thinking | calling_tool | writing_result
    task_id: str | None = None
    task_type: str | None = None
    session_id: str | None = None
    current_tool: str | None = None
    tool_input_preview: str | None = None
    started_at: str | None = None
    last_event_at: str | None = None
    queries_this_hour: int = 0
    tokens_this_hour: int = 0
    last_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        # _lock is NOT a dataclass field — threading.Lock cannot be pickled or
        # deep-copied (LangGraph's MemorySaver calls deepcopy on the invoke config).
        # Using object.__setattr__ bypasses the frozen-dataclass guard if ever set.
        object.__setattr__(self, "_lock", threading.Lock())

    def __deepcopy__(self, memo: dict) -> "AgentStatus":
        # Create a copy with a fresh lock instead of trying to copy the old one.
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if k == "_lock":
                object.__setattr__(result, "_lock", threading.Lock())
            else:
                object.__setattr__(result, k, copy.deepcopy(v, memo))
        return result

    def set_idle(self) -> None:
        with self._lock:
            self.state = "idle"
            self.task_id = None
            self.task_type = None
            self.session_id = None
            self.current_tool = None
            self.tool_input_preview = None
            self.started_at = None
            self.last_event_at = _now()

    def set_thinking(
        self,
        session_id: str | None = None,
        task_id: str | None = None,
        task_type: str | None = None,
    ) -> None:
        with self._lock:
            if self.state == "idle":
                self.started_at = _now()
            self.state = "thinking"
            self.current_tool = None
            self.tool_input_preview = None
            self.last_event_at = _now()
            if session_id:
                self.session_id = session_id
            if task_id:
                self.task_id = task_id
            if task_type:
                self.task_type = task_type

    def set_calling_tool(self, tool_name: str, input_preview: str = "") -> None:
        with self._lock:
            self.state = "calling_tool"
            self.current_tool = tool_name
            self.tool_input_preview = input_preview[:120] if input_preview else None
            self.last_event_at = _now()

    def set_writing(self) -> None:
        with self._lock:
            self.state = "writing_result"
            self.current_tool = None
            self.tool_input_preview = None
            self.last_event_at = _now()

    def add_tokens(self, n: int) -> None:
        with self._lock:
            self.tokens_this_hour += n

    def to_dict(self) -> dict:
        # asdict() only serialises dataclass fields — _lock is not a field,
        # so no need to pop it. The lock is acquired to get a consistent snapshot.
        with self._lock:
            return asdict(self)


# ── Callback handler ──────────────────────────────────────────────────────────

class StatusCallbackHandler(BaseCallbackHandler):
    """
    Wires into LangChain's callback system to update AgentStatus and persist
    task_events in real time during a ReAct reasoning loop.

    Pass an instance to create_react_agent(..., callbacks=[handler]) or
    directly to agent.invoke(..., config={"callbacks": [handler]}).

    The task_id and session_id must be set on the handler before each invocation
    so event rows are tagged correctly.
    """

    def __init__(
        self,
        status: AgentStatus,
        agent_name: str,
        task_store=None,
        rate_limiter=None,
    ) -> None:
        super().__init__()
        self._status = status
        self._agent_name = agent_name
        self._task_store = task_store     # shared.task_store.TaskStore or None
        self._rate_limiter = rate_limiter  # shared.rate_limiter.RateLimiter or None

        # Set per-invocation via set_context()
        self._task_id: str | None = None
        self._session_id: str | None = None

    def set_context(
        self,
        session_id: str | None = None,
        task_id: str | None = None,
        task_type: str | None = None,
    ) -> None:
        """Call before agent.invoke() to attach session/task context."""
        self._session_id = session_id
        self._task_id = task_id
        self._status.set_thinking(
            session_id=session_id,
            task_id=task_id,
            task_type=task_type,
        )

    def clear_context(self) -> None:
        self._task_id = None
        self._session_id = None
        self._status.set_idle()

    # ── LLM hooks ─────────────────────────────────────────────────────────────

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any
    ) -> None:
        self._status.set_thinking(
            session_id=self._session_id,
            task_id=self._task_id,
        )
        self._emit("llm_start", None)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        # Extract token counts from OpenAI response metadata
        prompt_tokens = 0
        completion_tokens = 0
        try:
            usage = response.llm_output.get("token_usage", {}) if response.llm_output else {}
            prompt_tokens     = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
        except Exception:
            pass

        total = prompt_tokens + completion_tokens
        self._status.add_tokens(total)

        if self._rate_limiter and total > 0:
            try:
                model = ""
                try:
                    model = response.llm_output.get("model_name", "") if response.llm_output else ""
                except Exception:
                    pass
                cost = self._rate_limiter.record_usage(
                    agent=self._agent_name,
                    session_id=self._session_id or "",
                    task_id=self._task_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    model=model,
                )
                self._status.last_cost_usd = cost
            except Exception:
                pass

        self._emit("llm_end", {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens})

    def on_llm_error(self, error: Exception, **kwargs: Any) -> None:
        self._emit("llm_error", {"error": str(error)})

    # ── Tool hooks ─────────────────────────────────────────────────────────────

    def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, **kwargs: Any
    ) -> None:
        tool_name = serialized.get("name", "unknown_tool")
        self._status.set_calling_tool(tool_name, input_str)
        self._emit("tool_call", {"tool": tool_name, "input": input_str[:200]})

    def on_tool_end(self, output: str, **kwargs: Any) -> None:
        tool_name = self._status.current_tool or ""
        self._status.set_thinking()
        self._emit("tool_result", {"tool": tool_name, "output": str(output)[:300]})

    def on_tool_error(self, error: Exception, **kwargs: Any) -> None:
        tool_name = self._status.current_tool or ""
        self._status.set_thinking()
        self._emit("tool_error", {"tool": tool_name, "error": str(error)})

    # ── Chain hooks ────────────────────────────────────────────────────────────

    def on_chain_end(self, outputs: dict[str, Any], **kwargs: Any) -> None:
        self._status.set_writing()

    # ── internal ──────────────────────────────────────────────────────────────

    def _emit(self, event_type: str, detail: dict | None) -> None:
        if self._task_store and self._task_id:
            try:
                self._task_store.add_event(
                    self._task_id,
                    self._agent_name,
                    event_type,
                    detail,
                )
            except Exception:
                pass
