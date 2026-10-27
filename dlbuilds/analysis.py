"""Розрахунок найкращої збірки на героя за матчами поточного патча."""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from .api import ApiError, Client

# Ранги (average_badge = ранг*10 + підранг). None = без фільтра.
RANKS = [
    ("Усі ранги", None),
    ("Ritualist+", 50),
    ("Archon+", 70),
    ("Phantom+", 90),
]

PHASES = [("early", "Лейн / ранній етап"), ("mid", "Середина гри"), ("late", "Пізня гра")]
SLOT_NAMES = {"weapon": "Зброя", "vitality": "Живучість", "spirit": "Дух"}
# Предмети, які купують саме для підтримки союзників. Матчі з таким предметом —
# «саппорт», без жодного з них — «дамаг».
SUPPORT_ITEMS = {
    1804594021: "Rescue Beam",
    1662311306: "Divine Barrier",
    2956256701: "Healing Nova",
    2059712766: "Restorative Locket",
    857669956: "Guardian Ward",
}
STYLE_NAMES = {"": "Загальна", "damage": "Дамаг", "support": "Саппорт"}
HEALING_TAG = 38758251          # тег «Healing» в ігровому браузері збірок
STYLE_MIN_PICK = 0.10           # саппорт-стиль показуємо, якщо його грають ≥10% матчів
STYLE_MIN_MATCHES = 800

SMOOTH_K = 250          # байєсівське згладжування вінрейту (у матчах)
CORE_PICK = 0.08        # від цього пікрейту предмет вважається «основним»
SITU_PICK = 0.02


@dataclass
class ItemRec:
    item_id: int
    name: str
    slot: str
    tier: int
    cost: int
    matches: int
    winrate: float
    pickrate: float
    delta: float            # відносно середнього вінрейту предметів того ж тіру
    buy_min: float | None   # середня хвилина покупки
    phase: str = "mid"
    image: str | None = None


@dataclass
class Build:
    hero_id: int
    hero_name: str
    window_label: str
    start_ts: int
    hero_matches: int
    hero_wr: float | None
    phases: dict[str, list[ItemRec]] = field(default_factory=dict)
    situational: list[ItemRec] = field(default_factory=list)
    ability_order: list[str] | None = None
    ability_priority: list[str] | None = None
    ability_wr: float | None = None
    ability_matches: int = 0
    notes: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    hero_image: str | None = None
    style: str = ""                                   # "", "damage", "support"
    styles: list[str] = field(default_factory=list)   # доступні стилі для героя
    style_anchor: int | None = None                   # головний саппорт-предмет
    ability_icons: dict[str, str] = field(default_factory=dict)
    ingame: list[dict] = field(default_factory=list)   # найближчі опубліковані в грі збірки


def _num(row: dict, *keys, default=0):
    for k in keys:
        v = row.get(k)
        if v is not None:
            return v
    return default


def _matches(row: dict) -> int:
    m = row.get("matches") or row.get("total") or row.get("matches_played")
    if m is None:
        m = _num(row, "wins") + _num(row, "losses")
    return int(m or 0)


