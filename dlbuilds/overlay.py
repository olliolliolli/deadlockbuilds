"""Оверлей поверх гри (PySide6).

Окреме прозоре вікно «завжди зверху». Гра має бути в режимі
«Вікно без рамки» (Borderless / Fullscreen Windowed), інакше Windows
не покаже інші вікна поверх неї.
"""
from __future__ import annotations

import hashlib
import os
import time
from functools import partial
from concurrent.futures import ThreadPoolExecutor
import re
import threading
import sys
import traceback
from datetime import datetime
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QGraphicsOpacityEffect, QMenu, QSystemTrayIcon, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSizeGrip, QVBoxLayout, QWidget)

from . import config as cfgmod
from .analysis import PHASES, RANKS, SLOT_NAMES, STYLE_NAMES, Analyzer, Build, ItemRec, item_image
from .api import Client, force_ipv4
from .gamewatch import (GameWatcher, close_other_instances, game_running, launch_with_condebug,
                        normalize_hero_key)
from . import steamcfg, winfocus
from .hotkeys import HotkeyListener, pretty
from .hud import Hud, next_from_order, next_items
from .serverdata import ServerData, app_version, default_url

try:
    from shiboken6 import isValid
except ImportError:  # pragma: no cover
    def isValid(_o):
        return True

GOLD = "#d6b26a"
SLOT_COLORS = {"weapon": "#c97a2c", "vitality": "#5a9b3a", "spirit": "#8e5cc7"}
SLOT_BG = {"weapon": "#3b2614", "vitality": "#1f3317", "spirit": "#2b1e3d"}

STYLE = f"""
QWidget {{ color: #ece6da; font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 13px; }}
QComboBox, QPushButton {{
    background: #26221d; border: 1px solid #3d3730; border-radius: 6px; padding: 4px 8px; }}
QComboBox:hover, QPushButton:hover {{ border-color: {GOLD}; }}
QComboBox QAbstractItemView {{ background: #1d1a17; border: 1px solid #3d3730;
    selection-background-color: #4a3d28; outline: 0; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QPushButton#icon {{ padding: 2px; min-width: 26px; max-width: 26px; min-height: 22px; }}
QPushButton#primary {{ background: {GOLD}; color: #1a1612; border: none; font-weight: 600; padding: 6px 10px; }}
QPushButton#primary:hover {{ background: #e8c785; }}
QPushButton#primary:disabled {{ background: #5a5040; color: #2a2620; }}
QPushButton#chip {{ background: transparent; border: 1px solid #3d3730; border-radius: 10px;
    font-size: 11px; padding: 2px 8px; }}
QPushButton#chip:checked {{ border-color: {GOLD}; color: {GOLD}; }}
QFrame#status {{ background: rgba(214,178,106,0.07); border: 1px solid #3a3226; border-radius: 7px; }}
QPushButton#ghost {{ background: transparent; border: 1px solid #3d3730; font-size: 11px; padding: 3px 6px; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #4a433b; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
QLabel#heroName {{ font-size: 17px; font-weight: 700; color: #f4ede0; }}
QLabel#muted {{ color: #9a9286; font-size: 11px; }}
QLabel#section {{ color: {GOLD}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
QLabel#itemName {{ font-weight: 600; font-size: 13px; }}
QLabel#warn {{ color: #e0b25a; font-size: 11px; }}
QFrame#card {{ background: rgba(255,255,255,0.035); border: 1px solid #332e28; border-radius: 8px; }}
QFrame#row {{ background: transparent; border-radius: 6px; }}
QFrame#row:hover {{ background: rgba(255,255,255,0.05); }}
"""


