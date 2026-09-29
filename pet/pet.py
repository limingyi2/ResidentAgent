# -*- coding: utf-8 -*-
"""角色 · 桌面立绘（纯客户端）

她是**薄客户端**：只有立绘、气泡、聊天窗，没有任何本地模型、没有本地 HTTP 服务、
也不在本地存记忆 —— 脑子（记忆 / 人设 / 生活 / 日记）只有云上一份，走 SSH 隧道连过去。
这样两台进程不会各养一个"她"互相覆盖记忆。

- 透明窗口、置顶（原生钩子 + 定期保活，见 _TopMostGuard）、可拖拽、双击立绘打开聊天
- 大脑：remote_brain.RemoteBrain -> config.json 的 brain_remote（默认 http://127.0.0.1:18787）
- 感知：watcher.ActivityWatcher，只读前台窗口标题 / 进程名，不截屏、不用视觉模型
- 右键菜单：跟她聊天 / 置顶窗口 / 全屏时自动让开 / 设置 / 她的日记 / 主动说一句 / 清空记录 / 退出
- 打游戏挡画面：右键取消「置顶窗口」，或勾上「全屏 / 打游戏时自动让开」让她自己躲

历史（别再往回加）：
- 本地 4B 离线兜底模型 —— 2026-09-21 移除
- 截屏 + 视觉模型"看你的屏幕" —— 2026-09-28 移除（隐私成本 + 老把屏幕上的字抄下来当话题）
- 本地大脑兜底 + 局域网 HTTP 接口（mobile_server.py / 8787 端口）—— 2026-09-29 移除

运行：F:/zhixia/venv/Scripts/python.exe pet.py
"""
import sys, os, json, time, datetime, traceback, threading
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "brain"))  # 云端大脑（共享核心）也加进来


# ---------- 单实例：同一时间只允许一个桌宠 ----------
# 之前没有这层保护，看门狗拉起 / 开机自启 / 手动双击 run_pet.bat 任何入口都能再起一个，
# 每个都建一套窗口（立绘+聊天窗+气泡），于是桌面叠了十几个。
# 用 Windows 内核命名互斥量（跨进程、进程崩溃自动释放），第二个实例启动即退出。
_INSTANCE_MUTEX = None


def ensure_single_instance(name="LinZhixiaPet"):
    """返回 True 表示这是第一个实例；False 表示已有实例在跑，调用方应退出。
    非 Windows 或拿锁失败都返回 True（不阻塞启动）。"""
    global _INSTANCE_MUTEX
    try:
        import ctypes
        # 必须用 WinDLL(use_last_error=True) + ctypes.get_last_error()。
        # 原来用 ctypes.windll.kernel32 再调 kernel32.GetLastError()：
        # ctypes 自己的调用会把 LastError 冲掉，拿回来的几乎总是 0，
        # 于是“已经有实例在跑”永远判断不出来 —— 单实例保护等于没写，
        # 每双击一次就多一个她（2026-09-28 实测桌面上同时跑着两个）。
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        mutex = kernel32.CreateMutexW(None, False, "Global\\%sSingleInstance" % name)
        err = ctypes.get_last_error()
        if err == 183:                       # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(mutex)
            return False
        _INSTANCE_MUTEX = mutex              # 必须保活到进程结束，否则锁失效
        return True
    except Exception:
        return True

from PyQt6.QtWidgets import (
    QApplication, QLabel, QDialog, QVBoxLayout, QHBoxLayout,
    QScrollArea, QPushButton, QMenu, QWidget, QTextEdit, QMessageBox
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QPoint, QObject, QTimer, QRect, QRectF,
    QAbstractNativeEventFilter,
)
from PyQt6.QtGui import (
    QPixmap, QAction, QActionGroup, QColor, QFont, QFontMetrics,
    QPainter, QPainterPath, QPen
)

try:
    import paths
    CFG_PATH = paths.CONFIG_PATH         # config/config.json
    paths.ensure()                       # 顺手把该有的目录建出来
except Exception:
    CFG_PATH = r"F:\zhixia\brain\config\config.json"
# 立绘实际放在 brain/assets/ 下（pet/assets/ 里从来没有过这些图）。
# 2026-09-28 修：原来只认 pet/assets，三张图全读不到，桌宠窗口只能显示
# 「立绘读不到」几个字 —— 这就是"桌宠打不开"的直接原因。
# 两个位置都认，谁有图用谁的。
_ASSETS_SELF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_ASSETS_BRAIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain", "assets")
ASSETS_DIR = _ASSETS_BRAIN if os.path.isdir(_ASSETS_BRAIN) else _ASSETS_SELF
api_config = {}
if os.path.exists(CFG_PATH):
    try:
        api_config = json.load(open(CFG_PATH, encoding="utf-8"))
    except Exception:
        pass


# ---------- 置顶保活（Windows 原生，见 _TopMostGuard） ----------
# 为什么不能只靠 Qt 的 WindowStaysOnTopHint：
#   1) 系统里**别的置顶窗口**（悬浮窗 / 桌面歌词 / 输入法候选框 / 游戏启动器）被点一下，
#      就会升到置顶带更上面把她盖住。这时她自己的 WS_EX_TOPMOST 其实还在，
#      Qt 对此一无所知、也不会主动抢回来 —— 表现就是"点一下别的软件她就被压住"。
#   2) 个别多屏 / 驱动组合下，flags 不一定会立刻落到 z-order 上。
# 所以两件事一起做（**只在她该置顶时生效**，用户选"沉底"时一律不插手）：
#   · 定期 SetWindowPos(HWND_TOPMOST)：把她拎回置顶带最上面。已经是最上面时这个
#     调用是空操作，Windows 内部直接返回，不重绘、不掉帧（0.8 秒一次）。
#   · 原生事件过滤器拦 WM_WINDOWPOSCHANGING：谁想把她改成"非置顶"，当场改回去。
WM_WINDOWPOSCHANGING = 0x0046
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = -1
_SWP_NOMOVE, _SWP_NOSIZE = 0x0002, 0x0001
_SWP_NOACTIVATE, _SWP_NOOWNERZORDER = 0x0010, 0x0200
_TOPMOST_INTERVAL_MS = 800      # 保活周期；置顶带里抢回 z-order 靠它

try:
    import ctypes
    _user32 = ctypes.windll.user32

    class _MSG(ctypes.Structure):
        """Win32 MSG（只取用得到的字段，宽度按 64 位对齐）。"""
        _fields_ = [("hWnd", ctypes.c_ssize_t),
                    ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_ssize_t),
                    ("lParam", ctypes.c_ssize_t),
                    ("time", ctypes.c_uint),
                    ("ptX", ctypes.c_long),
                    ("ptY", ctypes.c_long)]

    class _WINDOWPOS(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_ssize_t),
                    ("hwndInsertAfter", ctypes.c_ssize_t),
                    ("x", ctypes.c_int), ("y", ctypes.c_int),
                    ("cx", ctypes.c_int), ("cy", ctypes.c_int),
                    ("flags", ctypes.c_uint)]
except Exception:                       # 非 Windows：置顶保活整体跳过
    ctypes = None
    _user32 = None
    _MSG = _WINDOWPOS = None


