"""Перемикання фокусу між грою та оверлеєм (Windows).

Поки гра активна, вона ховає курсор і тримає його в центрі екрана. Щоб
мишкою можна було клацати по оверлею, оверлей має стати активним вікном:
тоді гра сама «відпускає» курсор. Під час повернення фокус віддається
назад у гру, і вона знову забирає мишку.
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as wt

    _u = ctypes.WinDLL("user32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32")
    _u.GetForegroundWindow.restype = wt.HWND
    _u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _u.GetWindowThreadProcessId.restype = wt.DWORD
    _u.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    _u.SetForegroundWindow.argtypes = [wt.HWND]
    _u.BringWindowToTop.argtypes = [wt.HWND]
    _u.SetFocus.argtypes = [wt.HWND]
    _u.IsWindow.argtypes = [wt.HWND]
    _u.ClipCursor.argtypes = [ctypes.c_void_p]
    _u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]


def foreground() -> int:
    if sys.platform != "win32":
        return 0
    return int(_u.GetForegroundWindow() or 0)


def activate(hwnd: int) -> bool:
    """Робить вікно активним, навіть якщо зараз активна повноекранна гра."""
    if sys.platform != "win32" or not hwnd:
        return False
    fg = _u.GetForegroundWindow()
    if fg and int(fg) == hwnd:
        return True
    cur = _k.GetCurrentThreadId()
    fg_tid = _u.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_tid and fg_tid != cur and _u.AttachThreadInput(cur, fg_tid, True))
    try:
        _u.BringWindowToTop(hwnd)
        ok = bool(_u.SetForegroundWindow(hwnd))
        _u.SetFocus(hwnd)
    finally:
        if attached:
            _u.AttachThreadInput(cur, fg_tid, False)
    return ok


def release_cursor(hwnd: int):
    """Знімає обмеження курсора, яке поставила гра, і ставить курсор на оверлей."""
    if sys.platform != "win32":
        return
    _u.ClipCursor(None)
    r = wt.RECT()
    if hwnd and _u.GetWindowRect(hwnd, ctypes.byref(r)):
        _u.SetCursorPos((r.left + r.right) // 2, r.top + 90)


def is_window(hwnd: int) -> bool:
    return sys.platform == "win32" and bool(hwnd) and bool(_u.IsWindow(hwnd))
