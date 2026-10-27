"""Глобальні гарячі клавіші.

Як це працює: окремий потік кожні 25 мс питає Windows (GetAsyncKeyState),
чи натиснуто потрібну комбінацію.

Чому саме так:
- pynput (перша версія) ставив хук на ВСЮ клавіатуру. Коли програма була
  зайнята, Windows губила відпускання клавіш, і персонаж у грі «сам біг».
- RegisterHotKey (друга версія) «займає» комбінацію в системі: вона не
  працює, якщо її тримає інша програма (або стара копія цієї), і вимикає
  Ctrl+X («вирізати») в усіх інших програмах.
- Опитування стану клавіш нічого не перехоплює й нічого не займає:
  натискання доходять до гри та інших програм як завжди.
"""
from __future__ import annotations

import sys
import threading

MOD_VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12}
POLL_SEC = 0.033


def parse(combo: str) -> tuple[frozenset[str], int] | None:
    """'ctrl+shift+x' -> ({'ctrl','shift'}, VK_X)."""
    parts = [p.strip().lower().strip("<>") for p in combo.split("+") if p.strip()]
    mods = frozenset(p for p in parts if p in MOD_VK)
    keys = [p for p in parts if p not in MOD_VK]
    if len(keys) != 1:
        return None
    k = keys[0]
    if len(k) == 1 and k.isalnum():
        return mods, ord(k.upper())
    if k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
        return mods, 0x70 + int(k[1:]) - 1
    return None


def pretty(combo: str) -> str:
    return "+".join(p.strip().strip("<>").upper() for p in combo.split("+"))


def matches(pressed: set[int], mods: frozenset[str], vk: int) -> bool:
    """Комбінація натиснута рівно з цими модифікаторами
    (щоб Ctrl+X не спрацьовував при Ctrl+Shift+X)."""
    if vk not in pressed:
        return False
    for name, mvk in MOD_VK.items():
        if (mvk in pressed) != (name in mods):
            return False
    return True


class HotkeyListener(threading.Thread):
    def __init__(self, bindings: dict, on_error=None):
        super().__init__(daemon=True, name="hotkeys")
        self.combos = []
        bad = []
        for combo, cb in bindings.items():
            parsed = parse(combo)
            if parsed:
                self.combos.append((parsed[0], parsed[1], cb))
            else:
                bad.append(combo)
        self.on_error = on_error
        self._bad = bad
        self._stop = threading.Event()

    def run(self):
        if self._bad and self.on_error:
            self.on_error(self._bad)
        if sys.platform != "win32" or not self.combos:
            return
        import ctypes
        user32 = ctypes.WinDLL("user32")
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short

        main_keys = sorted({vk for _, vk, _ in self.combos})
        mod_keys = list(MOD_VK.values())
        was_down = [False] * len(self.combos)
        gaks = user32.GetAsyncKeyState
        while not self._stop.wait(POLL_SEC):
            # Зазвичай це 1 виклик за тік: модифікатори питаємо, лише коли основна клавіша натиснута.
            pressed = {vk for vk in main_keys if gaks(vk) & 0x8000}
            if not pressed:
                if any(was_down):
                    was_down = [False] * len(self.combos)
                continue
            pressed |= {vk for vk in mod_keys if gaks(vk) & 0x8000}
            for i, (mods, vk, cb) in enumerate(self.combos):
                down = matches(pressed, mods, vk)
                if down and not was_down[i]:          # лише в момент натискання
                    try:
                        cb()
                    except Exception:  # noqa: BLE001
                        pass
                was_down[i] = down

    def stop(self):
        self._stop.set()