class _TopMostGuard(QAbstractNativeEventFilter):
    """Windows 原生消息钩子：她该置顶时，没人能把她按下去。"""

    def __init__(self):
        super().__init__()
        self.owner = None               # PetWindow，窗口建好后接上

    def enable(self, owner):
        self.owner = owner

    def nativeEventFilter(self, evtype, message):
        try:
            if _user32 is None or self.owner is None:
                return False, 0
            if bytes(evtype) != b"windows_generic_MSG":
                return False, 0
            targets = self.owner.topmost_hwnds()
            if not targets:
                return False, 0
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
            if msg.message != WM_WINDOWPOSCHANGING:
                return False, 0
            hwnd = int(msg.hWnd)
            if hwnd not in targets:
                return False, 0
            # 只在她"本来就该是置顶"时插手；否则会把用户选的"沉底"顶掉
            if not (_user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST):
                return False, 0
            wp = ctypes.cast(int(msg.lParam),
                             ctypes.POINTER(_WINDOWPOS)).contents
            wp.hwndInsertAfter = HWND_TOPMOST
        except Exception:
            pass
        return False, 0


_TOPMOST_GUARD = None               # 懒建：QApplication 起来之后再挂


# ---------- 感知线程的信号桥 ----------
class ActSignal(QObject):
    """watcher 跑在普通线程里，Qt 的控件只能在主线程动，
    所以用信号把结果传回主线程。"""
    activity = pyqtSignal(dict)
    status = pyqtSignal(dict)
    game = pyqtSignal(dict)


class ProactiveWorker(QThread):
    """让角色针对当前场景主动说一句（走 API，不能卡住界面）"""
    reply = pyqtSignal(str, str)

    def __init__(self, brain, scene):
        super().__init__()
        self.brain, self.scene = brain, scene

    def run(self):
        try:
            ans, mode = self.brain.speak_on_scene(self.scene)
            self.reply.emit(ans, mode)
        except Exception:
            self.reply.emit("", "出错")


# ---------- 推理（后台） ----------
class ChatWorker(QThread):
    reply = pyqtSignal(str, str)

    def __init__(self, brain, q):
        super().__init__()
        self.brain, self.q = brain, q

    def run(self):
        try:
            ans, mode = self.brain.chat(self.q)
            self.reply.emit(ans, mode)
        except Exception as e:
            self.reply.emit("……我卡壳了，你再说一遍？", "出错")


# ---------- 她自己的生活（后台补算） ----------
class LifeWorker(QThread):
    """把「她自己的生活」补算到当前时刻。

    life_engine 用惰性补算：只在她马上要被用到时才补这段空白。
    会调模型、慢（几秒到几十秒），所以一定放后台，别挡界面。
    """
    done = pyqtSignal(dict)

    def __init__(self, life, force=False):
        super().__init__()
        self.life, self.force = life, force

    def run(self):
        try:
            self.done.emit(self.life.catch_up(force=self.force) or {})
        except Exception:
            self.done.emit({"ok": False})


class DiaryWorker(QThread):
    """立刻让她写一篇今天的日记（不等晚上）。测试 / 尝鲜用。"""
    done = pyqtSignal(bool, str)

    def __init__(self, life):
        super().__init__()
        self.life = life

    def run(self):
        try:
            self.life.catch_up(force=True)
            ds = datetime.date.today().isoformat()
            body = self.life.write_diary(ds)
            self.done.emit(bool(body), ds)
        except Exception:
            self.done.emit(False, "")


# ---------- 气泡（独立顶层窗口） ----------
class Bubble(QWidget):
    """独立窗口的气泡。

    原来是用 QLabel 当子控件放在立绘上方（负 y 坐标），但 Qt 会把子控件
    裁剪到父窗口范围内，负坐标那段根本画不出来，所以气泡一直没显示过。
    改成单独的顶层窗口就不受裁剪了。
    """

    def __init__(self, on_top=True):
        super().__init__(None)
        self._on_top = bool(on_top)
        self.setWindowFlags(self._flags(self._on_top))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.lab = QLabel(self)
        self.lab.setStyleSheet(
            "background:rgba(255,255,255,238); border:1px solid #e06c9f;"
            " border-radius:10px; padding:7px 11px; color:#333; font-size:13px;")
        self.lab.setWordWrap(True)
        self.lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.lab)
        self.hide()

    @staticmethod
    def _flags(on_top):
        f = (Qt.WindowType.FramelessWindowHint
             | Qt.WindowType.Tool
             | Qt.WindowType.WindowTransparentForInput)
        if on_top:
            f |= Qt.WindowType.WindowStaysOnTopHint
        return f

    def apply_on_top(self, on_top):
        """跟着立绘一起上/下 —— 否则她躲开了，气泡还悬在人家的游戏画面上。"""
        on_top = bool(on_top)
        if on_top == self._on_top:
            return
        self._on_top = on_top
        was = self.isVisible()
        pos = self.pos()
        self.setWindowFlags(self._flags(on_top))   # 改 flags 会把窗口藏起来
        if was:
            self.move(pos)
            self.show()

    def show_text(self, text, center_x, bottom_y):
        self.lab.setFixedWidth(260)
        self.lab.setText(text)
        self.lab.adjustSize()
        self.setFixedSize(self.lab.size())
        self.move(int(center_x - self.width() / 2), int(bottom_y - self.height()))
        self.show()
        self.raise_()


# ---------- 聊天窗（微信风格） ----------
DIALOG_W, DIALOG_H = 420, 580
CARD_W = DIALOG_W - 2                 # 左右各留 1px 画描边
AREA_W = CARD_W - 12 - 28             # 减去滚动条 + 消息区左右内边距
AVATAR = 34                           # 头像边长
BUBBLE_PAD_X = 13                     # 气泡左右内边距（与下面的样式表保持一致）
BUBBLE_MAX_FRAC = 0.68                # 气泡最大宽度占消息区比例

QSS_BUBBLE_ZHIXIA = ("background:#ffffff; color:#1a1a1a; border:0;"
                     " border-radius:6px; padding:9px 13px;")
QSS_BUBBLE_ME = ("background:#95ec69; color:#1a1a1a; border:0;"
                 " border-radius:6px; padding:9px 13px;")
COLOR_ZHIXIA = "#ffffff"
COLOR_ME = "#95ec69"


def bubble_font():
    """气泡用的字体。

    注意：字号必须用 setFont 设死，不能只写在样式表的 font-size 里 ——
    样式表要等控件 polish 之后才生效，量宽度时拿到的是旧字体，
    算出来的宽度会偏窄，短句就被折成竖条了。
    """
    f = QFont("Microsoft YaHei UI", 10)
    f.setPixelSize(14)
    return f

QSS_SCROLL = """
QScrollArea{background:transparent; border:0;}
QScrollArea > QWidget > QWidget{background:transparent;}
QScrollBar:vertical{background:transparent; width:8px; margin:2px 2px 2px 0;}
QScrollBar::handle:vertical{background:#c8c8c8; border-radius:4px; min-height:30px;}
QScrollBar::handle:vertical:hover{background:#b0b0b0;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0; border:0; background:transparent;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}
"""


