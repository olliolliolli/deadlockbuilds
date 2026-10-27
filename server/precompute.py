"""Серверний перерахунок збірок для всіх героїв і рангів.

Запускається в GitHub Actions за розкладом і пише один файл builds.json,
який оверлей завантажує при старті (замість десятків запитів до API).

    python server/precompute.py <папка_для_результату>
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dlbuilds.analysis import RANKS, Analyzer, build_to_dict, hero_image  # noqa: E402
from dlbuilds.api import Client  # noqa: E402

FORMAT_VERSION = 1


def main(out_dir: str) -> int:
    t0 = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(tempfile.mkdtemp(prefix="dlb-cache-"))
    an = Analyzer(Client(cache, ttl=3600, verbose=False))

    heroes = an.heroes()
    an.items()           # прогріваємо довідники до запуску потоків
    an.windows()
    jobs = [(int(h["id"]), ri) for h in heroes for ri in range(len(RANKS))]
    print(f"{len(heroes)} героїв × {len(RANKS)} рангів = {len(jobs)} збірок")

    builds, errors = {}, []
    with ThreadPoolExecutor(6) as ex:
        futs = {ex.submit(an.build, hid, RANKS[ri][1]): (hid, ri) for hid, ri in jobs}
        for f in as_completed(futs):
            hid, ri = futs[f]
            try:
                builds[f"{hid}:{ri}"] = build_to_dict(f.result())
            except Exception as e:  # noqa: BLE001
                errors.append(f"{hid}:{ri} {type(e).__name__}: {e}")

    for e in errors[:20]:
        print("ERR", e)
    if len(builds) < len(jobs) * 0.5:
        print(f"Забагато помилок ({len(errors)}/{len(jobs)}) — старі дані не перезаписую")
        return 1

    data = {
        "format": FORMAT_VERSION,
        "generated_at": int(time.time()),
        "ranks": [name for name, _ in RANKS],
        "heroes": [{"id": int(h["id"]), "name": h.get("name"), "class_name": h.get("class_name"),
                    "image": hero_image(h)} for h in heroes],
        "builds": builds,
    }
    path = out / "builds.json"
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"OK: {len(builds)} збірок, {path.stat().st_size // 1024} KB, {time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "out"))
