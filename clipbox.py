# -*- coding: utf-8 -*-
"""
剪贴板盒子 ClipBox
- 后台常驻，记录剪贴板历史（文本 / 图片 / 文件）
- 全局热键（默认 Ctrl+Alt+V）唤出粘贴面板，点击条目即粘贴到原窗口
- 关闭主界面后依然在后台运行，托盘右键：粘贴面板 / 主界面 / 退出
"""
import ctypes
import hashlib
import json
import os
import sys
import time
import traceback
import winreg
from ctypes import wintypes

from PyQt5.QtCore import (Qt, QTimer, QSize, QPoint, QRect, QRectF, QUrl, QObject,
                          QEvent, QMimeData, QIODevice, QByteArray, QBuffer,
                          QTranslator, QLibraryInfo)
from PyQt5.QtGui import (QIcon, QPixmap, QPainter, QColor, QFont, QFontMetrics,
                         QKeySequence, QImage, QPen, QBrush, QCursor, QLinearGradient)
from PyQt5.QtWidgets import (QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QListWidget, QListWidgetItem, QStyledItemDelegate,
                             QLineEdit, QSpinBox, QCheckBox, QComboBox, QKeySequenceEdit,
                             QSystemTrayIcon, QMenu, QAction, QStyle, QFrame, QMessageBox,
                             QGroupBox, QFormLayout, QAbstractItemView, QSizePolicy,
                             QDialog, QTextEdit, QColorDialog, QFileDialog, QGridLayout)

# ---------------------------------------------------------------- 常量
APP_NAME = "ClipBox"
APP_TITLE = "剪贴板盒子"
APP_VERSION = "1.12"
APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ClipBox")
IMG_DIR = os.path.join(APP_DIR, "images")
HIST_FILE = os.path.join(APP_DIR, "history.json")
CONF_FILE = os.path.join(APP_DIR, "config.json")
LOCK_FILE = os.path.join(APP_DIR, "run.lock")
MUTEX_NAME = "ClipBox_SingleInstance_Mutex"

SHOW_PANEL_MSG = "ClipBox_ShowPanel"

LOG_ON = "--log" in sys.argv
LOG_PATH = os.path.join(APP_DIR, "log.txt")


def log(msg):
    if not LOG_ON:
        return
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S") + "  " + str(msg) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 界面中文化
# Qt 内置对话框（颜色 / 文件）默认是英文，这里加载 Qt 官方中文语言包；
# 语言包缺失时由 localize_dialog() 逐控件兜底替换。
def zh_translator_paths():
    cands = []
    base = getattr(sys, "_MEIPASS", "") or ""
    if base:
        cands.append(os.path.join(base, "PyQt5", "Qt5", "translations", "qt_zh_CN.qm"))
        cands.append(os.path.join(base, "translations", "qt_zh_CN.qm"))
    try:
        import PyQt5
        cands.append(os.path.join(os.path.dirname(PyQt5.__file__), "Qt5",
                                  "translations", "qt_zh_CN.qm"))
    except Exception:
        pass
    try:
        cands.append(os.path.join(QLibraryInfo.location(QLibraryInfo.TranslationsPath),
                                  "qt_zh_CN.qm"))
    except Exception:
        pass
    return cands


def install_zh_translator(qapp):
    try:
        for p in zh_translator_paths():
            if os.path.exists(p):
                tr = QTranslator()
                if tr.load(p) and qapp.installTranslator(tr):
                    qapp.__dict__["_zh_tr"] = tr  # 防止被回收
                    log("已加载中文语言包: %s" % p)
                    return True
    except Exception:
        pass
    log("未找到中文语言包，改用逐控件替换")
    return False


# 常见英文 -> 中文（匹配时会忽略 & 与结尾冒号）
ZH_TEXT_MAP = {
    "basic colors": "基本颜色",
    "custom colors": "自定义颜色",
    "add to custom colors": "添加到自定义颜色",
    "pick screen color": "屏幕取色",
    "color": "颜色",
    "hue": "色相",
    "sat": "饱和度",
    "val": "明度",
    "red": "红",
    "green": "绿",
    "blue": "蓝",
    "alpha": "透明度",
    "html": "HTML",
    "ok": "确定",
    "cancel": "取消",
    "close": "关闭",
    "select color": "选择颜色",
    "look in": "查找范围",
    "file name": "文件名",
    "files of type": "文件类型",
    "open": "打开",
    "save": "保存",
    "directory": "目录",
    "computer": "计算机",
    "recent places": "最近的位置",
    "desktop": "桌面",
    "my computer": "我的电脑",
    "back": "后退",
    "forward": "前进",
    "parent directory": "上一级",
    "create new folder": "新建文件夹",
    "list view": "列表",
    "detail view": "详细信息",
    "all files": "所有文件",
}


def _zh_of(text):
    if not text:
        return None
    raw = text
    t = text.replace("&", "").strip()
    tail = ""
    while t.endswith(":") or t.endswith("："):
        tail = t[-1] + tail
        t = t[:-1].strip()
    key = t.lower()
    if key in ZH_TEXT_MAP:
        return ZH_TEXT_MAP[key] + tail
    if raw.strip() in ("#", "&HTML:"):
        return raw
    return None


def localize_dialog(dlg):
    """把 Qt 内置对话框里残留的英文按映射改成中文（忽略 & 快捷键标记）"""
    try:
        for w in dlg.findChildren(QWidget):
            try:
                getter = getattr(w, "text", None)
                if not callable(getter):
                    continue
                cur = getter()
                if not isinstance(cur, str) or not cur:
                    continue
                new = _zh_of(cur)
                if new and new != cur:
                    w.setText(new)
            except Exception:
                continue
        title = dlg.windowTitle()
        new = _zh_of(title)
        if new:
            dlg.setWindowTitle(new)
    except Exception:
        pass
    return dlg


def pick_color(parent, initial, title="选择颜色", on_change=None):
    """中文版取色对话框：强制 Qt 内置样式（可中文化），支持实时预览回调"""
    dlg = QColorDialog(initial, parent)
    dlg.setWindowTitle(title)
    dlg.setOption(QColorDialog.DontUseNativeDialog, True)
    dlg.setOption(QColorDialog.ShowAlphaChannel, False)
    localize_dialog(dlg)
    if on_change is not None:
        try:
            dlg.currentColorChanged.connect(lambda c: on_change(c.name()))
        except Exception:
            pass
    try:
        ok = (dlg.exec_() == QDialog.Accepted)
    except Exception:
        ok = False
    try:
        dlg.currentColorChanged.disconnect()
    except Exception:
        pass
    return dlg.currentColor() if ok else QColor()


WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
ERROR_ALREADY_EXISTS = 183

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = ctypes.c_uint
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM,
                                       wintypes.LPARAM, ctypes.c_uint, ctypes.c_uint,
                                       ctypes.POINTER(ctypes.c_ulong)]
user32.SendMessageTimeoutW.restype = ctypes.c_long
kernel32.OpenProcess.argtypes = [ctypes.c_ulong, wintypes.BOOL, ctypes.c_ulong]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]

_enum_cb_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def broadcast_show_panel():
    """异步通知已有实例弹面板（PostMessage 不会卡住本进程）"""
    mid = user32.RegisterWindowMessageW(SHOW_PANEL_MSG)
    if not mid:
        return
    seen = {"cb": None}

    def cb(hwnd, lparam):
        user32.PostMessageW(hwnd, mid, 0, 0)
        return True

    seen["cb"] = _enum_cb_type(cb)     # 保持回调引用
    user32.EnumWindows(seen["cb"], 0)
    del seen


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", ctypes.c_uint),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", ctypes.c_ulong), ("pt", wintypes.POINT)]


