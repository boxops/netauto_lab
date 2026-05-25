from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid


DEFAULT_SNAPSHOT_ENDPOINTS = [
    "dcim.location_types",
    "dcim.locations",
    "extras.roles",
    "dcim.manufacturers",
    "dcim.device_types",
    "dcim.platforms",
    "ipam.namespaces",
    "ipam.prefixes",
    "ipam.vlans",
    "extras.config_contexts",
    "extras.custom_fields",
    "extras.secrets",
    "extras.secrets_groups",
    "extras.secrets_groups_associations",
    "dcim.devices",
    "dcim.interfaces",
    "ipam.ip_addresses",
    "ipam.ip_address_to_interface",
    "dcim.cables",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _serialize_obj(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "serialize"):
        try:
            payload = obj.serialize()
            if isinstance(payload, dict):
                return _json_safe(payload)
        except Exception:
            pass

    fallback: dict[str, Any] = {}
    for key in ("id", "name", "model", "prefix", "vid", "address", "slug"):
        value = getattr(obj, key, None)
        if value is not None:
            fallback[key] = _json_safe(value)
    return fallback


def _resolve_endpoint(nb: Any, endpoint_path: str) -> Any:
    node = nb
    for part in endpoint_path.split("."):
        node = getattr(node, part)
    return node


def _build_indexes(objects: list[dict[str, Any]]) -> dict[str, dict[str, list[int]]]:
    keys = ["id", "name", "model", "prefix", "vid", "address", "slug"]
    indexes: dict[str, dict[str, list[int]]] = {k: {} for k in keys}

    for idx, obj in enumerate(objects):
        for key in keys:
            value = obj.get(key)
            if value is None:
                continue
            bucket = indexes[key].setdefault(str(value), [])
            bucket.append(idx)

    return indexes


def refresh_cache_snapshot(
    nb: Any,
    cache_payload: dict[str, Any],
    endpoint_paths: list[str] | None = None,
) -> dict[str, Any]:
    paths = endpoint_paths or DEFAULT_SNAPSHOT_ENDPOINTS
    cache_payload.setdefault("endpoints", {})

    stats = {
        "endpoints": 0,
        "objects": 0,
    }

    for endpoint_path in paths:
        endpoint = _resolve_endpoint(nb, endpoint_path)
        try:
            rows = list(endpoint.all())
        except Exception:
            rows = list(endpoint.filter(limit=1000))

        objects = [_serialize_obj(obj) for obj in rows]
        cache_payload["endpoints"][endpoint_path] = {
            "refreshed_at": _utc_now_iso(),
            "count": len(objects),
            "objects": objects,
            "indexes": _build_indexes(objects),
        }
        stats["endpoints"] += 1
        stats["objects"] += len(objects)

    return stats
