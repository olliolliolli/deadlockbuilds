"""Оверлей поверх гри (PySide6).

Окреме прозоре вікно «завжди зверху». Гра має бути в режимі
«Вікно без рамки» (Borderless / Fullscreen Windowed), інакше Windows
не покаже інші вікна поверх неї.
"""
from __future__ import annotations

import hashlib
import re
import threading
import sys
import traceback
from datetime import datetime
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QPoint, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSizeGrip, QVBoxLayout, QWidget)

from . import config as cfgmod
from .analysis import PHASES, RANKS, SLOT_NAMES, Analyzer, Build, ItemRec
from .api import Client, force_ipv4
from .gamewatch import GameWatcher, game_running, launch_with_condebug, normalize_hero_key
from .hotkeys import HotkeyListener, pretty
from .serverdata import ServerData, default_url

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
class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Job(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args, self.sig = fn, args, _Signals()

    @Slot()
    def run(self):
        try:
            self.sig.done.emit(self.fn(*self.args))
        except Exception as e:  # noqa: BLE001 — показуємо будь-яку помилку в оверлеї
            traceback.print_exc()
            self.sig.failed.emit(str(e))


class IconCache(QObject):
    """Завантажує іконки у фоні та кешує їх на диску."""
    loaded = Signal(str, QImage)

    def __init__(self, cache_dir: Path, pool: QThreadPool):
        super().__init__()
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pool = pool
        self.mem: dict[str, QPixmap] = {}
        self.waiting: dict[str, list] = {}
        self.loaded.connect(self._on_loaded)

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
            if not path.exists():
                r = requests.get(url, timeout=20)
                r.raise_for_status()
                path.write_bytes(r.content)
            return QImage(str(path))

        job = _Job(fetch)
        job.sig.done.connect(lambda img, u=url: self.loaded.emit(u, img))
        job.sig.failed.connect(lambda _m, u=url: self.waiting.pop(u, None))
        self.pool.start(job)

    def _on_loaded(self, url: str, img: QImage):
        if img.isNull():
            self.waiting.pop(url, None)
            return
        pm = QPixmap.fromImage(img)
        self.mem[url] = pm
        for cb in self.waiting.pop(url, []):
            cb(pm)

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
    def __init__(self, r: ItemRec, icons: IconCache):
        super().__init__()
        self.setObjectName("row")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 6, 3)
        lay.setSpacing(9)

        icon = _icon_box(38, r.slot)
        icons.set_icon(icon, r.image, 32, 4)
        lay.addWidget(icon)

        mid = QVBoxLayout()
        mid.setSpacing(0)
        mid.addWidget(_label(r.name, "itemName"))
        when = f"~{r.buy_min:.0f} хв · " if r.buy_min is not None else ""
        mid.addWidget(_label(f"{when}{r.cost} душ · пік {r.pickrate * 100:.0f}%", "muted"))
        lay.addLayout(mid, 1)

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
            f"Відносно інших предметів тіру: {r.delta * 100:+.1f} п.п.".replace(",", " "))


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
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(8, self.pool.maxThreadCount()))
        self.icons = IconCache(cfgmod.app_dir() / "icons", self.pool)
        self.clickthrough = False
        self._drag: QPoint | None = None
        self._req = 0
        self.build_data: Build | None = None

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("Deadlock Builds")
        self.setStyleSheet(STYLE)
        self.setWindowOpacity(float(cfg["opacity"]))
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
        self.footer = _label(f"{pretty(cfg['hotkey_toggle'])} — сховати · "
                             f"{pretty(cfg['hotkey_clickthrough'])} — крізь клік", "muted")
        foot.addWidget(self.footer)
        foot.addStretch()
        foot.addWidget(QSizeGrip(self))
        root.addLayout(foot)

        self.hero_box.currentIndexChanged.connect(lambda _i: self.load())
        self.rank_box.currentIndexChanged.connect(lambda _i: self.load())
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

    # ------------------------------------------------------------ painting / dragging
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)
        p.fillPath(path, QColor(18, 16, 14, 232))
        p.setPen(QColor(214, 178, 106, 90))
        p.drawPath(path)
        p.fillRect(14, 1, self.width() - 28, 2, QColor(214, 178, 106, 140))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 70:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e):
        self._drag = None

    def closeEvent(self, e):
        self.cfg["pos"] = [self.x(), self.y()]
        self.cfg["size"] = [self.width(), self.height()]
        self.cfg["rank_index"] = self.rank_box.currentIndex()
        cfgmod.save(self.cfg)
        super().closeEvent(e)
        QApplication.quit()

    # ------------------------------------------------------------ hotkeys
    def toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    # ------------------------------------------------------------ game detection
    def on_game(self, status: str, hero_key):
        self._game_status, self._game_hero = status, hero_key
        hero_id = self._hero_id_for(hero_key)
        name = self.hero_box.itemText(self.hero_box.findData(hero_id)) if hero_id else None
        texts = {
            "no_game_dir": ("#6f685f", "Не знайшов папку Deadlock у Steam", None),
            "not_running": ("#6f685f", "Гра не запущена", "Запустити гру"),
            "no_condebug": ("#e0b25a", "Щоб визначати героя, гру треба запустити з -condebug",
                            "Перезапустити"),
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
        if self._game_status == "no_condebug":
            self.status_text.setText("Закрий гру — програма перезапустить її з -condebug, "
                                     "або додай -condebug у параметри запуску в Steam.")
            if game_running():
                return
        if launch_with_condebug():
            self.status_text.setText("Запускаю Deadlock з автовизначенням героя…")
            self.status_btn.hide()

    def toggle_clickthrough(self):
        self.clickthrough = not self.clickthrough
        self.setWindowFlag(Qt.WindowTransparentForInput, self.clickthrough)
        self.setWindowOpacity(float(self.cfg["opacity"]) * (0.8 if self.clickthrough else 1))
        self.show()

    # ------------------------------------------------------------ data
    def _run(self, fn, ok, *args, quiet: bool = False):
        job = _Job(fn, *args)
        job.sig.done.connect(ok)
        if not quiet:
            job.sig.failed.connect(self._failed)
        self.pool.start(job)

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
            sb = self.server.build(hero_id, self.rank_box.currentIndex())
            if sb:
                self.show_build(sb, stale=not self.server.fresh, server_ts=self.server.generated_at)
                if self.server.fresh:
                    return

        # 1) миттєво — з кешу на диску (якщо цей герой уже відкривався)
        if self.offline and not force:
            def cached(build):
                if req == self._req and not self._fresh:
                    self.show_build(build, stale=True)
            self._run(self.offline.build, cached, hero_id, badge, quiet=True)

        # 2) свіжі дані з мережі
        def done(build):
            self.an.c.ttl = int(self.cfg["cache_ttl_sec"])
            if req == self._req:
                self._fresh = True
                self.show_build(build)

        self._run(self.an.build, done, hero_id, badge)

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

    def show_build(self, b: Build, stale: bool = False, server_ts: int = 0):
        self.build_data = b
        self.btn_refresh.setEnabled(not stale or bool(server_ts))
        self.hero_name.setText(b.hero_name)
        self.icons.set_icon(self.hero_icon, b.hero_image, 44, 7)
        wr = f" · WR {b.hero_wr * 100:.1f}%" if b.hero_wr is not None else ""
        upd = " · оновлюю…" if stale else ""
        if server_ts and not stale:
            upd = f" · оновлено о {datetime.fromtimestamp(server_ts):%H:%M}"
        self.hero_sub.setText(f"{b.hero_matches:,} матчів {b.window_label}{wr}{upd}".replace(",", " "))
        bar = self.findChild(QScrollArea).verticalScrollBar()
        pos = bar.value() if stale is False and getattr(self, "_last_hero", None) == b.hero_id else 0
        self._last_hero = b.hero_id

        self._clear()
        self._add(self._ingame_card(b))
        for key, title in PHASES:
            lst = b.phases.get(key) or []
            if not lst:
                continue
            sec = Section(title, f"{sum(r.cost for r in lst):,} душ".replace(",", " "))
            for r in lst:
                sec.lay.addWidget(ItemRow(r, self.icons))
            self._add(sec)
        if b.situational:
            sec = Section("Ситуативні / альтернативи")
            for r in b.situational:
                sec.lay.addWidget(ItemRow(r, self.icons))
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

        def copy(bid=best["id"], button=btn):
            QApplication.clipboard().setText(str(bid))
            button.setText("✓ Скопійовано — встав у пошук збірок у грі")
            QTimer.singleShot(2500, lambda: isValid(button) and button.setText(f"Копіювати ID збірки  {bid}"))

        btn.clicked.connect(copy)
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
                a.clicked.connect(lambda _c=False, bid=p["id"]: QApplication.clipboard().setText(str(bid)))
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


def main():
    _install_crash_log()
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
        "⚠ Не вдалося зайняти " + ", ".join(pretty(b) for b in bad)
        + " — змініть у config.json"))
    hk = HotkeyListener({
        cfg["hotkey_toggle"]: bridge.toggle.emit,
        cfg["hotkey_clickthrough"]: bridge.clickthrough.emit,
    }, on_error=bridge.hk_error.emit)
    hk.start()
    app.aboutToQuit.connect(hk.stop)
    app.aboutToQuit.connect(lambda: watcher.stop())
    bridge.game.connect(w.on_game)
    watcher = GameWatcher(bridge.game.emit, cfg.get("game_dir") or None)
    watcher.start()
    w.show()
    sys.exit(app.exec())