# ---------------------------------------------------------------- 快捷键解析
KEYMAP = {
    "Esc": 0x1B, "Escape": 0x1B, "Tab": 0x09, "Space": 0x20, "Backspace": 0x08,
    "Return": 0x0D, "Enter": 0x0D, "Ins": 0x2D, "Insert": 0x2D, "Del": 0x2E,
    "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PgUp": 0x21, "PageUp": 0x21,
    "PgDown": 0x22, "PageDown": 0x22, "Left": 0x25, "Up": 0x26, "Right": 0x27,
    "Down": 0x28, "Print": 0x2C, "Pause": 0x13, "Menu": 0x5D, "Numlock": 0x90,
    "ScrollLock": 0x91, "CapsLock": 0x14,
}
MODMAP = {"Ctrl": MOD_CONTROL, "Control": MOD_CONTROL, "Alt": MOD_ALT,
          "Shift": MOD_SHIFT, "Meta": MOD_WIN, "Win": MOD_WIN, "Super": MOD_WIN}


def vk_of(name):
    """把 Qt 键名转成虚拟键码，失败返回 0"""
    if not name:
        return 0
    if name in KEYMAP:
        return KEYMAP[name]
    if len(name) == 2 and name[0] == "F" and name[1:].isdigit():
        n = int(name[1:])
        if 1 <= n <= 24:
            return 0x6F + n
    if len(name) == 1:
        v = user32.VkKeyScanW(ctypes.c_wchar(name))
        v = v & 0xFFFF
        if v != -1:
            return v & 0xFF
    return 0


def parse_hotkey(seq_text):
    """'Ctrl+Alt+V' -> (mods, vk, 可读字符串)"""
    if not seq_text:
        return 0, 0, ""
    parts = [p for p in seq_text.split("+") if p]
    if not parts:
        return 0, 0, ""
    key = parts[-1]
    mods = 0
    for p in parts[:-1]:
        if p in MODMAP:
            mods |= MODMAP[p]
    vk = vk_of(key)
    if not vk:
        return 0, 0, ""
    names = []
    if mods & MOD_CONTROL:
        names.append("Ctrl")
    if mods & MOD_ALT:
        names.append("Alt")
    if mods & MOD_SHIFT:
        names.append("Shift")
    if mods & MOD_WIN:
        names.append("Win")
    names.append(key)
    return mods, vk, "+".join(names)


def send_ctrl_v():
    """向当前前台窗口发送 Ctrl+V"""
    time.sleep(0.02)

    def one(vk, up):
        scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
        ki = KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP if up else 0, 0, 0)
        inp = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=ki))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    one(0x11, False)   # Ctrl down
    one(0x56, False)   # V down
    one(0x56, True)
    one(0x11, True)
    time.sleep(0.02)


def foreground_hwnd():
    return user32.GetForegroundWindow() or 0


def pid_alive(pid):
    """进程是否存在"""
    try:
        h = kernel32.OpenProcess(0x1000, False, int(pid))
        if h:
            kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def acquire_single_instance():
    """占用实例锁，True 表示本进程是首个实例"""
    try:
        os.makedirs(APP_DIR, exist_ok=True)
    except Exception:
        pass
    try:
        if os.path.exists(LOCK_FILE):
            pid = 0
            try:
                with open(LOCK_FILE, "r") as f:
                    pid = int((f.read() or "0").strip() or 0)
            except Exception:
                pid = 0
            if pid and pid != os.getpid() and pid_alive(pid):
                return False
            try:
                os.remove(LOCK_FILE)
            except Exception:
                pass
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True


def release_single_instance():
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, "r") as f:
                if (f.read() or "").strip() == str(os.getpid()):
                    os.remove(LOCK_FILE)
    except Exception:
        pass


def hwnd_pid(hwnd):
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


# ---------------------------------------------------------------- 配置 / 历史
DEFAULT_CONF = {
    "hotkey": "Ctrl+Alt+V",
    "autostart": False,
    "max_items": 200,
    "panel_pos": 0,          # 0=跟随鼠标 1=屏幕居中
    "save_images": True,
    "max_image_px": 1600,
    "theme": {"mode": "light", "accent": "#3B82F6"},
}

PRESET_COLORS = ["#3B82F6", "#10B981", "#8B5CF6", "#F59E0B",
                 "#EF4444", "#06B6D4", "#EC4899", "#64748B"]


class Theme(object):
    """整体配色：浅色 / 深色 + 自定义主题色"""

    def __init__(self, mode="light", accent="#3B82F6"):
        self.mode = mode if mode in ("light", "dark") else "light"
        self.accent = QColor(accent) if QColor(accent).isValid() else QColor("#3B82F6")

    @property
    def dark(self):
        return self.mode == "dark"

    def c(self, key):
        a = self.accent
        if self.dark:
            d = {
                "bg": "#1E222B", "card": "#262B36", "input": "#2B313D", "text": "#E5E7EB",
                "sub": "#8B93A1", "border": "#39404E", "hover": "#303643", "head": "#232833",
                "group": "#262B36", "btn": "#2B313D", "tag": "#3B4252", "on_accent": "#FFFFFF",
            }
        else:
            d = {
                "bg": "#FFFFFF", "card": "#FFFFFF", "input": "#FFFFFF", "text": "#1F2937",
                "sub": "#9CA3AF", "border": "#D6DAE0", "hover": "#F4F6F9", "head": "#F7F8FA",
                "group": "#FFFFFF", "btn": "#FFFFFF", "tag": "#E5E7EB", "on_accent": "#FFFFFF",
            }
        if key == "accent":
            return a.name()
        if key == "sel":
            c = QColor(a)
            c.setAlpha(38 if self.dark else 30)
            return "rgba(%d,%d,%d,%d)" % (c.red(), c.green(), c.blue(), c.alpha())
        if key == "selborder":
            return a.name()
        return d.get(key, "#000000")

    def to_conf(self):
        return {"mode": self.mode, "accent": self.accent.name()}