class Avatar(QLabel):
    """圆角方形头像（圆角已经烤进 PNG，不用再裁）"""
    def __init__(self, path, fallback="夏"):
        super().__init__()
        self.setFixedSize(AVATAR, AVATAR)
        pm = QPixmap(path) if path else QPixmap()
        if pm.isNull():
            self.setText(fallback)
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setStyleSheet("background:#c8d3de; color:#fff;"
                               " border-radius:6px; font-size:13px;")
        else:
            self.setPixmap(pm.scaled(AVATAR, AVATAR,
                                     Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))


class BubbleRow(QWidget):
    """一行消息：头像 + 气泡（+ 可选小字），带微信那种小尖角。

    who="zhixia" 靠左白气泡，who="me" 靠右绿气泡。
    """
    def __init__(self, text, who, sub=""):
        super().__init__()
        self._me = (who == "me")
        color = COLOR_ME if self._me else COLOR_ZHIXIA

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(9)

        av = Avatar(os.path.join(ASSETS_DIR, "user_avatar.png" if self._me
                                 else "zhixia_avatar.png"),
                    "你" if self._me else "夏")

        self.bubble = QLabel(text)
        self.bubble.setWordWrap(True)
        self.bubble.setFont(bubble_font())
        self.bubble.setStyleSheet(QSS_BUBBLE_ME if self._me else QSS_BUBBLE_ZHIXIA)
        self.bubble.setFixedWidth(self._measure(self.bubble, text))

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        col.addWidget(self.bubble)
        if sub:
            cap = QLabel(sub)
            cap.setStyleSheet("color:#b8b8b8; font-size:10px; background:transparent;")
            cap.setAlignment(Qt.AlignmentFlag.AlignRight if self._me
                             else Qt.AlignmentFlag.AlignLeft)
            col.addWidget(cap)
        holder = QWidget()
        holder.setLayout(col)
        holder.setFixedWidth(self.bubble.width())
        holder.setStyleSheet("background:transparent;")

        top = Qt.AlignmentFlag.AlignTop
        if self._me:
            h.addStretch(1); h.addWidget(holder, 0, top); h.addWidget(av, 0, top)
        else:
            h.addWidget(av, 0, top); h.addWidget(holder, 0, top); h.addStretch(1)

    @staticmethod
    def _measure(label, text):
        """算气泡宽度：一句话放得下就贴合文字，放不下就固定到上限换行。

        不用 QLabel 自己的 sizeHint —— 它对自动换行的标签算出的宽度会偏窄，
        短句会被切成竖条。
        """
        fm = QFontMetrics(label.font())
        longest = max((fm.horizontalAdvance(ln) for ln in text.split("\n")), default=0)
        limit_w = int(AREA_W * BUBBLE_MAX_FRAC)
        content_limit = limit_w - 2 * BUBBLE_PAD_X
        if longest <= content_limit:
            return longest + 2 * BUBBLE_PAD_X + 4      # +4 是防抖余量
        return limit_w

    def paintEvent(self, e):
        """在气泡靠头像一侧画个小三角，接上微信那种气泡尾巴。"""
        tl = self.bubble.mapTo(self, QPoint(0, 0))
        g = QRect(tl, self.bubble.size())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(COLOR_ME if self._me else COLOR_ZHIXIA))
        path = QPainterPath()
        y = g.top() + 11
        if self._me:
            x = g.right()
            path.moveTo(x, y); path.lineTo(x + 6, y + 4); path.lineTo(x, y + 9)
        else:
            x = g.left()
            path.moveTo(x, y); path.lineTo(x - 6, y + 4); path.lineTo(x, y + 9)
        path.closeSubpath()
        p.drawPath(path)


class TitleBar(QWidget):
    """自绘标题栏（聊天窗是无边框窗口，靠它拖动 + 最小化/关闭）"""
    def __init__(self, dlg, name):
        super().__init__(dlg)
        self.dlg = dlg
        self._press = None
        self.setFixedHeight(46)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "background:#f2f2f2; border-bottom:1px solid #e4e4e4;"
            " border-top-left-radius:9px; border-top-right-radius:9px;")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(0)

        col = QVBoxLayout()
        col.setContentsMargins(0, 5, 0, 5)
        col.setSpacing(0)
        self.name = QLabel(name)
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setStyleSheet("color:#1a1a1a; font-size:14px; font-weight:600;"
                                " background:transparent; border:0;")
        self.status = QLabel("在线")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setStyleSheet("color:#9a9a9a; font-size:11px;"
                                  " background:transparent; border:0;")
        col.addWidget(self.name)
        col.addWidget(self.status)

        lay.addStretch(1)
        lay.addLayout(col)
        lay.addStretch(1)
        for text, slot, hover_bg, hover_fg in (("—", dlg.showMinimized, "#e0e0e0", "#1a1a1a"),
                                               ("✕", dlg.close, "#e81123", "#ffffff")):
            b = QPushButton(text, self)
            b.setFixedSize(34, 30)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setStyleSheet(
                "QPushButton{background:transparent; border:0; color:#5a5a5a;"
                " font-size:12px; border-radius:4px;}"
                f" QPushButton:hover{{background:{hover_bg}; color:{hover_fg};}}")
            b.clicked.connect(slot)
            lay.addWidget(b)

    def set_status(self, text):
        self.status.setText(text)

    # —— 拖动窗口 ——
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


class MsgEdit(QTextEdit):
    """输入框：回车发送，Shift+回车换行"""
    send_req = pyqtSignal()

    def keyPressEvent(self, e):
        enter = e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.send_req.emit()
            e.accept()
            return
        super().keyPressEvent(e)


