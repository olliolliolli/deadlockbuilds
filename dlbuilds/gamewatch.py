"""Автовизначення героя, на якому ти зараз граєш.

Гра, запущена з офіційним параметром -condebug, пише свою консоль у файл
<Deadlock>/game/citadel/console.log. Ми лише читаємо цей текстовий файл:
пам'ять гри не зачіпається, тож ризику бану немає. Так само працюють
Discord Rich Presence для Deadlock (шаблони рядків узято з відкритого коду
Jelloge/Deadlock-Rich-Presence і HeyTariq/deadlock-rpc).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

APP_ID = "1422450"
LOG_SUFFIX = Path("game") / "citadel" / "console.log"
PROCESS_NAMES = ("project8.exe", "deadlock.exe")
HIDEOUT_MAPS = {"dl_hideout"}

P = {k: re.compile(v, re.IGNORECASE) for k, v in {
    "loaded_hero": r"\[Server\] Loaded hero \d+/(hero_\w+)",
    "hero_vmdl": r"VMDL Camera Pose Success!.*models/heroes(?:_wip|_staging)?/(\w+)/",
    "map_info": r"\[Client\] Map:\s+\"([^\"]+)\"",
    "map_physics": r"\[Client\] Created physics for\s+([^\s]+)",
    "host_activate": r"\[HostStateManager\] Host activate:.*\(([^)]+)\)",
    "lobby_created": r"Lobby\s+\d+\s+for\s+Match\s+(\d+)\s+created",
    "lobby_destroyed": r"Lobby\s+\d+\s+for\s+Match\s+\d+\s+destroyed",
    "server_connect": r"\[Client\] CL:\s+Connected to '([^']+)'",
    "server_disconnect": r"\[Client\] Disconnecting from server:\s+(\S+)",
    "spectate": r"Playing Broadcast",
    "menu": r"LoopMode:\s*menu",
    "shutdown": r"Dispatching EventAppShutdown_t|Source2Shutdown",
}.items()}


def normalize_hero_key(raw: str) -> str:
    """'hero_haze_v2' / 'Haze' -> 'haze'."""
    s = raw.lower()
    s = re.sub(r"_v\d+$", "", s)
    if s.startswith("hero_"):
        s = s[5:]
    return s


_TS = re.compile(r"^(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d)")


def _line_time(line: str) -> float | None:
    """Рядки логу починаються з часу: «10/27 19:16:14 …» (без року)."""
    m = _TS.match(line)
    if not m:
        return None
    mo, d, hh, mm, ss = map(int, m.groups())
    now = datetime.now()
    try:
        dt = datetime(now.year, mo, d, hh, mm, ss)
    except ValueError:
        return None
    if dt.timestamp() > time.time() + 86400:      # рядок з минулого року (31.12 → 01.01)
        dt = dt.replace(year=now.year - 1)
    return dt.timestamp()


class LogParser:
    """Невеликий автомат станів: меню / хованка / матч / глядач."""

    def __init__(self):
        self.phase = "menu"
        self.hero: str | None = None
        self.window_open = True      # у матчі приймаємо лише перший сигнал героя
        self.line_ts: float | None = None      # час поточного рядка логу
        self.match_start: float | None = None  # коли почався поточний матч (за логом)
        self.match_id: str | None = None

    def _new_match(self, match_id: str | None = None):
        self.phase = "match"
        self.hero = None
        self.window_open = True
        self.match_start = self.line_ts or time.time()
        self.match_id = match_id

    def _map(self, name: str):
        name = name.lower().strip()
        if name.endswith(".vmap"):
            name = name[:-5]
        name = name.rsplit("/", 1)[-1]
        if name in HIDEOUT_MAPS:
            self.phase = "hideout"
            self.window_open = True
        elif self.phase not in ("match", "spectating"):
            self._new_match()

    def _hero(self, key: str):
        key = normalize_hero_key(key)
        if self.phase == "spectating":
            return
        if self.phase == "match":
            if self.hero and key != self.hero:
                return                # герой уже зафіксований, це чужа модель
            if not self.hero and not self.window_open:
                return
            self.window_open = False
        self.hero = key

    def feed(self, line: str) -> bool:
        """Повертає True, якщо змінився герой або фаза."""
        before = (self.hero, self.phase)
        ts = _line_time(line)
        if ts:
            self.line_ts = ts
        if m := P["loaded_hero"].search(line):
            self._hero(m.group(1))
        elif m := P["hero_vmdl"].search(line):
            self._hero(m.group(1))
        elif m := (P["map_info"].search(line) or P["map_physics"].search(line)
                   or P["host_activate"].search(line)):
            self._map(m.group(1))
        elif m := P["lobby_created"].search(line):
            self._new_match(m.group(1))
        elif m := P["server_connect"].search(line):
            if "loopback" not in m.group(1).lower() and self.phase != "match":
                self._new_match()
        elif P["spectate"].search(line):
            self.phase = "spectating"
        elif P["lobby_destroyed"].search(line) or P["menu"].search(line):
            if self.phase in ("match", "spectating"):
                self.phase = "menu"
        elif m := P["server_disconnect"].search(line):
            if self.phase in ("match", "spectating"):
                self.phase = "menu"
        elif P["shutdown"].search(line):
            self.phase, self.hero = "menu", None
        return (self.hero, self.phase) != before


# ---------------------------------------------------------------- Steam paths
def _steam_root() -> Path | None:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                return Path(winreg.QueryValueEx(k, "SteamPath")[0])
        except OSError:
            pass
        for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
            if Path(p).exists():
                return Path(p)
        return None
    for p in ("~/.steam/steam", "~/.local/share/Steam"):
        q = Path(p).expanduser()
        if q.exists():
            return q
    return None


def steam_exe() -> Path | None:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                return Path(winreg.QueryValueEx(k, "SteamExe")[0])
        except OSError:
            root = _steam_root()
            return root / "steam.exe" if root else None
    return None


def find_game_dir(override: str | None = None) -> Path | None:
    if override and (Path(override) / "game" / "citadel").exists():
        return Path(override)
    root = _steam_root()
    if not root:
        return None
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        libs += [Path(p.replace("\\\\", "\\")) for p in
                 re.findall(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8", errors="replace"))]
    except OSError:
        pass
    for lib in libs:
        manifest = lib / "steamapps" / f"appmanifest_{APP_ID}.acf"
        try:
            m = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text("utf-8", errors="replace"))
        except OSError:
            continue
        if m:
            game = lib / "steamapps" / "common" / m.group(1)
            if game.exists():
                return game
    for lib in libs:
        game = lib / "steamapps" / "common" / "Deadlock"
        if (game / "game" / "citadel").exists():
            return game
    return None


def _win_process_names() -> set[str]:
    """Імена запущених процесів через Toolhelp32 (без запуску tasklist)."""
    import ctypes
    from ctypes import wintypes as wt

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    if not snap or snap == wt.HANDLE(-1).value:
        return set()
    names = set()
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            names.add(e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return names


def game_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        names = _win_process_names()
    except OSError:
        return False
    return any(n in names for n in PROCESS_NAMES)


def read_shared(path: Path, offset: int, size: int) -> bytes:
    """Читання з повним спільним доступом (FILE_SHARE_READ|WRITE|DELETE),
    щоб гра могла писати, перейменовувати й видаляти лог, поки ми читаємо."""
    if sys.platform != "win32":
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(size)
    import ctypes
    from ctypes import wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    k32.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wt.DWORD]
    k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    h = k32.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None)   # GENERIC_READ, share all, OPEN_EXISTING
    if not h or h == wt.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW")
    try:
        k32.SetFilePointerEx(h, offset, None, 0)
        buf = ctypes.create_string_buffer(size)
        got = wt.DWORD(0)
        if not k32.ReadFile(h, buf, size, ctypes.byref(got), None):
            raise OSError(ctypes.get_last_error(), "ReadFile")
        return buf.raw[:got.value]
    finally:
        k32.CloseHandle(h)


def launch_with_condebug() -> bool:
    """Запускає Deadlock через Steam з -condebug (лише на цей запуск)."""
    exe = steam_exe()
    try:
        if exe and exe.exists():
            subprocess.Popen([str(exe), "-applaunch", APP_ID, "-condebug"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        if sys.platform == "win32":
            os.startfile(f"steam://run/{APP_ID}//-condebug/")  # noqa: S606
            return True
    except OSError:
        pass
    return False


# ---------------------------------------------------------------- watcher
class GameWatcher(threading.Thread):
    """Фоновий потік. on_change(status, hero_key) викликається з цього потоку.

    status: 'no_game_dir' | 'not_running' | 'no_condebug' | 'menu' | 'hideout'
            | 'match' | 'spectating'
    """

    def __init__(self, on_change, game_dir: str | None = None, poll: float = 2.0):
        super().__init__(daemon=True)
        self.on_change = on_change
        self.override = game_dir
        self.poll = poll
        self.parser = LogParser()
        self.status: str | None = None
        self._stop = threading.Event()
        self._pos = 0
        self._last_proc_check = 0.0
        self._running = False

    def stop(self):
        self._stop.set()

    def _emit(self, status: str):
        pr = self.parser
        info = {"match_start": pr.match_start, "match_id": pr.match_id} if status == "match" else {}
        key = (status, pr.hero, pr.match_id, pr.match_start)
        if key != getattr(self, "_last", None):
            self._last = key
            self.status = status
            self.on_change(status, pr.hero, info)

    def _read_new(self, path: Path, resync: bool) -> bool:
        size = path.stat().st_size
        if size < self._pos:              # файл перезаписано новим запуском гри
            self._pos = 0
            resync = True
        if resync:
            self._pos = max(0, size - 400_000)
            self.parser = LogParser()
        if size == self._pos:
            return False
        data = read_shared(path, self._pos, min(size - self._pos, 4_000_000))
        self._pos += len(data)
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if resync and self._pos > 0 and lines:
            lines = lines[1:]             # перший рядок обрізаний
        changed = False
        for line in lines:
            changed |= self.parser.feed(line)
        return changed

    def run(self):
        log: Path | None = None
        resync = True
        started_at = 0.0
        while not self._stop.is_set():
            try:
                if log is None:
                    game = find_game_dir(self.override)
                    if not game:
                        self._emit("no_game_dir")
                        self._stop.wait(15)
                        continue
                    log = game / LOG_SUFFIX

                now = time.time()
                # Поки гра працює, список процесів переглядаємо рідко; поки не працює — частіше,
                # щоб швидко помітити запуск.
                if now - self._last_proc_check > (15 if self._running else 5):
                    was = self._running
                    self._running = game_running()
                    self._last_proc_check = now
                    if self._running and not was:
                        started_at, resync = now, True
                if not self._running:
                    self.parser = LogParser()
                    self._emit("not_running")
                    self._stop.wait(3)
                    continue

                # Під час запуску гра пише в лог сотні рядків. Якщо через хвилину
                # після старту файл так і не оновився — -condebug не увімкнено.
                if not log.exists() or (now - started_at > 60
                                        and log.stat().st_mtime < started_at - 30
                                        and self.parser.hero is None):
                    self._emit("no_condebug")
                    self._stop.wait(3)
                    continue

                self._read_new(log, resync)
                resync = False
                self._emit(self.parser.phase)
            except OSError:
                pass
            self._stop.wait(self.poll)


def close_other_instances(exe_name: str = "DeadlockBuilds.exe") -> int:
    """Закриває інші запущені копії програми (наприклад, стару версію,
    що лишилась у фоні й тримає гарячі клавіші). Повертає кількість закритих.

    exe від PyInstaller складається з двох процесів (завантажувач + програма),
    тож свої — це поточний процес і його батьківський."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return 0
    mine = {os.getpid(), os.getppid()}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(["tasklist", "/NH", "/FO", "CSV", "/FI", f"IMAGENAME eq {exe_name}"],
                             capture_output=True, text=True, timeout=5, creationflags=flags).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    killed = 0
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2 or not parts[1].strip('"').isdigit():
            continue
        pid = int(parts[1].strip('"'))
        if pid in mine:
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                           timeout=5, creationflags=flags)
            killed += 1
        except (OSError, subprocess.SubprocessError):
            pass
    return killed
