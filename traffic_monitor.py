#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================
 远程服务器流量监控悬浮窗        Remote Traffic Monitor
================================================================
 功能：
   - 用 paramiko 通过 SSH 读取服务器 /proc/net/dev 里 eth0 的流量
   - 无边框、半透明、可鼠标拖拽的桌面悬浮窗
   - 显示：下载总量 / 上传总量 / 节点连接状态
   - 每 10 秒自动刷新一次
   - 右键菜单：立即刷新 / 重新连接 / 查看错误详情 / 退出

 ★★ 核心设计：一条长连接，只握手一次 ★★
   本程序【没有】使用 paramiko.SSHClient.connect()，因为那样每调用一次
   就会新建 TCP 连接 + 重新做完整的 SSH 握手和密钥认证。
   改成用 paramiko.Transport 建立一条长连接后反复复用：
       启动时  → 建 TCP → SSH 握手 → 密钥认证  （全程只有这一次）
       每 10 秒 → 复用上面那条连接开一个 channel 执行命令（零认证）
   这样服务器上的 sshd 只会看到 1 次登录记录，Fail2ban 完全不会被触发。

 运行环境：Windows + Python 3.8 以上
 安装依赖：pip install paramiko
================================================================
"""

import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog

# ------------------------------------------------------------------
# 导入 paramiko
# ------------------------------------------------------------------
# ★ 这里【故意】用 except Exception 而不是 except ImportError：
#   打包后最常出问题的不是"没安装"，而是 cryptography 的 DLL 加载失败，
#   那种情况抛的是 OSError（DLL load failed），用 ImportError 根本接不住。
#   而且必须把真实异常显示出来，否则只会误导成"没装 paramiko"。
try:
    import paramiko
except Exception as _exc:                  # noqa: BLE001
    import traceback
    _detail = traceback.format_exc()

    # 把完整堆栈写到 exe（或脚本）同目录，方便事后排查
    try:
        _base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                 else os.path.dirname(os.path.abspath(__file__)))
        _log_path = os.path.join(_base, "import_error.log")
        with open(_log_path, "w", encoding="utf-8") as _f:
            _f.write(_detail)
    except Exception:
        _log_path = "(日志写入失败)"

    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "依赖加载失败",
        f"paramiko 存在但导入失败。\n\n"
        f"真实原因：{type(_exc).__name__}: {_exc}\n\n"
        f"完整堆栈已写入：\n{_log_path}\n\n"
        f"常见原因：\n"
        f"  · 打包缓存没清（删掉 build/dist 重新打包）\n"
        f"  · 系统缺 Visual C++ 运行库\n"
        f"  · 打包时漏了 --collect-all cryptography")
    sys.exit(1)

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
    把 'ctrl+shift+2' 解析成一串虚拟键码（VK）。
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
            vks.append(0x30 + int(part))            # '2' -> 0x32
        elif len(part) == 1 and "a" <= part <= "z":
            vks.append(ord(part.upper()))           # 'd' -> 0x44
        else:
            raise ValueError(f'快捷键格式不对：{part!r}（示例 "ctrl+shift+2"）')
    return vks


# ---- RegisterHotKey：系统级注册，按下瞬间由系统投递消息，100% 不会漏 ----
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 0x5A18

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
    """把 'ctrl+shift+2' 解析成 (修饰键掩码, 主键VK)，给 RegisterHotKey 用"""
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
            raise ValueError(f'快捷键格式不对：{part!r}（示例 "ctrl+shift+2"）')
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
#  1. 配置区：改这里就能换服务器 / 换外观
# ============================================================

# ★★ 使用前必改：把下面 YOUR_ 开头的值换成你自己的 ★★
HOST = "YOUR_SERVER_IP"         # 服务器 IP，例如 203.0.113.10
PORT = "YOUR_SSH_PORT"          # SSH 端口，例如 2222（换成数字后可以去掉引号）
USER = "YOUR_SSH_USER"          # 登录用户名，例如 root
KEY_NAME = "id_ed25519"         # 私钥文件名
# 私钥路径（只是"兜底"候选之一，真正的查找顺序见 find_key_file()）
# ★ 私钥永远不要提交到仓库，也不要打包进 exe
KEY_FILE_HINT = os.path.join(os.path.expanduser("~"), ".ssh", KEY_NAME)

IFACE = "eth0"                  # 要监控的网卡名

REFRESH_SEC = 10                # 刷新间隔（秒）
CONNECT_TIMEOUT = 10            # SSH 建连 / 认证超时（秒）
CMD_TIMEOUT = 10                # 单条命令的执行超时（秒）
KEEPALIVE_SEC = 30              # SSH 心跳间隔（秒）
MAX_BACKOFF_SEC = 300           # 连续失败时最长退避到 5 分钟，避免密集重连

# ---- 外观 ----
BORDER_COLOR = "#4a4a4a"        # 窗口 1 像素描边
BG_COLOR = "#1a1a1a"            # 背景
FG_TITLE = "#dddddd"
FG_LABEL = "#9a9a9a"
FG_DOWN = "#57d9a3"             # 下载：绿
FG_UP = "#ffb86b"               # 上传：橙
FG_OK = "#57d9a3"               # 状态：已连接（绿）
FG_WARN = "#ffd75f"             # 状态：连接中 / 重试中（黄）
FG_BAD = "#ff6b6b"              # 状态：已断开（红）
FG_DIM = "#777777"

FONT_TITLE = ("Microsoft YaHei UI", 10)
FONT_STATUS = ("Microsoft YaHei UI", 9)
FONT_LABEL = ("Microsoft YaHei UI", 10)
FONT_VALUE = ("Consolas", 15, "bold")     # 等宽：数字变化时宽度不跳
FONT_SMALL = ("Consolas", 9)
FONT_PIN = ("Segoe UI Symbol", 10)      # ⬇ / 📌 用符号字体更保险

# ---- ★ 置顶 / 沉底 ----
# 全局快捷键：窗口没焦点、已沉底时也能用来唤醒。
# ★ 两个程序同时开着时必须用不同的键（时钟 ctrl+shift+1，本程序 ctrl+shift+2），
#   否则一次按键会把两个窗口同时切走。
HOTKEY = "ctrl+shift+2"
PIN_TEXT_UP = "⬇"              # 置顶状态下显示这个（点它 → 沉底）
PIN_TEXT_DOWN = "📌"            # 沉底状态下显示这个（点它 → 置顶）
HOTKEY_POLL_MS = 80            # 快捷键轮询间隔（毫秒）
# 如果 ⬇ / 📌 显示成方框，改成纯文字即可：PIN_TEXT_UP = "沉" / PIN_TEXT_DOWN = "顶"

# ============================================================


def enable_dpi_awareness():
    """让窗口在高分屏（125%/150% 缩放）上保持清晰。失败也不影响运行。"""
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def human_bytes(n):
    """把字节数变成人能读的单位（1024 进制）"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0