# ---------------------------------------------------------------- background jobs
class Dispatcher(QObject):
    """Живе в GUI-потоці весь час роботи програми. Фонові потоки передають
    сюди функції, і вони виконуються в GUI-потоці.

    Раніше кожне завдання мало власний QRunnable з сигналами. Python знищував
    його, поки Qt ще доставляв результат, і це давало «access violation»."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.QueuedConnection)
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="dlb")

    @Slot(object)
    def _run(self, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def submit(self, fn, ok, fail=None, *args):
        def task():
            try:
                res = fn(*args)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                if fail:
                    msg = str(e)
                    self.call.emit(lambda: fail(msg))
                return
            self.call.emit(lambda: ok(res))
        self.pool.submit(task)


class IconCache(QObject):
    """Завантажує іконки у фоні та кешує їх на диску. Картинка (QImage)
    створюється лише в GUI-потоці з байтів, отриманих фоновим потоком."""

    def __init__(self, cache_dir: Path, dispatcher: Dispatcher):
        super().__init__()
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.d = dispatcher
        self.mem: dict[str, QPixmap] = {}
        self.waiting: dict[str, list] = {}

    def _path(self, url: str) -> Path:
        return self.dir / (hashlib.sha1(url.encode()).hexdigest() + Path(url).suffix[:5])

    def set_icon(self, label: QLabel, url: str | None, size: int, rounded: int = 6):
        label.setProperty("icon_size", size)
        label.setProperty("icon_round", rounded)
        label.setPixmap(QPixmap())
        if url:
            self.request(url, lambda pm, l=label: isValid(l) and self._apply(l, pm))

    def request(self, url: str, cb):
        """cb(QPixmap) викликається в GUI-потоці, щойно іконка готова."""
        if url in self.mem:
            cb(self.mem[url])
            return
        self.waiting.setdefault(url, []).append(cb)
        if len(self.waiting[url]) > 1:
            return
        path = self._path(url)

        def fetch():
            if path.exists():
                return path.read_bytes()
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            path.write_bytes(r.content)
            return r.content

        self.d.submit(fetch, lambda data, u=url: self._on_loaded(u, data),
                      lambda _m, u=url: self.waiting.pop(u, None))

    def _on_loaded(self, url: str, data: bytes):
        img = QImage.fromData(data)
        if img.isNull():
            self.waiting.pop(url, None)
            return
        pm = QPixmap.fromImage(img)
        self.mem[url] = pm
        for cb in self.waiting.pop(url, []):
            try:
                cb(pm)
            except RuntimeError:   # віджет уже видалено
                pass

    @staticmethod
    def _apply(label: QLabel, pm: QPixmap):
        size = int(label.property("icon_size") or 32)
        rad = int(label.property("icon_round") or 0)
        dpr = label.devicePixelRatioF()
        scaled = pm.scaled(QSize(size, size) * dpr, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        out = QPixmap(QSize(size, size) * dpr)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if rad:
            clip = QPainterPath()
            clip.addRoundedRect(0, 0, size * dpr, size * dpr, rad * dpr, rad * dpr)
            p.setClipPath(clip)
        p.drawPixmap(int((out.width() - scaled.width()) / 2), int((out.height() - scaled.height()) / 2), scaled)
        p.end()
        out.setDevicePixelRatio(dpr)
        label.setPixmap(out)


class HotkeyBridge(QObject):
    toggle = Signal()
    clickthrough = Signal()
    game = Signal(str, object)     # (status, hero_key) з потоку GameWatcher
    hk_error = Signal(list)


# ---------------------------------------------------------------- widgets
def _label(text: str = "", obj: str | None = None, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if obj:
        lbl.setObjectName(obj)
    lbl.setWordWrap(wrap)
    return lbl


def _icon_box(size: int, slot: str | None = None) -> QLabel:
    lbl = QLabel()
    lbl.setFixedSize(size, size)
    lbl.setAlignment(Qt.AlignCenter)
    border = SLOT_COLORS.get(slot or "", "#3d3730")
    bg = SLOT_BG.get(slot or "", "#221e1a")
    lbl.setStyleSheet(f"background:{bg}; border:1px solid {border}; border-radius:7px;")
    return lbl


class ItemRow(QFrame):
    def __init__(self, r: ItemRec, icons: IconCache, bought: bool = False, on_toggle=None):
        super().__init__()
        self.setObjectName("row")
        self.item_id = r.item_id
        self.on_toggle = on_toggle
        if on_toggle:
            self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 6, 3)
        lay.setSpacing(9)

        icon = _icon_box(38, r.slot)
        icons.set_icon(icon, r.image, 32, 4)
        lay.addWidget(icon)

        mid = QVBoxLayout()
        mid.setSpacing(0)
        self.name_lbl = _label(r.name, "itemName")
        mid.addWidget(self.name_lbl)
        when = f"~{r.buy_min:.0f} хв · " if r.buy_min is not None else ""
        pick = f" · пік {r.pickrate * 100:.0f}%" if r.matches else ""
        mid.addWidget(_label(f"{when}{r.cost} душ{pick}", "muted"))
        lay.addLayout(mid, 1)
        if not r.matches:                         # предмет зі збірки гри без статистики
            self.setToolTip(f"<b>{r.name}</b><br>{r.cost} душ<br><i>Клік — позначити купленим</i>")
            self.set_bought(bought)
            return

        right = QVBoxLayout()
        right.setSpacing(0)
        wr = _label(f"{r.winrate * 100:.1f}%")
        wr.setAlignment(Qt.AlignRight)
        wr.setStyleSheet("font-weight:600;")
        col = "#7fd17f" if r.delta >= 0.005 else "#e08a7a" if r.delta <= -0.005 else "#a8a095"
        d = _label(f"{'+' if r.delta >= 0 else ''}{r.delta * 100:.1f}")
        d.setAlignment(Qt.AlignRight)
        d.setStyleSheet(f"color:{col}; font-size:11px; font-weight:600;")
        right.addWidget(wr)
        right.addWidget(d)
        lay.addLayout(right)

        self.setToolTip(
            f"<b>{r.name}</b><br>{SLOT_NAMES.get(r.slot, r.slot)} · тір {r.tier} · {r.cost} душ<br>"
            f"Вінрейт {r.winrate * 100:.1f}% ({r.matches:,} матчів)<br>"
            f"Відносно інших предметів тіру: {r.delta * 100:+.1f} п.п.<br>"
            f"<i>Клік — позначити купленим</i>".replace(",", " "))
        self.set_bought(bought)

    def set_bought(self, on: bool):
        self.bought = on
        self.setGraphicsEffect(None)
        if on:
            eff = QGraphicsOpacityEffect(self)
            eff.setOpacity(0.38)
            self.setGraphicsEffect(eff)
        f = self.name_lbl.font()
        f.setStrikeOut(on)
        self.name_lbl.setFont(f)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.on_toggle:
            self.set_bought(not self.bought)
            self.on_toggle(self.item_id, self.bought)
            e.accept()
            return
        super().mousePressEvent(e)


class Section(QWidget):
    def __init__(self, title: str, extra: str = ""):
        super().__init__()
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 8, 0, 0)
        self.lay.setSpacing(2)
        head = QHBoxLayout()
        head.addWidget(_label(title.upper(), "section"))
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background:#3a332b;")
        head.addWidget(line, 1)
        if extra:
            head.addWidget(_label(extra, "muted"))
        self.lay.addLayout(head)


# ---------------------------------------------------------------- main window
class Overlay(QWidget):
    def __init__(self, analyzer: Analyzer, cfg: dict, offline: Analyzer | None = None,
                 server: ServerData | None = None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.an, self.cfg = analyzer, cfg
        self.offline = offline
        self.server = server if server and server.enabled else None
        self.bought: set[int] = set()
        self.match_started: float | None = None
        self.disp = Dispatcher()
        self.icons = IconCache(cfgmod.app_dir() / "icons", self.disp)
        self.hud = Hud(self.icons, tuple(cfg.get("hud_offset") or (20, 20)),
                       cfg.get("hud_corner") or "top_left")
        self.hud_timer = QTimer(self)
        self.hud_timer.timeout.connect(self.update_hud)
        self.hud_timer.start(30_000)
        self.clickthrough = False
        self._drag: QPoint | None = None
        self._req = 0
        self.build_data: Build | None = None

        # Непрозоре вікно: напівпрозорі вікна поверх гри змушують Windows змішувати
        # їх з кожним кадром гри й помітно знижують FPS.
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setWindowTitle("Deadlock Builds")
        self.setStyleSheet(STYLE)
        self.move(*cfg["pos"])
        self.resize(*cfg["size"])
        self.setMinimumSize(330, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 8)
        root.setSpacing(8)

        # --- header: hero portrait + name + buttons
        head = QHBoxLayout()
        head.setSpacing(10)
        self.hero_icon = _icon_box(46)
        head.addWidget(self.hero_icon)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.hero_name = _label("Deadlock Builds", "heroName")
        self.hero_sub = _label("Завантаження…", "muted", wrap=True)
        titles.addWidget(self.hero_name)
        titles.addWidget(self.hero_sub)
        head.addLayout(titles, 1)
        self.btn_refresh = QPushButton("⟳")
        self.btn_refresh.setObjectName("icon")
        self.btn_refresh.setToolTip("Перерахувати без кешу")
        self.btn_refresh.clicked.connect(lambda: self.load(force=True))
        btn_close = QPushButton("✕")
        btn_close.setObjectName("icon")
        btn_close.clicked.connect(self.close)
        head.addWidget(self.btn_refresh, 0, Qt.AlignTop)
        head.addWidget(btn_close, 0, Qt.AlignTop)
        root.addLayout(head)

        # --- pickers
        pick = QHBoxLayout()
        self.hero_box = QComboBox()
        self.hero_box.setIconSize(QSize(22, 22))
        self.hero_box.setMaxVisibleItems(18)
        self.hero_box.addItem("Завантаження героїв…")
        self.hero_box.setEnabled(False)
        self.rank_box = QComboBox()
        for name, _ in RANKS:
            self.rank_box.addItem(name)
        self.rank_box.setCurrentIndex(int(cfg.get("rank_index", 2)))
        pick.addWidget(self.hero_box, 3)
        pick.addWidget(self.rank_box, 2)
        self.style_box = QComboBox()
        self.style_box.setToolTip("Стиль гри: загальна статистика, дамаг або саппорт")
        self.style_box.hide()
        pick.addWidget(self.style_box, 2)
        root.addLayout(pick)

        # --- статус автовизначення героя
        self.status_box = QFrame()
        self.status_box.setObjectName("status")
        st = QHBoxLayout(self.status_box)
        st.setContentsMargins(8, 4, 6, 4)
        st.setSpacing(6)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color:#6f685f; font-size:10px;")
        self.status_text = _label("Шукаю гру…", "muted", wrap=True)
        self.status_btn = QPushButton("Запустити гру")
        self.status_btn.setObjectName("ghost")
        self.status_btn.setCursor(Qt.PointingHandCursor)
        self.status_btn.hide()
        self.status_btn.clicked.connect(self._launch_game)
        self.auto_btn = QPushButton("Авто")
        self.auto_btn.setObjectName("chip")
        self.auto_btn.setCheckable(True)
        self.auto_btn.setChecked(bool(cfg.get("auto_hero", True)))
        self.auto_btn.setToolTip("Автоматично перемикати на героя, за якого ти граєш")
        self.auto_btn.toggled.connect(self._auto_toggled)
        st.addWidget(self.status_dot)
        st.addWidget(self.status_text, 1)
        st.addWidget(self.status_btn)
        st.addWidget(self.auto_btn)
        root.addWidget(self.status_box)
        self._game_status, self._game_hero = None, None
        self._autodetect_cfg = None        # чи прописано -condebug у Steam (None — невідомо)
        self._hero_by_key: dict[str, int] = {}

        # --- body
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 4, 0)
        self.body_lay.setSpacing(4)
        self.body_lay.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.body)
        root.addWidget(scroll, 1)

        # --- footer
        foot = QHBoxLayout()
        self.footer = _label(f"{pretty(cfg['hotkey_toggle'])} — збірки/гра · "
                             f"{pretty(cfg['hotkey_clickthrough'])} — підказка · v{app_version()}", "muted")
        foot.addWidget(self.footer)
        foot.addStretch()
        foot.addWidget(QSizeGrip(self))
        root.addLayout(foot)

        self.hero_box.currentIndexChanged.connect(lambda _i: self.load())
        self.rank_box.currentIndexChanged.connect(lambda _i: self.load())
        self.style_box.activated.connect(self._style_changed)
        self._heroes_shown = False
        if self.server:
            # Готові збірки з сервера: спершу збережена копія (миттєво), потім свіжа.
            if self.server.load_cached() and self.server.heroes():
                self._heroes_loaded(self.server.heroes())
            self._run(self.server.refresh, self._server_refreshed, quiet=True)
        if self.offline:
            # Список героїв із кешу — миттєво; потім оновлюємо з мережі.
            self._run(self.offline.heroes, self._heroes_loaded, quiet=True)
        self._run(self.an.heroes, self._heroes_loaded)
        self._check_autodetect_cfg()

    # ------------------------------------------------------------ painting / dragging
    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(20, 18, 16))
        p.setPen(QColor(90, 76, 52))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.fillRect(0, 0, self.width(), 2, QColor(214, 178, 106))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 70:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e):
        self._drag = None

    def closeEvent(self, e):
        super().closeEvent(e)
        self.quit_app()

    def quit_app(self):
        """Зберегти налаштування й повністю завершити програму."""
        self.cfg["pos"] = [self.x(), self.y()]
        self.cfg["size"] = [self.width(), self.height()]
        self.cfg["rank_index"] = self.rank_box.currentIndex()
        cfgmod.save(self.cfg)
        if getattr(self, "_tray", None):
            self._tray.hide()
        QApplication.quit()

    # ------------------------------------------------------------ hotkeys
    def toggle_visible(self):
        """Ctrl+X:
        - оверлей схований → показати й забрати курсор у гри;
        - показаний, але активна гра → забрати курсор (оверлей стає активним);
        - показаний і активний → сховати й повернути курсор у гру."""
        hwnd = int(self.winId())
        if self.isVisible() and (winfocus.foreground() == hwnd or not self.cfg.get("take_focus", True)):
            self.hide()                    # назад у гру: лишається підказка в куті
            self._return_focus()
            self.update_hud()
            return
        if not self.isVisible():
            self.show()
        self.hud.hide()
        self.raise_()
        if self.cfg.get("take_focus", True):
            self._take_focus()

    def show_full(self):
        """Показати вікно зі збірками (з трея)."""
        if not self.isVisible():
            self.show()
        self.hud.hide()
        self.raise_()
        self._take_focus()

    def _take_focus(self):
        hwnd = int(self.winId())
        fg = winfocus.foreground()
        if fg and fg != hwnd:
            self._prev_fg = fg            # куди повернути фокус (гра)
        winfocus.activate(hwnd)
        self.activateWindow()
        winfocus.release_cursor(hwnd)

    def _return_focus(self):
        prev = getattr(self, "_prev_fg", 0)
        if prev and winfocus.is_window(prev):
            winfocus.activate(prev)

    # ------------------------------------------------------------ game detection
    def on_game(self, status: str, hero_key):
        if status == "match" and self._game_status != "match":
            self.match_started = time.time()     # новий матч — скидаємо позначки
            self.bought.clear()
        elif status != "match":
            self.match_started = None
        self._game_status, self._game_hero = status, hero_key
        QTimer.singleShot(0, self.update_hud)
        hero_id = self._hero_id_for(hero_key)
        name = self.hero_box.itemText(self.hero_box.findData(hero_id)) if hero_id else None
        texts = {
            "no_game_dir": ("#6f685f", "Не знайшов папку Deadlock у Steam", None),
            "not_running": (("#6f685f", "Гра не запущена", "Запустити гру")
                            if self._autodetect_cfg is not False else
                            ("#e0b25a", "Автовизначення героя вимкнене", "Увімкнути")),
            "no_condebug": (("#e0b25a", "Автовизначення вимкнене. Закрий гру й натисни «Увімкнути»",
                             "Увімкнути") if self._autodetect_cfg is not True else
                            ("#e0b25a", "Гру запущено до налаштування — перезапусти її", None)),
            "menu": ("#9a9286", "У меню" + (f" · останній герой: {name}" if name else ""), None),
            "hideout": ("#7fb3d1", f"У хованці · {name}" if name else "У хованці", None),
            "match": ("#7fd17f", f"У матчі · граєш за {name}" if name else "У матчі · обираєш героя…", None),
            "spectating": ("#9a9286", "Перегляд матчу", None),
        }
        color, text, btn = texts.get(status, ("#6f685f", status, None))
        self.status_dot.setStyleSheet(f"color:{color}; font-size:10px;")
        self.status_text.setText(text)
        self.status_btn.setVisible(bool(btn))
        if btn:
            self.status_btn.setText(btn)
        if status == "no_condebug":
            self.status_text.setToolTip("Steam → Deadlock → Властивості → Параметри запуску: -condebug\n"
                                        "Або натисни «Перезапустити» — програма відкриє гру з цим параметром.")
        if hero_id and self.auto_btn.isChecked() and status in ("hideout", "match", "menu"):
            if self.hero_box.currentData() != hero_id:
                self.hero_box.setCurrentIndex(self.hero_box.findData(hero_id))

    def _hero_id_for(self, key) -> int | None:
        if not key:
            return None
        return self._hero_by_key.get(re.sub(r"[^a-z0-9]", "", normalize_hero_key(key)))

    def _auto_toggled(self, on: bool):
        self.cfg["auto_hero"] = on
        cfgmod.save(self.cfg)
        if on and self._game_status:
            self.on_game(self._game_status, self._game_hero)

    def _launch_game(self):
        if self.status_btn.text() == "Увімкнути":
            self._enable_autodetect()
            return
        if launch_with_condebug():
            self.status_text.setText("Запускаю Deadlock з автовизначенням героя…")
            self.status_btn.hide()

    def _check_autodetect_cfg(self):
        def done(v):
            self._autodetect_cfg = v
            if self._game_status:
                self.on_game(self._game_status, self._game_hero)
        self._run(steamcfg.is_enabled, done, quiet=True)

    def _enable_autodetect(self):
        """Один раз додає -condebug у параметри запуску Deadlock у Steam."""
        self.status_btn.setEnabled(False)

        def progress(msg):
            self.disp.call.emit(lambda: self.status_text.setText(msg))

        def done(res):
            ok, msg = res
            self.status_btn.setEnabled(True)
            if ok:
                self._autodetect_cfg = True
                self.status_btn.hide()
            self.status_text.setText(msg)
            self.status_dot.setStyleSheet(f"color:{'#7fd17f' if ok else '#e08a7a'}; font-size:10px;")

        self._run(steamcfg.enable, done, progress)

    def toggle_clickthrough(self):
        """Ctrl+Shift+X — увімкнути/вимкнути підказку в правому нижньому куті."""
        self.cfg["hud_enabled"] = not self.cfg.get("hud_enabled", True)
        cfgmod.save(self.cfg)
        if getattr(self, "act_hud", None):
            self.act_hud.setChecked(self.cfg["hud_enabled"])
        self.update_hud()

    # ------------------------------------------------------------ HUD
    def _elapsed_min(self) -> float | None:
        if self._game_status == "match" and self.match_started:
            return (time.time() - self.match_started) / 60
        return None

    def update_hud(self):
        show = (self.cfg.get("hud_enabled", True) and not self.isVisible()
                and self.build_data is not None
                and self._game_status not in (None, "not_running", "no_game_dir"))
        if not show:
            self.hud.hide()
            return
        active = self._active_ingame()
        if active:
            items, how = next_from_order(self._ingame_order(active), self.bought, self._elapsed_min())
        else:
            items, how = next_items(self.build_data, self.bought, self._elapsed_min())
        self.hud.update_items(self.build_data.hero_name, items, how)
        self.hud.show()

    def _toggle_bought(self, item_id: int, on: bool):
        (self.bought.add if on else self.bought.discard)(item_id)
        self.update_hud()

    # ------------------------------------------------------------ in-game build
    def _active_ingame(self) -> dict | None:
        hero_id = self.hero_box.currentData()
        return (self.cfg.get("active_ingame") or {}).get(str(hero_id))

    def _set_active_ingame(self, entry: dict | None):
        hero_id = self.hero_box.currentData()
        act = dict(self.cfg.get("active_ingame") or {})
        if entry:
            act[str(hero_id)] = {"id": int(entry["id"]), "name": entry.get("name", ""),
                                 "cats": entry.get("cats") or []}
        else:
            act.pop(str(hero_id), None)
        self.cfg["active_ingame"] = act
        cfgmod.save(self.cfg)
        self.bought.clear()
        if self.build_data:
            self.show_build(self.build_data)

    def _copy_build(self, entry: dict, button, *_ignored):
        self._copy_id(entry["id"], button)
        if entry.get("cats"):
            self._set_active_ingame(entry)       # підказка в куті веде саме цю збірку

    def _item_rec(self, iid: int, b: Build | None) -> ItemRec | None:
        """Предмет збірки з гри: статистика з нашої збірки, якщо є, інакше лише назва/ціна."""
        if b:
            for r in [*sum((b.phases.get(k) or [] for k, _ in PHASES), []), *b.situational]:
                if r.item_id == iid:
                    return r
        meta = None
        if self.server and self.server.data:
            meta = (self.server.data.get("items") or {}).get(str(iid))
        if not meta:
            for an in (self.an, self.offline):
                raw = (getattr(an, "_items", None) or {}).get(iid) if an else None
                if raw:
                    meta = {"name": raw.get("name"), "cost": raw.get("cost"), "tier": raw.get("item_tier"),
                            "slot": str(raw.get("item_slot_type") or "").lower(), "image": item_image(raw)}
                    break
        if not meta or meta.get("name") is None:
            return None
        return ItemRec(iid, meta["name"], meta.get("slot") or "", int(meta.get("tier") or 0),
                       int(meta.get("cost") or 0), 0, 0.0, 0.0, 0.0, None, "", meta.get("image"))

    def _ingame_order(self, active: dict) -> list[ItemRec]:
        out = []
        for c in active.get("cats") or []:
            if c.get("optional"):
                continue
            for iid in c.get("ids") or []:
                r = self._item_rec(int(iid), self.build_data)
                if r and all(x.item_id != r.item_id for x in out):
                    out.append(r)
        return out

    def _copy_id(self, bid, button, *_ignored):
        if isinstance(bid, bool) or not str(bid).isdigit():
            return                                  # захист: копіюємо лише число
        QApplication.clipboard().setText(str(int(bid)))
        if button is not None and isValid(button):
            old = button.text()
            button.setText("✓ Скопійовано — встав у пошук збірок у грі")
            QTimer.singleShot(2500, lambda: isValid(button) and button.setText(old))

    # ------------------------------------------------------------ data
    def _run(self, fn, ok, *args, quiet: bool = False):
        self.disp.submit(fn, ok, None if quiet else self._failed, *args)

    def _failed(self, msg):
        self.hero_sub.setText(f"Помилка: {msg}")
        self.btn_refresh.setEnabled(True)

    def _heroes_loaded(self, heroes):
        from .analysis import hero_image
        if self._heroes_shown and self.hero_box.count() == len(heroes):
            return   # онлайн-список такий самий, як із кешу
        first = not self._heroes_shown
        self._heroes_shown = True
        keep = self.hero_box.currentData() if not first else self.cfg.get("hero_id")
        self.hero_box.blockSignals(True)
        self.hero_box.clear()
        self._hero_imgs = {}
        self._hero_by_key = {}
        for h in heroes:
            hid = int(h["id"])
            for k in (h.get("class_name") or "", h.get("name") or ""):
                k = re.sub(r"[^a-z0-9]", "", normalize_hero_key(k))
                if k:
                    self._hero_by_key.setdefault(k, hid)
        for i, h in enumerate(heroes):
            hid = int(h["id"])
            self.hero_box.addItem(h["name"], hid)
            url = h.get("image") or hero_image(h)
            self._hero_imgs[hid] = url
            if url:
                self.icons.request(url, lambda pm, idx=i, box=self.hero_box:
                                   isValid(box) and idx < box.count() and box.setItemIcon(idx, QIcon(pm)))
        idx = self.hero_box.findData(keep)
        self.hero_box.setCurrentIndex(max(idx, 0))
        self.hero_box.setEnabled(True)
        self.hero_box.blockSignals(False)
        if first:
            self.load()
        if self._game_status:            # гра вже відома — застосовуємо героя
            self.on_game(self._game_status, self._game_hero)

    def load(self, force: bool = False):
        if not self._heroes_shown or self.hero_box.currentIndex() < 0:
            return
        hero_id = self.hero_box.currentData()
        if hero_id is None:
            return
        badge = RANKS[self.rank_box.currentIndex()][1]
        style = (self.cfg.get("styles") or {}).get(str(hero_id), "damage")   # типово — дамаг
        if self.cfg.get("hero_id") != hero_id:
            self.bought.clear()
        self.cfg["hero_id"] = hero_id
        self.cfg["rank_index"] = self.rank_box.currentIndex()
        cfgmod.save(self.cfg)
        if force:
            self.an.c.ttl = 0
        self._req += 1
        req = self._req
        self._fresh = False
        self.hero_name.setText(self.hero_box.currentText())
        self.hero_sub.setText("Завантажую…")
        self.icons.set_icon(self.hero_icon, getattr(self, "_hero_imgs", {}).get(hero_id), 44, 7)
        self.btn_refresh.setEnabled(False)

        # 0) готова збірка з сервера
        if self.server and not force:
            sb = self.server.build(hero_id, self.rank_box.currentIndex(), style)
            if sb:
                self.show_build(sb, stale=not self.server.fresh, server_ts=self.server.generated_at)
                if self.server.fresh:
                    return

        # 1) миттєво — з кешу на диску (якщо цей герой уже відкривався)
        if self.offline and not force:
            def cached(build):
                if req == self._req and not self._fresh:
                    self.show_build(build, stale=True)
            self._run(self.offline.build, cached, hero_id, badge, style, quiet=True)

        # 2) свіжі дані з мережі
        def done(build):
            self.an.c.ttl = int(self.cfg["cache_ttl_sec"])
            if req == self._req:
                self._fresh = True
                self.show_build(build)

        self._run(self.an.build, done, hero_id, badge, style)

    # ------------------------------------------------------------ rendering
    def _clear(self):
        while self.body_lay.count() > 1:
            w = self.body_lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def _add(self, w: QWidget):
        self.body_lay.insertWidget(self.body_lay.count() - 1, w)

    def _server_refreshed(self, changed):
        if changed:
            if not self._heroes_shown and self.server.heroes():
                self._heroes_loaded(self.server.heroes())
            elif self._heroes_shown:
                self.load()

    def _style_changed(self, idx: int):
        hero_id = self.hero_box.currentData()
        if hero_id is None:
            return
        styles = dict(self.cfg.get("styles") or {})
        styles[str(hero_id)] = self.style_box.itemData(idx) or ""
        self.cfg["styles"] = styles
        cfgmod.save(self.cfg)
        self.bought.clear()
        self.load()

    def _sync_style_box(self, b: Build):
        self.style_box.blockSignals(True)
        self.style_box.clear()
        if b.styles:
            for key in [k for k in ("damage", "support", "") if k in b.styles or k == ""]:
                self.style_box.addItem(STYLE_NAMES.get(key, key), key)
            self.style_box.setCurrentIndex(max(0, self.style_box.findData(b.style or "")))
            self.style_box.setEnabled(True)
            self.style_box.show()
        else:
            self.style_box.hide()
        self.style_box.blockSignals(False)

    def show_build(self, b: Build, stale: bool = False, server_ts: int = 0):
        self.build_data = b
        self._sync_style_box(b)
        self.btn_refresh.setEnabled(not stale or bool(server_ts))
        self.hero_name.setText(b.hero_name)
        self.icons.set_icon(self.hero_icon, b.hero_image, 44, 7)
        wr = f" · WR {b.hero_wr * 100:.1f}%" if b.hero_wr is not None else ""
        if b.style:
            wr = f" · {STYLE_NAMES.get(b.style, b.style).lower()}{wr}"
        upd = " · оновлюю…" if stale else ""
        if server_ts and not stale:
            upd = f" · оновлено о {datetime.fromtimestamp(server_ts):%H:%M}"
        self.hero_sub.setText(f"{b.hero_matches:,} матчів {b.window_label}{wr}{upd}".replace(",", " "))
        bar = self.findChild(QScrollArea).verticalScrollBar()
        pos = bar.value() if stale is False and getattr(self, "_last_hero", None) == b.hero_id else 0
        self._last_hero = b.hero_id

        self._clear()
        self._add(self._ingame_card(b))
        active = self._active_ingame()
        if active:
            for i, c in enumerate(active.get("cats") or [], 1):
                rows = [r for r in (self._item_rec(int(x), b) for x in c.get("ids") or []) if r]
                if not rows:
                    continue
                title = c.get("name") or f"Розділ {i}"
                sec = Section(("Необов'язково · " if c.get("optional") else "Гра · ") + title,
                              f"{sum(r.cost for r in rows):,} душ".replace(",", " "))
                for r in rows:
                    sec.lay.addWidget(ItemRow(r, self.icons, r.item_id in self.bought, self._toggle_bought))
                self._add(sec)
            self._add(_label("Нижче — статистична збірка програми для порівняння.", "muted", wrap=True))
        for key, title in PHASES:
            lst = b.phases.get(key) or []
            if not lst:
                continue
            sec = Section(title, f"{sum(r.cost for r in lst):,} душ".replace(",", " "))
            for r in lst:
                sec.lay.addWidget(ItemRow(r, self.icons, r.item_id in self.bought, self._toggle_bought))
            self._add(sec)
        if b.situational:
            sec = Section("Ситуативні / альтернативи")
            for r in b.situational:
                sec.lay.addWidget(ItemRow(r, self.icons, r.item_id in self.bought, self._toggle_bought))
            self._add(sec)
        if b.ability_priority:
            self._add(self._abilities(b))
        for n in b.notes:
            self._add(_label("⚠ " + n, "warn", wrap=True))
        legend = _label("Відсоток — вінрейт з предметом; число під ним — різниця з іншими "
                        "предметами того ж тіру (п.п.). Дані: deadlock-api.com", "muted", wrap=True)
        legend.setStyleSheet("color:#6f685f; font-size:10px; margin-top:6px;")
        self._add(legend)
        QTimer.singleShot(0, lambda: bar.setValue(pos))
        self.update_hud()

    def _abilities(self, b: Build) -> QWidget:
        wr = f"WR {b.ability_wr * 100:.1f}%" if b.ability_wr else ""
        sec = Section("Прокачка здібностей", wr)
        row = QHBoxLayout()
        row.setSpacing(4)
        for i, name in enumerate(b.ability_priority):
            if i:
                arrow = _label("›")
                arrow.setStyleSheet(f"color:{GOLD}; font-size:18px;")
                row.addWidget(arrow)
            col = QVBoxLayout()
            col.setSpacing(2)
            icon = _icon_box(40)
            self.icons.set_icon(icon, b.ability_icons.get(name), 34, 4)
            icon.setToolTip(name)
            col.addWidget(icon, 0, Qt.AlignHCenter)
            cap = _label(name, "muted")
            cap.setAlignment(Qt.AlignHCenter)
            cap.setWordWrap(True)
            cap.setMaximumWidth(70)
            col.addWidget(cap)
            row.addLayout(col)
        row.addStretch()
        holder = QWidget()
        holder.setLayout(row)
        sec.lay.addWidget(holder)

        # Порядок очок: маленька сітка з номерами рівнів
        order = b.ability_order or []
        if order:
            names = list(dict.fromkeys(b.ability_priority or order))
            grid = QGridLayout()
            grid.setSpacing(2)
            for r_i, n in enumerate(names):
                ic = _icon_box(20)
                self.icons.set_icon(ic, b.ability_icons.get(n), 18, 3)
                grid.addWidget(ic, r_i, 0)
                for c_i, m in enumerate(order[:16]):
                    cell = QLabel(str(c_i + 1) if m == n else "")
                    cell.setFixedSize(16, 18)
                    cell.setAlignment(Qt.AlignCenter)
                    cell.setStyleSheet(
                        f"background:{GOLD if m == n else '#24201c'}; color:#1a1612; "
                        "border-radius:3px; font-size:9px; font-weight:700;")
                    grid.addWidget(cell, r_i, c_i + 1)
            g = QWidget()
            g.setLayout(grid)
            sec.lay.addWidget(g)
        return sec

    def _ingame_card(self, b: Build) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(4)
        lay.addWidget(_label("ЗБІРКА ДЛЯ ГРИ", "section"))
        active = self._active_ingame()
        if active:
            lay.addWidget(_label(f"✓ Підказка в куті веде збірку «{active.get('name')}» "
                                 f"(ID {active.get('id')}). Її предмети — нижче.", "warn", wrap=True))
            back = QPushButton("Повернути статистичну збірку")
            back.setObjectName("ghost")
            back.clicked.connect(lambda *_: self._set_active_ingame(None))
            lay.addWidget(back)
        if not b.ingame:
            lay.addWidget(_label("Не знайшов опублікованої збірки, схожої на рекомендовану. "
                                 "Можна зібрати її вручну за списком нижче.", "muted", wrap=True))
            return card

        best = b.ingame[0]
        lay.addWidget(_label(best["name"], "itemName", wrap=True))
        upd = ""
        if best.get("updated"):
            upd = f" · оновлена {datetime.fromtimestamp(int(best['updated'])):%d.%m.%y}"
            if best.get("outdated"):
                upd += " (до патча)"
        lay.addWidget(_label(
            f"Збіг {best['match'] * 100:.0f}% · {best['core_got']}/{best['core_total']} основних предметів"
            f" · ★ {best['favorites']}{upd}", "muted", wrap=True))

        btn = QPushButton(f"Копіювати ID збірки  {best['id']}")
        btn.setObjectName("primary")
        btn.setCursor(Qt.PointingHandCursor)

        # partial: clicked додає свій bool «checked» лише в кінець (*_ignored).
        btn.clicked.connect(partial(self._copy_build, best, btn))
        lay.addWidget(btn)
        how = _label("У грі: магазин → «Збірки» (Builds) → пошук → Ctrl+V → обрати збірку.",
                     "muted", wrap=True)
        lay.addWidget(how)

        if len(b.ingame) > 1:
            alt = QHBoxLayout()
            alt.addWidget(_label("Інші:", "muted"))
            for p in b.ingame[1:]:
                a = QPushButton(f"{p['match'] * 100:.0f}% · {p['id']}")
                a.setObjectName("ghost")
                a.setToolTip(f"{p['name']} · ★ {p['favorites']}")
                a.clicked.connect(partial(self._copy_build, p, a))
                alt.addWidget(a)
            alt.addStretch()
            lay.addLayout(alt)
        return card


# ---------------------------------------------------------------- entry point
def _install_crash_log():
    """Записує всі помилки й аварійні завершення в crash.log."""
    import faulthandler
    path = cfgmod.app_dir() / "crash.log"
    try:
        if path.exists() and path.stat().st_size > 256_000:
            path.unlink()
        f = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    f.write(f"\n=== start {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
    faulthandler.enable(f)
    if sys.stderr is None:          # pythonw.exe не має консолі
        sys.stderr = f
    if sys.stdout is None:
        sys.stdout = f

    def hook(tp, val, tb):
        traceback.print_exception(tp, val, tb, file=f)

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    main._crash_file = f  # тримаємо файл відкритим


def _app_icon() -> QIcon:
    """Іконка програми (малюється кодом, без файлів)."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r = size * 0.18
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, size - 1, size - 1, r, r)
        p.fillPath(path, QColor(GOLD))
        p.setPen(QColor(26, 22, 18))
        f = QFont("Segoe UI", 1)
        f.setBold(True)
        f.setPixelSize(int(size * 0.62))
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, "D")
        p.end()
        icon.addPixmap(pm)
    return icon


