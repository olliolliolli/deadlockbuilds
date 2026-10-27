"""Автоматичне налаштування параметра запуску -condebug для Deadlock.

Параметри запуску зберігаються в Steam/userdata/<id>/config/localconfig.vdf.
Steam читає цей файл під час старту й перезаписує при виході, тому міняти
його можна лише при закритому Steam. Перед зміною робиться резервна копія.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .gamewatch import APP_ID, _steam_root, game_running, steam_exe

FLAG = "-condebug"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- VDF (KeyValues)
class VdfError(ValueError):
    pass


def _tokens(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(text[i + 1], "\\" + text[i + 1]))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            if i >= n:
                raise VdfError("незакриті лапки")
            i += 1
            yield ("s", "".join(buf))
        else:
            raise VdfError(f"неочікуваний символ {c!r} на позиції {i}")


def loads(text: str) -> dict:
    it = _tokens(text)
    stack = [{}]
    key = None
    for tok in it:
        cur = stack[-1]
        if tok == "{":
            if key is None:
                raise VdfError("{ без ключа")
            new = {}
            cur[key] = new
            stack.append(new)
            key = None
        elif tok == "}":
            if len(stack) == 1 or key is not None:
                raise VdfError("зайва }")
            stack.pop()
        else:
            if key is None:
                key = tok[1]
            else:
                cur[key] = tok[1]
                key = None
    if len(stack) != 1 or key is not None:
        raise VdfError("неповний файл")
    return stack[0]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def dumps(d: dict, indent: int = 0) -> str:
    out = []
    tab = "\t" * indent
    for k, v in d.items():
        if isinstance(v, dict):
            out.append(f'{tab}"{_esc(k)}"\n{tab}{{\n{dumps(v, indent + 1)}{tab}}}\n')
        else:
            out.append(f'{tab}"{_esc(k)}"\t\t"{_esc(v)}"\n')
    return "".join(out)


def _ci(d: dict, key: str, create: bool = False):
    """Пошук ключа без урахування регістру (Steam пише і "apps", і "Apps")."""
    for k in d:
        if k.lower() == key.lower() and isinstance(d[k], dict):
            return d[k]
    if create:
        d[key] = {}
        return d[key]
    return None


def _app_section(root: dict, create: bool):
    node = _ci(root, "UserLocalConfigStore", create)
    for k in ("Software", "Valve", "Steam", "apps"):
        if node is None:
            return None
        node = _ci(node, k, create)
    if node is None:
        return None
    return _ci(node, APP_ID, create)


# ---------------------------------------------------------------- public API
def config_paths() -> list[Path]:
    root = _steam_root()
    if not root:
        return []
    return sorted((root / "userdata").glob("*/config/localconfig.vdf"))


def is_enabled() -> bool | None:
    """True — -condebug уже є хоча б в одному профілі Steam з Deadlock;
    None — не вдалося знайти конфіг Steam."""
    paths = config_paths()
    if not paths:
        return None
    for p in paths:
        try:
            app = _app_section(loads(p.read_text("utf-8", errors="replace")), False)
        except (OSError, ValueError):
            continue
        if app and FLAG in str(app.get("LaunchOptions", "")).split():
            return True
    return False


def steam_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/NH", "/FO", "CSV", "/FI", "IMAGENAME eq steam.exe"],
                             capture_output=True, text=True, timeout=5, creationflags=NOWIN).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return '"steam.exe"' in out.lower()


def _token_spans(text: str):
    """Токени з позиціями: ('{'|'}', start, end) або ('s', start, end, value).
    Для рядка (start, end) — межі разом із лапками."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield (c, i, i + 1, None)
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise VdfError("незакриті лапки")
            raw = text[i + 1:j]
            yield ("s", i, j + 1, raw.replace('\\"', '"').replace("\\\\", "\\"))
            i = j + 1
        else:
            raise VdfError(f"неочікуваний символ {c!r}")


