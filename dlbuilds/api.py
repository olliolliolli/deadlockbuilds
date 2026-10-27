"""Клієнт до community-API deadlock-api.com з локальним кешем.

API не пов'язане з Valve. Гра не зачіпається взагалі: програма лише робить
HTTP-запити до публічного API, тож ризику бану VAC немає.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://api.deadlock-api.com"


class ApiError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def force_ipv4():
    """Деякі мережі мають «зламаний» IPv6: з'єднання не падає одразу, а висить
    до тайм-ауту, і кожен запит займає десятки секунд. Браузер з цим справляється
    сам, а Python — ні, тому примусово використовуємо IPv4."""
    try:
        import urllib3.util.connection as uc
        uc.HAS_IPV6 = False
    except Exception:  # noqa: BLE001
        pass


class Client:
    """offline=True — брати дані тільки з кешу (навіть застарілого), без мережі.
    Так оверлей при запуску миттєво показує останній результат, а свіжі дані
    підтягує окремим онлайн-клієнтом у фоні."""

    def __init__(self, cache_dir: Path, ttl: int = 3600, verbose: bool = False,
                 offline: bool = False, log_path: Path | None = None,
                 attempts: int = 3, min_interval: float = 0.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.verbose = verbose
        self.offline = offline
        self.log_path = log_path
        self.attempts = attempts
        self.min_interval = min_interval     # пауза між запитами (для сервера)
        self._rate_lock = threading.Lock()
        self._next_at = 0.0
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "dl-builds-overlay/1.1 (personal use)"
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16)
        self.s.mount("https://", adapter)

    # ------------------------------------------------------------ low level
    def _cache_path(self, url: str, params: dict | None) -> Path:
        key = url + json.dumps(params or {}, sort_keys=True)
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def _log(self, msg: str):
        if self.verbose:
            print(msg)
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
            except OSError:
                pass

    @staticmethod
    def _read(path: Path):
        return json.loads(path.read_text("utf-8"))

    def get(self, url: str, params: dict | None = None, ttl: int | None = None):
        ttl = self.ttl if ttl is None else ttl
        params = {k: v for k, v in (params or {}).items() if v is not None}
        path = self._cache_path(url, params)
        if path.exists():
            if self.offline or time.time() - path.stat().st_mtime < ttl:
                try:
                    return self._read(path)
                except (ValueError, OSError):
                    pass
        if self.offline:
            raise ApiError("немає збережених даних", 0)

        last_err: Exception | None = None
        for attempt in range(self.attempts):
            if self.min_interval:
                with self._rate_lock:
                    wait = self._next_at - time.time()
                    self._next_at = max(time.time(), self._next_at) + self.min_interval
                if wait > 0:
                    time.sleep(wait)
            t0 = time.time()
            try:
                r = self.s.get(url, params=params, timeout=(5, 30))
            except requests.RequestException as e:
                last_err = e
                self._log(f"ERR  {time.time() - t0:5.1f}s {url} {params} {type(e).__name__}: {e}")
                time.sleep(1 + attempt)
                continue
            self._log(f"{r.status_code}  {time.time() - t0:5.1f}s {len(r.content) // 1024}KB {url} {params}")
            if r.status_code == 429:
                wait = r.headers.get("Retry-After")
                time.sleep(min(10.0, float(wait)) if wait and wait.replace(".", "").isdigit() else 2 + 2 * attempt)
                last_err = ApiError("429 rate limit", 429)
                continue
            if r.status_code >= 500:
                last_err = ApiError(f"{r.status_code} {r.text[:200]}", r.status_code)
                time.sleep(1 + attempt)
                continue
            if r.status_code == 403 and "<html" in r.text[:500].lower():
                # Захист Cloudflare від частих запитів — чекаємо й пробуємо знову.
                last_err = ApiError("403 Cloudflare", 403)
                time.sleep(min(60, 10 * (attempt + 1)))
                continue
            if r.status_code >= 400:
                raise ApiError(f"{r.status_code} {url}: {r.text[:300]}", r.status_code)
            data = r.json()
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            os.replace(tmp, path)
            return data

        # Немає мережі / API лежить — віддаємо застарілий кеш, якщо він є.
        if path.exists():
            return self._read(path)
        raise ApiError(f"Не вдалося отримати {url}: {last_err}")

    def analytics(self, endpoint: str, params: dict, hero_id: int | None = None):
        """Запит до /v1/analytics/* з запасними варіантами параметрів.

        Назви параметрів у API іноді змінюються, тому при 400 пробуємо
        прибрати необов'язкові фільтри та альтернативну назву фільтра героя.
        """
        url = f"{API}/v1/analytics/{endpoint}"
        variants = []
        base = dict(params)
        if hero_id is not None:
            variants += [{**base, "hero_ids": str(hero_id)}, {**base, "hero_id": hero_id}]
        else:
            variants.append(base)
        # Друге коло — без game_mode (на випадок, якщо параметр не підтримується).
        variants += [{k: v for k, v in v_.items() if k != "game_mode"} for v_ in list(variants)]

        last: ApiError | None = None
        for p in variants:
            try:
                return self.get(url, p)
            except ApiError as e:
                if e.status != 400:
                    raise
                last = e
        raise last or ApiError("bad request")

    # ------------------------------------------------------------ assets
    def heroes(self) -> list[dict]:
        data = self.get(f"{API}/v1/assets/heroes", {"only_active": "true"}, ttl=6 * 3600)
        out = []
        for h in data:
            if h.get("disabled") or h.get("in_development"):
                continue
            if h.get("player_selectable") is False:
                continue
            out.append(h)
        return sorted(out, key=lambda h: h.get("name", ""))

    def items(self) -> list[dict]:
        return self.get(f"{API}/v1/assets/items", ttl=6 * 3600)

    # ------------------------------------------------------------ patches
    def patches(self) -> list[tuple[int, bool]]:
        """[(unix_ts, is_big_patch)] від найновішого.

        Дата патча береться з заголовка патчноута («09-29-2026 Update»), бо
        pub_date на форумі змінюється при редагуванні поста.
        """
        out: dict[str, tuple[int, bool]] = {}
        try:
            for d in self.get(f"{API}/v1/patches/big-days", ttl=1800):
                ts = parse_date(d)
                if ts:
                    out[_day(ts)] = (ts, True)
        except ApiError:
            pass
        try:
            for p in self.get(f"{API}/v1/patches", ttl=1800):
                m = re.search(r"(\d{2})-(\d{2})-(\d{4})", p.get("title", ""))
                if m:
                    mo, dd, yy = map(int, m.groups())
                    ts = int(datetime(yy, mo, dd, 18, 0, tzinfo=timezone.utc).timestamp())
                else:
                    ts = parse_date(p.get("pub_date"))
                if ts and _day(ts) not in out:
                    out[_day(ts)] = (ts, False)
        except ApiError:
            pass
        return sorted(out.values(), reverse=True)

    # ------------------------------------------------------------ analytics
    # flt — додаткові фільтри матчів, напр. {"include_item_ids": "123"} (купив предмет)
    # або {"exclude_item_ids": "1,2,3"} (не купував жодного з них).
    def hero_stats(self, min_ts: int, min_badge: int | None, flt: dict | None = None):
        return self.analytics("hero-stats", {
            "min_unix_timestamp": min_ts, "min_average_badge": min_badge, "game_mode": "normal",
            **(flt or {})})

    def item_stats(self, hero_id: int, min_ts: int, min_badge: int | None, flt: dict | None = None):
        return self.analytics("item-stats", {
            "min_unix_timestamp": min_ts, "min_average_badge": min_badge,
            "game_mode": "normal", **(flt or {})}, hero_id=hero_id)

    def item_stats_all(self, min_ts: int, min_badge: int | None, flt: dict | None = None):
        """Статистика предметів одразу для всіх героїв (bucket = hero_id)."""
        return self.analytics("item-stats", {
            "min_unix_timestamp": min_ts, "min_average_badge": min_badge,
            "game_mode": "normal", "bucket": "hero", **(flt or {})})

    def builds(self, hero_id: int, since: int | None = None, tag: int | None = None) -> list[dict]:
        """Опубліковані в ігровому браузері збірки героя (останні версії).
        since — лише оновлені після цього моменту (наприклад, після патча)."""
        url = f"{API}/v1/builds"
        base = {"hero_id": hero_id, "only_latest": "true", "limit": 100, "min_unix_timestamp": since,
                "tag": tag}
        last: ApiError | None = None
        for sort in ("weekly_favorites", "favorites", None):
            try:
                return self.get(url, {**base, "sort_by": sort, "sort_direction": "desc" if sort else None})
            except ApiError as e:
                if e.status != 400:
                    raise
                last = e
        raise last or ApiError("bad request")

    def ability_order(self, hero_id: int, min_ts: int, min_badge: int | None, flt: dict | None = None):
        return self.analytics("ability-order-stats", {
            "min_unix_timestamp": min_ts, "min_average_badge": min_badge,
            "game_mode": "normal", **(flt or {})}, hero_id=hero_id)


def parse_date(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(parsedate_to_datetime(v).timestamp())
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