class Analyzer:
    def __init__(self, client: Client, min_hero_matches: int = 1500, fallback_days: int = 14,
                 bulk: bool = False):
        """bulk=True (сервер): статистика предметів одним запитом для всіх героїв,
        порядок здібностей — без фільтра рангу. У ~4 рази менше запитів до API."""
        self.c = client
        self.bulk = bulk
        self._bulk_items: dict = {}
        self._bulk_lock = __import__("threading").Lock()
        self.min_hero_matches = min_hero_matches
        self.fallback_days = fallback_days
        self._items: dict[int, dict] | None = None
        self._heroes: list[dict] | None = None

    # ------------------------------------------------------------ static data
    def heroes(self) -> list[dict]:
        if self._heroes is None:
            self._heroes = self.c.heroes()
        return self._heroes

    def items(self) -> dict[int, dict]:
        if self._items is None:
            self._items = {int(i["id"]): i for i in self.c.items() if "id" in i}
        return self._items

    # ------------------------------------------------------------ patch window
    def windows(self) -> list[tuple[int, str]]:
        """Кандидати на початок вікна: останній патч, попередні дрібні патчі,
        аж до останнього великого оновлення (і ще одного перед ним)."""
        now = int(time.time())
        out, bigs = [], 0
        for ts, big in self.c.patches():
            if ts >= now:
                continue
            kind = "оновлення" if big else "патча"
            out.append((ts, f"з {kind} {datetime.fromtimestamp(ts):%d.%m.%Y}"))
            bigs += big
            if bigs >= 2 or len(out) >= 8:
                break
        if not out:
            ts = now - self.fallback_days * 86400
            out.append((ts, f"останні {self.fallback_days} днів (дата патча недоступна)"))
        return out

    def _hero_row(self, start: int, badge: int | None, hero_id: int) -> dict | None:
        rows = self.c.hero_stats(start, badge)
        for r in rows if isinstance(rows, list) else []:
            if int(r.get("hero_id", -1)) == hero_id:
                return r
        return None

    def pick_window(self, hero_id: int, badge: int | None):
        """Бере найсвіжіший патч, де для героя вже достатньо матчів."""
        cands = self.windows()
        chosen = None
        for i, (start, label) in enumerate(cands):
            try:
                row = self._hero_row(start, badge, hero_id)
            except ApiError:
                if getattr(self.c, "offline", False):
                    raise
                row = None
            m = _matches(row) if row else 0
            chosen = (start, label, row, m, i)
            if m >= self.min_hero_matches:
                break
        start, label, row, m, i = chosen
        if i > 0:
            label = f"{label} (після останнього патча ще мало матчів)"
        return start, label, row, m

    def _ingame_entries(self, hero_id: int, start: int, tag: int | None = None) -> list[dict]:
        """Популярні збірки, оновлені після патча, + популярні загалом
        (для саппорта — ще й збірки з тегом «Healing»)."""
        out, seen = [], set()
        lists = []
        if tag:
            try:
                lists.append(self.c.builds(hero_id, tag=tag))
            except (ApiError, TypeError):
                pass
        try:
            lists.append(self.c.builds(hero_id, since=start))
        except (ApiError, TypeError):
            pass
        lists.append(self.c.builds(hero_id))
        for lst in lists:
            for e in lst if isinstance(lst, list) else []:
                hb = e.get("hero_build", e) if isinstance(e, dict) else {}
                bid = hb.get("hero_build_id")
                if bid not in seen:
                    seen.add(bid)
                    out.append(e)
        return out

    def _bulk_rows(self, hero_id: int, start: int, badge: int | None,
                   flt: dict | None = None) -> list[dict]:
        key = (start, badge, tuple(sorted((flt or {}).items())))
        with self._bulk_lock:
            if key not in self._bulk_items:
                grouped: dict[int, list] = {}
                for r in self.c.item_stats_all(start, badge, flt):
                    grouped.setdefault(int(r.get("bucket", -1)), []).append(r)
                self._bulk_items[key] = grouped
        return self._bulk_items[key].get(hero_id, [])

    def _rows(self, hero_id, start, badge, flt=None):
        if self.bulk:
            return self._bulk_rows(hero_id, start, badge, flt)
        return self.c.item_stats(hero_id, start, badge, flt)

    # ------------------------------------------------------------ main
    def build(self, hero_id: int, badge: int | None, style: str = "") -> Build:
        hero = next((h for h in self.heroes() if int(h["id"]) == hero_id), {"name": str(hero_id)})
        items = self.items()
        start, label, hero_row, hero_m = self.pick_window(hero_id, badge)
        hero_wr = (_num(hero_row, "wins") / hero_m) if hero_row and hero_m else None
        abil_badge = None if self.bulk else badge

        # Загальна статистика потрібна завжди: з неї видно, чи є в героя саппорт-стиль.
        with ThreadPoolExecutor(3) as ex:
            f_items = ex.submit(self._rows, hero_id, start, badge)
            f_abil = ex.submit(self.c.ability_order, hero_id, start, abil_badge) if not style else None
            f_builds = ex.submit(self._ingame_entries, hero_id, start) if not style else None
            rows = f_items.result()
        if not hero_m:
            hero_m = max((_matches(r) for r in rows), default=0)
        styles, anchor = _detect_styles(rows, hero_m)
        if style not in styles:
            style = ""
        if not style and f_abil is None:      # просили стиль, якого в героя немає
            with ThreadPoolExecutor(2) as ex:
                f_abil = ex.submit(self.c.ability_order, hero_id, start, abil_badge)
                f_builds = ex.submit(self._ingame_entries, hero_id, start)

        flt = None
        if style:
            flt = ({"include_item_ids": str(anchor)} if style == "support"
                   else {"exclude_item_ids": ",".join(str(i) for i in SUPPORT_ITEMS)})
            with ThreadPoolExecutor(4) as ex:
                f_items = ex.submit(self._rows, hero_id, start, badge, flt)
                f_hero = ex.submit(self.c.hero_stats, start, badge, flt)
                f_abil = ex.submit(self.c.ability_order, hero_id, start, abil_badge, flt)
                f_builds = ex.submit(self._ingame_entries, hero_id, start,
                                     HEALING_TAG if style == "support" else None)
                rows = f_items.result()
                try:
                    hrow = next((r for r in f_hero.result() if int(r.get("hero_id", -1)) == hero_id), None)
                except ApiError:
                    hrow = None
            if hrow and _matches(hrow):
                hero_m, hero_wr = _matches(hrow), _num(hrow, "wins") / _matches(hrow)
            else:
                hero_m, hero_wr = max((_matches(r) for r in rows), default=0), None

        b = Build(hero_id, hero.get("name", "?"), label, start, hero_m, hero_wr,
                  hero_image=hero_image(hero), style=style, styles=styles, style_anchor=anchor)
        if not hero_m:
            b.notes.append("Немає матчів для цього героя в обраному вікні.")
            return b

        buy_min = {int(r["item_id"]): float(r["avg_buy_time_s"]) / 60
                   for r in rows if r.get("item_id") is not None and r.get("avg_buy_time_s") is not None}
        recs = _score_items(rows, items, hero_m, buy_min)
        _fill_build(b, recs)

        try:
            _ability_order(b, f_abil.result(), items, hero_m)
        except ApiError as e:
            if not getattr(self.c, "offline", False):
                b.notes.append(f"Порядок здібностей недоступний ({e.status or 'помилка'}).")
        try:
            b.ingame = match_ingame_builds(b, f_builds.result())
        except ApiError:
            if not getattr(self.c, "offline", False):
                b.notes.append("Не вдалося отримати опубліковані в грі збірки.")
        if hero_m < 500:
            b.notes.append("Мала вибірка — рекомендації можуть бути неточними.")
        return b