def _find_app_block(text: str):
    """Знаходить блок apps/1422450: повертає (позиція '{', позиція '}', рядок
    LaunchOptions як (start, end, value) або None)."""
    path = ["userlocalconfigstore", "software", "valve", "steam", "apps", APP_ID]
    stack: list[str] = []
    key = None
    block_open = None
    launch = None
    for kind, st, en, val in _token_spans(text):
        if kind == "{":
            stack.append((key or "").lower())
            if stack == path:
                block_open = st
            key = None
        elif kind == "}":
            if stack == path:
                return block_open, st, launch
            stack.pop()
            key = None
        else:
            if key is None:
                key = val
            else:
                if stack == path and key.lower() == "launchoptions":
                    launch = (st, en, val)
                key = None
    return None


def _patch_file(p: Path) -> bool:
    """Точкова зміна тексту: лише значення LaunchOptions у блоці Deadlock.
    Решта файлу лишається байт у байт такою самою."""
    text = p.read_text("utf-8", errors="strict")
    loads(text)                                     # файл має бути коректним VDF
    found = _find_app_block(text)
    if not found:
        return False                                # Deadlock у цьому профілі не запускався
    open_pos, close_pos, launch = found
    if launch:
        st, en, val = launch
        if FLAG in val.split():
            return False
        new_val = (val.strip() + " " + FLAG).strip()
        new = text[:st] + '"' + _esc(new_val) + '"' + text[en:]
    else:
        # відступ як у рядка з закриваючою дужкою + 1 таб
        line_start = text.rfind("\n", 0, close_pos) + 1
        indent = text[line_start:close_pos]
        indent = indent if indent.strip() == "" else ""
        ins = f'{indent}\t"LaunchOptions"\t\t"{FLAG}"\n'
        new = text[:line_start] + ins + text[line_start:]
    data_new = loads(new)                           # перевірка: файл досі коректний
    app = _app_section(data_new, False)
    if not app or FLAG not in str(app.get("LaunchOptions", "")).split():
        raise VdfError("перевірка запису не пройшла")
    bak = p.with_name(p.name + ".dlbuilds.bak")
    if not bak.exists():
        shutil.copy2(p, bak)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(new, "utf-8")
    os.replace(tmp, p)
    return True


def enable(progress=lambda msg: None) -> tuple[bool, str]:
    """Додає -condebug у параметри запуску Deadlock. Закриває й знову
    відкриває Steam, якщо він запущений."""
    if game_running():
        return False, "Спершу закрий Deadlock, потім натисни ще раз."
    paths = config_paths()
    if not paths:
        return False, "Не знайшов конфіг Steam. Додай -condebug вручну: Steam → Deadlock → Властивості."
    exe = steam_exe()
    was_running = steam_running()
    if was_running:
        if not exe or not exe.exists():
            return False, "Не знайшов steam.exe. Закрий Steam вручну й натисни ще раз."
        progress("Закриваю Steam на кілька секунд…")
        subprocess.Popen([str(exe), "-shutdown"], creationflags=NOWIN)
        for _ in range(60):
            time.sleep(1)
            if not steam_running():
                break
        else:
            return False, "Steam не закрився. Закрий його вручну й натисни ще раз."
        time.sleep(1.5)                              # Steam дописує файли після виходу
    progress("Додаю -condebug у параметри запуску…")
    changed, errors = 0, []
    for p in paths:
        try:
            changed += _patch_file(p)
        except (OSError, ValueError) as e:          # ValueError: VdfError, кодування
            errors.append(f"{p.parent.parent.name}: {e}")
    if was_running and exe:
        progress("Запускаю Steam…")
        subprocess.Popen([str(exe), "-silent"], creationflags=NOWIN)
    if errors and not changed:
        return False, "Не вдалося змінити конфіг Steam: " + "; ".join(errors)
    if is_enabled():
        return True, "Готово! Автовизначення героя працюватиме при кожному запуску гри."
    return False, "Не знайшов Deadlock у профілі Steam. Запусти гру хоча б раз і спробуй знову."
