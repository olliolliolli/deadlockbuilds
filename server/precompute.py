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
    an = Analyzer(Client(cache, ttl=3600, attempts=5, min_interval=0.4), bulk=True)

    heroes = an.heroes()
    an.items()           # прогріваємо довідники до запуску потоків
    # Прогріваємо спільні запити послідовно, щоб потоки не дублювали їх.
    for start, _ in an.windows()[:3]:
        for _, badge in RANKS:
            an.c.hero_stats(start, badge)
    jobs = [(int(h["id"]), ri) for h in heroes for ri in range(len(RANKS))]
    print(f"{len(heroes)} героїв × {len(RANKS)} рангів = {len(jobs)} збірок")

    builds, errors = {}, {}

    def run(batch, workers):
        with ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(an.build, hid, RANKS[ri][1]): (hid, ri) for hid, ri in batch}
            for f in as_completed(futs):
                hid, ri = futs[f]
                try:
                    builds[f"{hid}:{ri}"] = build_to_dict(f.result())
                    errors.pop(f"{hid}:{ri}", None)
                except Exception as e:  # noqa: BLE001
                    errors[f"{hid}:{ri}"] = f"{type(e).__name__}: {e}"[:300]

    run(jobs, 2)
    # Друге й третє коло для тих, що впали (ліміт запитів, тайм-аути).
    for attempt in (1, 2):
        failed = [(int(k.split(":")[0]), int(k.split(":")[1])) for k in errors]
        if not failed:
            break
        print(f"Повтор {attempt}: {len(failed)} збірок")
        time.sleep(20 * attempt)
        run(failed, 1)

    errors = list(errors.items())
    for k, e in errors[:20]:
        print("ERR", k, e)
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
        "errors": dict(errors[:50]),
    }
    path = out / "builds.json"
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"OK: {len(builds)} збірок, {path.stat().st_size // 1024} KB, {time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "out"))
