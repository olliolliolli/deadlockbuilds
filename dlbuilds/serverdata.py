"""Готові збірки з сервера (GitHub Actions перераховує їх кожні 2 години)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import requests

from .analysis import Build, build_from_dict

FORMAT_VERSION = 1
FRESH_SEC = 12 * 3600        # старіші дані показуємо, але дораховуємо локально


def app_version() -> str:
    try:
        from ._build_info import VERSION
        return VERSION or "dev"
    except ImportError:
        return "dev"


def default_url() -> str:
    try:
        from ._build_info import DATA_URL
        return DATA_URL or ""
    except ImportError:
        return ""


class ServerData:
    def __init__(self, url: str, cache_dir: Path):
        self.url = url.strip()
        self.path = Path(cache_dir) / "server_builds.json"
        self.data: dict | None = None
        self.lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _accept(self, data) -> bool:
        if not isinstance(data, dict) or data.get("format") != FORMAT_VERSION:
            return False
        with self.lock:
            if self.data and self.data.get("generated_at", 0) > data.get("generated_at", 0):
                return False
            self.data = data
        return True

    def load_cached(self) -> bool:
        try:
            return self._accept(json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError):
            return False

    def refresh(self) -> bool:
        """Завантажує свіжий файл. True — якщо дані оновились."""
        if not self.enabled:
            return False
        old = self.generated_at
        r = requests.get(self.url, timeout=(5, 30), headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        data = r.json()
        if not self._accept(data):
            return False
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
        os.replace(tmp, self.path)
        return self.generated_at != old

    @property
    def generated_at(self) -> int:
        return int((self.data or {}).get("generated_at") or 0)

    @property
    def fresh(self) -> bool:
        return bool(self.data) and time.time() - self.generated_at < FRESH_SEC

    def heroes(self) -> list[dict]:
        return list((self.data or {}).get("heroes") or [])

    def build(self, hero_id: int, rank_index: int) -> Build | None:
        d = ((self.data or {}).get("builds") or {}).get(f"{hero_id}:{rank_index}")
        if not d:
            return None
        try:
            return build_from_dict(d)
        except (TypeError, ValueError):
            return None