class ChatDialog(QDialog):
    def __init__(self, pet):
        super().__init__()
        self.pet = pet
        self._last_ts = 0.0
        self.setWindowTitle("角色")
        self.setFixedSize(DIALOG_W, DIALOG_H)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFont(QFont("Microsoft YaHei UI", 10))

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)     # 留 1px 给描边
        root.setSpacing(0)

        self.title = TitleBar(self, "角色")
        root.addWidget(self.title)

        # —— 消息区 ——
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(QSS_SCROLL)
        self.scroll.viewport().setAutoFillBackground(False)
        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;")
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(14, 14, 14, 14)
        self.body_lay.setSpacing(14)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

        # —— 输入区 ——
        box = QWidget()
        box.setObjectName("inpbox")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box.setStyleSheet(
            "#inpbox{background:#fafafa; border-top:1px solid #e4e4e4;"
            " border-bottom-left-radius:9px; border-bottom-right-radius:9px;}")
        v = QVBoxLayout(box)
        v.setContentsMargins(12, 9, 12, 10)
        v.setSpacing(6)

        self.inp = MsgEdit()
        self.inp.setPlaceholderText("发消息…")
        self.inp.setFixedHeight(60)
        self.inp.setStyleSheet("QTextEdit{background:transparent; border:0;"
                               " color:#1a1a1a; font-size:14px;}")
        self.inp.send_req.connect(self.send)
        # 内容高度变了就贴底（比单次延时可靠：布局稳定后才会触发）
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda *_: self._scroll_bottom())

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        hint = QLabel("回车发送 · Shift+回车换行")
        hint.setStyleSheet("color:#b0b0b0; font-size:11px; background:transparent;")
        send_btn = QPushButton("发送")
        send_btn.setFixedSize(76, 30)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        send_btn.setStyleSheet(
            "QPushButton{background:#07c160; color:#fff; border:0;"
            " border-radius:4px; font-size:13px;}"
            " QPushButton:hover{background:#06ad56;}"
            " QPushButton:pressed{background:#059a4c;}")
        send_btn.clicked.connect(self.send)
        bottom.addWidget(hint)
        bottom.addStretch(1)
        bottom.addWidget(send_btn)

        v.addWidget(self.inp)
        v.addLayout(bottom)
        root.addWidget(box)

        # 有聊天存档就把记录填回来（重启桌宠后还能接着看），没有才让她先开口
        try:
            past = self.pet.brain.read_history(40)
        except Exception:
            past = []
        self.restore(past)

    # —— 绘制圆角卡片 + 1px 描边 ——
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor("#f5f5f5"))
        p.setPen(QPen(QColor("#d5d5d5"), 1))
        p.drawRoundedRect(r, 10, 10)

    # —— 消息操作 ——
    def _add_sep(self, text):
        lab = QLabel(text)
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lab.setStyleSheet("color:#b4b4b4; font-size:11px; background:transparent;")
        self.body_lay.addWidget(lab)

    def _add_bubble(self, text, who, sub=""):
        import time
        now = time.time()
        if now - self._last_ts > 180:            # 隔了 3 分钟就补一条时间分隔
            self._add_sep("今天 " + time.strftime("%H:%M"))
        self._last_ts = now
        self.body_lay.addWidget(BubbleRow(text, who, sub))
        QTimer.singleShot(0, self._scroll_bottom)

    def _scroll_bottom(self):
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _greet(self):
        self._add_bubble("在呢～想聊点什么？", "zhixia")

    # —— 重启后把聊天记录填回来 ——
    def restore(self, records):
        """把存档里的消息铺成气泡。records 为空就当她先开口。"""
        if not records:
            self._greet()
            return
        prev = None
        for m in records:
            when = self._parse_ts(m.get("t"))
            # 隔了 3 分钟以上就插一条时间分隔（跟实时聊天一个规矩）
            if when and (prev is None or (when - prev).total_seconds() > 180):
                self._add_sep(self._sep_label(when))
            if when:
                prev = when
            who = "me" if m.get("role") == "user" else "zhixia"
            self.body_lay.addWidget(BubbleRow(m.get("text", ""), who, ""))
        self._last_ts = time.time()
        QTimer.singleShot(0, self._scroll_bottom)

    @staticmethod
    def _parse_ts(s):
        try:
            return datetime.datetime.strptime(s or "", "%Y-%m-%d %H:%M")
        except Exception:
            return None

    @staticmethod
    def _sep_label(dt):
        today = datetime.date.today()
        if dt.date() == today:
            return "今天 " + dt.strftime("%H:%M")
        if dt.date() == today - datetime.timedelta(days=1):
            return "昨天 " + dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")

    def clear_all(self):
        """把气泡全清掉，回到开场白（存档由 pet 那边删）"""
        while self.body_lay.count():
            item = self.body_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._last_ts = time.time()     # 别在开场白上面多冒一条时间分隔
        self._greet()

    def show_typing(self):
        self.title.set_status("正在输入…")

    def send(self):
        q = self.inp.toPlainText().strip()
        if not q:
            return
        self.inp.clear()
        self._add_bubble(q, "me")
        if not self.pet.ready:
            self._add_bubble("我还在准备，稍等一下下～", "zhixia")
            return
        self.show_typing()
        self.pet.chat(q, self)

    def show_reply(self, text, mode=""):
        self.title.set_status("在线")
        if not text:
            text = "……我走神了，你再说一遍？"
        sub = f"· {mode}" if mode else ""
        self._add_bubble(text, "zhixia", sub)
        self.inp.setFocus()


# ---------- 立绘窗口 ----------

WIN_LEVELS = ("top", "bottom")
WIN_LEVEL_LABEL = {"top": "置顶（在所有窗口上面）",
                   "bottom": "沉底（在所有窗口下面）"}


def _read_win_level(wcfg):
    """读出窗口层级。老配置是 always_on_top 布尔值 / 三档，迁移成 置顶/沉底 两档。"""
    lv = str(wcfg.get("level") or "").strip().lower()
    if lv in WIN_LEVELS:
        return lv
    if wcfg.get("always_on_bottom") or lv == "bottom":
        return "bottom"
    # 兼容旧写法：always_on_top = true/false，以及老的 "normal"
    return "top" if wcfg.get("always_on_top", True) else "bottom"


def _pc_uptime_sec():
    """电脑已经开了多久（秒）。拿不到返回 None。

    为什么要它：以前不管什么原因启动，她都开口说「刚打开电脑」——
    可你电脑开了一整天、只是重启了一下桌宠，那句话听着就很假。
    GetTickCount64 拿的是内核记的开机时长，不用装 psutil。
    """
    try:
        import ctypes
        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:
        return None


def _human_min(m):
    """把分钟数说成人话（"刚"、"23 分钟"、"1.7 小时"）。"""
    if m < 2:
        return "刚刚"
    if m < 60:
        return "%.0f 分钟" % m
    if m < 60 * 24:
        return "%.1f 小时" % (m / 60.0)
    return "%.1f 天" % (m / 1440.0)


def _startup_facts():
    """把「电脑开了多久 / 上次说话隔了多久」写成一句人话。

    **只给事实，不给结论** —— 要不要开口、说什么，交给她自己判断。
    写死"刚打开电脑"就是这么坏掉的：你电脑开了一整天、只是重启下桌宠，
    她照样说刚打开电脑，一听就假。
    """
    bits = []
    up = _pc_uptime_sec()
    if up is not None:
        bits.append("他的电脑已经开了 " + _human_min(up / 60.0))
    gap = _last_chat_gap_min()
    if gap is None:
        bits.append("你们还没聊过")
    else:
        bits.append("你们上次说话是 " + _human_min(gap) + "前")
    return "；".join(bits)


def _last_chat_gap_min():
    """上次跟她说话到现在过了多久（分钟）。没有记录返回 None。

    只读文件末尾一小段，别为了一个问候把整个存档读一遍。
    """
    try:
        from brain import CHAT_LOG
    except Exception:
        return None
    try:
        if not os.path.exists(CHAT_LOG):
            return None
        size = os.path.getsize(CHAT_LOG)
        with open(CHAT_LOG, "rb") as f:
            if size > 4096:
                f.seek(size - 4096)
                f.readline()                 # 丢掉可能被截断的半行
            tail = f.read().decode("utf-8", "ignore")
        last = ""
        for ln in tail.splitlines():
            if ln.strip():
                last = ln.strip()
        if not last:
            return None
        t = json.loads(last).get("t") or ""
        dt = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M")
        return (datetime.datetime.now() - dt).total_seconds() / 60.0
    except Exception:
        return None


