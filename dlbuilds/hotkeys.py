"""Глобальні гарячі клавіші через системний механізм Windows RegisterHotKey.

Раніше тут був pynput, але він ставить низькорівневий хук на ВСЮ клавіатуру:
кожне натискання в грі спершу проходило через Python. Коли програма була зайнята
(розбір даних), Windows затримувала клавіші, губила їх відпускання — персонаж
«сам біг», а клавіатура не реагувала. RegisterHotKey не перехоплює клавіатуру:
Windows сама повідомляє програму лише про нашу комбінацію.
"""
from __future__ import annotations

import sys
import threading

MODS = {"ctrl": 0x2, "shift": 0x4, "alt": 0x1}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def parse(combo: str) -> tuple[frozenset[str], int] | None:
    """'ctrl+shift+x' -> ({'ctrl','shift'}, VK_X)."""
    parts = [p.strip().lower().strip("<>") for p in combo.split("+") if p.strip()]
    mods = frozenset(p for p in parts if p in MODS)
    keys = [p for p in parts if p not in MODS]
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


class HotkeyListener(threading.Thread):
    def __init__(self, bindings: dict, on_error=None):
        super().__init__(daemon=True)
        self.bindings = bindings
        self.on_error = on_error
        self.failed: list[str] = []
        self._tid = None

    def run(self):
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes as wt

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32")
        user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
        user32.RegisterHotKey.restype = wt.BOOL
        user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        self._tid = kernel32.GetCurrentThreadId()

        callbacks = {}
        for i, (combo, cb) in enumerate(self.bindings.items(), start=1):
            parsed = parse(combo)
            if not parsed:
                self.failed.append(combo)
                continue
            mods, vk = parsed
            flags = MOD_NOREPEAT
            for m in mods:
                flags |= MODS[m]
            if user32.RegisterHotKey(None, i, flags, vk):
                callbacks[i] = cb
            else:
                self.failed.append(combo)   # комбінацію вже зайняла інша програма
        if self.failed and self.on_error:
            self.on_error(self.failed)

        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    cb = callbacks.get(int(msg.wParam))
                    if cb:
                        try:
                            cb()
                        except Exception:  # noqa: BLE001
                            pass
        finally:
            for i in callbacks:
                user32.UnregisterHotKey(None, i)

    def stop(self):
        if self._tid and sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