def _setup_tray(app, w: "Overlay", cfg: dict, set_watch=None):
    icon = _app_icon()
    app.setWindowIcon(icon)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("Deadlock Builds")
    menu = QMenu()
    menu.setStyleSheet("QMenu { background:#1d1a17; color:#ece6da; border:1px solid #3d3730; }"
                       "QMenu::item { padding:5px 18px; } QMenu::item:selected { background:#4a3d28; }")
    menu.addAction("Показати збірки", w.show_full)
    w.act_hud = menu.addAction("Підказка в куті")
    w.act_hud.setCheckable(True)
    w.act_hud.setChecked(bool(cfg.get("hud_enabled", True)))
    w.act_hud.triggered.connect(lambda *_: w.toggle_clickthrough())
    focus = menu.addAction("Забирати курсор у гри (Ctrl+X)")
    focus.setCheckable(True)
    focus.setChecked(bool(cfg.get("take_focus", True)))
    focus.setToolTip("Без цього FPS гри не падає, але курсор у вікні треба діставати самому")

    def _focus(on):
        cfg["take_focus"] = on
        cfgmod.save(cfg)
    focus.toggled.connect(_focus)
    if set_watch:
        act = menu.addAction("Читати лог гри (автовизначення)")
        act.setCheckable(True)
        act.setChecked(bool(cfg.get("read_game_log", True)))
        act.toggled.connect(set_watch)
    menu.addSeparator()
    menu.addAction("Закрити програму", w.quit_app)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: w.show_full()
                           if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
    tray.show()
    w._tray, w._tray_menu = tray, menu                  # тримаємо посилання


