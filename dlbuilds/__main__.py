"""Запуск:
    python -m dlbuilds                 — оверлей
    python -m dlbuilds --hero Haze     — збірка в консолі
    python -m dlbuilds --doctor        — перевірка API, якщо щось не працює
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import config as cfgmod
from .analysis import PHASES, RANKS, Analyzer
from .api import API, ApiError, Client


def _console(hero_name: str, rank: int):
    cfg = cfgmod.load()
    an = Analyzer(Client(cfgmod.app_dir() / "cache", int(cfg["cache_ttl_sec"])),
                  int(cfg["min_hero_matches"]), int(cfg["fallback_days"]))
    hero = next((h for h in an.heroes() if h["name"].lower() == hero_name.lower()), None) \
        or next((h for h in an.heroes() if hero_name.lower() in h["name"].lower()), None)
    if not hero:
        sys.exit(f"Героя не знайдено. Доступні: {', '.join(h['name'] for h in an.heroes())}")
    b = an.build(int(hero["id"]), RANKS[rank][1])
    print(f"\n{b.hero_name} — {b.hero_matches} матчів, {b.window_label}, ранги: {RANKS[rank][0]}")
    for key, title in PHASES + [("situational", "Ситуативні")]:
        lst = b.situational if key == "situational" else b.phases.get(key, [])
        if not lst:
            continue
        print(f"\n[{title}]")
        for r in lst:
            t = f"~{r.buy_min:.0f}хв" if r.buy_min is not None else f"T{r.tier}"
            print(f"  {r.name:<28} {r.slot:<9} {t:>6}  WR {r.winrate*100:5.1f}%  "
                  f"{r.delta*100:+5.1f}  пік {r.pickrate*100:4.0f}%")
    if b.ability_priority:
        print(f"\n[Здібності] {' > '.join(b.ability_priority)}")
    for n in b.notes:
        print("! " + n)


def _doctor():
    from .api import force_ipv4
    force_ipv4()
    c = Client(cfgmod.app_dir() / "cache", ttl=0, verbose=True)
    week = int(time.time()) - 7 * 86400
    checks = [
        ("heroes", c.heroes),
        ("items", c.items),
        ("patches", c.patches),
        ("hero-stats", lambda: c.hero_stats(week, None)),
        ("item-stats", lambda: c.item_stats(1, week, None)),
        ("ability-order", lambda: c.ability_order(1, week, None)),
    ]
    for name, fn in checks:
        t0 = time.time()
        try:
            data = fn()
            sample = data[0] if isinstance(data, list) and data else data
            print(f"OK   {name}: {len(data) if isinstance(data, list) else '-'} записів, {time.time() - t0:.1f} с")
            print("     приклад:", json.dumps(sample, ensure_ascii=False)[:300])
        except ApiError as e:
            print(f"FAIL {name}: {e}")


def main():
    p = argparse.ArgumentParser(prog="dlbuilds")
    p.add_argument("--hero")
    p.add_argument("--rank", type=int, default=2, help="0=усі, 1=Ritualist+, 2=Archon+, 3=Phantom+")
    p.add_argument("--doctor", action="store_true")
    a = p.parse_args()
    if a.doctor:
        _doctor()
    elif a.hero:
        _console(a.hero, a.rank)
    else:
        from .overlay import main as overlay_main
        overlay_main()


if __name__ == "__main__":
    main()
