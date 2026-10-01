# -*- coding: utf-8 -*-
"""她的日记本

一个能翻页的小窗口，看角色自己写的日记（journal/YYYY-MM-DD.md）。

刻意做成「纸质」的样子：米黄底、楷体、居中窄栏，跟聊天窗的微信风区分开 ——
这是她自己写给自己看的东西，不该长得像聊天记录。

数据全部来自 life_engine.LifeEngine：list_diaries() 看有哪些天写了、
read_diary(ds) 读某天正文。这个模块不写盘，只读。
"""
import datetime
import html
import re

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QTextEdit
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

DIARY_W, DIARY_H = 460, 610

PAPER = "#f8f3e8"          # 纸
PAPER_EDGE = "#e2d8c3"     # 纸边
PAPER_TOP = "#efe7d6"      # 标题栏
INK = "#3b352c"            # 正文墨色
INK_SOFT = "#9a9080"       # 次要字

QSS_SCROLL = """
QScrollArea{background:transparent; border:0;}
QScrollArea > QWidget > QWidget{background:transparent;}
QScrollBar:vertical{background:transparent; width:8px; margin:2px 2px 2px 0;}
QScrollBar::handle:vertical{background:#cfc4ac; border-radius:4px; min-height:30px;}
QScrollBar::handle:vertical:hover{background:#bcb096;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0; border:0; background:transparent;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}
"""


def diary_font(pixel=17):
    """日记正文字体：优先楷体，没有就退回默认衬线感。"""
    f = QFont("KaiTi")
    if not f.exactMatch():
        f = QFont("楷体")
    if not f.exactMatch():
        f = QFont("Microsoft YaHei UI")
    f.setPixelSize(pixel)
    return f


def pretty_date(ds):
    try:
        d = datetime.date.fromisoformat(ds)
        return f"{d.year} 年 {d.month} 月 {d.day} 日 · {WEEKDAY[d.weekday()]}"
    except Exception:
        return ds


def diary_html(body, ink=INK):
    """把日记正文排成分段富文本：段间留白、首行缩进两字、行距放宽。

    日记正文是一整段连续文字（模型爱一口气写完），直接贴进 QLabel 就是一大坨，读着累。
    这里按空行切段 —— 切不出多段就退化成按单换行切。
    """
    text = (body or "").strip()
    if not text:
        return ""
    parts = [x.strip() for x in re.split(r"\n\s*\n", text) if x.strip()]
    if len(parts) <= 1:
        one = [x.strip() for x in text.splitlines() if x.strip()]
        if len(one) > 1:
            parts = one
    parts = [x.replace("\n", " ").strip() for x in parts if x.strip()]
    if not parts:
        return ""
    out = []
    for i, p in enumerate(parts):
        tail = "" if i == len(parts) - 1 else " margin-bottom:15px;"
        out.append(f'<p style="color:{ink}; margin:0; text-indent:2em;'
                   f' line-height:180%;{tail}">{html.escape(p)}</p>')
    return "".join(out)


class DiaryTitleBar(QWidget):
    """日记本的自绘标题栏（无边框窗口靠它拖动 / 关闭）"""

    def __init__(self, dlg, title="她的日记"):
        super().__init__(dlg)
        self.dlg = dlg
        self._press = None
        self.setFixedHeight(44)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"background:{PAPER_TOP}; border-bottom:1px solid {PAPER_EDGE};"
            " border-top-left-radius:9px; border-top-right-radius:9px;")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 8, 0)
        lay.setSpacing(0)

        self.title = QLabel(title)
        self.title.setStyleSheet(f"color:{INK}; font-size:14px; font-weight:600;"
                                 " background:transparent; border:0;")
        lay.addWidget(self.title)
        lay.addStretch(1)

        b = QPushButton("✕", self)
        b.setFixedSize(34, 28)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setStyleSheet(
            "QPushButton{background:transparent; border:0; color:#7a7263;"
            " font-size:12px; border-radius:4px;}"
            " QPushButton:hover{background:#e81123; color:#ffffff;}")
        b.clicked.connect(dlg.close)
        lay.addWidget(b)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.globalPosition().toPoint() - self.dlg.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._press is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self.dlg.move(e.globalPosition().toPoint() - self._press)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._press = None