def _detect_styles(rows, hero_m) -> tuple[list[str], int | None]:
    """Чи грають героя як саппорта достатньо часто, і який саппорт-предмет головний."""
    if not hero_m:
        return [], None
    best, best_m = None, 0
    for r in rows if isinstance(rows, list) else []:
        iid = r.get("item_id")
        if iid is not None and int(iid) in SUPPORT_ITEMS and _matches(r) > best_m:
            best, best_m = int(iid), _matches(r)
    if best and best_m / hero_m >= STYLE_MIN_PICK and best_m >= STYLE_MIN_MATCHES:
        return ["damage", "support"], best
    return [], best


# ---------------------------------------------------------------- helpers
def _score_items(rows, items, hero_m, buy_min) -> list[ItemRec]:
    raw = []
    for r in rows if isinstance(rows, list) else []:
        iid = r.get("item_id")
        meta = items.get(int(iid)) if iid is not None else None
        if not meta or meta.get("type") != "upgrade" or meta.get("disabled"):
            continue
        if meta.get("shopable") is False:
            continue
        m = _matches(r)
        if m <= 0:
            continue
        raw.append((meta, m, float(_num(r, "wins"))))

    # Базовий вінрейт по тіру: предмети пізніх тірів купують переважно ті, хто
    # виграє, тож порівнюємо предмет лише з іншими предметами того ж тіру.
    tier_base: dict[int, float] = {}
    for tier in {int(m.get("item_tier") or 0) for m, _, _ in raw}:
        w = sum(wi for me, mi, wi in raw if int(me.get("item_tier") or 0) == tier)
        n = sum(mi for me, mi, wi in raw if int(me.get("item_tier") or 0) == tier)
        tier_base[tier] = w / n if n else 0.5

    recs = []
    for meta, m, w in raw:
        tier = int(meta.get("item_tier") or 0)
        base = tier_base.get(tier, 0.5)
        smooth = (w + SMOOTH_K * base) / (m + SMOOTH_K)
        bm = buy_min.get(int(meta["id"]))
        recs.append(ItemRec(
            item_id=int(meta["id"]), name=meta.get("name", meta.get("class_name", "?")),
            slot=str(meta.get("item_slot_type") or "").lower(), tier=tier,
            cost=int(meta.get("cost") or 0), matches=m, winrate=w / m,
            pickrate=m / hero_m, delta=smooth - base, buy_min=bm,
            phase=_phase(tier, bm), image=item_image(meta)))
    return recs