def qss(t, kind):
    """生成样式表：kind = main / panel / dialog"""
    base = ("QWidget { font-family:'Microsoft YaHei UI'; font-size:12px; color:%s; }\n"
            "QLabel { color:%s; background:transparent; }\n"
            "QLabel#sub { color:%s; font-size:10px; }\n"
            "QLineEdit, QTextEdit, QSpinBox, QComboBox, QKeySequenceEdit {"
            " border:1px solid %s; border-radius:6px; padding:5px 8px;"
            " background:%s; color:%s; }\n"
            "QLineEdit:focus, QTextEdit:focus, QKeySequenceEdit:focus { border:1px solid %s; }\n"
            "QPushButton { border:1px solid %s; border-radius:6px; padding:6px 12px;"
            " background:%s; color:%s; }\n"
            "QPushButton:hover { background:%s; }\n"
            "QPushButton#primary { background:%s; border:none; color:%s; font-weight:bold; }\n"
            "QPushButton#danger { color:#DC2626; }\n"
            "QCheckBox { color:%s; }\n"
            "QScrollBar:vertical { background:transparent; width:9px; margin:0; }\n"
            "QScrollBar::handle:vertical { background:%s; border-radius:4px; min-height:24px; }\n"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }\n"
            % (t.c("text"), t.c("text"), t.c("sub"), t.c("border"), t.c("input"), t.c("text"),
               t.c("accent"), t.c("border"), t.c("btn"), t.c("text"), t.c("hover"),
               t.c("accent"), t.c("on_accent"), t.c("text"), t.c("border")))
    if kind == "panel":
        return base + (
            "QWidget#Panel { background:%s; border:1px solid %s; border-radius:10px; }\n"
            "QFrame#header { background:%s; border-radius:8px; }\n"
            "QListWidget { border:none; background:%s; }\n"
            "QPushButton#mini { border:none; background:transparent; color:%s;"
            " font-size:15px; padding:0; min-width:26px; max-width:26px; min-height:24px; }\n"
            "QPushButton#mini:hover { background:%s; color:%s; }\n"
            % (t.c("bg"), t.c("border"), t.c("head"), t.c("bg"), t.c("sub"),
               t.c("border"), t.c("text")))
    return base + (
        "QGroupBox { border:1px solid %s; border-radius:8px; margin-top:14px; padding-top:14px;"
        " font-weight:bold; background:%s; }\n"
        "QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 4px; color:%s; }\n"
        "QPushButton#swatch { border:2px solid transparent; border-radius:5px;"
        " min-width:24px; max-width:24px; min-height:24px; max-height:24px; padding:0; }\n"
        "QPushButton#swatch[on=\"1\"] { border:2px solid %s; }\n"
        % (t.c("border"), t.c("group"), t.c("text"), t.c("text")))


def load_conf():
    conf = dict(DEFAULT_CONF)
    try:
        with open(CONF_FILE, "r", encoding="utf-8") as f:
            conf.update(json.load(f))
    except Exception:
        pass
    return conf


def save_conf(conf):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONF_FILE, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fmt_time(ts):
    t = time.localtime(ts)
    n = time.localtime()
    if t.tm_year == n.tm_year and t.tm_yday == n.tm_yday:
        return time.strftime("%H:%M:%S", t)
    return time.strftime("%m-%d %H:%M", t)


def item_digest(kind, text, files, blob=b""):
    if kind == "text":
        base = text.encode("utf-8", "ignore")
    elif kind == "files":
        base = "\n".join(files).encode("utf-8", "ignore")
    else:
        base = blob
    return hashlib.md5(base).hexdigest()


