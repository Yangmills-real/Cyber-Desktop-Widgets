#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================
 桌面双时区时钟 · 节假日特殊颜色标记版
 Dual Timezone Desktop Clock with Holiday Highlighting
================================================================
 本版新增（番茄钟休息提醒）：
   - 右下角一个极小的 ⚙ 设置按钮
   - 点击可设定工作倒计时（默认 45 分钟），带 25/45/60/90 快捷预设
   - 倒计时结束 → 弹出提醒窗"喝口水，休息一下眼睛吧！"
   - 同时用 winsound 发出"滴"的提示音
   - 确认提醒后自动重置，直接开始下一轮

 上一版新增：
   - 北京日期 / 美西日期 分别按节假日规则着色
   - 中国：法定节假日=深绿、调休上班日=橙、普通周末=浅绿
   - 美国：主要节假日=蓝、周六/周日=浅绿
   - 自定义特殊日期区间 → 红色（优先级最高）
   - 休息时段：主/副时区各自判断，谁的钟点落在区间内，谁的时间就变红
     （和它排在大字还是小字无关）；提示文字随谁在休息切换：
     大字在休息→"我方"，小字在休息→"对方"，两个都在→"双方"

 保留的原有功能：
   - 无边框、半透明、始终置顶、可拖拽
   - 大字北京时间 + 小字美西时间
   - ⇄ 按钮 / 右键菜单 互换主副时区
   - 每秒刷新、右键菜单退出

 运行环境：Windows + Python 3.9 以上
 安装依赖：pip install tzdata chinesecalendar
          注意：pip 包名是 chinesecalendar（没有下划线），
                但代码里 import 的是 chinese_calendar（有下划线）。
