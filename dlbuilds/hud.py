"""Компактна підказка в правому нижньому куті: що купити далі.

Вікно прозоре для миші (кліки проходять у гру) і не забирає фокус.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLayout, QVBoxLayout, QWidget

from .analysis import PHASES, Build, ItemRec

SLOT_COLORS = {"weapon": "#c97a2c", "vitality": "#5a9b3a", "spirit": "#8e5cc7"}
SLOT_BG = {"weapon": "#3b2614", "vitality": "#1f3317", "spirit": "#2b1e3d"}


def build_order(b: Build) -> list[ItemRec]:
    """Основні предмети збірки в порядку покупки."""
    out: list[ItemRec] = []
    for key, _ in PHASES:
        out += sorted(b.phases.get(key) or [],
                      key=lambda r: (r.buy_min if r.buy_min is not None else r.tier * 10, r.cost))
    return out


def next_items(b: Build | None, bought: set[int], elapsed_min: float | None,
               n: int = 4) -> tuple[list[ItemRec], str]:
    """Наступні n предметів і пояснення, як їх обрано.

    Якщо ти позначав куплені предмети — пропускаємо саме їх. Інакше, якщо
    відомий час матчу, пропускаємо предмети, які зазвичай купують на 3+ хв
    раніше (ймовірно, вже куплені)."""
    if not b:
        return [], ""
    order = build_order(b)
    if bought:
        rest = [r for r in order if r.item_id not in bought]
        return rest[:n], "без позначених як куплені"
    if elapsed_min is not None and elapsed_min > 1:
        rest = [r for r in order if r.buy_min is None or r.buy_min >= elapsed_min - 3]
        return rest[:n], f"за часом матчу (~{elapsed_min:.0f} хв)"
    return order[:n], "початок збірки"


class Hud(QWidget):
    def __init__(self, icons, margins=(24, 230)):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.icons = icons
        self.margins = margins
        self.setStyleSheet("QWidget { color:#ece6da; font-family:'Segoe UI'; font-size:12px; }"
                           "QLabel#t { color:#d6b26a; font-size:10px; font-weight:700; letter-spacing:1px; }"
                           "QLabel#m { color:#9a9286; font-size:10px; }")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(12, 9, 12, 10)
        self.lay.setSpacing(4)
        # Вікно завжди рівно під розмір вмісту; кожна зміна розміру → resizeEvent → place().
        self.lay.setSizeConstraint(QLayout.SetFixedSize)
        self.setMinimumWidth(250)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
        p.fillPath(path, QColor(16, 14, 12, 200))
        p.setPen(QColor(214, 178, 106, 80))
        p.drawPath(path)

    def _clear(self):
        while self.lay.count():
            item = self.lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
            elif item.layout():
                lay = item.layout()
                while lay.count():
                    sub = lay.takeAt(0).widget()
                    if sub:
                        sub.hide()
                        sub.setParent(None)
                        sub.deleteLater()

    def update_items(self, hero: str, items: list[ItemRec], how: str):
        self._clear()
        t = QLabel(f"ДАЛІ КУПИТИ · {hero.upper()}")
        t.setObjectName("t")
        self.lay.addWidget(t)
        if not items:
            m = QLabel("Усі предмети збірки куплено 👍")
            m.setObjectName("m")
            self.lay.addWidget(m)
        for r in items:
            row = QHBoxLayout()
            row.setSpacing(8)
            ic = QLabel()
            ic.setFixedSize(28, 28)
            ic.setStyleSheet(f"background:{SLOT_BG.get(r.slot, '#221e1a')}; "
                             f"border:1px solid {SLOT_COLORS.get(r.slot, '#3d3730')}; border-radius:5px;")
            self.icons.set_icon(ic, r.image, 24, 3)
            row.addWidget(ic)
            name = QLabel(r.name)
            name.setStyleSheet("font-weight:600;")
            row.addWidget(name, 1)
            cost = QLabel(f"{r.cost:,}".replace(",", " "))
            cost.setStyleSheet("color:#7fd1c4; font-weight:700;")
            row.addWidget(cost)
            self.lay.addLayout(row)
        if items:
            total = sum(r.cost for r in items)
            m = QLabel(f"Разом {total:,} душ · {how}".replace(",", " "))
            m.setObjectName("m")
            self.lay.addWidget(m)
        self.ensurePolished()
        self.lay.activate()
        self.place()

    def place(self):
        """Прив'язка правого нижнього кута вікна до кута екрана."""
        scr = QGuiApplication.primaryScreen().geometry()
        right, bottom = self.margins
        x = scr.right() - self.width() - right
        y = scr.bottom() - self.height() - bottom
        if self.pos().x() != x or self.pos().y() != y:
            self.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.place()

    def showEvent(self, e):
        super().showEvent(e)
        self.place()
