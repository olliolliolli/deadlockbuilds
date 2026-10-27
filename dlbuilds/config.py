from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_VERSION = 2

DEFAULTS = {
    "version": CONFIG_VERSION,
    "hero_id": None,
    "rank_index": 2,           # Archon+
    "opacity": 0.94,
    "pos": [40, 120],
    "size": [400, 680],
    # Формат: ctrl / shift / alt + одна літера або F1..F12
    "hotkey_toggle": "ctrl+x",
    "hotkey_clickthrough": "ctrl+shift+x",
    "min_hero_matches": 1500,  # якщо після патча матчів менше — беремо ще попередній
    "fallback_days": 14,
    "cache_ttl_sec": 3600,
    "force_ipv4": True,
    "auto_hero": True,         # перемикатися на героя, за якого граєш
    "game_dir": "",            # папка Deadlock, якщо не знайшлась автоматично
    "hud_enabled": True,       # підказка «далі купити» в правому нижньому куті
    "hud_corner": "top_left",  # кут підказки: top_left / top_right / bottom_left / bottom_right
    "hud_offset": [20, 20],    # відступ підказки від цього кута (по горизонталі, по вертикалі)
    "data_url": "",            # адреса builds.json з сервера (порожньо = вбудована в exe)
}


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    p = Path(base) / "DeadlockBuilds"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load() -> dict:
    path = app_dir() / "config.json"
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            saved = json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            saved = {}
        if int(saved.get("version", 1)) < CONFIG_VERSION:
            # Старі версії зберігали гарячі клавіші F8 у форматі pynput — скидаємо.
            for k in ("hotkey_toggle", "hotkey_clickthrough", "opacity", "size", "version"):
                saved.pop(k, None)
        cfg.update(saved)
    cfg["version"] = CONFIG_VERSION
    return cfg


def save(cfg: dict):
    (app_dir() / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