class History(object):
    """剪贴板历史：最新在前"""

    def __init__(self):
        self.items = []
        self._thumbs = {}
        self.load()

    # ---- 持久化
    def load(self):
        self.items = []
        try:
            with open(HIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
        if not isinstance(data, list):
            data = []
        for it in data:
            try:
                if it.get("k") == "image":
                    p = os.path.join(IMG_DIR, it.get("f", ""))
                    if not os.path.exists(p):
                        continue
                self.items.append(it)
            except Exception:
                continue

    def save(self):
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(HIST_FILE, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False)
        except Exception:
            pass

    def clear(self):
        for it in self.items:
            if it.get("k") == "image" and it.get("f"):
                try:
                    os.remove(os.path.join(IMG_DIR, it["f"]))
                except Exception:
                    pass
        self.items = []
        self._thumbs = {}
        self.save()

    def gc_images(self):
        """删除没有被引用的图片文件"""
        try:
            used = {it.get("f") for it in self.items if it.get("k") == "image"}
            for name in os.listdir(IMG_DIR):
                if name not in used:
                    try:
                        os.remove(os.path.join(IMG_DIR, name))
                    except Exception:
                        pass
        except Exception:
            pass

    def trim(self, limit):
        removed = []
        if len(self.items) > limit:
            removed = self.items[limit:]
            self.items = self.items[:limit]
        for it in removed:
            if it.get("k") == "image" and it.get("f"):
                try:
                    os.remove(os.path.join(IMG_DIR, it["f"]))
                except Exception:
                    pass
                self._thumbs.pop(it.get("f"), None)

    # ---- 增删
    def add(self, item, limit):
        for i, old in enumerate(self.items):
            if old.get("h") == item["h"]:
                self.items.pop(i)
                break
        self.items.insert(0, item)
        self.trim(limit)
        self.save()
        return True

    def remove(self, item):
        for i, old in enumerate(self.items):
            if old.get("h") == item.get("h"):
                self.items.pop(i)
                break
        if item.get("k") == "image" and item.get("f"):
            try:
                os.remove(os.path.join(IMG_DIR, item["f"]))
            except Exception:
                pass
            self._thumbs.pop(item.get("f"), None)
        self.save()

    # ---- 图片缓存
    def thumb(self, item, size=46):
        key = item.get("f", "")
        if key in self._thumbs:
            return self._thumbs[key]
        pm = QPixmap()
        try:
            img = QImage(os.path.join(IMG_DIR, key))
            if not img.isNull():
                pm = QPixmap.fromImage(img).scaled(size, size, Qt.KeepAspectRatio,
                                                   Qt.SmoothTransformation)
        except Exception:
            pass
        self._thumbs[key] = pm
        return pm

    def preview(self, item, max_lines=2):
        k = item.get("k")
        if k == "text":
            return item.get("t", "")
        if k == "files":
            names = [os.path.basename(p) for p in item.get("u", [])]
            head = "、".join(names[:3])
            if len(names) > 3:
                head += " 等 %d 个文件" % len(names)
            return head
        return "[图片]"


# ---------------------------------------------------------------- 图标
def make_icon_pixmap(size=64, accent="#3B82F6"):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    ac = QColor(accent) if QColor(accent).isValid() else QColor("#3B82F6")

    def R(x, y, w, h):
        return QRectF(x * s, y * s, w * s, h * s)

    # 底板
    p.setPen(Qt.NoPen)
    p.setBrush(ac)
    p.drawRoundedRect(R(2, 2, 60, 60), 14 * s, 14 * s)
    # 夹板
    p.setBrush(QColor("#FFFFFF"))
    p.drawRoundedRect(R(17, 12, 30, 44), 5 * s, 5 * s)
    # 夹子
    p.setBrush(QColor(ac.lighter(140).name()))
    p.drawRoundedRect(R(25, 8, 14, 10), 3 * s, 3 * s)
    # 线条
    p.setBrush(QColor(ac.lighter(165).name()))
    p.drawRoundedRect(R(22, 28, 20, 3), 1.5 * s, 1.5 * s)
    p.drawRoundedRect(R(22, 35, 20, 3), 1.5 * s, 1.5 * s)
    p.drawRoundedRect(R(22, 42, 12, 3), 1.5 * s, 1.5 * s)
    p.end()
    return pm


# ---------------------------------------------------------------- 列表绘制
class ItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        it = index.data(Qt.UserRole)
        if isinstance(it, dict) and it.get("k") == "image":
            return QSize(0, 76)
        return QSize(0, 58)

    def paint(self, painter, option, index):
        it = index.data(Qt.UserRole)
        if not isinstance(it, dict):
            super(ItemDelegate, self).paint(painter, option, index)
            return
        p = painter
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(6, 3, -6, -3)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)

        t = APP_INSTANCE.theme
        if selected:
            bg = QColor(t.c("sel"))
        elif hover:
            bg = QColor(t.c("hover"))
        else:
            bg = QColor(t.c("card"))
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(r), 8, 8)
        if selected:
            p.setPen(QPen(QColor(t.c("accent")), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 8, 8)

        is_img = it.get("k") == "image"
        box = QRect(r.left() + 9, r.top() + (r.height() - 46) // 2, 46, 46)
        if is_img:
            pm = APP_INSTANCE.hist.thumb(it, 46)
            if not pm.isNull():
                p.drawPixmap(box.topLeft(), pm)
            else:
                p.setBrush(QColor(t.c("tag")))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(QRectF(box), 6, 6)
        else:
            tag = {"text": "文", "files": "文"}.get(it.get("k"), "?")
            color = "#10B981" if it.get("k") == "files" else t.c("accent")
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(box), 8, 8)
            p.setPen(QColor("#FFFFFF"))
            f = QFont("Microsoft YaHei UI", 15, QFont.Bold)
            p.setFont(f)
            p.drawText(QRectF(box), Qt.AlignCenter, tag)

        # 文本区
        tx = box.right() + 10
        tw = r.right() - 10 - tx
        if tw > 20:
            fm = QFontMetrics(QFont("Microsoft YaHei UI", 10))
            raw = APP_INSTANCE.hist.preview(it).replace("\r", " ").replace("\n", " ")
            raw = " ".join(raw.split())
            p.setPen(QColor(t.c("text")))
            p.setFont(QFont("Microsoft YaHei UI", 10))
            if is_img:
                info = "图片"
                try:
                    info = "图片  %s" % fmt_time(it.get("ts", 0))
                except Exception:
                    pass
                p.drawText(QRect(tx, r.top() + 14, tw, 20), Qt.AlignVCenter,
                           fm.elidedText(info, Qt.ElideRight, tw))
            else:
                line1 = fm.elidedText(raw, Qt.ElideRight, tw)
                p.drawText(QRect(tx, r.top() + 10, tw, 20), Qt.AlignVCenter, line1)
                p.setPen(QColor(t.c("sub")))
                p.setFont(QFont("Microsoft YaHei UI", 8))
                sub = "%s · %d 字符" % (fmt_time(it.get("ts", 0)), len(raw))
                if it.get("k") == "files":
                    sub = "%s · %d 个文件" % (fmt_time(it.get("ts", 0)), len(it.get("u", [])))
                p.drawText(QRect(tx, r.top() + 30, tw, 16), Qt.AlignVCenter,
                           QFontMetrics(QFont("Microsoft YaHei UI", 8)).elidedText(sub, Qt.ElideRight, tw))
        p.setPen(QColor(t.c("sub")))
        p.setFont(QFont("Microsoft YaHei UI", 8))
        p.drawText(QRect(r.right() - 60, r.bottom() - 18, 56, 14), Qt.AlignRight | Qt.AlignVCenter,
                   "Enter 粘贴")
        p.restore()


# ---------------------------------------------------------------- 粘贴面板
class Panel(QWidget):
    def __init__(self, app):
        super(Panel, self).__init__()
        self.app = app
        self.setObjectName("Panel")
        self.setWindowTitle(APP_TITLE)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        self.setMinimumWidth(430)
        self.resize(470, 500)
        self.guard_until = 0.0
        self._drag = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        # 顶栏：按住可拖动窗口
        self.header = QFrame()
        self.header.setObjectName("header")
        self.header.setFixedHeight(30)
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(10, 0, 6, 0)
        hl.setSpacing(2)
        self.lb_title = QLabel(APP_TITLE)
        self.lb_title.setStyleSheet("font-weight:bold;")
        hl.addWidget(self.lb_title)
        hl.addStretch(1)
        self.lb_drag = QLabel("拖动此处移动")
        self.lb_drag.setObjectName("sub")
        hl.addWidget(self.lb_drag)
        self.btn_add = QPushButton("＋")
        self.btn_add.setObjectName("mini")
        self.btn_add.setToolTip("新增剪贴板内容")
        self.btn_add.clicked.connect(self.on_add)
        hl.addWidget(self.btn_add)
        self.btn_x = QPushButton("×")
        self.btn_x.setObjectName("mini")
        self.btn_x.setToolTip("关闭 (Esc)")
        self.btn_x.clicked.connect(self.close_panel)
        hl.addWidget(self.btn_x)
        root.addWidget(self.header)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索剪贴板内容…  (输入过滤，Esc 关闭)")
        self.search.textChanged.connect(self.fill)
        self.search.returnPressed.connect(self.paste_current)
        top.addWidget(self.search, 1)
        btn_clear = QPushButton("清空历史")
        btn_clear.clicked.connect(self.on_clear)
        top.addWidget(btn_clear)
        root.addLayout(top)

        self.list = QListWidget()
        self.list.setItemDelegate(ItemDelegate())
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.itemClicked.connect(lambda _: self.paste_current())
        self.list.itemActivated.connect(lambda _: self.paste_current())
        self.list.installEventFilter(self)
        root.addWidget(self.list, 1)

        bottom = QHBoxLayout()
        self.tip = QLabel("↑↓ 选择 · Enter 粘贴 · 1-9 快选 · Delete 删除 · Esc 关闭")
        self.tip.setObjectName("sub")
        bottom.addWidget(self.tip, 1)
        self.count = QLabel("")
        self.count.setObjectName("sub")
        bottom.addWidget(self.count)
        root.addLayout(bottom)

        self.apply_theme()

    def apply_theme(self):
        self.setStyleSheet(qss(self.app.theme, "panel"))
        self.list.viewport().update()

    # ---- 拖动移动（顶栏与空白区域均可）
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = ev.globalPos() - self.frameGeometry().topLeft()
            self.setCursor(Qt.ClosedHandCursor)
            ev.accept()
        else:
            super(Panel, self).mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.move(ev.globalPos() - self._drag)
            ev.accept()
        else:
            super(Panel, self).mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag = None
        self.unsetCursor()
        super(Panel, self).mouseReleaseEvent(ev)

    # ---- 数据填充
    def fill(self):
        kw = self.search.text().strip().lower()
        self.list.clear()
        n = 0
        for it in self.app.hist.items:
            if kw:
                hay = self.app.hist.preview(it).lower()
                if it.get("k") == "files":
                    hay += " " + " ".join(it.get("u", [])).lower()
                if kw not in hay:
                    continue
            li = QListWidgetItem()
            li.setData(Qt.UserRole, it)
            li.setSizeHint(QSize(10, 76 if it.get("k") == "image" else 58))
            self.list.addItem(li)
            n += 1
        self.count.setText("%d 条" % n)
        if n:
            self.list.setCurrentRow(0)

    def show_panel(self):
        self.search.clear()
        self.fill()
        # 定位
        scr = QApplication.desktop()
        if self.app.conf.get("panel_pos", 0) == 0:
            geo = scr.availableGeometry(QCursor.pos())
            pos = QCursor.pos()
        else:
            geo = scr.availableGeometry()
            pos = geo.center()
        w, h = self.width(), self.height()
        x = min(max(pos.x() - w // 2, geo.left() + 6), geo.right() - w - 6)
        y = min(max(pos.y() - 40, geo.top() + 6), geo.bottom() - h - 6)
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()
        self.guard_until = time.time() + 0.4

    def close_panel(self):
        self.hide()

    # ---- 操作
    def current_item(self):
        li = self.list.currentItem()
        if li is None:
            return None
        d = li.data(Qt.UserRole)
        return d if isinstance(d, dict) else None

    def paste_current(self):
        it = self.current_item()
        if it:
            self.app.paste(it)
        else:
            self.close_panel()

    def on_add(self):
        self.app.add_item_dialog(self)

    def on_clear(self):
        r = QMessageBox.question(self, APP_TITLE, "确定清空全部剪贴板历史？",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            self.app.hist.clear()
            self.fill()

    def eventFilter(self, obj, ev):
        if obj is self.search and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_PageUp):
                self.list.setFocus()
                QApplication.sendEvent(self.list, ev)
                return True
        return super(Panel, self).eventFilter(obj, ev)

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key_Escape:
            self.close_panel()
            return
        if k in (Qt.Key_Return, Qt.Key_Enter):
            self.paste_current()
            return
        if k == Qt.Key_Delete and self.list.hasFocus():
            it = self.current_item()
            if it:
                self.app.hist.remove(it)
                self.fill()
            return
        if Qt.Key_1 <= k <= Qt.Key_9:
            idx = k - Qt.Key_1
            li = self.list.item(idx)
            if li:
                d = li.data(Qt.UserRole)
                if isinstance(d, dict):
                    self.app.paste(d)
                    return
        if ev.text() and self.list.hasFocus():
            self.search.setFocus()
            self.search.insert(ev.text())
            return
        super(Panel, self).keyPressEvent(ev)

    def event(self, ev):
        if ev.type() == QEvent.WindowDeactivate and self.isVisible():
            # 粘贴流程中会主动隐藏，这里只处理"点到别处"的情况
            if (not self.app.pasting and not self.app.dialog_open
                    and time.time() > getattr(self, "guard_until", 0)):
                self.hide()
        return super(Panel, self).event(ev)


# ---------------------------------------------------------------- 主界面
class MainWindow(QMainWindow):
    def __init__(self, app):
        super(MainWindow, self).__init__()
        self.app = app
        self.setWindowTitle("%s  v%s" % (APP_TITLE, APP_VERSION))
        self.setFixedSize(500, 690)

        c = QWidget()
        self.setCentralWidget(c)
        root = QVBoxLayout(c)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        self.lb_icon = QLabel()
        head.addWidget(self.lb_icon)
        t = QLabel(APP_TITLE)
        t.setStyleSheet("font-size:16px; font-weight:bold;")
        head.addWidget(t)
        ver = QLabel("v" + APP_VERSION)
        ver.setObjectName("sub")
        head.addWidget(ver)
        head.addStretch(1)
        self.status = QLabel("后台常驻中")
        head.addWidget(self.status)
        root.addLayout(head)

        g1 = QGroupBox("基本设置")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignRight)
        self.hk = QKeySequenceEdit(QKeySequence(app.conf.get("hotkey", "Ctrl+Alt+V")))
        f1.addRow("呼出快捷键：", self.hk)
        self.cb_auto = QCheckBox("开机自动启动")
        self.cb_auto.setChecked(bool(app.conf.get("autostart")))
        f1.addRow("", self.cb_auto)
        self.cb_img = QCheckBox("记录图片")
        self.cb_img.setChecked(bool(app.conf.get("save_images", True)))
        f1.addRow("", self.cb_img)
        self.sp_max = QSpinBox()
        self.sp_max.setRange(20, 2000)
        self.sp_max.setSingleStep(20)
        self.sp_max.setValue(int(app.conf.get("max_items", 200)))
        f1.addRow("最多保存条数：", self.sp_max)
        self.cb_pos = QComboBox()
        self.cb_pos.addItems(["面板跟随鼠标位置", "面板在屏幕中央"])
        self.cb_pos.setCurrentIndex(int(app.conf.get("panel_pos", 0)))
        f1.addRow("面板位置：", self.cb_pos)
        root.addWidget(g1)

        g3 = QGroupBox("外观")
        v3 = QVBoxLayout(g3)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("整体风格："))
        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["浅色", "深色"])
        self.cb_mode.setCurrentIndex(1 if app.theme.dark else 0)
        self.cb_mode.currentIndexChanged.connect(self.on_mode_change)
        r1.addWidget(self.cb_mode, 1)
        r1.addStretch(2)
        v3.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("主题色："))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.swatch_btns = []
        for i, col in enumerate(PRESET_COLORS):
            b = QPushButton("")
            b.setFixedSize(24, 24)
            b.setToolTip(col)
            b.clicked.connect(lambda _=False, x=col: self.set_accent(x))
            grid.addWidget(b, 0, i)
            self.swatch_btns.append(b)
        r2.addLayout(grid)
        r2.addStretch(1)
        v3.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(QLabel("色号："))
        self.ed_color = QLineEdit()
        self.ed_color.setMaxLength(7)
        self.ed_color.setFixedWidth(88)
        self.ed_color.setPlaceholderText("#3B82F6")
        self.ed_color.textChanged.connect(self.on_color_typed)
        self.ed_color.returnPressed.connect(self.on_color_typed)
        r3.addWidget(self.ed_color)
        b_pick = QPushButton("调色板…")
        b_pick.clicked.connect(self.on_pick_color)
        r3.addWidget(b_pick)
        r3.addStretch(1)
        v3.addLayout(r3)
        root.addWidget(g3)

        g2 = QGroupBox("历史")
        v2 = QVBoxLayout(g2)
        row = QHBoxLayout()
        self.lb_count = QLabel("")
        row.addWidget(self.lb_count, 1)
        b_add = QPushButton("＋ 新增内容")
        b_add.clicked.connect(self.on_add)
        row.addWidget(b_add)
        b_clear = QPushButton("清空全部")
        b_clear.setObjectName("danger")
        b_clear.clicked.connect(self.on_clear)
        row.addWidget(b_clear)
        v2.addLayout(row)
        hint = QLabel("提示：关闭本窗口不会退出程序，程序继续在后台记录剪贴板。\n"
                      "要彻底退出，请在任务栏托盘图标上右键选择「退出」。")
        hint.setObjectName("sub")
        hint.setWordWrap(True)
        v2.addWidget(hint)
        root.addWidget(g2)

        root.addStretch(1)
        b1 = QHBoxLayout()
        b_save = QPushButton("保存设置")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self.on_save)
        b_panel = QPushButton("打开粘贴面板")
        b_panel.clicked.connect(lambda: self.app.show_panel())
        b1.addWidget(b_save)
        b1.addWidget(b_panel)
        root.addLayout(b1)
        b2 = QHBoxLayout()
        b_hide = QPushButton("隐藏到托盘")
        b_hide.clicked.connect(self.hide)
        b_quit = QPushButton("退出程序")
        b_quit.setObjectName("danger")
        b_quit.clicked.connect(self.app.quit_app)
        b2.addWidget(b_hide)
        b2.addWidget(b_quit)
        root.addLayout(b2)

        self.apply_theme()
        self.refresh()

    # ---- 外观
    def apply_theme(self):
        t = self.app.theme
        self.setStyleSheet(qss(t, "main"))
        self.setWindowIcon(self.app.icon)
        self.lb_icon.setPixmap(self.app.icon.pixmap(30, 30))
        self.status.setStyleSheet("color:%s; font-size:11px;"
                                  % ("#22C55E" if not t.dark else "#4ADE80"))
        self.ed_color.blockSignals(True)
        self.ed_color.setText(t.accent.name().upper())
        self.ed_color.blockSignals(False)
        cur = t.accent.name().lower()
        for b, col in zip(self.swatch_btns, PRESET_COLORS):
            on = (col.lower() == cur)
            b.setStyleSheet(
                "QPushButton { background:%s; border-radius:5px;"
                " border:2px solid %s; }" % (col, t.c("text") if on else "transparent"))

    def on_mode_change(self):
        self.app.set_theme(mode="dark" if self.cb_mode.currentIndex() == 1 else "light")
        self.apply_theme()

    def on_color_typed(self):
        """色号框：边输边生效（凑够 7 位且合法就应用）"""
        txt = self.ed_color.text().strip()
        if not txt.startswith("#"):
            txt = "#" + txt
        if len(txt) == 7 and QColor(txt).isValid():
            self.set_accent(txt)

    def on_pick_color(self):
        """调色板：拖动滑块/选色时实时预览，确定即保留，取消回滚"""
        old = self.app.theme.accent.name()
        col = pick_color(self, self.app.theme.accent, "选择主题色",
                         on_change=self.set_accent)
        if col.isValid():
            self.set_accent(col.name())
        else:
            self.set_accent(old)

    def set_accent(self, color):
        try:
            self.app.set_theme(accent=color)
        except Exception:
            traceback.print_exc()
        self.apply_theme()
        self.flash("已应用主题色 %s" % str(color).upper())

    def flash(self, msg):
        """在状态栏短暂显示一次提示"""
        try:
            self.status.setText(msg)
            if getattr(self, "_flash_timer", None) is None:
                self._flash_timer = QTimer(self)
                self._flash_timer.setSingleShot(True)
                self._flash_timer.timeout.connect(self.refresh)
            self._flash_timer.start(1800)
        except Exception:
            pass

    def on_add(self):
        self.app.add_item_dialog(self)

    def refresh(self):
        self.lb_count.setText("当前共 %d 条记录" % len(self.app.hist.items))
        hk = self.app.hotkey_text or "(未注册)"
        self.status.setText("热键 %s" % hk)

    def on_save(self):
        seq = self.hk.keySequence()
        text = seq.toString(QKeySequence.PortableText)
        if not text:
            QMessageBox.warning(self, APP_TITLE, "请按下要使用的快捷键组合")
            return
        mods, vk, name = parse_hotkey(text)
        if not vk:
            QMessageBox.warning(self, APP_TITLE, "无法识别该按键，请换一个组合")
            return
        ok = self.app.register_hotkey(mods, vk)
        if not ok:
            QMessageBox.warning(self, APP_TITLE,
                                "注册 %s 失败，可能已被其它程序占用，请换一个组合" % name)
            return
        self.app.conf["hotkey"] = name
        self.app.conf["autostart"] = self.cb_auto.isChecked()
        self.app.conf["save_images"] = self.cb_img.isChecked()
        self.app.conf["max_items"] = self.sp_max.value()
        self.app.conf["panel_pos"] = self.cb_pos.currentIndex()
        save_conf(self.app.conf)
        self.app.set_autostart(self.cb_auto.isChecked())
        QApplication.processEvents()
        self.refresh()
        QMessageBox.information(self, APP_TITLE, "设置已保存，快捷键：%s" % name)

    def on_clear(self):
        r = QMessageBox.question(self, APP_TITLE, "确定清空全部剪贴板历史？",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            self.app.hist.clear()
            self.refresh()

    def closeEvent(self, ev):
        ev.ignore()
        self.hide()
        if self.app.tray is not None:
            self.app.tray.showMessage(APP_TITLE, "已隐藏到托盘，程序继续在后台运行",
                                      QSystemTrayIcon.Information, 2000)


# ---------------------------------------------------------------- 新增内容对话框
class AddItemDialog(QDialog):
    def __init__(self, app, parent=None):
        super(AddItemDialog, self).__init__(parent)
        self.app = app
        self.image_path = None
        self.setWindowTitle("新增剪贴板内容")
        self.setStyleSheet(qss(app.theme, "dialog"))
        self.resize(430, 300)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(8)
        tip = QLabel("保存后作为一条记录置顶，可在粘贴面板里直接选用")
        tip.setObjectName("sub")
        v.addWidget(tip)
        self.edit = QTextEdit()
        self.edit.setPlaceholderText("在此输入要保存的文本…")
        v.addWidget(self.edit, 1)
        self.cb_clip = QCheckBox("同时写入系统剪贴板（之后可直接 Ctrl+V）")
        self.cb_clip.setChecked(True)
        v.addWidget(self.cb_clip)
        h = QHBoxLayout()
        b_img = QPushButton("导入图片…")
        b_img.clicked.connect(self.pick_image)
        h.addWidget(b_img)
        h.addStretch(1)
        b_cancel = QPushButton("取消")
        b_cancel.clicked.connect(self.reject)
        h.addWidget(b_cancel)
        b_ok = QPushButton("保存")
        b_ok.setObjectName("primary")
        b_ok.clicked.connect(self.accept)
        h.addWidget(b_ok)
        v.addLayout(h)
        self.edit.setFocus()

    def pick_image(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择图片", "",
                                           "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        if p:
            self.image_path = p
            self.accept()


# ---------------------------------------------------------------- 消息窗口（热键）
class MsgWindow(QWidget):
    def __init__(self, app):
        super(MsgWindow, self).__init__()
        self.app = app
        self.hwnd = 0

    def nativeEvent(self, eventType, message):
        try:
            msg = MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self.app.on_hotkey()
            elif self.app.msg_show_panel and msg.message == self.app.msg_show_panel:
                self.app.on_hotkey()
        except Exception:
            pass
        return False, 0


# ---------------------------------------------------------------- 主应用
class App(QObject):
    def __init__(self, argv):
        super(App, self).__init__()
        global APP_INSTANCE
        APP_INSTANCE = self
        self.argv = argv
        self.selftest = "--selftest" in argv
        self.pasting = False
        self.hotkey_text = ""
        self.last_hwnd = 0
        self.self_set_until = 0.0

        os.makedirs(APP_DIR, exist_ok=True)
        os.makedirs(IMG_DIR, exist_ok=True)
        self.conf = load_conf()
        th = self.conf.get("theme") or {}
        self.theme = Theme(th.get("mode", "light"), th.get("accent", "#3B82F6"))
        self.hist = History()
        self.icon = QIcon(make_icon_pixmap(64, self.theme.accent.name()))
        self.dialog_open = False

        self.msgwin = MsgWindow(self)
        self.msgwin.winId()
        self.hwnd = int(self.msgwin.winId())
        # 与 main() 中广播用的名字必须一致，否则收不到"二次启动"通知
        self.msg_show_panel = user32.RegisterWindowMessageW(SHOW_PANEL_MSG)

        self.panel = Panel(self)
        self.win = MainWindow(self)

        self.tray = None
        if not self.selftest:
            self.tray = QSystemTrayIcon(self.icon, self)
            self.tray.setToolTip(APP_TITLE)
            menu = QMenu()
            a1 = QAction("打开粘贴面板", menu)
            a1.triggered.connect(self.show_panel)
            a2 = QAction("打开主界面", menu)
            a2.triggered.connect(self.show_main)
            a3 = QAction("退出程序", menu)
            a3.triggered.connect(self.quit_app)
            menu.addAction(a1)
            menu.addAction(a2)
            menu.addSeparator()
            menu.addAction(a3)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.on_tray_activated)
            self.tray.show()

        # 热键
        mods, vk, name = parse_hotkey(self.conf.get("hotkey", "Ctrl+Alt+V"))
        if not vk:
            mods, vk, name = MOD_CONTROL | MOD_ALT, 0x56, "Ctrl+Alt+V"
        self.register_hotkey(mods, vk)
        self.conf["hotkey"] = name

        # 剪贴板
        self.clip = QApplication.clipboard()
        self.last_seq = user32.GetClipboardSequenceNumber()
        try:
            self.clip.dataChanged.connect(self.on_clip_changed)
        except Exception:
            pass

        if not self.selftest:
            self.t_clip = QTimer(self)
            self.t_clip.timeout.connect(self.poll_clip)
            self.t_clip.start(400)
            self.t_fg = QTimer(self)
            self.t_fg.timeout.connect(self.poll_fg)
            self.t_fg.start(500)
            self.hist.gc_images()

    # ---- 热键
    def register_hotkey(self, mods, vk):
        try:
            user32.UnregisterHotKey(self.hwnd, 1)
        except Exception:
            pass
        for _ in range(2):
            if user32.RegisterHotKey(self.hwnd, 1, mods or MOD_CONTROL | MOD_ALT, vk):
                self.hotkey_mods, self.hotkey_vk = mods, vk
                self.hotkey_text = self._hk_name(mods, vk)
                log("热键注册成功 %s (mods=%d vk=0x%X)" % (self.hotkey_text, mods, vk))
                return True
            time.sleep(0.05)
        self.hotkey_text = ""
        log("热键注册失败 mods=%d vk=0x%X" % (mods, vk))
        return False

    def _hk_name(self, mods, vk):
        names = []
        if mods & MOD_CONTROL:
            names.append("Ctrl")
        if mods & MOD_ALT:
            names.append("Alt")
        if mods & MOD_SHIFT:
            names.append("Shift")
        if mods & MOD_WIN:
            names.append("Win")
        ch = ""
        if 0x41 <= vk <= 0x5A:
            ch = chr(vk)
        elif 0x30 <= vk <= 0x39:
            ch = chr(vk)
        elif 0x70 <= vk <= 0x87:
            ch = "F%d" % (vk - 0x6F)
        else:
            for k, v in KEYMAP.items():
                if v == vk:
                    ch = k
                    break
        names.append(ch or "?")
        return "+".join(names)

    def on_hotkey(self):
        now = time.time()
        if now - getattr(self, "_last_hotkey", 0) < 0.3:
            return
        self._last_hotkey = now
        log("热键触发")
        # 面板弹出前，当前前台窗口就是要粘贴进去的目标窗口
        h = foreground_hwnd()
        if h and h != self.hwnd:
            try:
                if hwnd_pid(h) != os.getpid():
                    self.last_hwnd = h
            except Exception:
                pass
        self.show_panel()

    # ---- 托盘
    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_panel()

    def show_panel(self):
        log("显示面板，目标窗口 hwnd=%d" % self.last_hwnd)
        self.panel.show_panel()

    def show_main(self):
        self.win.refresh()
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()

    def quit_app(self):
        try:
            user32.UnregisterHotKey(self.hwnd, 1)
        except Exception:
            pass
        self.tray.hide()
        QApplication.quit()

    # ---- 剪贴板
    def on_clip_changed(self):
        self.poll_clip(force=True)

    def poll_clip(self, force=False):
        if self.pasting:
            return
        try:
            seq = user32.GetClipboardSequenceNumber()
        except Exception:
            seq = 0
        if not force and seq == self.last_seq:
            return
        self.last_seq = seq
        if time.time() < self.self_set_until:
            return
        try:
            self.grab()
        except Exception:
            pass

    def grab(self):
        md = self.clip.mimeData()
        if md is None:
            return None
        item = None
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            paths = [p for p in paths if p]
            if paths:
                item = {"k": "files", "u": paths, "ts": time.time(),
                        "h": item_digest("files", "", paths)}
        if item is None and md.hasImage() and self.conf.get("save_images", True):
            try:
                img = md.imageData()
                if isinstance(img, QImage) and not img.isNull():
                    item = self.image_item(img)
            except Exception:
                item = None
        if item is None:
            txt = md.text()
            if txt and txt.strip():
                item = {"k": "text", "t": txt, "ts": time.time(),
                        "h": item_digest("text", txt, [])}
        if item:
            log("捕获剪贴板 %s %s" % (item["k"], (item.get("t", "") or item.get("f", ""))[:30]))
            self.hist.add(item, int(self.conf.get("max_items", 200)))
            self.panel.fill()
            if self.win.isVisible():
                self.win.refresh()
        return item

    # ---- 主题
    def set_theme(self, mode=None, accent=None):
        if mode:
            self.theme.mode = mode
        if accent:
            c = QColor(accent)
            if c.isValid():
                self.theme.accent = c
        self.icon = QIcon(make_icon_pixmap(64, self.theme.accent.name()))
        self.conf["theme"] = self.theme.to_conf()
        save_conf(self.conf)
        self.panel.apply_theme()
        self.win.apply_theme()
        if self.tray is not None:
            self.tray.setIcon(self.icon)
        qa = QApplication.instance()
        if qa is not None:
            qa.setWindowIcon(self.icon)
        log("主题 %s %s" % (self.theme.mode, self.theme.accent.name()))

    # ---- 手动新增内容
    def image_item(self, img):
        """QImage -> 历史条目（落盘 PNG）"""
        mx = int(self.conf.get("max_image_px", 1600))
        if img.width() > mx or img.height() > mx:
            img = img.scaled(mx, mx, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        blob = bytes(ba.data())
        h = item_digest("image", "", [], blob)
        fn = h + ".png"
        os.makedirs(IMG_DIR, exist_ok=True)
        with open(os.path.join(IMG_DIR, fn), "wb") as f:
            f.write(blob)
        return {"k": "image", "f": fn, "ts": time.time(), "h": h,
                "w": img.width(), "hh": img.height()}

    def add_text_item(self, text, to_clip=False):
        text = (text or "").strip()
        if not text:
            return False
        item = {"k": "text", "t": text, "ts": time.time(),
                "h": item_digest("text", text, [])}
        self.hist.add(item, int(self.conf.get("max_items", 200)))
        if to_clip:
            self.self_set_until = time.time() + 1.5
            self.set_clip(item)
            try:
                self.last_seq = user32.GetClipboardSequenceNumber()
            except Exception:
                pass
        self.after_change()
        log("手动新增文本 %d 字" % len(text))
        return True

    def add_image_file(self, path):
        img = QImage(path)
        if img.isNull():
            return False
        item = self.image_item(img)
        self.hist.add(item, int(self.conf.get("max_items", 200)))
        self.after_change()
        log("手动导入图片 %s" % os.path.basename(path))
        return True

    def after_change(self):
        self.panel.fill()
        if self.win.isVisible():
            self.win.refresh()

    def add_item_dialog(self, parent=None):
        dlg = AddItemDialog(self, parent or self.panel)
        self.dialog_open = True
        try:
            if dlg.exec_() != QDialog.Accepted:
                return False
            if getattr(dlg, "image_path", None):
                return self.add_image_file(dlg.image_path)
            return self.add_text_item(dlg.edit.toPlainText(), dlg.cb_clip.isChecked())
        finally:
            self.dialog_open = False

    def set_clip(self, item):
        md = QMimeData()
        k = item.get("k")
        if k == "text":
            md.setText(item.get("t", ""))
        elif k == "image":
            try:
                img = QImage(os.path.join(IMG_DIR, item.get("f", "")))
                if not img.isNull():
                    md.setImageData(img)
            except Exception:
                pass
        elif k == "files":
            md.setUrls([QUrl.fromLocalFile(p) for p in item.get("u", [])])
        self.clip.setMimeData(md)

    def paste(self, item):
        """把条目写回剪贴板并粘贴到原来的窗口"""
        self.pasting = True
        hwnd = self.last_hwnd
        log("粘贴 %s -> hwnd=%d" % (item.get("k"), hwnd))
        self.self_set_until = time.time() + 1.2
        # 趁本进程还占着前台，先把目标窗口切到前台
        try:
            if hwnd and user32.IsWindow(hwnd):
                user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self.panel.close_panel()
        try:
            self.set_clip(item)
            self.last_seq = user32.GetClipboardSequenceNumber()
        except Exception:
            pass

        def focus_and_send():
            try:
                if hwnd and user32.IsWindow(hwnd):
                    user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            try:
                send_ctrl_v()
            except Exception:
                pass
            self.pasting = False

        QTimer.singleShot(180, focus_and_send)

    def poll_fg(self):
        h = foreground_hwnd()
        if not h:
            return
        if h == self.hwnd or h == int(self.panel.winId()) or h == int(self.win.winId()):
            return
        try:
            if hwnd_pid(h) == os.getpid():
                return
        except Exception:
            pass
        self.last_hwnd = h

    # ---- 开机自启
    def set_autostart(self, on):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            if on:
                exe = sys.executable
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, '"%s" --minimized' % exe)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except Exception:
                    pass
            winreg.CloseKey(key)
        except Exception:
            pass


APP_INSTANCE = None


# ---------------------------------------------------------------- 自检
def selftest(app):
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra) if extra else ""))
        if not cond:
            ok = False

    m, v, n = parse_hotkey("Ctrl+Alt+V")
    chk("热键解析 Ctrl+Alt+V", (m, v) == (MOD_CONTROL | MOD_ALT, 0x56), n)
    m, v, n = parse_hotkey("Ctrl+Shift+F1")
    chk("热键解析 Ctrl+Shift+F1", (m & MOD_SHIFT) and v == 0x70, n)
    m, v, n = parse_hotkey("Alt+Space")
    chk("热键解析 Alt+Space", (m, v) == (MOD_ALT, 0x20), n)
    chk("无效键", parse_hotkey("Ctrl")[1] == 0)

    chk("历史初始为空或可写", isinstance(app.hist.items, list), len(app.hist.items))
    before = len(app.hist.items)
    app.hist.add({"k": "text", "t": "自检条目 hello", "ts": time.time(), "h": "selftest1"}, 200)
    chk("新增条目置顶", app.hist.items[0].get("h") == "selftest1")
    app.hist.add({"k": "text", "t": "自检条目 hello", "ts": time.time(), "h": "selftest1"}, 200)
    chk("重复条目不叠加", len(app.hist.items) == before + 1, len(app.hist.items))

    img = QImage(80, 40, QImage.Format_RGB32)
    img.fill(QColor("#3366CC"))
    os.makedirs(IMG_DIR, exist_ok=True)
    p = os.path.join(IMG_DIR, "selftest.png")
    chk("图片写盘", img.save(p, "PNG"))
    app.hist.add({"k": "image", "f": "selftest.png", "ts": time.time(), "h": "selftestimg"}, 200)
    chk("图片缩略图生成", not app.hist.thumb(app.hist.items[0], 46).isNull())
    chk("预览文本", isinstance(app.hist.preview({"k": "text", "t": "abc"}), str))

    app.hist.remove({"k": "image", "f": "selftest.png", "h": "selftestimg"})
    app.hist.remove({"k": "text", "h": "selftest1"})
    chk("删除后回到初始数量", len(app.hist.items) == before, len(app.hist.items))

    chk("面板构造", app.panel is not None)
    app.panel.fill()
    chk("面板列表填充", app.panel.list.count() == len(app.hist.items))
    app.panel.search.setText("不存在的关键字zzz")
    app.panel.fill()
    chk("搜索过滤", app.panel.list.count() == 0)
    app.panel.search.setText("")
    app.panel.fill()

    n0 = len(app.hist.items)
    chk("手动新增文本", app.add_text_item("自检添加的文本XYZ", False))
    chk("新增后置顶", app.hist.items[0].get("t") == "自检添加的文本XYZ")
    chk("空内容不新增", not app.add_text_item("   ", False))
    app.hist.remove(app.hist.items[0])
    chk("新增后可清理", len(app.hist.items) == n0)

    img2 = QImage(30, 20, QImage.Format_RGB32)
    img2.fill(QColor("#00AA88"))
    it2 = app.image_item(img2)
    chk("图片条目生成", it2.get("k") == "image"
        and os.path.exists(os.path.join(IMG_DIR, it2.get("f", ""))))
    app.hist.remove(it2)

    app.set_theme(mode="dark")
    chk("切换深色", app.theme.dark and app.conf["theme"]["mode"] == "dark")
    app.set_theme(accent="#EF4444")
    chk("自定义主题色", app.theme.accent.name().lower() == "#ef4444")
    app.set_theme(mode="light", accent="#3B82F6")
    chk("切回浅色", (not app.theme.dark) and app.theme.accent.name().lower() == "#3b82f6")
    chk("深色样式表非空", "1E222B" in qss(Theme("dark", "#3B82F6"), "panel").upper())

    dlg = AddItemDialog(app, None)
    chk("新增对话框可构造", dlg.edit is not None and dlg.cb_clip.isChecked())
    dlg.edit.setPlainText("abc")
    chk("对话框内容可读", dlg.edit.toPlainText() == "abc")

    chk("单实例锁", acquire_single_instance() is True)
    chk("PID 存活检查", pid_alive(os.getpid()) and not pid_alive(999997))
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getppid()))          # 假装被一个活着的进程占用
        chk("已有存活实例时不接管", acquire_single_instance() is False)
        with open(LOCK_FILE, "w") as f:
            f.write("999997")                   # 死进程留下的锁应被接管
        chk("残留锁可接管", acquire_single_instance() is True)
    except Exception as e:
        chk("锁文件测试", False, e)

    chk("图标非空", not app.icon.isNull())
    chk("托盘类可用", QSystemTrayIcon.isSystemTrayAvailable() or app.tray is None)
    chk("配置持久化", (save_conf(app.conf), os.path.exists(CONF_FILE))[1])
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口
def main():
    argv = sys.argv[1:]
    selftest_mode = "--selftest" in argv

    # 单实例：已运行时通知它弹面板（文件锁 + 系统互斥体双保险）
    if not selftest_mode:
        kernel32.CreateMutexW(None, True, MUTEX_NAME)
        mutex_first = ctypes.get_last_error() != ERROR_ALREADY_EXISTS
        file_first = acquire_single_instance()
        if not (mutex_first and file_first):
            log("已有实例在运行，通知它弹面板 (mutex=%s file=%s)" % (mutex_first, file_first))
            broadcast_show_panel()
            os._exit(0)

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(QIcon(make_icon_pixmap(64)))
    qapp.setApplicationName(APP_NAME)
    install_zh_translator(qapp)

    import atexit
    atexit.register(release_single_instance)

    app = App(argv)

    if selftest_mode:
        return selftest(app)

    minimized = "--minimized" in argv
    first_run = not os.path.exists(CONF_FILE)
    save_conf(app.conf)
    if not minimized:
        app.show_main()
        if first_run:
            app.tray.showMessage(APP_TITLE,
                                 "默认快捷键 Ctrl+Alt+V 呼出粘贴面板\n关闭窗口后仍在后台运行",
                                 QSystemTrayIcon.Information, 4000)
    else:
        app.tray.showMessage(APP_TITLE, "已启动到后台 (Ctrl+Alt+V 呼出)",
                             QSystemTrayIcon.Information, 2500)
    return qapp.exec_()


if __name__ == "__main__":
    sys.exit(main())
