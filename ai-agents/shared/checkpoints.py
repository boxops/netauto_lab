"""
Checkpointer factory for the interactive chat agent.

MemorySaver loses every chat thread on restart. When the optional
langgraph-checkpoint-sqlite package is installed, chat history is persisted to
a SQLite file (CHAT_CHECKPOINT_DB overrides the path; default is
chat_checkpoints.db next to the activity DB, which sits on the shared volume
in Docker). When the package is missing or the file can't be opened, the
factory falls back to MemorySaver with a log line — persistence is an upgrade,
never a hard dependency.
"""
from __future__ import annotations

import logging
import os
import sqlite3

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)


def get_chat_checkpointer():
    path = os.getenv("CHAT_CHECKPOINT_DB", "").strip()
    if not path:
        activity = os.getenv("ACTIVITY_DB_PATH", "./activity.db")
        path = os.path.join(os.path.dirname(activity) or ".", "chat_checkpoints.db")

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        logger.info(
            "langgraph-checkpoint-sqlite not installed — chat history is in-memory "
            "only and will be lost on restart"
        )
        return MemorySaver()

    try:
        # check_same_thread=False: /chat runs in the threadpool; SqliteSaver
        # serialises access with its own internal lock.
        conn = sqlite3.connect(path, check_same_thread=False)
        saver = SqliteSaver(conn)
        logger.info("Chat checkpoints persisted to %s", path)
        return saver
    except Exception as exc:
        logger.warning(
            "Failed to open chat checkpoint DB %s (%s) — falling back to in-memory",
            path, exc,
        )
        return MemorySaver()
