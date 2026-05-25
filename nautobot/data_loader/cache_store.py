from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CACHE_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CacheStore:
    def __init__(self, cache_file: Path):
        self.cache_file = cache_file

    def default_cache(self) -> dict[str, Any]:
        now = _utc_now_iso()
        return {
            "version": CACHE_VERSION,
            "meta": {
                "created_at": now,
                "updated_at": now,
                "last_refresh_at": None,
                "source": None,
                "nautobot_url": None,
                "last_run_mode": None,
            },
            "endpoints": {},
        }

    def load(self) -> dict[str, Any]:
        if not self.cache_file.exists():
            return self.default_cache()

        with open(self.cache_file, "r", encoding="utf-8") as f:
            payload = json.load(f)

        if not isinstance(payload, dict):
            raise ValueError("Cache file content must be a JSON object")

        payload.setdefault("version", CACHE_VERSION)
        payload.setdefault("meta", {})
        payload.setdefault("endpoints", {})

        meta = payload["meta"]
        meta.setdefault("created_at", _utc_now_iso())
        meta.setdefault("updated_at", _utc_now_iso())
        meta.setdefault("last_refresh_at", None)
        meta.setdefault("source", None)
        meta.setdefault("nautobot_url", None)
        meta.setdefault("last_run_mode", None)

        return payload

    def save(self, cache: dict[str, Any]) -> None:
        cache["version"] = CACHE_VERSION
        cache.setdefault("meta", {})
        cache.setdefault("endpoints", {})
        cache["meta"]["updated_at"] = _utc_now_iso()

        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
            f.write("\n")
        tmp_file.replace(self.cache_file)

    def touch_refresh(self, cache: dict[str, Any], *, source: str, nautobot_url: str) -> None:
        cache.setdefault("meta", {})
        cache["meta"]["last_refresh_at"] = _utc_now_iso()
        cache["meta"]["source"] = source
        cache["meta"]["nautobot_url"] = nautobot_url

    def touch_runtime(self, cache: dict[str, Any], *, run_mode: str, source: str, nautobot_url: str) -> None:
        cache.setdefault("meta", {})
        cache["meta"]["last_run_mode"] = run_mode
        cache["meta"]["source"] = source
        cache["meta"]["nautobot_url"] = nautobot_url

    def is_stale(self, cache: dict[str, Any], ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            return False

        last_refresh_at = cache.get("meta", {}).get("last_refresh_at")
        if not last_refresh_at:
            return True

        try:
            last_dt = datetime.fromisoformat(str(last_refresh_at))
        except ValueError:
            return True

        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)

        age_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return age_seconds > ttl_seconds
