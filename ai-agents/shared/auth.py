"""
API key authentication for agent HTTP endpoints.

When AGENT_API_KEY is set in .env, all non-health endpoints require the key
via the X-API-Key header or ?api_key= query parameter.

When AGENT_API_KEY is not set (dev/test), authentication is bypassed with a
startup warning so the system still works out of the box.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader, APIKeyQuery

from shared.config import settings

logger = logging.getLogger(__name__)

_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)
_query_scheme  = APIKeyQuery(name="api_key",    auto_error=False)

# Endpoints that don't require authentication
_PUBLIC_PATHS = {"/health", "/webhook/alert"}


async def require_api_key(
    request: Request,
    header_key: str | None = Security(_header_scheme),
    query_key:  str | None = Security(_query_scheme),
) -> None:
    """
    FastAPI dependency that enforces API key authentication.

    Skips auth for paths in _PUBLIC_PATHS (health + Alertmanager webhook).
    When AGENT_API_KEY is empty, logs a warning on startup and allows all requests.
    """
    if not settings.agent_api_key:
        return  # dev mode — auth disabled

    if request.url.path in _PUBLIC_PATHS:
        return

    provided = header_key or query_key
    if not provided:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Provide X-API-Key header or ?api_key= query parameter.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if provided != settings.agent_api_key:
        raise HTTPException(
            status_code=403,
            detail="Invalid API key.",
        )


def warn_if_no_api_key(agent_name: str) -> None:
    """Call at application startup to warn operators when auth is disabled."""
    if not settings.agent_api_key:
        logger.warning(
            "%s: AGENT_API_KEY is not set — all endpoints are unauthenticated. "
            "Set AGENT_API_KEY in .env before deploying to production.",
            agent_name,
        )
