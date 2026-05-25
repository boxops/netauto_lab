from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

STATE_VERSION = 1


class StateStore:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.lock_file = state_file.with_suffix(state_file.suffix + ".lock")

    def acquire_lock(self) -> None:
        wait_seconds = int(os.getenv("DATA_LOADER_LOCK_WAIT_SECONDS", "120"))
        stale_seconds = int(os.getenv("DATA_LOADER_STALE_LOCK_SECONDS", "300"))
        deadline = time.time() + wait_seconds

        while True:
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"pid={os.getpid()} created={int(time.time())}\n".encode("utf-8"))
                os.close(fd)
                return
            except FileExistsError:
                try:
                    mtime = self.lock_file.stat().st_mtime
                    if time.time() - mtime > stale_seconds:
                        self.lock_file.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue

                if time.time() >= deadline:
                    raise RuntimeError(
                        f"State lock already exists at '{self.lock_file}' and did not clear within {wait_seconds}s."
                    )
                time.sleep(1)

    def release_lock(self) -> None:
        self.lock_file.unlink(missing_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {
                "version": STATE_VERSION,
                "resources": {},
            }

        with open(self.state_file, "r", encoding="utf-8") as f:
            payload = json.load(f)

        if not isinstance(payload, dict):
            raise ValueError("State file content must be a JSON object")

        payload.setdefault("version", STATE_VERSION)
        payload.setdefault("resources", {})
        return payload

    def save(self, state: dict[str, Any]) -> None:
        state["version"] = STATE_VERSION
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_file, self.state_file)