class DiaryDialog(QDialog):
    """翻页看她的日记。engine 是 LifeEngine 实例。"""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.days = []
        self.idx = 0

        self.setWindowTitle("她的日记")
        self.setFixedSize(DIARY_W, DIARY_H)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFont(QFont("Microsoft YaHei UI", 10))

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        self.title = DiaryTitleBar(self)
        root.addWidget(self.title)

        # —— 正文区 ——
        card = QWidget()
        card.setObjectName("card")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setStyleSheet(f"#card{{background:{PAPER};}}")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(QSS_SCROLL)
        self.scroll.viewport().setAutoFillBackground(False)

        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(34, 26, 30, 30)
        iv.setSpacing(0)

        self.head = QLabel("")
        self.head.setStyleSheet(f"color:{INK_SOFT}; font-size:12px;"
                                " background:transparent;")
        self.head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        iv.addWidget(self.head)
        iv.addSpacing(16)

        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setFont(diary_font())
        self.body.setStyleSheet(f"color:{INK}; background:transparent;")
        iv.addWidget(self.body)
        iv.addStretch(1)

        self.scroll.setWidget(inner)
        cv.addWidget(self.scroll, 1)
        root.addWidget(card, 1)

        # —— 底部：翻页 ——
        bar = QWidget()
        bar.setObjectName("bar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setStyleSheet(
            f"#bar{{background:{PAPER_TOP}; border-top:1px solid {PAPER_EDGE};"
            " border-bottom-left-radius:9px; border-bottom-right-radius:9px;}")
        b = QHBoxLayout(bar)
        b.setContentsMargins(14, 8, 14, 9)
        b.setSpacing(8)

        self.btn_prev = self._nav_btn("◀  前一天")
        self.btn_prev.clicked.connect(lambda: self.go(-1))
        self.btn_next = self._nav_btn("后一天  ▶")
        self.btn_next.clicked.connect(lambda: self.go(1))

        self.pager = QLabel("")
        self.pager.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pager.setStyleSheet(f"color:{INK_SOFT}; font-size:12px;"
                                 " background:transparent;")

        b.addWidget(self.btn_prev)
        b.addStretch(1)
        b.addWidget(self.pager)
        b.addStretch(1)
        b.addWidget(self.btn_next)
        root.addWidget(bar)

        self.refresh()

    def _nav_btn(self, text):
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setStyleSheet(
            "QPushButton{background:transparent; border:0; color:#6f6757;"
            " font-size:12px; padding:4px 8px; border-radius:4px;}"
            " QPushButton:hover{background:#e6ddc9; color:#3b352c;}"
            " QPushButton:disabled{color:#c3b9a4; background:transparent;"
            " border:0;}")
        return btn

    # ---------- 数据 ----------
    def refresh(self, keep_date=None):
        """重新读盘。keep_date 给了就尽量停在那一篇。"""
        try:
            self.days = self.engine.list_diaries()
        except Exception:
            self.days = []
        if not self.days:
            self.idx = 0
            self._render_empty()
            return
        if keep_date and keep_date in self.days:
            self.idx = self.days.index(keep_date)
        else:
            self.idx = len(self.days) - 1        # 默认看最近一篇
        self._render()

    def go(self, step):
        if not self.days:
            return
        i = self.idx + step
        if 0 <= i < len(self.days):
            self.idx = i
            self._render()

    # ---------- 渲染 ----------
    def _render_empty(self):
        self.head.setText("")
        self.body.setFont(diary_font(14))
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setText(
            f'<p style="color:{INK_SOFT}; margin:0; line-height:180%;">'
            "她还没写过日记。<br><br>"
            "等她过完这一天，晚上会自己写一篇。<br>"
            "（也可以先跟她聊几句，她写的时候会记得。）</p>")
        self.pager.setText("暂无日记")
        self.btn_prev.setEnabled(False)
        self.btn_next.setEnabled(False)

    def _render(self):
        ds = self.days[self.idx]
        try:
            body = self.engine.read_diary(ds)
        except Exception:
            body = ""
        self.head.setText(pretty_date(ds))
        self.body.setFont(diary_font())
        self.body.setTextFormat(Qt.TextFormat.RichText)
        rich = diary_html(body)
        if rich:
            self.body.setText(rich)
        else:
            self.body.setText(f'<p style="color:{INK_SOFT}; margin:0;'
                              ' line-height:180%;">（这一天她没写什么。）</p>')
        self.pager.setText(f"{self.idx + 1} / {len(self.days)}")
        self.btn_prev.setEnabled(self.idx > 0)
        self.btn_next.setEnabled(self.idx < len(self.days) - 1)
        bar = self.scroll.verticalScrollBar()
        bar.setValue(0)

    # ---------- 键盘翻页 ----------
    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Left:
            self.go(-1)
        elif e.key() == Qt.Key.Key_Right:
            self.go(1)
        elif e.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)