================================================================
"""

import sys
import queue                           # 后台热键线程 → 主线程 的事件队列
import threading                       # 播放提示音放到后台线程，避免卡住界面
import time                            # 番茄钟用单调时钟计时，改系统时间也不受影响
from datetime import datetime, date
from zoneinfo import ZoneInfo          # Python 3.9+ 标准库

import tkinter as tk

# ------------------------------------------------------------------
# 中国节假日库（可选依赖：没装也不影响计时，只是没有颜色标记）
# ------------------------------------------------------------------
try:
    import chinese_calendar
    HAS_CN_CAL = True
except ImportError:
    chinese_calendar = None
    HAS_CN_CAL = False

# ------------------------------------------------------------------
# winsound 只有 Windows 才有。其他系统上自动静音，不影响任何其他功能。
# ------------------------------------------------------------------
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    winsound = None
    HAS_WINSOUND = False

# ==================================================================
# 窗口沉底 / 置顶（纯 ctypes 调 Windows API，不需要第三方库）
# ==================================================================
# Windows 下光靠 root.lower() 往往压不到最底（尤其在无边框窗口上），
# 必须直接调 SetWindowPos 并传 HWND_BOTTOM 才行。
import ctypes

IS_WINDOWS = sys.platform == "win32"

_HWND_TOP = 0            # 放到 Z 序最顶（普通层）
_HWND_BOTTOM = 1         # 放到 Z 序最底（壁纸之上、其它窗口之下）
_HWND_TOPMOST = -1       # 放到置顶层最顶

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040

_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,                  # hWnd
        ctypes.c_void_p,                  # hWndInsertAfter
        ctypes.c_int, ctypes.c_int,       # X, Y
        ctypes.c_int, ctypes.c_int,       # cx, cy
        ctypes.c_uint,                    # uFlags
    ]
    _user32.SetWindowPos.restype = ctypes.c_int

    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short

    # 64 位系统用 GetWindowLongPtrW，32 位只有 GetWindowLongW
    try:
        _user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        _get_window_style = _user32.GetWindowLongPtrW
    except AttributeError:
        _user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _user32.GetWindowLongW.restype = ctypes.c_long
        _get_window_style = _user32.GetWindowLongW
else:
    _user32 = None
    _get_window_style = None


def _hwnd_value(signed):
    """
    HWND_TOP(0) / HWND_BOTTOM(1) / HWND_TOPMOST(-1) 是"伪句柄"，
    -1 必须按指针宽度转成无符号（64 位下是 0xFFFFFFFFFFFFFFFF），
    否则 SetWindowPos 会把它当成无效值。
    """
    bits = 8 * ctypes.sizeof(ctypes.c_void_p)
    return signed & ((1 << bits) - 1)


def get_hwnd(root):
    """取 Tk 顶层窗口真正的 HWND（无边框窗口下 wm_frame 就是它）"""
    root.update_idletasks()
    try:
        return int(root.wm_frame(), 16)     # 返回形如 '0x1a2b3c' 的字符串
    except Exception:
        return root.winfo_id()              # 兜底：Tk 自己的窗口句柄


def win_set_pos(hwnd, insert_after, flags):
    """调用 SetWindowPos，成功返回 True"""
    if not IS_WINDOWS:
        return False
    return bool(_user32.SetWindowPos(hwnd, _hwnd_value(insert_after),
                                     0, 0, 0, 0, flags))


def win_is_topmost(hwnd):
    """窗口当前是否带 WS_EX_TOPMOST 样式（自测/排错用）"""
    if not IS_WINDOWS:
        return False
    return bool(_get_window_style(hwnd, _GWL_EXSTYLE) & _WS_EX_TOPMOST)


def key_state(vk):
    """
    读取按键的原始状态（全局，窗口没焦点也能读）。
      bit 15 (0x8000) = 当前正按着
      bit 0  (0x0001) = 自上次查询以来按下过（读一次就清零）
    ★ 每个键每轮只能读一次 —— 第二次读会把低 bit 吃掉。
    """
    if not IS_WINDOWS:
        return 0
    return _user32.GetAsyncKeyState(vk)


def key_down(vk):
    """键当前是否按着"""
    return bool(key_state(vk) & 0x8000)


def parse_hotkey(spec):
    """
    把 'ctrl+shift+1' 解析成一串虚拟键码（VK）。
    支持 ctrl / shift / alt / win + 单个字母或数字。
    """
    mods = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
    vks = []
    for part in str(spec).lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in mods:
            vks.append(mods[part])
        elif len(part) == 1 and part.isdigit():
            vks.append(0x30 + int(part))            # '1' -> 0x31
        elif len(part) == 1 and "a" <= part <= "z":
            vks.append(ord(part.upper()))           # 'd' -> 0x44
        else:
            raise ValueError(f'快捷键格式不对：{part!r}（示例 "ctrl+shift+1"）')
    return vks


# ---- RegisterHotKey：系统级注册，按下瞬间由系统投递消息，100% 不会漏 ----
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 0x5A17

if IS_WINDOWS:
    from ctypes import wintypes
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    _user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.c_uint, ctypes.c_uint]
    _user32.RegisterHotKey.restype = ctypes.c_int
    _user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.UnregisterHotKey.restype = ctypes.c_int
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint,
                                           ctypes.c_void_p, ctypes.c_void_p]
    _user32.PostThreadMessageW.restype = ctypes.c_int


def parse_hotkey_win(spec):
    """把 'ctrl+shift+1' 解析成 (修饰键掩码, 主键VK)，给 RegisterHotKey 用"""
    mask, vk = 0, None
    table = {"ctrl": _MOD_CONTROL, "shift": _MOD_SHIFT,
             "alt": _MOD_ALT, "win": _MOD_WIN}
    for part in str(spec).lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in table:
            mask |= table[part]
        elif len(part) == 1 and part.isdigit():
            vk = 0x30 + int(part)
        elif len(part) == 1 and "a" <= part <= "z":
            vk = ord(part.upper())
        else:
            raise ValueError(f'快捷键格式不对：{part!r}（示例 "ctrl+shift+1"）')
    if vk is None:
        raise ValueError("快捷键必须包含一个字母或数字主键")
    return mask, vk


class HotkeyWatcher(threading.Thread):
    """
    在独立线程里用 RegisterHotKey 注册系统级热键。

    为什么不用轮询 GetAsyncKeyState：
      实测发现 Ctrl/Shift 这类修饰键的"自上次查询以来按下过"低位会被系统
      自己消费掉 —— 只有主键可靠（实测：三个键一起按 50ms，Ctrl/Shift 读到
      0x0000，主键读到 0x0001）。于是窗口沉底、按得又快时，轮询可能整个漏掉。
      RegisterHotKey 由系统在按键瞬间投递 WM_HOTKEY，不存在漏检。
    """

    def __init__(self, spec, out_queue):
        super().__init__(daemon=True)
        self.spec = spec
        self.out_queue = out_queue
        self.ok = False             # 注册成功才为 True
        self.error = None
        self._tid = None
        self._stopping = False

    def run(self):
        if not IS_WINDOWS:
            return
        try:
            mask, vk = parse_hotkey_win(self.spec)
        except Exception as exc:
            self.error = str(exc)
            print(f"[警告] {self.error}")
            return
        self._tid = _kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, _HOTKEY_ID,
                                      mask | _MOD_NOREPEAT, vk):
            self.error = (f"热键 {self.spec} 注册失败，多半被其它程序占用了，"
                          f"改用按键轮询兜底")
            print(f"[提示] {self.error}")
            return
        self.ok = True
        msg = wintypes.MSG()
        try:
            while not self._stopping:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self.out_queue.put(True)      # 交给主线程去切换
        finally:
            _user32.UnregisterHotKey(None, _HOTKEY_ID)

    def stop(self):
        """取消注册并让阻塞中的 GetMessage 返回"""
        self._stopping = True
        if self._tid and IS_WINDOWS:
            _user32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)


# ============================================================
#  1. 配置区
# ============================================================

# ---- 两个时区 ----
ZONE_A_NAME = "北京时间"
ZONE_A_REGION = "Asia/Shanghai"
ZONE_B_NAME = "美西时间"
ZONE_B_REGION = "America/Los_Angeles"

# ---- 刷新与外观（这几项是你之前调过的值，保持不变）----
REFRESH_MS = 1000        # 刷新间隔（毫秒）
ALPHA = 0.85              # 整体透明度
# ⚠ 关于 ALPHA 与"颜色可辨识"的权衡（实测数据）：
#   -alpha 是【整个窗口】的透明度，文字也会一起变透明，所以它会放大
#   所有颜色的对比度损失。实测 WCAG 对比度（深色壁纸 / 浅色壁纸）：
#     ALPHA=0.5  →  1.76 ~ 3.45   ← 低于 3.0 时颜色很难分辨
#     ALPHA=0.85 →  3.62 ~ 7.22   ← 颜色清晰可辨（推荐）
#     ALPHA=1.0  →  4.86 ~ 10.85  ← 对比度最好，但窗口不透明了
#   想让节假日颜色真正"看得出来"，建议 ALPHA 设为 0.8 ~ 0.9。
#
#   如果你就是想要"背景几乎全透、文字完全不透明"的效果（另一种风格），
#   可以改用 chroma-key 方案代替 -alpha：
#       TRANSPARENT_KEY = "#010203"     # 一个不会出现在界面上的怪颜色
#       root.attributes("-transparentcolor", TRANSPARENT_KEY)
#       root.configure(bg=TRANSPARENT_KEY)   # 背景用这个颜色 → 完全透明
#       # 然后把 BG_COLOR 给内部的 Frame 用，文字就 100% 不透明、非常锐利
#   代价是失去"磨砂半透明面板"的观感，背景变成完全镂空。
# 描边色：本轮已按你的要求去掉灰色边框，这个常量不再使用。
# 如果你想恢复 1px 描边，把它取消注释，再把 _build_window 里的
# root.configure(bg=...) 改回 BORDER_COLOR、_build_widgets 里的
# pad.pack 改回 padx=1/pady=1 即可。
# BORDER_COLOR = "#4a4a4a"
BG_COLOR = "#1e1e1e"      # ★ 窗口背景（已恢复成实色深灰，不是透明键颜色）
FG_MAIN = "#ffffff"       # 主时间（大字）
FG_SUB = "#7fd1ff"        # 点缀色（现在只给右上角 ⇄ 按钮用）
FG_DIM = "#9a9a9a"        # 主区说明文字
FG_SILVER = "#c8ccd2"     # ★ 淡银色：底部副时钟的常规颜色（时区名/时间/普通日期）

FONT_BIG_TIME = ("Consolas", 36, "bold")
FONT_BIG_NAME = ("Microsoft YaHei UI", 11)
FONT_SMALL_TIME = ("Consolas", 15, "bold")
FONT_SMALL_NAME = ("Microsoft YaHei UI", 10)
FONT_DATE = ("Microsoft YaHei UI", 10)      # 日期专用字体
FONT_HINT = ("Microsoft YaHei UI", 9)       # 底部提示小字
FONT_BUTTON = ("Segoe UI Symbol", 12)

WEEKDAY_CN = "一二三四五六日"

# ---- ★ 特殊日期区间（优先级最高，命中就显示红色）----
# 格式必须是 "YYYY-MM-DD"；填错了程序会在控制台警告并自动忽略这个功能
SPECIAL_START = "2026-11-01"
SPECIAL_END = "2026-11-15"

# ---- ★ 日期文字颜色 ----
# 说明：窗口背景是深色 #1e1e1e，所以"深绿"不能用真正的深色
#       （比如 #14532d 在深色背景上几乎看不清）。
#       这里选的是一组在深色背景上可读、同时彼此区分明显的颜色。
COLOR_CN_HOLIDAY = "#00a04a"   # 中国法定节假日 —— 深绿
COLOR_CN_MAKEUP = "#ff9f1c"    # 中国调休上班日 —— 橙色
COLOR_CN_WEEKEND = "#9fe0a3"   # 中国普通周末 —— 浅绿
COLOR_US_HOLIDAY = "#4aa8ff"   # 美国主要节假日 —— 蓝色
COLOR_US_WEEKEND = "#9fe0a3"   # 美国周六/周日 —— 浅绿（默认和中国周末同色）
COLOR_SPECIAL = "#ff4d4f"      # 特殊日期区间 —— 红色（最高优先级）
COLOR_NORMAL = "#9a9a9a"       # 普通工作日 —— 灰色

# ---- ★ 休息时段（动态提示 + 时间变红）----
REST_START = 23                # 休息开始（时，24 小时制）
REST_END = 7                   # 休息结束（时）。允许跨零点，如 23 → 7

# 提示文字：按"是谁在休息"自动切换
#   大字（主时区）= 我方      小字（副时区）= 对方
REST_HINT_SELF = "🌙 我方可能正在休息"            # 只有主时区（大字）在休息
REST_HINT_OTHER = "🌙 对方可能正在休息"           # 只有副时区（小字）在休息
REST_HINT_BOTH = "🌙 我方和对方都可能正在休息"     # 两个都在休息时段内
# 如果 🌙 显示成"方框/乱码"（少数精简版 Windows 缺 emoji 字体），
# 把上面三行里的 🌙 去掉即可。

FG_REST_HINT = "#ffd75f"       # 提示文字本身的颜色（黄）
COLOR_REST = "#ff4d4f"         # 休息时段内的时间颜色（红）

# ---- 判定规则：红色直接绑"时间段"，两个时区各自判断、互不影响 ----
#   主时区的钟点落在 REST_START~REST_END 内 → 主时间变红
#   副时区的钟点落在 REST_START~REST_END 内 → 副时间变红
#   提示文字随"谁在休息"切换：我方 / 对方 / 双方
# 也就是说：红不红只看"这个时区现在是几点"，和它排在大字还是小字无关。

# ---- ★ 番茄钟休息提醒 ----
POMODORO_MINUTES = 45          # 默认工作时长（分钟）
POMODORO_REMINDER = "喝口水，休息一下眼睛吧！"
POMODORO_TITLE = "🍅 休息提醒"
POMODORO_BEEP_HZ = 1000        # "滴"的音高（Hz）
POMODORO_BEEP_MS = 300         # "滴"的时长（毫秒）
GEAR_TEXT = "⚙"                # 设置按钮上的字符
# 如果你的系统上 ⚙ 显示成方框，改成纯文字即可：GEAR_TEXT = "设"
# ⚙ 是符号字形，用 Segoe UI Symbol 比中文字体更保险（⇄ 也是用这个字体）
FONT_GEAR = ("Segoe UI Symbol", 10)

# ---- ★ 置顶 / 沉底 ----
# 全局快捷键：窗口没焦点、已沉底时也能用来唤醒。
# ★ 两个程序同时开着时**必须用不同的键**，否则一次按键会把两个窗口同时切走。
#   时钟用 1，流量监控用 2。
HOTKEY = "ctrl+shift+1"
PIN_TEXT_UP = "⬇"              # 置顶状态下按钮显示这个（点它 → 沉底）
PIN_TEXT_DOWN = "📌"            # 沉底状态下显示这个（点它 → 置顶）
HOTKEY_POLL_MS = 80            # 快捷键轮询间隔（毫秒）
# 如果 ⬇ / 📌 显示成方框，改成纯文字即可：PIN_TEXT_UP = "沉" / PIN_TEXT_DOWN = "顶"

# ---- 启动时在控制台打印一次判定结果，方便确认功能是否正常 ----
DEBUG_PRINT = True

# ============================================================
#  2. 美国主要节假日表（内置，无需联网、无需第三方库）
# ============================================================
# 规则说明：
#   - 固定日期的节日（元旦/独立日/圣诞等）如果正好落在周六，
#     美国会在前一个周五"观察"（observed）；落在周日则顺延到周一。
#     下表把这些"观察日"也一并列出，因为实际放假的是观察日。
#   - 浮动日期的节日（马丁路德金日/总统日/劳工节/感恩节等）
#     已按规则算好，直接写死。
#   - 目前收录 2026、2027 两年。到 2028 年需要手动补充（见文末注释）。
US_HOLIDAYS = {
    # ---------- 2026 年 ----------
    "2026-01-01": "元旦",
    "2026-01-19": "马丁·路德·金日",      # 1 月第 3 个周一
    "2026-02-16": "总统日",               # 2 月第 3 个周一
    "2026-05-25": "阵亡将士纪念日",        # 5 月最后一个周一
    "2026-06-19": "六月节",
    "2026-07-03": "独立日(观察日)",        # 7/4 是周六 → 提前到周五
    "2026-07-04": "独立日",
    "2026-09-07": "劳工节",               # 9 月第 1 个周一
    "2026-10-12": "哥伦布日",             # 10 月第 2 个周一
    "2026-11-11": "退伍军人节",
    "2026-11-26": "感恩节",               # 11 月第 4 个周四
    "2026-12-25": "圣诞节",
    # ---------- 2027 年 ----------
    "2027-01-01": "元旦",
    "2027-01-18": "马丁·路德·金日",
    "2027-02-15": "总统日",
    "2027-05-31": "阵亡将士纪念日",
    "2027-06-18": "六月节(观察日)",        # 6/19 是周六
    "2027-06-19": "六月节",
    "2027-07-04": "独立日",
    "2027-07-05": "独立日(观察日)",        # 7/4 是周日 → 顺延到周一
    "2027-09-06": "劳工节",
    "2027-10-11": "哥伦布日",
    "2027-11-11": "退伍军人节",
    "2027-11-25": "感恩节",
    "2027-12-24": "圣诞节(观察日)",        # 12/25 是周六
    "2027-12-25": "圣诞节",
    # 2028 年及以后：按上面的规则补两行即可，例如
    # "2028-01-01": "元旦",
    # "2028-01-17": "马丁·路德·金日",
}

# ---- chinese_calendar 返回的是英文节名，这里映射成中文显示 ----
CN_HOLIDAY_NAME = {
    "New Year's Day": "元旦",
    "Spring Festival": "春节",
    "Tomb-sweeping Day": "清明节",
    "Labour Day": "劳动节",
    "Dragon Boat Festival": "端午节",
    "Mid-autumn Festival": "中秋节",
    "National Day": "国庆节",
    "Anti-Fascist 70th Day": "抗战胜利纪念日",
}

# chinese_calendar 目前只覆盖这些年份，超出会抛 NotImplementedError
CN_CAL_YEAR_MIN, CN_CAL_YEAR_MAX = 2004, 2026

# ============================================================


def enable_dpi_awareness():
    """让窗口在高分屏上保持清晰。失败也不影响运行。"""
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def load_zone(region):
    """把 IANA 时区名转成 ZoneInfo；找不到时给人话提示。"""
    try:
        return ZoneInfo(region)
    except Exception:
        print("=" * 62)
        print(f"[错误] 找不到时区数据：{region}")
        print("原因：Windows 系统自身不带 IANA 时区数据库。")
        print("解决：在命令行执行  pip install tzdata  然后重新运行。")
        print("=" * 62)
        sys.exit(1)


def _parse_ymd(s):
    """把 'YYYY-MM-DD' 转成 date；格式错就返回 None（不抛异常）"""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        print(f"[警告] 特殊日期格式不对：{s!r}，应写成 'YYYY-MM-DD'，"
              f"本次忽略特殊区间功能。")
        return None


SPECIAL_START_D = _parse_ymd(SPECIAL_START)
SPECIAL_END_D = _parse_ymd(SPECIAL_END)


def in_special_range(d):
    """判断某天是否落在自定义特殊区间内（自动容忍起止写反的情况）"""
    if SPECIAL_START_D is None or SPECIAL_END_D is None:
        return False
    lo, hi = sorted((SPECIAL_START_D, SPECIAL_END_D))
    return lo <= d <= hi


# ------------------------------------------------------------
#  3. 核心：日期 → (颜色, 节日名称)
# ------------------------------------------------------------
def _cn_base_style(d):
    """
    中国日期的基础配色。返回 (颜色, 名称)，名称可能是空字符串。

    判定完全依赖 chinese_calendar.get_holiday_detail(d)，它返回 (是否放假, 节名)：
        (True,  'National Day')   → 法定节假日
        (False, 'National Day')   → ★ 调休上班日（周末但要上班）
        (True,  None)             → 普通周末
        (False, None)             → 普通工作日

    注意：不能用 is_in_lieu() 判断调休上班日！它的语义是
          "因法定节假日而休息的调休放假日"，不是"调休上班日"。
    """
    if not HAS_CN_CAL:
        # 没装库：退化成"只看是不是周末"
        return (COLOR_CN_WEEKEND if d.weekday() >= 5 else COLOR_NORMAL), ""

    try:
        on_holiday, name = chinese_calendar.get_holiday_detail(d)
    except NotImplementedError:
        # 年份超出库的数据范围（目前只到 2026）→ 也退化成周末判断
        return (COLOR_CN_WEEKEND if d.weekday() >= 5 else COLOR_NORMAL), ""

    if not on_holiday and name is not None:
        # 周末但被指定为上班日 = 调休上班日
        return COLOR_CN_MAKEUP, CN_HOLIDAY_NAME.get(name, name)
    if on_holiday and name is not None:
        # 有节日名称的休息日 = 法定节假日
        return COLOR_CN_HOLIDAY, CN_HOLIDAY_NAME.get(name, name)
    if on_holiday:
        # 没有任何节日名称的休息日 = 普通周末
        return COLOR_CN_WEEKEND, ""
    return COLOR_NORMAL, ""


def _us_base_style(d):
    """
    美国日期的基础配色。优先级：主要节假日(蓝) > 周六日(浅绿) > 普通(灰)。

    周末判断故意放在节假日之后 —— 比如 2026-07-04 独立日正好是周六，
    这时显示蓝色（节日优先），和中国那边"法定节假日优先于普通周末"一致。
    """
    name = US_HOLIDAYS.get(d.isoformat())
    if name:
        return COLOR_US_HOLIDAY, name
    if d.weekday() >= 5:                       # 5=周六, 6=周日
        return COLOR_US_WEEKEND, ""
    return COLOR_NORMAL, ""


def date_style(region, d):
    """
    统一的对外入口：按"时区"决定用哪套规则，
    然后在最外层套用特殊区间（红色优先级最高）。
    """
    if region == ZONE_A_REGION:
        color, name = _cn_base_style(d)
    elif region == ZONE_B_REGION:
        color, name = _us_base_style(d)
    else:
        color, name = COLOR_NORMAL, ""

    # ★ 最高优先级：特殊日期区间强制红色（名称信息保留）
    if in_special_range(d):
        color = COLOR_SPECIAL
    return color, name


def format_date(d, name):
    """拼出 '2026-10-16 周五 · 国庆节' 这样的显示文字"""
    text = f"{d:%Y-%m-%d} 周{WEEKDAY_CN[d.weekday()]}"
    return f"{text} · {name}" if name else text


class DualClock:
    """双时区时钟主类"""

    def __init__(self, root):
        self.root = root

        # 主/副时区放进列表，互换时只要 reverse() 一下
        self.slots = [
            {"name": ZONE_A_NAME, "region": ZONE_A_REGION,
             "tz": load_zone(ZONE_A_REGION)},
            {"name": ZONE_B_NAME, "region": ZONE_B_REGION,
             "tz": load_zone(ZONE_B_REGION)},
        ]

        self._drag_x = 0
        self._drag_y = 0

        # ---- 番茄钟状态 ----
        self.pomo_minutes = POMODORO_MINUTES   # 当前设定的工作时长（分钟）
        self.pomo_end = None                   # 目标时刻（time.monotonic）；None = 未运行
        self.pomo_reminder = None              # 当前提醒窗口对象

        # ---- 置顶 / 沉底状态 ----
        self.pinned = True                     # True = 置顶；False = 已沉底
        self.closing = False                   # 退出标志，用于停掉热键轮询
        self._hotkey_queue = queue.Queue()     # 热键线程 → 主线程 的事件队列
        self._hotkey_watcher = None
        self._hotkey_prev = False              # 兜底轮询用的边沿状态
        self._hotkey_armed_at = 0.0
        self._tick_job = None                  # after 定时器 id（退出时取消用）
        self._hotkey_job = None
        try:
            self._hotkey_vks = parse_hotkey(HOTKEY)
        except ValueError as exc:
            print(f"[警告] {exc}，全局快捷键已禁用")
            self._hotkey_vks = []

        self._build_window()
        self._build_widgets()
        self._lock_size_and_place()
        self.tick()
        self._start_hotkey()               # 启动全局快捷键

        if DEBUG_PRINT:
            self._print_diagnosis()

    # ---------------------------------------------------------
    def _build_window(self):
        root = self.root
        root.overrideredirect(True)        # 无边框
        root.attributes("-topmost", True)  # 始终置顶
        try:
            root.attributes("-alpha", ALPHA)   # 半透明
        except tk.TclError:
            pass
        # ★ 修复①：root 底色直接用面板背景色。
        #   这样即使窗口比内容宽一丢丢，多出来的区域也是面板色，
        #   绝不会再露出灰边。
        root.configure(bg=BG_COLOR)

        # ★ 修复②：删掉下面这行 —— 它就是"主背景被改成透明"的元凶。
        #   -transparentcolor 会把所有 BG_COLOR 像素抠成完全透明，
        #   于是整个面板镂空、只剩文字浮在壁纸上，而 root 的描边色
        #   反而露成左右两条灰条。想恢复实色背景，这行必须移除。
        # root.attributes("-transparentcolor", BG_COLOR)

        # 右键菜单：无边框窗口没有关闭按钮，这是主要的退出方式
        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="⇄  互换主/副时区", command=self.swap)
        self.menu.add_command(label="⬇/📌  沉底 / 置顶", command=self.toggle_pin)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self._quit)
        root.bind("<Button-3>", self._popup_menu)
        root.bind("<Escape>", lambda e: self._quit())

    # ---------------------------------------------------------
    def _build_widgets(self):
        # ★ 修复③：让内容帧【铺满整个窗口】，并去掉原来 1px 描边的留白。
        #   原来是 padx=1/pady=1 且不铺满，一旦窗口比内容宽一点，
        #   多出来的空间就露出 root 的底色 —— 也就是你截图里的左右灰条。
        #   现在 pad 覆盖整个窗口，root 的颜色永远不可能被看到。
        pad = tk.Frame(self.root, bg=BG_COLOR)
        pad.pack(fill="both", expand=True)

        # body 也铺满，让每行的左右元素分别贴住窗口两侧；
        # 这样中间多出来的宽度会自然变成"名称 ↔ 日期"之间的间距，
        # 而不是变成两侧的空边。
        body = tk.Frame(pad, bg=BG_COLOR)
        body.pack(fill="both", expand=True, padx=14, pady=10)

        # ---------- 第 1 行：主时区名 + 主日期 + 切换按钮 ----------
        row1 = tk.Frame(body, bg=BG_COLOR)
        row1.pack(fill="x")

        self.big_name = tk.Label(row1, text="", font=FONT_BIG_NAME,
                                 fg=FG_DIM, bg=BG_COLOR, anchor="w")
        self.big_name.pack(side="left")

        self.btn = tk.Button(row1, text="⇄", font=FONT_BUTTON,
                             fg=FG_SUB, bg=BG_COLOR,
                             activeforeground=FG_MAIN,
                             activebackground="#333333",
                             relief="flat", bd=0, highlightthickness=0,
                             cursor="hand2", width=3, command=self.swap)
        self.btn.pack(side="right")
        self.btn.bind("<Enter>", lambda e: self.btn.config(fg=FG_MAIN))
        self.btn.bind("<Leave>", lambda e: self.btn.config(fg=FG_SUB))

        # ★ 沉底 / 置顶按钮（放在 ⇄ 左边，不参与拖动，避免想点却拖走窗口）
        self.pin_btn = tk.Button(row1, text=self._pin_text(), font=FONT_GEAR,
                                 fg=FG_SUB, bg=BG_COLOR,
                                 activeforeground=FG_MAIN,
                                 activebackground="#333333",
                                 relief="flat", bd=0, highlightthickness=0,
                                 cursor="hand2", width=2,
                                 command=self.toggle_pin)
        self.pin_btn.pack(side="right")
        self.pin_btn.bind("<Enter>", lambda e: self.pin_btn.config(fg=FG_MAIN))
        self.pin_btn.bind("<Leave>", lambda e: self._update_pin_btn())

        # ★ 主日期：颜色随节假日变化
        self.date_label = tk.Label(row1, text="", font=FONT_DATE,
                                   fg=COLOR_NORMAL, bg=BG_COLOR, anchor="e")
        self.date_label.pack(side="right", padx=(0, 6))

        # ---------- 第 2 行：主时间（大字） + 右侧的休息提示 ----------
        row2 = tk.Frame(body, bg=BG_COLOR)
        row2.pack(fill="x")

        self.big_time = tk.Label(row2, text="", font=FONT_BIG_TIME,
                                 fg=FG_MAIN, bg=BG_COLOR, anchor="w")
        self.big_time.pack(side="left")

        # ★ 休息提示贴在主时间右侧。
        #   不在休息时间时文字为空，标签宽度归零，整行看不出任何痕迹。
        self.hint_label = tk.Label(row2, text="", font=FONT_HINT,
                                   fg=FG_REST_HINT, bg=BG_COLOR, anchor="e")
        self.hint_label.pack(side="right", padx=(8, 0))

        # ---------- 分隔线 ----------
        tk.Frame(body, bg="#3a3a3a", height=1).pack(fill="x", pady=(6, 4))

        # ---------- 第 3 行：副时区名 + 副日期 + 副时间 ----------
        row3 = tk.Frame(body, bg=BG_COLOR)
        row3.pack(fill="x")

        # ---- 底部副时钟整行统一用淡银色 ----
        # 没有特殊情况（节假日、休息时间等）时，时区名/时间/日期都是同一个
        # 淡银色，视觉上是一整块，不再出现"蓝时间 + 灰名称 + 彩色日期"的杂色。
        self.small_name = tk.Label(row3, text="", font=FONT_SMALL_NAME,
                                   fg=FG_SILVER, bg=BG_COLOR, anchor="w")
        self.small_name.pack(side="left")

        self.small_time = tk.Label(row3, text="", font=FONT_SMALL_TIME,
                                   fg=FG_SILVER, bg=BG_COLOR, anchor="e")
        self.small_time.pack(side="right")

        # ★ 副日期：默认也是淡银色；命中节假日规则时仍按规则变色
        #   （法定节假日深绿 / 调休橙 / 周末浅绿 / 美国节假日蓝 / 特殊区间红）
        self.sub_date = tk.Label(row3, text="", font=FONT_DATE,
                                 fg=FG_SILVER, bg=BG_COLOR, anchor="e")
        self.sub_date.pack(side="right", padx=(0, 10))

        # ---------- 第 4 行：倒计时 + ⚙ 设置按钮（右下角）----------
        # 休息提示已经移到第 2 行主时间右侧，这一行只留控制区
        row4 = tk.Frame(body, bg=BG_COLOR)
        row4.pack(fill="x", pady=(5, 0))

        # ★ 右下角：极小的设置按钮。
        #   pack 的规则是"先 pack 的靠最右"，所以按钮写在倒计时前面，
        #   最终排列为 [⏳ 45:00 ⚙]
        self.gear_btn = tk.Button(row4, text=GEAR_TEXT, font=FONT_GEAR,
                                  fg=FG_DIM, bg=BG_COLOR,
                                  activeforeground=FG_MAIN,
                                  activebackground="#333333",
                                  relief="flat", bd=0, highlightthickness=0,
                                  cursor="hand2", width=2,
                                  command=self._open_settings)
        self.gear_btn.pack(side="right")
        self.gear_btn.bind("<Enter>", lambda e: self.gear_btn.config(fg=FG_MAIN))
        self.gear_btn.bind("<Leave>", lambda e: self.gear_btn.config(fg=FG_DIM))

        # 番茄钟剩余时间，只有倒计时运行中才显示文字
        self.pomo_label = tk.Label(row4, text="", font=FONT_HINT,
                                   fg=FG_DIM, bg=BG_COLOR, anchor="e")
        self.pomo_label.pack(side="right", padx=(0, 4))

        # ---------- 让整块区域都能拖动（齿轮按钮除外：它是用来点的）----------
        for widget in (self.root, pad, body, row1, row2, row3, row4,
                       self.big_name, self.date_label, self.big_time,
                       self.hint_label, self.small_name, self.sub_date,
                       self.small_time, self.pomo_label):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)

    # ---------------------------------------------------------
    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _drag(self, event):
        new_x = self.root.winfo_x() + event.x - self._drag_x
        new_y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{new_x}+{new_y}")

    # ---------------------------------------------------------
    def _lock_size_and_place(self):
        """
        先用"最长可能"的样例文字把每个标签填满，量出最大需要的尺寸，
        再把窗口尺寸固定下来并摆到屏幕右上角。

        为什么要这样：
          1. 标签空着的时候窗口很窄，按那个宽度定位会把窗口摆到屏幕外
          2. 节假日名称会让日期文字变长，如果按普通日期量尺寸，
             以后显示"· 国庆节"时文字会被裁掉
          3. 尺寸固定住，切换主副时区、显示深夜提示时窗口都不会跳动
        """
        longest_name = max(
            (f'{s["name"]} · {s["region"]}' for s in self.slots), key=len)
        sample_date = "2026-12-31 周六 · 端午节"      # 最长形态的日期文字

        self.big_name.config(text=longest_name)
        self.small_name.config(text=longest_name)
        self.date_label.config(text=sample_date)
        self.sub_date.config(text=sample_date)
        self.big_time.config(text="00:00:00")
        self.small_time.config(text="00:00:00")
        # 用最长的那个提示文案来量宽度，保证三种文案都不会被裁掉
        self.hint_label.config(text=REST_HINT_BOTH)
        # 倒计时文字也先占好位置，否则它一出现/消失就会挤压同一行的其他元素
        self.pomo_label.config(text="⏳ 88:88")

        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        x = self.root.winfo_screenwidth() - w - 40
        y = 40
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.update()      # 立刻换成真实内容
        self.pomo_label.config(text="")    # 番茄钟初始未启动，显示为空

    # ---------------------------------------------------------
    def _popup_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---------------------------------------------------------
    def swap(self):
        """互换主 / 副时区。颜色是跟着"时区"走的，不会跟着位置走"""
        self.slots.reverse()
        self.update()

    # ---------------------------------------------------------
    def update(self):
        """刷新一次界面（不负责循环），颜色逻辑全在这里"""
        now_main = datetime.now(self.slots[0]["tz"])
        now_sub = datetime.now(self.slots[1]["tz"])

        # ★ 两个时区各自判断：谁的钟点落在休息区间，谁的时间就变红
        rest_main = self._hour_in_rest(now_main.hour)   # 我方（大字）
        rest_sub = self._hour_in_rest(now_sub.hour)     # 对方（小字）

        # ---- 主区（大字）----
        color, name = date_style(self.slots[0]["region"], now_main.date())
        # 主时区的钟点落在休息区间 → 大时间变红（不管它是哪个城市）
        self.big_time.config(text=now_main.strftime("%H:%M:%S"),
                             fg=COLOR_REST if rest_main else FG_MAIN)
        self.big_name.config(
            text=f'{self.slots[0]["name"]} · {self.slots[0]["region"]}')
        self.date_label.config(text=format_date(now_main.date(), name),
                               fg=color)

        # ---- 副区（小字）：整行统一淡银色 ----
        color, name = date_style(self.slots[1]["region"], now_sub.date())
        # 常规日（规则返回 COLOR_NORMAL）→ 改用淡银色，
        # 和左边的时区名、右边的副时间保持一致，整行才"统一"。
        # 节假日、特殊日期区间等"其他情况"仍保留各自的规则色，不受影响。
        if color == COLOR_NORMAL:
            color = FG_SILVER
        # 副时区的钟点落在休息区间 → 副时间变红（名称和日期仍是淡银）
        self.small_time.config(text=now_sub.strftime("%H:%M:%S"),
                               fg=COLOR_REST if rest_sub else FG_SILVER)
        self.small_name.config(
            text=f'{self.slots[1]["name"]} · {self.slots[1]["region"]}',
            fg=FG_SILVER)
        self.sub_date.config(text=format_date(now_sub.date(), name), fg=color)

        # ---- 休息提示：文字按"谁在休息"切换，贴在主时间右侧 ----
        if rest_main and rest_sub:
            hint_text = REST_HINT_BOTH      # 两个都在休息时段内
        elif rest_main:
            hint_text = REST_HINT_SELF      # 只有我方（大字）在休息
        elif rest_sub:
            hint_text = REST_HINT_OTHER     # 只有对方（小字）在休息
        else:
            hint_text = ""                  # 都醒着 → 不显示
        self.hint_label.config(text=hint_text)

    # ---------------------------------------------------------
    #  休息时段判定（主/副时区各自独立判断）
    # ---------------------------------------------------------
    @staticmethod
    def _hour_in_rest(hour):
        """
        判断某个小时是否落在休息区间内。
        支持跨零点：REST_START=23 / REST_END=7 表示 23:00 ~ 次日 07:00。
        """
        if REST_START == REST_END:
            return False                        # 起止相同 = 不启用
        if REST_START < REST_END:
            return REST_START <= hour < REST_END
        return hour >= REST_START or hour < REST_END

    def tick(self):
        """每秒刷新一次，然后预约下一次（绝不能用 while + sleep）"""
        self.update()
        self._tick_pomodoro()      # 顺便推进番茄倒计时
        self._tick_job = self.root.after(REFRESH_MS, self.tick)

    # =========================================================
    #  番茄钟：倒计时 / 提醒 / 设置
    # =========================================================
    def _start_pomodoro(self, minutes=None):
        """开始（或重新开始）一轮工作倒计时"""
        if minutes is not None:
            self.pomo_minutes = int(minutes)
        if self.pomo_minutes <= 0:
            self._stop_pomodoro()
            return
        # ★ 关键：记的是"目标时刻"，不是每秒减 1。
        #   这样即使拖动窗口、系统卡顿导致中间漏掉几秒刷新，
        #   剩余时间也不会累积误差。
        self.pomo_end = time.monotonic() + self.pomo_minutes * 60
        self._refresh_pomo_label()

    def _stop_pomodoro(self):
        """停止倒计时并清空显示"""
        self.pomo_end = None
        self.pomo_label.config(text="")

    def _refresh_pomo_label(self):
        """把剩余时间画到右下角"""
        if self.pomo_end is None:
            self.pomo_label.config(text="")
            return
        rem = self.pomo_end - time.monotonic()
        if rem <= 0:
            self.pomo_label.config(text="")
            return
        total = int(rem + 0.999)          # 向上取整，避免最后 1 秒显示 00:00
        m, s = divmod(total, 60)
        self.pomo_label.config(text=f"⏳ {m:02d}:{s:02d}")

    def _tick_pomodoro(self):
        """由 tick() 每秒调用一次"""
        if self.pomo_end is None:
            return
        if time.monotonic() >= self.pomo_end:
            self.pomo_end = None          # 先熄火，防止重复触发提醒
            self.pomo_label.config(text="")
            self._fire_pomodoro()
            return
        self._refresh_pomo_label()

    def _fire_pomodoro(self):
        """倒计时结束：响一声 + 弹提醒窗"""
        self._play_beep()
        self._show_reminder()

    def _play_beep(self):
        """
        播放一声"滴"。
        winsound.Beep 是同步阻塞的（会卡住界面 POMODORO_BEEP_MS 毫秒），
        所以丢到后台线程去播；非 Windows 系统直接静默跳过。
        """
        if not HAS_WINSOUND:
            return

        def worker():
            try:
                winsound.Beep(POMODORO_BEEP_HZ, POMODORO_BEEP_MS)
            except Exception:
                try:
                    winsound.MessageBeep()        # Beep 不可用就退化成系统提示音
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _show_reminder(self):
        """弹出简短的休息提醒窗口"""
        # 已经开着一个提醒窗就不重复弹，只把它提到最前
        if self.pomo_reminder is not None:
            try:
                if self.pomo_reminder.winfo_exists():
                    self.pomo_reminder.lift()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        self.pomo_reminder = win
        win.title(POMODORO_TITLE)
        win.configure(bg=BG_COLOR)
        win.resizable(False, False)
        win.attributes("-topmost", True)      # 时钟也是置顶的，这里要盖过它
        win.protocol("WM_DELETE_WINDOW", self._close_reminder)

        tk.Label(win, text=POMODORO_REMINDER,
                 font=("Microsoft YaHei UI", 13),
                 fg=FG_MAIN, bg=BG_COLOR, padx=26, pady=20).pack()

        tk.Button(win, text="好的", font=FONT_SMALL_NAME,
                  fg="#ffffff", bg="#2f6f4f", activebackground="#3a8a63",
                  activeforeground="#ffffff", relief="flat", bd=0,
                  padx=22, pady=3, cursor="hand2",
                  command=self._close_reminder).pack(pady=(0, 16))

        win.bind("<Return>", lambda e: self._close_reminder())
        win.bind("<Escape>", lambda e: self._close_reminder())

        # 居中显示到屏幕
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.focus_force()

    def _close_reminder(self):
        """关掉提醒窗，并 ★ 自动重置倒计时开始下一轮"""
        if self.pomo_reminder is not None:
            try:
                self.pomo_reminder.destroy()
            except Exception:
                pass
            self.pomo_reminder = None
        self._start_pomodoro()          # ← "提醒完自动重置倒计时"

    def _open_settings(self):
        """点击 ⚙ 弹出番茄钟设置窗口"""
        dlg = tk.Toplevel(self.root)
        dlg.title("番茄钟设置")
        dlg.configure(bg=BG_COLOR)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)

        tk.Label(dlg, text="工作倒计时（分钟）", font=FONT_SMALL_NAME,
                 fg=FG_SILVER, bg=BG_COLOR).pack(padx=18, pady=(14, 6))

        init = self.pomo_minutes if self.pomo_minutes > 0 else POMODORO_MINUTES
        var = tk.StringVar(value=str(init))
        entry = tk.Entry(dlg, textvariable=var, width=8, justify="center",
                         font=FONT_SMALL_TIME, bg="#2b2b2b", fg=FG_MAIN,
                         insertbackground=FG_MAIN, relief="flat")
        entry.pack(pady=(0, 10))
        entry.focus_set()
        entry.select_range(0, "end")

        # 常用预设：点一下直接填进输入框
        preset_row = tk.Frame(dlg, bg=BG_COLOR)
        preset_row.pack(pady=(0, 12))
        for mins in (25, 45, 60, 90):
            tk.Button(preset_row, text=str(mins), font=FONT_HINT,
                      fg=FG_SILVER, bg="#2b2b2b", activebackground="#3a3a3a",
                      activeforeground=FG_MAIN, relief="flat", bd=0,
                      width=4, cursor="hand2",
                      command=lambda m=mins: var.set(str(m))
                      ).pack(side="left", padx=2)

        def on_ok(event=None):
            try:
                n = int(var.get().strip())
            except (ValueError, AttributeError):
                n = 0
            if n <= 0:
                self._stop_pomodoro()          # 填 0 或乱填 = 关闭倒计时
            else:
                self._start_pomodoro(min(n, 600))   # 上限 10 小时，防手滑多打一个 0
            dlg.destroy()

        def on_stop():
            self._stop_pomodoro()
            dlg.destroy()

        btn_row = tk.Frame(dlg, bg=BG_COLOR)
        btn_row.pack(pady=(0, 14))
        for text, fg, bg, cb in (
                ("停止", "#ff9f1c", "#2b2b2b", on_stop),
                ("取消", FG_SILVER, "#2b2b2b", dlg.destroy),
                ("确定", "#ffffff", "#2f6f4f", on_ok)):
            tk.Button(btn_row, text=text, font=FONT_HINT, fg=fg, bg=bg,
                      activebackground="#3a3a3a", activeforeground=FG_MAIN,
                      relief="flat", bd=0, width=6, cursor="hand2",
                      command=cb).pack(side="left", padx=3)

        dlg.bind("<Return>", on_ok)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        # 摆在时钟窗口的左下方，尽量不挡住时钟本体
        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        x = self.root.winfo_x() + self.root.winfo_width() - w
        y = self.root.winfo_y() + self.root.winfo_height() + 8
        x = max(10, min(x, dlg.winfo_screenwidth() - w - 10))
        y = max(10, min(y, dlg.winfo_screenheight() - h - 10))
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        dlg.focus_force()

    # =========================================================
    #  置顶 / 沉底
    # =========================================================
    def _pin_text(self):
        """按钮图标：置顶时显示 ⬇（点它沉底），沉底时显示 📌（点它置顶）"""
        return PIN_TEXT_UP if self.pinned else PIN_TEXT_DOWN

    def _update_pin_btn(self):
        try:
            self.pin_btn.config(text=self._pin_text(),
                                fg=FG_SUB if self.pinned else FG_DIM)
        except Exception:
            pass

    def toggle_pin(self):
        """按钮和全局快捷键共用的开关"""
        if self.pinned:
            self.sink_to_bottom()
        else:
            self.raise_to_top()

    def sink_to_bottom(self):
        """
        沉到所有窗口的底层。

        顺序很重要：★ 必须先在 Tk 层取消 -topmost，再用 HWND_BOTTOM。
        因为 topmost 是"置顶层"的属性，不取消的话窗口仍留在置顶层里，
        HWND_BOTTOM 只能把它排到置顶层的最底 —— 依旧盖着普通窗口。
        """
        self.pinned = False
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass
        if IS_WINDOWS:
            hwnd = get_hwnd(self.root)
            if not win_set_pos(hwnd, _HWND_BOTTOM,
                               _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE):
                # 极少数情况会失败，去掉 NOACTIVATE 再试一次
                win_set_pos(hwnd, _HWND_BOTTOM, _SWP_NOMOVE | _SWP_NOSIZE)
        self._update_pin_btn()

    def raise_to_top(self):
        """重新置顶并显示到最前面（全局快捷键唤醒走这里）"""
        self.pinned = True
        try:
            self.root.deiconify()               # 万一被隐藏过，先恢复显示
        except Exception:
            pass
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass
        if IS_WINDOWS:
            hwnd = get_hwnd(self.root)
            win_set_pos(hwnd, _HWND_TOPMOST,
                        _SWP_NOMOVE | _SWP_NOSIZE
                        | _SWP_SHOWWINDOW | _SWP_NOACTIVATE)
        try:
            self.root.lift()                    # 双保险
        except Exception:
            pass
        self._update_pin_btn()

    def _start_hotkey(self):
        """启动全局快捷键：优先 RegisterHotKey，注册失败才退回轮询"""
        if not IS_WINDOWS or not self._hotkey_vks:
            return
        self._hotkey_watcher = HotkeyWatcher(HOTKEY, self._hotkey_queue)
        self._hotkey_watcher.start()
        # 给注册线程一点时间，过了宽限期再判断是否要轮询兜底
        self._hotkey_armed_at = time.monotonic() + 0.4
        self.root.after(HOTKEY_POLL_MS, self._poll_hotkey)

    def _poll_hotkey(self):
        """
        主路径：RegisterHotKey 线程收到按键就往队列里放，这里取出来切换。
                （窗口沉底、没有焦点时照样有效，这是唤醒窗口的唯一手段）
        兜底：万一注册失败（键被别的程序占用），退回按键轮询。
        """
        if self.closing:
            return
        try:
            # 1) 主路径：处理系统投递过来的热键事件
            while True:
                try:
                    self._hotkey_queue.get_nowait()
                except queue.Empty:
                    break
                self.toggle_pin()

            # 2) 兜底轮询：仅在注册失败、且已过宽限期时启用
            w = self._hotkey_watcher
            if (w is not None and not w.ok
                    and time.monotonic() >= self._hotkey_armed_at
                    and self._hotkey_vks):
                states = [key_state(vk) for vk in self._hotkey_vks]
                held = all(s & 0x8000 for s in states)
                if held and not self._hotkey_prev:
                    self.toggle_pin()
                self._hotkey_prev = held
        except Exception:
            pass
        try:
            self._hotkey_job = self.root.after(HOTKEY_POLL_MS, self._poll_hotkey)
        except Exception:
            pass                                # 窗口已销毁

    def _quit(self):
        """统一退出：先停掉快捷键、取消定时器，再销毁窗口"""
        if self.closing:
            return
        self.closing = True
        try:
            if self._hotkey_watcher is not None:
                self._hotkey_watcher.stop()     # 注销热键，别占着键
        except Exception:
            pass
        # 取消还没执行的 after 回调，否则销毁后 Tk 会打
        # "invalid command name ..." 的警告
        for job in (self._tick_job, self._hotkey_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._tick_job = self._hotkey_job = None
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------------------------------------------------------
    def _print_diagnosis(self):
        """启动时在控制台打印判定结果，方便确认节假日功能是否正常"""
        print("=" * 60)
        print("chinese_calendar:",
              "已加载" if HAS_CN_CAL else "未安装（pip install chinesecalendar）")
        if HAS_CN_CAL and not (CN_CAL_YEAR_MIN <= date.today().year
                               <= CN_CAL_YEAR_MAX):
            print(f"  ⚠ 该库目前只覆盖 {CN_CAL_YEAR_MIN}~{CN_CAL_YEAR_MAX} 年，"
                  f"当前年份超出范围，中国节假日将退化为周末判断。")
        print(f"特殊日期区间: {SPECIAL_START} ~ {SPECIAL_END} "
              f"({'已生效' if SPECIAL_START_D and SPECIAL_END_D else '未生效'})")
        for slot in self.slots:
            d = datetime.now(slot["tz"]).date()
            color, name = date_style(slot["region"], d)
            print(f"  {slot['name']:6s} {format_date(d, name):32s} → {color}")
        # 休息时段状态：两个时区各自判断
        h_main = datetime.now(self.slots[0]["tz"]).hour
        h_sub = datetime.now(self.slots[1]["tz"]).hour
        r_main = self._hour_in_rest(h_main)
        r_sub = self._hour_in_rest(h_sub)
        if r_main and r_sub:
            hint = REST_HINT_BOTH
        elif r_main:
            hint = REST_HINT_SELF
        elif r_sub:
            hint = REST_HINT_OTHER
        else:
            hint = "(不显示)"
        print(f"休息时段: {REST_START}:00 ~ {REST_END}:00  "
              f"我方/主({h_main:02d}点){'红' if r_main else '正常'}  "
              f"对方/副({h_sub:02d}点){'红' if r_sub else '正常'}  "
              f"提示={hint}")
        print("=" * 60)


def main():
    enable_dpi_awareness()      # 必须在创建 Tk() 之前
    root = tk.Tk()
    DualClock(root)
    root.mainloop()


if __name__ == "__main__":
    main()