class PetWindow(QLabel):
    def __init__(self):
        super().__init__()
        # ---- 窗口层级：两档（置顶开关），用户手动选，她自己不判断 ----
        #   "top"    压在所有窗口上面（置顶窗口=开）
        #   "bottom" 沉在所有窗口下面（置顶窗口=关，别的软件都能盖住她）
        # 另外 sink_on_fullscreen 是"她自己判断全屏/游戏并让开"的开关，
        # 默认关 —— 用户明确说不要她自己判断，想要手动控。
        wcfg = api_config.get("window")
        wcfg = wcfg if isinstance(wcfg, dict) else {}
        self.win_level = _read_win_level(wcfg)
        self.sink_on_fullscreen = bool(wcfg.get("sink_on_fullscreen", False))
        self._level = self.win_level
        self._fs_app = False                # 前台现在是全屏/游戏吗
        self.setWindowFlags(self._flags_for(self._level))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setText("加载中...")
        self.setStyleSheet("color:#e06c9f; font-size:16px; padding:20px;")
        self.setMinimumSize(200, 120)
        screen = QApplication.primaryScreen() or QApplication.screens()[0]
        scr = screen.availableGeometry()   # 排除任务栏，避免被挡住
        self.move(scr.width() - 260, scr.height() - 300)
        self.drag = QPoint()
        self._dragging = False          # 拖动时暂停呼吸动画，否则会被拽回原位
        self.talking = False
        self.base_y = self.y()
        self.t = 0
        self.pix = {}
        self._talk_on = False
        self.last_activity = ""      # 最近一次感知到的活动（给"主动搭话"当话题）

        # ---- 大脑只在云上：桌宠是纯客户端 ----
        # 本地兜底大脑（brain_remote 为空时在自己电脑上养一个"她"）已于 2026-09-29 摘掉：
        # 两台进程各养一个她只会互相覆盖记忆，而且本地兜底从来没真正救过场
        # （隧道断的时候，本地那份记忆跟云上已经分叉了，聊起来更像"另一个人"）。
        self._remote_base = str(api_config.get("brain_remote") or "").strip()
        if not self._remote_base:
            raise RuntimeError("config.json 里没配 brain_remote")
        from remote_brain import RemoteBrain, RemoteLife
        # 访问码：新配置放 brain_token；老配置在 mobile.token 里，继续认（迁移期兜底）
        _tok = str(api_config.get("brain_token")
                   or (api_config.get("mobile") or {}).get("token") or "")
        self.mem = None
        self.brain = RemoteBrain({"base": self._remote_base, "token": _tok})
        # 生活/日记是"只读远程视图"：日记窗口能看云上的日记，
        # 本地不写任何东西（catch_up/write_diary 都是空操作）
        self.life = RemoteLife(self._remote_base, _tok)
        self.ready = True
        print(f"[角色] 远程大脑模式：{self._remote_base}"
              "（记忆在云上，本地只显示）", flush=True)

        # 重启后把聊天记录灌回上下文 —— 她还记得刚才聊到哪儿，不会一上来就"初次见面"
        try:
            n = self.brain.seed_history()
            if n:
                print(f"[角色] 接上了上次的聊天记录（{n} 条）", flush=True)
        except Exception as e:
            print(f"[角色] 聊天记录没读回来（不影响聊天）：{e}", flush=True)

        # 立绘先显示，不等任何模型 —— 说话一律走 API
        self._load_pixmaps()
        print(f"[角色] 立绘已显示，大脑在 {self._remote_base}"
              "（云端连不上她就说不了话，检查 SSH 隧道）", flush=True)

        # ---- 活动感知（只看窗口标题/进程名，不截屏）----
        self._setup_watcher()

        # ---- 把她的生活补到当前（后台，不挡启动）----
        QTimer.singleShot(1500, lambda: self._catch_up_life())

        # ---- 开机问候：启动几秒后她自己先开个口（可在设置里关）----
        QTimer.singleShot(5000, self._startup_greet)

        # ---- 置顶保活：等窗口真正显示出来（winId 才有效）再挂 ----
        QTimer.singleShot(300, self._start_topmost_guard)

    # ---------- 她自己的生活 ----------
    def _catch_up_life(self, force=False, then=None):
        """后台把她的生活补到当前时刻。节流内的重复调用只是空转。"""
        if getattr(self, "life", None) is None:
            if then:
                try:
                    then()
                except Exception:
                    pass
            return
        w = LifeWorker(self.life, force=force)

        def on_done(res):
            if res.get("events") or res.get("diary"):
                print(f"[生活] 补算 {res.get('events', 0)} 条经历、"
                      f"{res.get('diary', 0)} 篇日记", flush=True)
            if then:
                try:
                    then()
                except Exception:
                    pass

        w.done.connect(on_done)
        w.start()
        self._life_workers = getattr(self, "_life_workers", []) + [w]

    def open_diary(self):
        """打开日记本。先把生活补到当前，免得看到的日记是旧的。"""
        if getattr(self, "life", None) is None:
            self.show_bubble("我还不会写日记…")
            return
        from diary_dialog import DiaryDialog
        if getattr(self, "diary", None) is None:
            self.diary = DiaryDialog(self.life)
        self.diary.refresh()
        self.diary.show()
        self.diary.raise_()
        self.diary.activateWindow()
        cur = self.diary.days[self.diary.idx] if self.diary.days else None
        self._catch_up_life(then=lambda: self._refresh_diary(cur))

    def _refresh_diary(self, keep_date=None):
        try:
            if getattr(self, "diary", None) is not None:
                self.diary.refresh(keep_date=keep_date)
        except Exception:
            pass

    def write_diary_now(self):
        """不等晚上，立刻补算 + 写一篇今天的日记（测试用）"""
        if getattr(self, "life", None) is None:
            return
        self.show_bubble("我记一下今天…")
        w = DiaryWorker(self.life)

        def on_done(ok, ds):
            if ok:
                self.show_bubble("写好啦，去「她的日记」看看～")
                self._refresh_diary(ds)
                QTimer.singleShot(400, self.open_diary)
            else:
                self.show_bubble("今天还没什么好写的…")

        w.done.connect(on_done)
        w.start()
        self._diary_workers = getattr(self, "_diary_workers", []) + [w]

    def _setup_watcher(self):
        """启动活动感知线程（只看窗口标题/进程名，不截屏、不用视觉模型）。"""
        self._act_sig = ActSignal()          # 线程 -> 界面的桥
        self._act_sig.activity.connect(self._on_activity)
        self._act_sig.status.connect(self._on_status)
        self._act_sig.game.connect(self._on_game_state)

        wcfg = dict(api_config.get("watcher", {}))
        wcfg.setdefault("enabled", True)
        if isinstance(api_config.get("proactive"), dict):
            wcfg["proactive"] = dict(api_config["proactive"])

        self.watcher = None
        try:
            from watcher import ActivityWatcher
            self.watcher = ActivityWatcher(
                api_config, wcfg,
                on_activity=lambda d: self._act_sig.activity.emit(d),
                on_status=lambda d: self._act_sig.status.emit(d),
                on_game=lambda d: self._act_sig.game.emit(d),
            )
            self.watcher.start()
            print("[角色] 活动感知已启动：轻量模式（只看窗口标题 / 进程名，不截屏）",
                  flush=True)
        except Exception as e:
            print(f"[角色] 活动感知没起来（不影响聊天）：{e}", flush=True)

        # 表情包素材库：后台线程慢慢给没描述的图补标签（手动丢进
        # data/stickers/ 的图会在这里被她"看"一遍，之后她才知道每张是什么）
        threading.Thread(target=self._sticker_backfill_loop,
                         daemon=True).start()

    def _sticker_backfill_loop(self):
        """每隔几分钟扫一次素材库，给内容未知的表情包补视觉描述。"""
        import time as _t
        _t.sleep(90)                      # 等主程序先稳下来
        while True:
            try:
                import stickers, vision
                cfg = api_config or {}
                todo = [n for n, v in stickers._ensure_loaded().items()
                        if not (v.get("desc") or "").strip()
                        and os.path.exists(stickers.path_of(n))]
                for name in todo[:5]:     # 每轮最多补 5 张，控制成本
                    desc = vision.describe(stickers.path_of(name), cfg)
                    if desc:
                        with stickers._lock:
                            stickers._ensure_loaded()[name]["desc"] = desc[:80]
                            stickers._save_index()
                        print(f"[素材库] 已认识表情包 {name}：{desc[:40]}",
                              flush=True)
                    _t.sleep(20)
            except Exception as e:
                print(f"[素材库] 补标签出错（下轮继续）：{e}", flush=True)
            _t.sleep(300)


    def _on_status(self, d):
        """只是知道他在干嘛，先不说话 —— 记下来备着"""
        self.last_activity = d.get("what", "")
        print(f"[感知] {d.get('what')}（来源:{d.get('source')}）", flush=True)

    def _on_activity(self, d):
        """感知到"值得说"的内容 -> 让她主动开口"""
        if not self.ready:
            return
        # 她要开口了，顺手把她的日子补到当前（后台 + 节流，不会每次真跑）
        self._catch_up_life()
        detail = f"{d.get('what','')}；{d.get('detail','')}"
        print(f"[感知] 触发主动搭话：{detail}", flush=True)
        w = ProactiveWorker(self.brain, d)

        def on_reply(text, mode):
            if not text or text == "无":
                return
            self.show_bubble(text)
            self.start_talking()
            QTimer.singleShot(2600, self.stop_talking)
            # 聊天窗开着的话也同步显示
            dlg = getattr(self, "dlg", None)
            if dlg and dlg.isVisible():
                dlg.show_reply(text, mode)
        w.reply.connect(on_reply)
        w.start()
        self.workers = getattr(self, "workers", []) + [w]

    def _load_pixmaps(self):
        """把三张立绘读进来并立刻显示，不依赖模型加载"""
        H = 230
        for k, path in [("normal", os.path.join(ASSETS_DIR, "zhixia.png")),
                        ("blink", os.path.join(ASSETS_DIR, "zhixia_blink.png")),
                        ("talk", os.path.join(ASSETS_DIR, "zhixia_talk.png"))]:
            pm = QPixmap(path)
            if pm.isNull():
                print(f"[角色] 立绘读不到：{path}", flush=True)
                continue
            self.pix[k] = pm.scaledToHeight(H, Qt.TransformationMode.SmoothTransformation)
        if not self.pix:
            self.setText("立绘读不到\n见终端")
            return
        self.setPixmap(self.pix["normal"])
        self.resize(self.pix["normal"].size())
        self.setMinimumSize(0, 0)
        self.setStyleSheet("")
        self.base_y = self.y()
        self._start_anim()

    def _start_anim(self):
        import math, random
        from PyQt6.QtCore import QTimer
        self._math = math
        self._random = random
        self.breath_timer = QTimer(self)
        self.breath_timer.timeout.connect(self._breath)
        self.breath_timer.start(50)
        self._schedule_blink()

    def _breath(self):
        if self._dragging:
            return                      # 拖动中不呼吸，免得跟她抢位置
        import math
        self.t += 0.08
        y_off = int(math.sin(self.t) * 3)
        self.move(self.x(), self.base_y + y_off)

    def _schedule_blink(self):
        import random
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(int(2500 + random.random() * 3500), self._do_blink)

    def _do_blink(self):
        from PyQt6.QtCore import QTimer
        if self.talking:
            self._schedule_blink()
            return
        self.setPixmap(self.pix["blink"])
        QTimer.singleShot(160, self._restore_normal)

    def _restore_normal(self):
        if not self.talking:
            self.setPixmap(self.pix["normal"])
        self._schedule_blink()

    def start_talking(self):
        if not self.pix:
            return                      # 立绘还没加载好，跳过口型动画
        self.talking = True
        from PyQt6.QtCore import QTimer
        self._talk_on = True
        def flap():
            if not self._talk_on:
                return
            self.setPixmap(self.pix["talk"])
            QTimer.singleShot(130, lambda: self.setPixmap(self.pix["normal"]) if self._talk_on else None)
            QTimer.singleShot(260, flap)
        flap()

    def stop_talking(self):
        self._talk_on = False
        self.talking = False
        if self.pix:
            self.setPixmap(self.pix["normal"])

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragging = True
            e.accept()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton and not self.drag.isNull():
            self.move(e.globalPosition().toPoint() - self.drag)
            self.base_y = self.y()      # 关键：把呼吸基准点跟到新位置，否则一放手就弹回去
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.base_y = self.y()      # 记住落点，呼吸从这儿重新开始
            e.accept()

    def open_chat(self):
        """打开聊天窗（右键菜单和双击立绘都走这里）"""
        if not hasattr(self, "dlg"):
            self.dlg = ChatDialog(self)      # 只建一次，聊天记录留着
        self.dlg.showNormal()               # 关掉 / 最小化之后都能拉回来
        self.dlg.raise_()                   # 置顶并抢焦点，免得开在别的窗口后面找不到
        self.dlg.activateWindow()
        self.dlg.inp.setFocus()
        # 她马上要跟人说话了，先把她的日子补到当前（后台，不卡界面）
        self._catch_up_life()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.open_chat()

    def contextMenuEvent(self, e):
        self._build_menu().exec(e.globalPos())

    def _build_menu(self):
        """右键菜单。抽成独立方法：contextMenuEvent 里的 exec() 会阻塞，没法测。"""
        m = QMenu(self)

        a_chat = QAction("跟她聊天", self)
        a_chat.setToolTip("打开聊天窗口（也可以直接双击立绘）")
        a_chat.triggered.connect(self.open_chat)
        m.addAction(a_chat)

        m.addSeparator()

        # 置顶窗口：唯一一个层级开关。开=在所有窗口上面，关=在所有窗口下面
        a_top = QAction("置顶窗口", self)
        a_top.setCheckable(True)
        a_top.setChecked(self.win_level == "top")
        a_top.setToolTip("勾上：她压在所有窗口上面（包括全屏游戏）；"
                         "取消：她沉在所有窗口下面，任何软件都能盖住她")
        a_top.triggered.connect(
            lambda _c: self.set_win_level("top" if a_top.isChecked() else "bottom"))
        m.addAction(a_top)

        a_sink = QAction("全屏 / 打游戏时自动让开", self)
        a_sink.setCheckable(True)
        a_sink.setChecked(self.sink_on_fullscreen)
        a_sink.setToolTip("开着的话她会自己判断你是不是在打游戏/看全屏，然后临时躲开"
                          "（默认关 —— 层级由你手动定）")
        a_sink.toggled.connect(self.set_sink_on_fullscreen)
        m.addAction(a_sink)

        m.addSeparator()

        a_set = QAction("设置…", self)
        a_set.setToolTip("改人设、主动搭话频率、免打扰时段")
        a_set.triggered.connect(self.open_settings)
        m.addAction(a_set)

        a_diary = QAction("她的日记", self)
        a_diary.setToolTip("看她自己写的日记（她在晚上写；也能翻看以前那些天）")
        a_diary.triggered.connect(self.open_diary)
        m.addAction(a_diary)

        a_wd = QAction("让她现在写篇日记（测试）", self)
        a_wd.setToolTip("不等晚上，立刻补算她的生活并写一篇今天的日记")
        a_wd.triggered.connect(self.write_diary_now)
        m.addAction(a_wd)
        m.addSeparator()

        a_pro = QAction("让她主动说一句", self)
        a_pro.setToolTip("用最近感知到的活动当话题，让她自己开个口（测试主动搭话）")
        a_pro.triggered.connect(self.proactive_now)
        m.addAction(a_pro)

        a_clr = QAction("清空聊天记录", self)
        a_clr.setToolTip("删掉聊天存档，她就当今天第一次跟你说话")
        a_clr.triggered.connect(self.clear_chat_history)
        m.addAction(a_clr)

        a_quit = QAction("退出", self)
        a_quit.triggered.connect(self.quit_clean)
        m.addAction(a_quit)
        return m

    def proactive_now(self):
        """手动让她主动说一句（测试用，也方便想听她说点啥）。

        用最近一次感知到的活动当话题；如果还没感知到，就随便开个头。
        """
        what = getattr(self, "last_activity", "") or "在发呆"
        self._on_activity({"what": what, "detail": "", "mood": "",
                          "hook": "", "source": "manual"})

    def clear_chat_history(self):
        """清空聊天记录：存档 + 气泡 + 她的上下文一起清（人设和记忆库不动）"""
        r = QMessageBox.question(
            self, "清空聊天记录",
            "确定要清掉聊天记录吗？\n她会忘记你们刚才聊过什么（人设和记忆库都不动）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        from brain import Brain
        Brain.clear_history()
        try:
            self.brain.reset()
        except Exception as e:
            print("[角色] 清上下文失败：", e, flush=True)
        dlg = getattr(self, "dlg", None)
        if dlg is not None:
            dlg.clear_all()
        self.show_bubble("好，刚才聊的都忘掉啦～")
        print("[角色] 聊天记录已清空", flush=True)

    # ---------- 窗口层级：置顶 / 全屏时让位 ----------
    @staticmethod
    def _flags_for(level):
        """立绘的窗口样式：置顶多一个 StaysOnTop，沉底多一个 StaysOnBottom。"""
        f = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if level == "top":
            f |= Qt.WindowType.WindowStaysOnTopHint
        elif level == "bottom":
            f |= Qt.WindowType.WindowStaysOnBottomHint
        return f

    def _apply_level(self, level):
        """真正切换层级。

        坑：setWindowFlags 会让系统把窗口隐藏 / 重建，位置和可见性都得自己兜回来。
        所以先存坐标、切完再 move 回来 + show；base_y 也要更新 ——
        不然呼吸动画会拿旧坐标把她拽回去。
        """
        if level not in WIN_LEVELS:
            level = "bottom"
        if level == self._level:
            # 层级没变也要补一刀：用户可能刚在设置里重存过，或者刚才被别的
            # 置顶窗口（悬浮窗 / 输入法）挤下去了 —— 这里顺手把她拎回来
            self.enforce_topmost()
            return
        self._level = level
        pos = self.pos()
        self.setWindowFlags(self._flags_for(level))
        self.move(pos)
        self.show()
        self.base_y = self.y()
        self._sync_win32_zorder(level)
        self.enforce_topmost()
        b = getattr(self, "bubble", None)
        if b is not None:
            b.apply_on_top(level == "top")

    def _sync_win32_zorder(self, level):
        """再补一刀 SetWindowPos。

        Qt 的 flags 已经会加上/清掉 WS_EX_TOPMOST，这里只是兜底：
        个别多屏 / 驱动环境下 flags 不一定立刻反映到 z-order。
        沉底用 HWND_BOTTOM —— 光靠 Qt 的 StaysOnBottomHint 有时压不住。
        """
        if _user32 is None:
            return
        _HWND_BOTTOM, _HWND_NOTOPMOST = 1, -2
        try:
            if level == "top":
                hwnd_after = HWND_TOPMOST
            elif level == "bottom":
                hwnd_after = _HWND_BOTTOM
            else:
                hwnd_after = _HWND_NOTOPMOST
            _user32.SetWindowPos(
                int(self.winId()), hwnd_after,
                0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
        except Exception:
            pass

    # ---------- 置顶保活（原生钩子 + 定期抢回 z-order） ----------
    def topmost_hwnds(self):
        """当前需要保持"置顶带最上面"的窗口句柄（立绘 + 气泡）。

        只在她该置顶时返回句柄；用户选了"沉底"就返回空 ——
        这样保活逻辑绝不会把用户自己选的层级顶掉。
        """
        if _user32 is None or self._level != "top":
            return ()
        out = []
        for w in (self, getattr(self, "bubble", None)):
            if w is None:
                continue
            try:
                if not w.isVisible():
                    continue
                h = int(w.winId())
            except Exception:
                continue
            if h:
                out.append(h)
        return tuple(out)

    def enforce_topmost(self):
        """把她（和气泡）拎回置顶带最上面。

        已经是置顶带最上面时 SetWindowPos 是空操作（Windows 内部直接返回，
        不重绘、不掉帧），所以可以放心地每隔几百毫秒调一次。
        """
        for h in self.topmost_hwnds():
            try:
                _user32.SetWindowPos(
                    h, HWND_TOPMOST, 0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
                    | _SWP_NOOWNERZORDER)
            except Exception:
                pass

    def _start_topmost_guard(self):
        """挂上置顶保活。必须等窗口 show 过再调 —— 那时 winId 才有效。"""
        global _TOPMOST_GUARD
        if _user32 is None:
            return
        try:
            if _TOPMOST_GUARD is None:
                _TOPMOST_GUARD = _TopMostGuard()
                QApplication.instance().installNativeEventFilter(_TOPMOST_GUARD)
            _TOPMOST_GUARD.enable(self)
            print("[角色] 置顶保活已开：原生钩子 + 每 %d 毫秒抢回一次"
                  % _TOPMOST_INTERVAL_MS, flush=True)
        except Exception as e:
            print(f"[角色] 置顶原生钩子没挂上（还有定期保活兜底）：{e}", flush=True)
        try:
            self._topmost_timer = QTimer(self)
            self._topmost_timer.setInterval(_TOPMOST_INTERVAL_MS)
            self._topmost_timer.timeout.connect(self.enforce_topmost)
            self._topmost_timer.start()
        except Exception as e:
            print(f"[角色] 置顶定时器没起来：{e}", flush=True)
        self.enforce_topmost()

    def set_win_level(self, level, persist=True):
        """用户自己的选择（右键菜单 / 设置窗改的就是这个）。"""
        if level not in WIN_LEVELS:
            return
        self.win_level = level
        self._apply_level(level)
        if persist:
            self.save_window_cfg()
        print(f"[角色] 窗口层级：{WIN_LEVEL_LABEL[level]}", flush=True)

    def set_sink_on_fullscreen(self, v, persist=True):
        """全屏 / 打游戏时要不要自动让开（默认关 —— 用户要手动控，不要她自己判断）。"""
        self.sink_on_fullscreen = bool(v)
        if persist:
            self.save_window_cfg()
        # 关掉这个行为时，如果正因为全屏沉在下面，就把她捞回用户想要的样子
        if not self.sink_on_fullscreen and self._fs_app:
            self._apply_level(self.win_level)

    def save_window_cfg(self):
        try:
            api_config["window"] = {"level": self.win_level,
                                    "sink_on_fullscreen": self.sink_on_fullscreen}
            json.dump(api_config, open(CFG_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception as e:
            print("[设置] 窗口设置没存上：", e, flush=True)

    def _on_game_state(self, d):
        """前台切成游戏 / 全屏了 —— 只在用户开了"自动让位"时才动。

        默认关：用户明确说不要她自己判断，想要手动控层级。
        """
        playing = bool(d.get("playing"))
        fullscreen = bool(d.get("fullscreen"))
        blocking = playing or fullscreen
        if blocking == self._fs_app:
            return
        self._fs_app = blocking
        if not self.sink_on_fullscreen:
            if blocking:
                why = "打游戏" if playing else "全屏"
                print(f"[角色] 你在{why}，按你选的层级不动（{WIN_LEVEL_LABEL[self.win_level]}）",
                      flush=True)
            return
        why = "打游戏" if playing else ("全屏" if fullscreen else "")
        if blocking:
            if self._level != "bottom":
                print(f"[角色] 你在{why}，我先躲到后面"
                      f"（不想要这行为就在右键菜单里关掉「全屏时自动让开」）", flush=True)
                self._apply_level("bottom")
        elif self._level != self.win_level:
            print(f"[角色] 退出来了，我回到「{WIN_LEVEL_LABEL[self.win_level]}」", flush=True)
            self._apply_level(self.win_level)

    # ---------- 设置 ----------
    def open_settings(self):
        from settings_dialog import SettingsDialog
        SettingsDialog(self, api_config).exec()

    def apply_settings(self):
        """设置保存后热更新：人设 + 主动搭话策略 + 本地兜底模型。不用重启。"""
        try:
            self.brain.reload_persona()
            print("[设置] 人设已热更新", flush=True)
        except Exception as e:
            print("[设置] 人设热更新失败：", e, flush=True)
        if self.watcher:
            self.watcher.set_proactive(api_config.get("proactive") or {})
            try:
                self.watcher.cfg["title_proactive_interval"] = int(
                    (api_config.get("watcher") or {}).get("title_proactive_interval", 180))
            except Exception:
                pass

        # 窗口层级（设置窗「桌面显示」页改的）
        wcfg = api_config.get("window")
        wcfg = wcfg if isinstance(wcfg, dict) else {}
        self.sink_on_fullscreen = bool(wcfg.get("sink_on_fullscreen", False))
        self.win_level = _read_win_level(wcfg)
        # 正打着游戏就先别把她抬起来 —— 等退出全屏，_on_game_state 会自然把她送回前面
        if not (self._fs_app and self.sink_on_fullscreen):
            self._apply_level(self.win_level)

        # 本地接口已在 2026-09-29 移除 —— 这里不再有端口要重启
        self.show_bubble("设置好啦～")

    def _startup_greet(self):
        """她一启动，自己先开个口（可在设置里关）。

        **刻意不替她决定要不要说话。** 以前写死一句「刚打开电脑」，
        所以你电脑开了一整天、只是重启一下桌宠，她也说刚打开电脑 —— 一听就假。
        现在只把事实递给她（电脑开了多久、上次说话隔了多久），
        开不开口、说什么，她自己拿主意；她觉得没什么好说的就回「无」，不会打扰你。
        """
        pro = api_config.get("proactive") or {}
        if not pro.get("on_startup", True) or not self.ready:
            return
        w = getattr(self, "watcher", None)
        if w is not None:
            try:
                if not w._may_speak():      # 免打扰 / 到量了就不打扰
                    return
            except Exception:
                pass

        self._on_activity({
            "what": "他这边刚启动",
            "detail": _startup_facts(),
            "mood": "",
            "hook": "",
            "source": "startup",
        })
        if w is not None:
            try:
                w._bump_count()
            except Exception:
                pass

    def quit_clean(self):
        if self.watcher:
            self.watcher.stop()
        QApplication.quit()

    def closeEvent(self, e):
        if getattr(self, "watcher", None):
            self.watcher.stop()
        super().closeEvent(e)

    def chat(self, q, dlg):
        if not self.ready:
            return
        self.start_talking()
        w = ChatWorker(self.brain, q)
        def on_reply(a, mode):
            self.stop_talking()
            dlg.show_reply(a, mode)
            self.show_bubble(a)
        w.reply.connect(on_reply)
        w.start()
        self.workers = getattr(self, "workers", []) + [w]

    def show_bubble(self, text):
        """立绘上方短暂气泡"""
        if not hasattr(self, "bubble"):
            # 气泡只有"置顶 / 不置顶"两态，所以传布尔值
            self.bubble = Bubble(self._level == "top")
        g = self.frameGeometry()
        self.bubble.show_text(text, g.center().x(), g.top() - 8)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(6000, self.bubble.hide)


if __name__ == "__main__":
    # 单实例守卫：先抢内核互斥量，抢不到说明桌宠已在跑，直接退出（不建窗口）。
    # 必须在创建 QApplication 之前判断，否则会闪一下再退。
    if not ensure_single_instance():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "桌宠已经在运行啦～", "提示", 0)
        except Exception:
            pass
        sys.exit(0)

    # 她的大脑只在云上（本地兜底大脑已于 2026-09-29 移除）—— 没配地址就别装作能聊。
    # 必须给弹窗：这个脚本是 pythonw 跑的，写在控制台的报错永远看不见。
    if not str(api_config.get("brain_remote") or "").strip():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "config.json 里没配 brain_remote。\n\n"
                "桌宠现在只做客户端：她的大脑（记忆 / 人设 / 生活 / 日记）\n"
                "只在云服务器上，本地不再养第二个她。\n\n"
                "请在 brain\\config\\config.json 里填：\n"
                "  \"brain_remote\": \"http://127.0.0.1:18787\"\n"
                "并确认 SSH 隧道是通的（双击 run_pet.bat 会自动建）。",
                "角色 · 起不来", 0)
        except Exception:
            pass
        sys.exit(1)

    app = QApplication(sys.argv)
    # 立绘是 Qt::Tool 窗口，Qt 不把它算进"主窗口"；聊天窗才是普通主窗口。
    # 所以一关聊天窗，Qt 就以为最后一个窗口关了 -> 整个程序退出，立绘跟着没。
    # 这里关掉自动退出：程序只允许通过右键「退出」结束。
    app.setQuitOnLastWindowClosed(False)
    pet = PetWindow()
    pet.show()
    pet.raise_()
    pet.activateWindow()
    sys.exit(app.exec())