def _phase(tier: int, buy_min: float | None) -> str:
    if buy_min is not None:
        return "early" if buy_min < 12 else "mid" if buy_min < 25 else "late"
    return {1: "early", 2: "early", 3: "mid", 4: "late", 5: "late"}.get(tier, "mid")


def _fill_build(b: Build, recs: list[ItemRec]):
    def core_score(r: ItemRec):
        return r.delta + 0.04 * math.sqrt(min(r.pickrate, 0.7))

    core = [r for r in recs if r.pickrate >= CORE_PICK and r.delta > -0.015]
    limits = {"early": 6, "mid": 6, "late": 5}
    used = set()
    for key, _ in PHASES:
        lst = sorted((r for r in core if r.phase == key), key=core_score, reverse=True)[:limits[key]]
        lst.sort(key=lambda r: (r.buy_min if r.buy_min is not None else r.tier * 10, r.cost))
        b.phases[key] = lst
        used.update(r.item_id for r in lst)

    situ = [r for r in recs if r.item_id not in used and r.pickrate >= SITU_PICK
            and r.matches >= 300 and r.delta >= 0.015]
    b.situational = sorted(situ, key=lambda r: r.delta, reverse=True)[:6]


def _ability_order(b: Build, rows, items, hero_m, prefix_len: int = 12):
    """Найкращий порядок прокачки здібностей.

    API повертає послідовності різної довжини, а довші майже завжди мають
    вищий вінрейт: у довгих матчах встигають вкласти більше очок, а довгі матчі
    частіше виграють. Тому порівнюємо лише перші prefix_len очок.
    """
    rows = [r for r in rows if isinstance(r, dict) and r.get("abilities")] if isinstance(rows, list) else []
    groups: dict[tuple, dict] = {}
    for r in rows:
        seq = tuple(int(x) for x in r["abilities"])
        if len(seq) < prefix_len:
            continue
        g = groups.setdefault(seq[:prefix_len], {"wins": 0, "matches": 0, "full": seq, "full_m": 0})
        g["wins"] += _num(r, "wins")
        g["matches"] += _matches(r)
        if _matches(r) > g["full_m"]:
            g["full"], g["full_m"] = seq, _matches(r)
    if not groups:
        return
    top = sorted(groups.values(), key=lambda g: g["matches"], reverse=True)[:4]
    base = sum(g["wins"] for g in top) / max(1, sum(g["matches"] for g in top))
    best = max(top, key=lambda g: (g["wins"] + SMOOTH_K * base) / (g["matches"] + SMOOTH_K))

    def name(a):
        return items.get(a, {}).get("name") or str(a)

    full = [name(a) for a in best["full"]]
    for a in best["full"]:
        url = item_image(items.get(a, {}))
        if url:
            b.ability_icons[name(a)] = url
    b.ability_order = full
    b.ability_matches = best["matches"]
    b.ability_wr = best["wins"] / best["matches"] if best["matches"] else None

    # Пріоритет прокачки: здібність, яка першою отримує всі свої очки, — головна.
    counts: dict[str, int] = {}
    for n in full:
        counts[n] = counts.get(n, 0) + 1
    seen: dict[str, int] = {}
    done_at: dict[str, int] = {}
    for i, n in enumerate(full):
        seen[n] = seen.get(n, 0) + 1
        if seen[n] == counts[n]:
            done_at[n] = i
    # Здібності, що отримали лише 1 очко (відкрита, але не прокачана), — в кінець.
    b.ability_priority = sorted(counts, key=lambda n: (counts[n] <= 1, done_at[n]))


