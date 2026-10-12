"""Tiny on-disk JSON cache so repeated searches don't hammer Kiwi/FlixBus."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(os.environ.get("ZAGFLIGHTS_CACHE_DIR") or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "zagflights")


class Cache:
    def __init__(self, directory: str | Path | None = DEFAULT_DIR, enabled: bool = True):
        self.enabled = enabled and directory is not None
        self.dir = Path(directory) if directory else None
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, key: Any) -> Path:
        digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:32]
        return self.dir / f"{namespace}-{digest}.json"

    def get(self, namespace: str, key: Any, ttl_seconds: float) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - entry.get("saved", 0) > ttl_seconds:
            return None
        return entry.get("value")

    def set(self, namespace: str, key: Any, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(namespace, key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved": time.time(), "key": key, "value": value}, default=str), encoding="utf-8")
        tmp.replace(path)
