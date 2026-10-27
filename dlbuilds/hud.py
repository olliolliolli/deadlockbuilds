"""Компактна підказка в правому нижньому куті: що купити далі.

Вікно прозоре для миші (кліки проходять у гру) і не забирає фокус.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

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


ROWS = 4
WIDTH, HEIGHT = 270, 200


SOULS_PER_MIN = 1000   # грубо: скільки душ гравець витрачає за хвилину матчу


def next_from_order(order: list[ItemRec], bought: set[int], elapsed_min: float | None,
                    n: int = 4) -> tuple[list[ItemRec], str]:
    """Як next_items, але для збірки з гри (порядок автора, без статистики часу).
    Без позначок пропускає предмети, на які за цей час уже мали б назбирати душ."""
    if bought:
        return [r for r in order if r.item_id not in bought][:n], "без позначених як куплені"
    if elapsed_min is not None and elapsed_min > 1:
        budget, spent, i = SOULS_PER_MIN * elapsed_min * 0.9, 0, 0
        while i < len(order) and spent + order[i].cost <= budget:
            spent += order[i].cost
            i += 1
        return order[i:i + n], f"за часом матчу (~{elapsed_min:.0f} хв)"
    return order[:n], "початок збірки"


class Hud(QWidget):
    """Фіксований розмір і фіксовані рядки: вікно ніколи не змінює розмір,
    тому не «стрибає». Прив'язане правим нижнім кутом до кута екрана."""

    def __init__(self, icons, margins=(24, 230)):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)       # непрозоре — не знижує FPS гри
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.icons = icons
        self.margins = margins
        self._sig = None
        self.setFixedSize(WIDTH, HEIGHT)
        self.setStyleSheet("QWidget { color:#ece6da; font-family:'Segoe UI'; font-size:12px; }"
                           "QLabel#t { color:#d6b26a; font-size:10px; font-weight:700; letter-spacing:1px; }"
                           "QLabel#m { color:#9a9286; font-size:10px; }"
                           "QLabel#n { font-weight:600; }"
                           "QLabel#c { color:#7fd1c4; font-weight:700; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 10)
        lay.setSpacing(4)
        lay.addStretch(1)                        # порожнє місце — зверху, вміст притиснутий донизу
        self.title = QLabel()
        self.title.setObjectName("t")
        lay.addWidget(self.title)
        self.rows = []
        for _ in range(ROWS):
            w = QWidget()
            w.setFixedHeight(30)
            row = QHBoxLayout(w)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            ic = QLabel()
            ic.setFixedSize(28, 28)
            name = QLabel()
            name.setObjectName("n")
            cost = QLabel()
            cost.setObjectName("c")
            row.addWidget(ic)
            row.addWidget(name, 1)
            row.addWidget(cost)
            lay.addWidget(w)
            self.rows.append((w, ic, name, cost))
        self.footer = QLabel()
        self.footer.setObjectName("m")
        lay.addWidget(self.footer)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(18, 16, 14))
        p.setPen(QColor(90, 76, 52))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def update_items(self, hero: str, items: list[ItemRec], how: str):
        sig = (hero, tuple(r.item_id for r in items), how)
        if sig == self._sig:
            self.place()
            return                                # нічого не змінилось — не перемальовуємо
        self._sig = sig
        self.title.setText(f"ДАЛІ КУПИТИ · {hero.upper()}")
        for i, (w, ic, name, cost) in enumerate(self.rows):
            if i < len(items):
                r = items[i]
                ic.setStyleSheet(f"background:{SLOT_BG.get(r.slot, '#221e1a')}; "
                                 f"border:1px solid {SLOT_COLORS.get(r.slot, '#3d3730')}; border-radius:5px;")
                self.icons.set_icon(ic, r.image, 24, 3)
                name.setText(r.name)
                cost.setText(f"{r.cost:,}".replace(",", " "))
                w.show()
            else:
                w.hide()
        if items:
            total = sum(r.cost for r in items)
            self.footer.setText(f"Разом {total:,} душ · {how}".replace(",", " "))
        else:
            self.footer.setText("Усі предмети збірки куплено")
        self.place()

    def place(self):
        """Правий нижній кут вікна = кут екрана мінус відступи."""
        scr = QGuiApplication.primaryScreen().geometry()
        right, bottom = self.margins
        x = scr.x() + scr.width() - WIDTH - right
        y = scr.y() + scr.height() - HEIGHT - bottom
        if self.x() != x or self.y() != y:
            self.move(x, y)

    def showEvent(self, e):
        super().showEvent(e)
        self.place()