def parse_net_dev(text, iface):
    """
    从 /proc/net/dev 的内容里取出指定网卡的 (接收字节数, 发送字节数)。

    /proc/net/dev 每行的格式是：
        网卡名:  接收字节 包数 错误 丢弃 ...  发送字节 包数 ...
                  ↑ 第 1 个字段            ↑ 第 9 个字段
    """
    for line in text.splitlines():
        if ":" not in line:
            continue                        # 跳过前两行表头
        name, _, rest = line.partition(":")
        if name.strip() != iface:
            continue
        nums = rest.split()
        if len(nums) < 9:
            continue
        return int(nums[0]), int(nums[8])   # (rx_bytes, tx_bytes)
    raise ValueError(f"/proc/net/dev 里没有找到网卡 {iface}，"
                     f"请用 ip -o -4 addr show 确认真实网卡名")


def find_key_file():
    """
    按优先级查找私钥文件，返回路径；全都找不到返回 None。

    ★ 为什么不能只写一个相对路径？
      打包成 --onefile 的 exe 后，每次运行都会把自身内容解压到一个
      临时目录（sys._MEIPASS），当前工作目录完全不可靠，
      写 "id_ed25519" 这种相对路径 100% 找不到文件。
      所以这里用「exe 同级目录 → 用户 .ssh 目录 → 写死的绝对路径」
      依次尝试，只要有一处存在就能用。
    """
    if getattr(sys, "frozen", False):
        # 打包后：sys.executable 就是 exe 自身的完整路径
        base = os.path.dirname(sys.executable)
    else:
        # 未打包：脚本所在目录
        base = os.path.dirname(os.path.abspath(__file__))

    candidates = [
        os.path.join(base, KEY_NAME),                              # exe 同级
        os.path.join(base, "key", KEY_NAME),                       # exe/key/ 子目录
        os.path.join(os.path.expanduser("~"), ".ssh", KEY_NAME),   # 用户 .ssh
        KEY_FILE_HINT,                                             # 兜底绝对路径
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_private_key(path, passphrase=None):
    """
    尝试用各种算法加载私钥。私钥带密码时会抛 PasswordRequiredException，
    交给调用方去弹窗询问，不要在这里吞掉。
    """
    errors = []
    for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_cls.from_private_key_file(path, password=passphrase)
        except paramiko.PasswordRequiredException:
            # 注意：它是 SSHException 的子类，必须放在下面那个 except 前面，
            # 否则会被当成"格式不对"而误吞
            raise
        except paramiko.SSHException as exc:
            errors.append(f"{key_cls.__name__}: {exc}")
    raise paramiko.SSHException("无法识别该私钥格式：\n" + "\n".join(errors))


# ============================================================
#  2. SSH 长连接封装
# ============================================================
class SSHTraffic:
    """
    封装一条可复用的 SSH 长连接。

    对比一下就明白区别了：
      ✗ SSHClient.connect()  —— 每次调用都重新 TCP + 握手 + 认证
      ✓ Transport + open_session —— 只在 connect() 里认证一次，
                                   之后 run() 复用同一条连接
    """

    def __init__(self, host, port, user, pkey):
        self.host = host
        self.port = port
        self.user = user
        self.pkey = pkey            # PKey 对象可以反复用于重连
        self.transport = None

    # ----------------------------------------------------------
    def connect(self):
        """建立 TCP + SSH 握手 + 密钥认证。★ 整个程序里只有这里会认证"""
        self.close()                # 先确保旧连接已清掉

        # 1) 建 TCP 连接
        sock = socket.create_connection((self.host, self.port),
                                        timeout=CONNECT_TIMEOUT)
        # 建连后交回阻塞模式：超时统一由下面的 Channel.settimeout 控制，
        # 否则长连接在空闲时会被这个 socket 超时误伤
        sock.settimeout(None)

        # 2) 在 socket 上建 Transport（这就是长连接的载体）
        transport = paramiko.Transport(sock)

        # 3) 心跳：每 30 秒发一次，防止 NAT / 防火墙把空闲连接静默掐断
        transport.set_keepalive(KEEPALIVE_SEC)

        # 4) SSH 协议握手
        transport.start_client(timeout=CONNECT_TIMEOUT)

        # 5) 密钥认证 —— ★ 全程唯一的一次认证
        transport.auth_publickey(self.user, self.pkey)

        self.transport = transport

    # ----------------------------------------------------------
    def is_connected(self):
        """连接是否还活着"""
        return self.transport is not None and self.transport.is_active()

    # ----------------------------------------------------------
    def run(self, command):
        """
        复用已有连接执行一条命令并返回它的标准输出。
        ★ 这里只开一个 channel，不产生任何新的 SSH 握手或认证。
        """
        if not self.is_connected():
            raise RuntimeError("SSH 连接不可用")

        chan = self.transport.open_session()
        try:
            chan.settimeout(CMD_TIMEOUT)
            chan.exec_command(command)

            # 手动收数据，比 makefile().read() 更可控，不会卡死
            chunks = []
            deadline = time.monotonic() + CMD_TIMEOUT
            while time.monotonic() < deadline:
                if chan.recv_ready():
                    chunks.append(chan.recv(65536))
                    continue
                if chan.exit_status_ready():
                    break
                time.sleep(0.02)        # 让出 CPU，别空转
            else:
                raise TimeoutError(f"命令执行超时（{CMD_TIMEOUT} 秒）")

            # 退出前把缓冲区里剩的数据也收干净
            while chan.recv_ready():
                chunks.append(chan.recv(65536))

            exit_code = chan.recv_exit_status()
            if exit_code != 0:
                err = b""
                while chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                raise RuntimeError(f"远程命令返回 {exit_code}："
                                   f"{err.decode('utf-8', 'replace').strip()}")
        finally:
            chan.close()

        return b"".join(chunks).decode("utf-8", "replace")

    # ----------------------------------------------------------
    def close(self):
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None


# ============================================================
#  3. 后台采集线程
# ============================================================
class TrafficWorker(threading.Thread):
    """
    后台线程：负责 SSH 连接和数据采集。

    ★ 为什么一定要放后台线程？
      1. SSH 读取是阻塞操作，网络不好时可能等好几秒。放在 tkinter 主线程里，
         窗口在这几秒内会彻底卡死（变成"无响应"）。
      2. tkinter 不是线程安全的：子线程【只能】往 queue 里塞数据，
         绝对不能直接改 Label 的文字。改界面必须回到主线程做。
    """

    def __init__(self, ssh, iface, interval, out_queue):
        super().__init__(daemon=True)   # daemon：主程序退出时它自动结束
        self.ssh = ssh
        self.iface = iface
        self.interval = interval
        self.out_queue = out_queue
        self.wake = threading.Event()      # 用来"立刻刷新"或"立刻退出"
        self.stopped = threading.Event()
        self._last = None                  # 上一次的 (rx, tx)，用来算增量

    def put(self, msg):
        self.out_queue.put(msg)

    # ----------------------------------------------------------
    def run(self):
        failures = 0

        while not self.stopped.is_set():
            delay = self.interval
            try:
                # 没连上就（重新）连一次
                if not self.ssh.is_connected():
                    self.put(("status", "连接中", FG_WARN))
                    self.ssh.connect()          # ★ 只有这里会做 SSH 握手
                    self.put(("status", "已连接", FG_OK))

                text = self.ssh.run("cat /proc/net/dev")
                rx, tx = parse_net_dev(text, self.iface)

                # 算这一个周期的增量（服务器重启会让计数器归零，负数按 0 处理）
                drx = dtx = 0
                if self._last is not None:
                    drx = max(0, rx - self._last[0])
                    dtx = max(0, tx - self._last[1])
                self._last = (rx, tx)

                self.put(("data", rx, tx, drx, dtx))
                failures = 0                    # 成功就重置退避

            except Exception as exc:
                failures += 1
                self.ssh.close()                # 出错就丢掉这条连接，下次重建
                self.put(("error", f"{type(exc).__name__}: {exc}"))

                # ★ 退避：连续失败时把重试间隔指数级拉长（10→20→40…最长 5 分钟），
                #   绝不允许"每秒狂连"，那是把自己送进 Fail2ban 的最快方式
                delay = min(self.interval * (2 ** min(failures, 5)),
                            MAX_BACKOFF_SEC)
                self.put(("status", f"重试中 {int(delay)}s", FG_BAD))

            # 等下一次刷新；期间如果被 refresh_now()/stop() 叫醒就立刻继续
            self.wake.wait(delay)
            self.wake.clear()

    # ----------------------------------------------------------
    def refresh_now(self):
        """让线程立刻刷新一次，不用等定时到点"""
        self.wake.set()

    def stop(self):
        self.stopped.set()
        self.wake.set()


# ============================================================
#  4. 悬浮窗界面
# ============================================================
class TrafficMonitorApp:

    def __init__(self, root):
        self.root = root
        self.ssh = None
        self.worker = None
        self.closing = False
        self.last_error = "（暂无错误）"
        self.q = queue.Queue()

        # ---- 置顶 / 沉底状态 ----
        self.pinned = True                     # True = 置顶；False = 已沉底
        self._hotkey_queue = queue.Queue()     # 热键线程 → 主线程 的事件队列
        self._hotkey_watcher = None
        self._hotkey_prev = False              # 兜底轮询用的边沿状态
        self._hotkey_armed_at = 0.0
        self._hotkey_job = None                # after 定时器 id（退出时取消用）
        self._queue_job = None
        try:
            self._hotkey_vks = parse_hotkey(HOTKEY)
        except ValueError as exc:
            print(f"[警告] {exc}，全局快捷键已禁用")
            self._hotkey_vks = []

        self._build_window()
        self._build_widgets()
        self._lock_size_and_place()
        self._boot()                       # 找私钥、连服务器
        self.root.after(200, self._poll_queue)
        self._start_hotkey()               # 启动全局快捷键

    # ----------------------------------------------------------
    #  窗口本身
    # ----------------------------------------------------------
    def _build_window(self):
        root = self.root
        root.overrideredirect(True)         # ★ 无边框
        root.attributes("-topmost", True)   # ★ 始终置顶
        try:
            root.attributes("-alpha", 0.88)  # ★ 半透明
        except tk.TclError:
            pass
        root.configure(bg=BORDER_COLOR)     # 这层颜色从内层留白透出来 = 描边

        # 右键菜单（无边框窗口没有关闭按钮，退出必须靠它）
        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="立即刷新", command=self._refresh_now)
        self.menu.add_command(label="重新连接", command=self._reconnect)
        self.menu.add_command(label="查看错误详情", command=self._show_error)
        self.menu.add_command(label="⬇/📌  沉底 / 置顶", command=self.toggle_pin)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.on_quit)

        root.bind("<Button-3>", self._popup_menu)          # 右键
        root.bind("<Escape>", lambda e: self.on_quit())    # Esc 也能退出
        root.protocol("WM_DELETE_WINDOW", self.on_quit)

    # ----------------------------------------------------------
    #  界面部件（用 grid 做两列对齐）
    # ----------------------------------------------------------
    def _build_widgets(self):
        pad = tk.Frame(self.root, bg=BG_COLOR)
        pad.pack(padx=1, pady=1)
        body = tk.Frame(pad, bg=BG_COLOR)
        body.pack(padx=14, pady=10)

        # ---- 第 0 行：标题 + 连接状态 ----
        self.lbl_title = tk.Label(body, text="远程流量监控", font=FONT_TITLE,
                                  fg=FG_TITLE, bg=BG_COLOR, anchor="w")
        self.lbl_title.grid(row=0, column=0, sticky="w")

        self.lbl_status = tk.Label(body, text="● 连接中", font=FONT_STATUS,
                                   fg=FG_WARN, bg=BG_COLOR, anchor="e", width=12)
        self.lbl_status.grid(row=0, column=1, sticky="e")

        # ★ 沉底 / 置顶按钮（标题行最右侧，不参与拖动，避免想点却拖走窗口）
        self.pin_btn = tk.Button(body, text=self._pin_text(), font=FONT_PIN,
                                 fg=FG_TITLE, bg=BG_COLOR,
                                 activeforeground=FG_TITLE,
                                 activebackground="#333333",
                                 relief="flat", bd=0, highlightthickness=0,
                                 cursor="hand2", width=2,
                                 command=self.toggle_pin)
        self.pin_btn.grid(row=0, column=2, sticky="e", padx=(6, 0))
        self.pin_btn.bind("<Enter>", lambda e: self.pin_btn.config(fg=FG_TITLE))
        self.pin_btn.bind("<Leave>", lambda e: self._update_pin_btn())

        # ---- 分隔线 ----
        tk.Frame(body, bg="#333333", height=1).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(6, 8))

        # ---- 第 2 行：下载总量 ----
        self.lbl_down_cap = tk.Label(body, text="↓  下载总量", font=FONT_LABEL,
                                     fg=FG_LABEL, bg=BG_COLOR, anchor="w")
        self.lbl_down_cap.grid(row=2, column=0, sticky="w")
        self.lbl_down = tk.Label(body, text="--", font=FONT_VALUE, fg=FG_DOWN,
                                 bg=BG_COLOR, anchor="e", width=11)
        self.lbl_down.grid(row=2, column=1, sticky="e")

        # ---- 第 3 行：上传总量 ----
        self.lbl_up_cap = tk.Label(body, text="↑  上传总量", font=FONT_LABEL,
                                   fg=FG_LABEL, bg=BG_COLOR, anchor="w")
        self.lbl_up_cap.grid(row=3, column=0, sticky="w", pady=(3, 0))
        self.lbl_up = tk.Label(body, text="--", font=FONT_VALUE, fg=FG_UP,
                               bg=BG_COLOR, anchor="e", width=11)
        self.lbl_up.grid(row=3, column=1, sticky="e", pady=(3, 0))

        # ---- 第 4 行：一个周期的增量（小字，不想要可以直接删掉这一行）----
        self.lbl_rate = tk.Label(body, text="", font=FONT_SMALL, fg=FG_DIM,
                                 bg=BG_COLOR, anchor="w", width=30)
        self.lbl_rate.grid(row=4, column=0, columnspan=3, sticky="w", pady=(7, 0))

        # ---- 让整块区域都能拖动窗口 ----
        # 注意：每个部件都要单独绑定。点击落在子部件上时，
        #       事件不会"冒泡"到父窗口，只绑 root 是拖不动的
        for widget in (self.root, pad, body, self.lbl_title, self.lbl_status,
                       self.lbl_down_cap, self.lbl_down,
                       self.lbl_up_cap, self.lbl_up, self.lbl_rate):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)

    # ----------------------------------------------------------
    #  拖动
    # ----------------------------------------------------------
    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _drag(self, event):
        new_x = self.root.winfo_x() + event.x - self._drag_x
        new_y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{new_x}+{new_y}")

    # ----------------------------------------------------------
    #  固定尺寸 + 摆到屏幕右上角
    # ----------------------------------------------------------
    def _lock_size_and_place(self):
        # 先把每个标签的文字填上占位内容再量尺寸，否则量到的是空窗口的宽度，
        # 定位会算错，窗口会被摆到屏幕外面去
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        x = self.root.winfo_screenwidth() - w - 40
        y = 40
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ----------------------------------------------------------
    #  右键菜单
    # ----------------------------------------------------------
    def _popup_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()        # 不释放的话菜单会一直抓着鼠标

    def _refresh_now(self):
        if self.worker:
            self.lbl_status.config(text="● 刷新中", fg=FG_WARN)
            self.worker.refresh_now()

    def _reconnect(self):
        """丢掉当前连接，让后台线程重新握手一次"""
        if self.ssh:
            self.ssh.close()
        self._last_reset()
        self._refresh_now()

    def _last_reset(self):
        if self.worker:
            self.worker._last = None        # 重连后计数器基准要重来

    def _show_error(self):
        messagebox.showinfo("最近一次错误", self.last_error)

    # ----------------------------------------------------------
    #  启动：找私钥 → 加载 → 拉起后台线程
    # ----------------------------------------------------------
    def _boot(self):
        key_path = find_key_file()
        if not key_path:
            messagebox.showerror(
                "找不到私钥",
                f"没有找到私钥文件 {KEY_NAME}。\n\n"
                f"请把它放到下面任意一个位置（任选其一即可）：\n\n"
                f"  1) 程序同级目录\n"
                f"  2) 程序同级目录下的 key\\ 子目录\n"
                f"  3) {os.path.join(os.path.expanduser('~'), '.ssh')}\n\n"
                f"注意：私钥【不要】打包进 exe。")
            self.on_quit()
            return

        # 加载私钥；如果私钥设了密码，弹窗问一次（密码只存在内存里）
        try:
            pkey = load_private_key(key_path)
        except paramiko.PasswordRequiredException:
            pw = simpledialog.askstring(
                "私钥密码",
                f"私钥有密码保护：\n{key_path}\n\n请输入密码：", show="*")
            if not pw:
                messagebox.showwarning("已取消", "没有输入私钥密码，程序退出。")
                self.on_quit()
                return
            try:
                pkey = load_private_key(key_path, pw)
            except Exception as exc:
                messagebox.showerror("私钥加载失败",
                                     f"{type(exc).__name__}: {exc}")
                self.on_quit()
                return
        except Exception as exc:
            messagebox.showerror(
                "私钥加载失败",
                f"{type(exc).__name__}: {exc}\n\n文件位置：\n{key_path}")
            self.on_quit()
            return

        # 建长连接客户端 + 启动采集线程
        self.ssh = SSHTraffic(HOST, PORT, USER, pkey)
        self.worker = TrafficWorker(self.ssh, IFACE, REFRESH_SEC, self.q)
        self.worker.start()

    # ----------------------------------------------------------
    #  从队列里取后台线程的消息并更新界面（只在主线程执行）
    # ----------------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]

                if kind == "data":
                    _, rx, tx, drx, dtx = msg
                    self.lbl_down.config(text=human_bytes(rx))
                    self.lbl_up.config(text=human_bytes(tx))
                    self.lbl_rate.config(
                        text=f"近 {REFRESH_SEC} 秒  ↓{human_bytes(drx)}"
                             f"  ↑{human_bytes(dtx)}")
                    self.lbl_status.config(text="● 已连接", fg=FG_OK)

                elif kind == "status":
                    self.lbl_status.config(text=f"● {msg[1]}", fg=msg[2])

                elif kind == "error":
                    self.last_error = msg[1]
                    # 错误信息直接显示在小字那行，方便一眼看出问题
                    self.lbl_rate.config(text=msg[1][:46])

        except queue.Empty:
            pass

        if not self.closing:
            self._queue_job = self.root.after(200, self._poll_queue)

    # ----------------------------------------------------------
    #  置顶 / 沉底
    # ----------------------------------------------------------
    def _pin_text(self):
        """按钮图标：置顶时显示 ⬇（点它沉底），沉底时显示 📌（点它置顶）"""
        return PIN_TEXT_UP if self.pinned else PIN_TEXT_DOWN

    def _update_pin_btn(self):
        try:
            self.pin_btn.config(text=self._pin_text(),
                                fg=FG_TITLE if self.pinned else FG_DIM)
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

    def _poll_hotkey(self):
        """
        每 HOTKEY_POLL_MS 毫秒检查一次全局按键。

        为什么用 GetAsyncKeyState 轮询，而不是 RegisterHotKey：
          - 不需要额外消息循环 / 子线程，代码短、不易出错
          - 读的是系统级按键状态，窗口没有焦点、已经沉底时照样能读到
          - 不会和其它程序抢注同一个键（RegisterHotKey 抢不到会直接失败）
        用"边沿检测"：按住不放只触发一次。
        """
        try:
            if self.closing:
                return
            if self._hotkey_vks:
                # ★ 每个键只查一次：低 bit（自上次查询以来按下过）读一次就清零，
                #   所以先一次性取回所有状态，再分别判断。
                states = [key_state(vk) for vk in self._hotkey_vks]
                held = all(s & 0x8000 for s in states)     # 三个键此刻都按着
                tapped = all(s & 0x0001 for s in states)   # 三个键期间都按过
                if held and not self._hotkey_prev:
                    self.toggle_pin()      # 正常按住 → 走上升沿，按住不放只触发一次
                elif tapped and not held:
                    self.toggle_pin()      # 快速轻按：轮询时已经松开，靠低 bit 兜住
                self._hotkey_prev = held
            self.root.after(HOTKEY_POLL_MS, self._poll_hotkey)
        except Exception:
            pass                                # 窗口已销毁就静默退出

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

    # ----------------------------------------------------------
    #  退出：先停线程、关连接，再销毁窗口
    # ----------------------------------------------------------
    def on_quit(self):
        if self.closing:
            return
        self.closing = True
        try:
            if self._hotkey_watcher is not None:
                self._hotkey_watcher.stop()    # 注销热键，别一直占着键
        except Exception:
            pass
        # 取消还没执行的 after 回调，否则销毁后 Tk 会打
        # "invalid command name ..." 的警告
        for job in (self._hotkey_job, self._queue_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._hotkey_job = self._queue_job = None
        try:
            if self.worker:
                self.worker.stop()
                self.worker.join(timeout=2)    # 最多等 2 秒，别卡住退出
        except Exception:
            pass
        try:
            if self.ssh:
                self.ssh.close()               # 主动断开，别在服务器上留半开连接
        except Exception:
            pass
        self.root.destroy()


# ============================================================
#  5. 入口
# ============================================================
def _check_config():
    """
    配置区还是占位符时，给出明确提示并退出。
    否则会抛出一个看不懂的 socket 错误，很难排查。
    """
    bad = [name for name, val in (("HOST", HOST), ("PORT", PORT), ("USER", USER))
           if "YOUR_" in str(val)]
    if not bad:
        return
    msg = ("请先把文件顶部「配置区」里的 "
           + "、".join(bad) + " 改成你服务器的真实值。\n\n"
           "例如：\n"
           '  HOST = "203.0.113.10"\n'
           "  PORT = 22\n"
           '  USER = "root"')
    try:
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror("配置还没改", msg)
        _r.destroy()
    except Exception:
        print("[错误]", msg)
    sys.exit(1)


def main():
    _check_config()                 # ★ 先检查配置有没有改
    enable_dpi_awareness()          # 必须在建 Tk() 之前调用
    root = tk.Tk()
    TrafficMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
