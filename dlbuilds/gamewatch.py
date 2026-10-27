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
    "lobby_created": r"Lobby\s+\d+\s+for\s+Match\s+\d+\s+created",
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


class LogParser:
    """Невеликий автомат станів: меню / хованка / матч / глядач."""

    def __init__(self):
        self.phase = "menu"
        self.hero: str | None = None
        self.window_open = True      # у матчі приймаємо лише перший сигнал героя

    def _new_match(self):
        self.phase = "match"
        self.hero = None
        self.window_open = True

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
        if m := P["loaded_hero"].search(line):
            self._hero(m.group(1))
        elif m := P["hero_vmdl"].search(line):
            self._hero(m.group(1))
        elif m := (P["map_info"].search(line) or P["map_physics"].search(line)
                   or P["host_activate"].search(line)):
            self._map(m.group(1))
        elif P["lobby_created"].search(line):
            self._new_match()
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


def game_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                             timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(f'"{n}"' in out for n in PROCESS_NAMES)


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

    def __init__(self, on_change, game_dir: str | None = None, poll: float = 1.0):
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
        key = (status, self.parser.hero)
        if key != getattr(self, "_last", None):
            self._last = key
            self.status = status
            self.on_change(status, self.parser.hero)

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
        with open(path, "rb") as f:
            f.seek(self._pos)
            data = f.read(size - self._pos)
        self._pos = size
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
                if now - self._last_proc_check > 5:
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
