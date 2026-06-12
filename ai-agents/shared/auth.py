"""
API key authentication for agent HTTP endpoints.

When AGENT_API_KEY is set in .env, all non-health endpoints require the key
via the X-API-Key header. (Query-parameter auth is deliberately not supported:
keys in URLs end up in access logs, browser history, and Referer headers.)

When AGENT_API_KEY is not set (dev/test), authentication is bypassed with a
startup warning so the system still works out of the box.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from shared.config import settings

logger = logging.getLogger(__name__)

_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)

# Endpoints that don't require authentication
_PUBLIC_PATHS = {"/health", "/webhook/alert"}


async def require_api_key(
    request: Request,
    header_key: str | None = Security(_header_scheme),
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

    if not header_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Provide the X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if not hmac.compare_digest(header_key, settings.agent_api_key):
        raise HTTPException(
            status_code=403,
            detail="Invalid API key.",
        )


async def require_webhook_secret(request: Request) -> None:
    """
    Gate for the public /webhook/alert endpoint (exempt from API-key auth so
    Alertmanager can reach it). When ALERT_WEBHOOK_SECRET is set, the request
    must carry "Authorization: Bearer <secret>" — Alertmanager sends it via
    http_config.authorization (see prometheus/alertmanager.yml). When unset,
    alerts are accepted unauthenticated (lab default).
    """
    if not settings.alert_webhook_secret:
        return
    provided = request.headers.get("Authorization", "")
    expected = f"Bearer {settings.alert_webhook_secret}"
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing webhook credentials.",
        )


def warn_if_no_api_key(agent_name: str) -> None:
    """Call at application startup to warn operators when auth is disabled."""
    if not settings.agent_api_key:
        logger.warning(
            "%s: AGENT_API_KEY is not set — all endpoints are unauthenticated. "
            "Set AGENT_API_KEY in .env before deploying to production.",
            agent_name,
        )
