from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_STORE_FILE = REPO_ROOT / "nautobot" / "data_loader" / "cache_store.py"

spec = importlib.util.spec_from_file_location("cache_store", CACHE_STORE_FILE)
cache_store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache_store)


def test_load_missing_cache_returns_defaults(tmp_path: Path):
    store = cache_store.CacheStore(tmp_path / "loader.cache.json")
    payload = store.load()

    assert payload["version"] == cache_store.CACHE_VERSION
    assert "meta" in payload
    assert "endpoints" in payload
    assert payload["meta"]["last_refresh_at"] is None


def test_save_and_load_roundtrip(tmp_path: Path):
    cache_file = tmp_path / "loader.cache.json"
    store = cache_store.CacheStore(cache_file)

    payload = store.load()
    payload["endpoints"]["dcim.devices"] = {
        "refreshed_at": "2026-01-01T00:00:00+00:00",
        "count": 2,
        "objects": [{"name": "spine1"}, {"name": "spine2"}],
    }
    store.touch_runtime(
        payload,
        run_mode="plan",
        source="unit-test",
        nautobot_url="http://localhost:8080",
    )
    store.save(payload)

    loaded = store.load()
    assert loaded["endpoints"]["dcim.devices"]["count"] == 2
    assert loaded["meta"]["last_run_mode"] == "plan"
    assert loaded["meta"]["source"] == "unit-test"


def test_is_stale_without_refresh_timestamp(tmp_path: Path):
    store = cache_store.CacheStore(tmp_path / "loader.cache.json")
    payload = store.load()

    assert store.is_stale(payload, ttl_seconds=300) is True
    assert store.is_stale(payload, ttl_seconds=0) is False
