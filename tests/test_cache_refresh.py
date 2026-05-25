from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_REFRESH_FILE = REPO_ROOT / "nautobot" / "data_loader" / "cache_refresh.py"

spec = importlib.util.spec_from_file_location("cache_refresh", CACHE_REFRESH_FILE)
cache_refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache_refresh)


class DummyObj:
    def __init__(self, payload):
        self._payload = payload

    def serialize(self):
        return self._payload


class DummyEndpoint:
    def __init__(self, rows):
        self._rows = [DummyObj(r) for r in rows]

    def all(self):
        return self._rows


class DummyDCIM:
    def __init__(self):
        self.devices = DummyEndpoint([
            {"id": "1", "name": "spine1", "model": "cEOS"},
            {"id": "2", "name": "leaf1", "model": "cEOS"},
        ])


class DummyNB:
    def __init__(self):
        self.dcim = DummyDCIM()


def test_refresh_cache_snapshot_populates_endpoint_section():
    nb = DummyNB()
    payload = {"version": 1, "meta": {}, "endpoints": {}}

    stats = cache_refresh.refresh_cache_snapshot(
        nb,
        payload,
        endpoint_paths=["dcim.devices"],
    )

    assert stats["endpoints"] == 1
    assert stats["objects"] == 2

    section = payload["endpoints"]["dcim.devices"]
    assert section["count"] == 2
    assert section["indexes"]["name"]["spine1"] == [0]
    assert section["indexes"]["model"]["cEOS"] == [0, 1]