# ---------------------------------------------------------------- images
def item_image(meta: dict) -> str | None:
    """PNG-іконка предмета/здібності (webp Qt без плагіна не завжди читає)."""
    for k in ("shop_image_small", "shop_image", "image"):
        v = meta.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def hero_image(hero: dict) -> str | None:
    imgs = hero.get("images") or {}
    for k in ("icon_image_small", "icon_hero_card", "minimap_image", "selection_image"):
        v = imgs.get(k)
        if isinstance(v, str) and v.startswith("http") and not v.endswith(".webp"):
            return v
    for v in imgs.values():
        if isinstance(v, str) and v.startswith("http") and v.endswith(".png"):
            return v
    return None


# ---------------------------------------------------------------- in-game builds
def _walk(o, key):
    """Знаходить усі значення ключа key у вкладеному JSON."""
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            yield from _walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, key)


def parse_ingame_build(entry: dict) -> dict | None:
    hb = entry.get("hero_build") if isinstance(entry.get("hero_build"), dict) else entry
    bid = hb.get("hero_build_id") or hb.get("build_id") or entry.get("hero_build_id")
    if not bid:
        return None
    items: set[int] = set()
    for cats in _walk(hb, "mod_categories"):
        for cat in cats if isinstance(cats, list) else []:
            for mod in (cat or {}).get("mods") or []:
                aid = (mod or {}).get("ability_id")
                if aid is not None:
                    items.add(int(aid))
    if not items:
        return None
    fav = entry.get("num_weekly_favorites")
    if fav is None:
        fav = entry.get("num_favorites", 0)
    return {
        "id": int(bid),
        "name": str(hb.get("name") or "Без назви").strip(),
        "version": hb.get("version"),
        "favorites": int(fav or 0),
        "updated": hb.get("last_updated_timestamp") or hb.get("publish_timestamp"),
        "items": items,
    }


def match_ingame_builds(b: Build, entries, top: int = 3) -> list[dict]:
    """Опубліковані в грі збірки, найбільш схожі на рекомендовану."""
    want: dict[int, float] = {}
    for lst in b.phases.values():
        for r in lst:
            want[r.item_id] = 1.0
    for r in b.situational:
        want.setdefault(r.item_id, 0.3)
    total = sum(want.values())
    if not total:
        return []

    out = []
    for e in entries if isinstance(entries, list) else []:
        p = parse_ingame_build(e) if isinstance(e, dict) else None
        if not p:
            continue
        # Збірки, не оновлені після патча, могли не адаптувати під зміни:
        # дуже старі відкидаємо, решту трохи штрафуємо.
        stale_days = (b.start_ts - int(p["updated"])) / 86400 if p["updated"] else 0
        p["outdated"] = stale_days > 0
        got = sum(w for iid, w in want.items() if iid in p["items"])
        core_total = sum(1 for w in want.values() if w == 1.0)
        core_got = sum(1 for iid, w in want.items() if w == 1.0 and iid in p["items"])
        p["match"] = got / total
        p["core_got"], p["core_total"] = core_got, core_total
        # Популярність важлива: перевірені збірки з тисячами «улюблених»
        # кращі за випадкову свіжу збірку з тим самим набором предметів.
        p["score"] = (p["match"] - 0.004 * max(0, len(p["items"]) - 30)
                      + 0.07 * math.log10(1 + p["favorites"])
                      - (0.06 if p["outdated"] else 0))
        if p["match"] >= 0.35:
            out.append(p)
    popular = [p for p in out if p["favorites"] >= 5]
    if len(popular) >= top:
        out = popular
    out.sort(key=lambda p: p["score"], reverse=True)
    for p in out:
        p.pop("items", None)
    return out[:top]


# ---------------------------------------------------------------- (de)serialization
def build_to_dict(b: Build) -> dict:
    from dataclasses import asdict
    d = asdict(b)
    d.pop("created", None)
    for lst in list(d["phases"].values()) + [d["situational"]]:
        for r in lst:
            for k in ("winrate", "pickrate", "delta"):
                r[k] = round(r[k], 4)
            if r.get("buy_min") is not None:
                r["buy_min"] = round(r["buy_min"], 1)
    return d


def build_from_dict(d: dict) -> Build:
    d = dict(d)
    phases = {k: [ItemRec(**r) for r in v] for k, v in (d.pop("phases", None) or {}).items()}
    situ = [ItemRec(**r) for r in d.pop("situational", None) or []]
    known = set(Build.__dataclass_fields__)
    b = Build(**{k: v for k, v in d.items() if k in known})
    b.phases, b.situational = phases, situ
    return b