def _lower_priority():
    """Програма поступається грі процесором: пріоритет «нижче звичайного»
    та режим енергоефективності Windows (EcoQoS)."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes as wt
    k32 = ctypes.WinDLL("kernel32")
    proc = k32.GetCurrentProcess()
    k32.SetPriorityClass(proc, 0x4000)                    # BELOW_NORMAL_PRIORITY_CLASS

    class PPTS(ctypes.Structure):
        _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG), ("StateMask", wt.ULONG)]
    st = PPTS(1, 1, 1)                                     # EXECUTION_SPEED throttling on
    try:
        k32.SetProcessInformation(proc, 4, ctypes.byref(st), ctypes.sizeof(st))
    except (AttributeError, OSError):
        pass


def main():
    _install_crash_log()
    _lower_priority()
    if close_other_instances():
        time.sleep(0.5)          # стара копія звільняє ресурси
    cfg = cfgmod.load()
    if cfg.get("force_ipv4", True):
        force_ipv4()
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 9))
    cache = cfgmod.app_dir() / "cache"
    log = cfgmod.app_dir() / "network.log"
    try:
        if log.exists() and log.stat().st_size > 512_000:
            log.unlink()
    except OSError:
        pass
    online = Analyzer(Client(cache, ttl=int(cfg["cache_ttl_sec"]), log_path=log),
                      int(cfg["min_hero_matches"]), int(cfg["fallback_days"]))
    offline = Analyzer(Client(cache, offline=True),
                       int(cfg["min_hero_matches"]), int(cfg["fallback_days"]))
    server = ServerData(cfg.get("data_url") or default_url(), cfgmod.app_dir())
    w = Overlay(online, cfg, offline, server)
    bridge = HotkeyBridge()
    bridge.toggle.connect(w.toggle_visible)
    bridge.clickthrough.connect(w.toggle_clickthrough)
    bridge.hk_error.connect(lambda bad: w.footer.setText(
        "⚠ Невідома гаряча клавіша: " + ", ".join(bad) + " — виправ у config.json"))
    hk = HotkeyListener({
        cfg["hotkey_toggle"]: bridge.toggle.emit,
        cfg["hotkey_clickthrough"]: bridge.clickthrough.emit,
    }, on_error=bridge.hk_error.emit)
    hk.start()
    app.aboutToQuit.connect(hk.stop)
    app.aboutToQuit.connect(lambda: watcher and watcher.stop())
    bridge.game.connect(w.on_game)
    watcher = None

    def set_watch(on: bool):
        """Тумблер «Читати лог гри»: повністю вмикає/вимикає взаємодію з грою."""
        nonlocal watcher
        cfg["read_game_log"] = on
        cfgmod.save(cfg)
        if on and watcher is None:
            watcher = GameWatcher(bridge.game.emit, cfg.get("game_dir") or None)
            watcher.start()
        elif not on and watcher is not None:
            watcher.stop()
            watcher = None
            w.on_game("not_running", None)
            w.status_text.setText("Читання логу гри вимкнене (трей → «Читати лог гри»)")

    set_watch(cfg.get("read_game_log", True))
    _setup_tray(app, w, cfg, set_watch)
    w.show()
    app.aboutToQuit.connect(w.hud.close)
    code = app.exec()
    # Гарантовано завершуємо процес: фонові потоки (мережа, лог гри) не мають
    # тримати програму «живою» без вікна.
    os._exit(code)
