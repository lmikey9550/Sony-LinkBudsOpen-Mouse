"""
Sony LinkBuds 耳周广域点击 (Wide Area Tap) 硬件按键截获监听器：
- 完美解决旧版 RegisterHotKey 因系统冲突 (1409 ERROR_HOTKEY_ALREADY_REGISTERED) 导致注册失败的问题
- 采用四重无死角事件捕获体系：
  1. 全局低级键盘钩子 (WH_KEYBOARD_LL)：微秒级拦截并静默吞除媒体按键，防止音乐播放器切歌
  2. 系统应用命令 (WM_APPCOMMAND)：拦截蓝牙 AVRCP 直接下发的媒体控制指令
  3. 系统 Shell 钩子 (RegisterShellHookWindow / HSHELL_APPCOMMAND)：捕获全局冒泡的媒体指令
  4. 原始输入接收器 (RegisterRawInputDevices / WM_INPUT)：后台捕获 Consumer Controls (Usage 0x000C) 硬件流
- 默认映射：
  * 左侧耳周敲击 (播放/暂停/音量减/上一首) -> 原生鼠标左键单击
  * 右侧耳周敲击 (下一首/音量加) -> 原生鼠标右键单击
- 内置 250ms 防抖算法，避免连击导致光标误触
"""

import time
import threading
import ctypes
from ctypes import wintypes

from silent_keepalive import SilentAudioKeepalive

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, ctypes.c_void_p, ctypes.c_void_p
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_int64

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_int64, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HMODULE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_int64, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.UINT),
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', ctypes.c_void_p),
        ('hIcon', wintypes.HICON),
        ('hCursor', wintypes.HICON),
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
        ('hIconSm', wintypes.HICON),
    ]

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ('vkCode', wintypes.DWORD),
        ('scanCode', wintypes.DWORD),
        ('flags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('dwExtraInfo', ctypes.POINTER(wintypes.ULONG))
    ]

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ('usUsagePage', wintypes.USHORT),
        ('usUsage', wintypes.USHORT),
        ('dwFlags', wintypes.DWORD),
        ('hwndTarget', wintypes.HWND)
    ]

class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ('dwType', wintypes.DWORD),
        ('dwSize', wintypes.DWORD),
        ('hDevice', wintypes.HANDLE),
        ('wParam', wintypes.WPARAM)
    ]

class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ('MakeCode', wintypes.USHORT),
        ('Flags', wintypes.USHORT),
        ('Reserved', wintypes.USHORT),
        ('VKey', wintypes.USHORT),
        ('Message', wintypes.UINT),
        ('ExtraInformation', wintypes.ULONG)
    ]

class RAWHID(ctypes.Structure):
    _fields_ = [
        ('dwSizeHid', wintypes.DWORD),
        ('dwCount', wintypes.DWORD),
        ('bRawData', ctypes.c_byte * 1)
    ]

class RAWINPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [('keyboard', RAWKEYBOARD), ('hid', RAWHID)]
    _anonymous_ = ('u',)
    _fields_ = [('header', RAWINPUTHEADER), ('u', _U)]

WM_APPCOMMAND = 0x0319
WM_INPUT = 0x00FF
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WH_KEYBOARD_LL = 13
HSHELL_APPCOMMAND = 12

# 常见 Windows 多媒体按键与命令常数
APPCOMMAND_VOLUME_MUTE          = 8
APPCOMMAND_VOLUME_DOWN          = 9
APPCOMMAND_VOLUME_UP            = 10
APPCOMMAND_MEDIA_NEXTTRACK      = 11
APPCOMMAND_MEDIA_PREVIOUSTRACK  = 12
APPCOMMAND_MEDIA_STOP           = 13
APPCOMMAND_MEDIA_PLAY_PAUSE     = 14
APPCOMMAND_MEDIA_PLAY           = 46
APPCOMMAND_MEDIA_PAUSE          = 47

VK_VOLUME_MUTE      = 0xAD
VK_VOLUME_DOWN      = 0xAE
VK_VOLUME_UP        = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP       = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

LEFT_COMMANDS = {
    APPCOMMAND_MEDIA_PLAY_PAUSE,
    APPCOMMAND_MEDIA_PLAY,
    APPCOMMAND_MEDIA_PAUSE,
    APPCOMMAND_MEDIA_PREVIOUSTRACK,
    APPCOMMAND_VOLUME_DOWN
}

RIGHT_COMMANDS = {
    APPCOMMAND_MEDIA_NEXTTRACK,
    APPCOMMAND_VOLUME_UP
}

LEFT_VKEYS = {
    VK_MEDIA_PLAY_PAUSE,
    VK_MEDIA_PREV_TRACK,
    VK_VOLUME_DOWN
}

RIGHT_VKEYS = {
    VK_MEDIA_NEXT_TRACK,
    VK_VOLUME_UP
}

class WideAreaTapListener:
    def __init__(self, on_left_click=None, on_right_click=None, enabled=True):
        self.on_left_click = on_left_click
        self.on_right_click = on_right_click
        self.enabled = enabled

        self.last_action_time = 0.0
        self.debounce_sec = 0.28  # 280ms 防抖

        self.hwnd = None
        self.thread = None
        self.thread_id = None
        self.running = False
        self.ready_event = threading.Event()
        self.h_hook = None
        self.wm_shellhook = None
        self.keepalive = SilentAudioKeepalive()

        self._wnd_proc_ref = WNDPROC(self._window_proc)
        self._ll_proc_ref = HOOKPROC(self._ll_keyboard_proc)

    def _trigger_action(self, side):
        if not self.enabled:
            return
        now = time.time()
        if now - self.last_action_time < self.debounce_sec:
            return
        self.last_action_time = now

        if side == 'left' and self.on_left_click:
            self.on_left_click()
        elif side == 'right' and self.on_right_click:
            self.on_right_click()

    def _ll_keyboard_proc(self, nCode, wParam, lParam):
        if nCode >= 0 and self.enabled:
            # 捕获 WM_KEYDOWN (0x0100) 或 WM_SYSKEYDOWN (0x0104)
            if wParam in (0x0100, 0x0104):
                p = KBDLLHOOKSTRUCT.from_address(lParam)
                vk = p.vkCode
                if vk in LEFT_VKEYS:
                    self._trigger_action('left')
                    return 1  # 吞掉按键，防止音乐播放器切歌/暂停
                elif vk in RIGHT_VKEYS:
                    self._trigger_action('right')
                    return 1  # 吞掉按键，防止音乐播放器切歌
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _window_proc(self, hwnd, msg, wp, lp):
        # 1. 拦截直接发给窗口的 WM_APPCOMMAND
        if msg == WM_APPCOMMAND:
            cmd = ((lp >> 16) & ~0xF000)
            if cmd in LEFT_COMMANDS:
                self._trigger_action('left')
                return 1
            elif cmd in RIGHT_COMMANDS:
                self._trigger_action('right')
                return 1
            return 1

        # 2. 拦截 ShellHook 广播的 HSHELL_APPCOMMAND
        if self.wm_shellhook and msg == self.wm_shellhook:
            if wp == HSHELL_APPCOMMAND:
                cmd = ((lp >> 16) & ~0xF000)
                if cmd in LEFT_COMMANDS:
                    self._trigger_action('left')
                    return 1
                elif cmd in RIGHT_COMMANDS:
                    self._trigger_action('right')
                    return 1
            return 0

        # 3. 拦截 Raw Input (WM_INPUT)
        if msg == WM_INPUT:
            dwSize = wintypes.UINT()
            user32.GetRawInputData(lp, 0x10000003, None, ctypes.byref(dwSize), ctypes.sizeof(RAWINPUTHEADER))
            if dwSize.value > 0:
                buf = (ctypes.c_byte * dwSize.value)()
                if user32.GetRawInputData(lp, 0x10000003, buf, ctypes.byref(dwSize), ctypes.sizeof(RAWINPUTHEADER)) > 0:
                    raw = RAWINPUT.from_buffer(buf)
                    if raw.header.dwType == 1:  # RIM_TYPEKEYBOARD
                        vk = raw.keyboard.VKey
                        # 只在按键按下时响应 (WM_KEYDOWN 或 WM_SYSKEYDOWN)
                        if raw.keyboard.Message in (0x0100, 0x0104):
                            if vk in LEFT_VKEYS:
                                self._trigger_action('left')
                            elif vk in RIGHT_VKEYS:
                                self._trigger_action('right')
                    elif raw.header.dwType == 2:  # RIM_TYPEHID (Consumer Control 报告)
                        hid_bytes = bytes(buf[ctypes.sizeof(RAWINPUTHEADER):ctypes.sizeof(RAWINPUTHEADER)+raw.hid.dwSizeHid])
                        # 0xCD: Play/Pause, 0xB5: Next, 0xB6: Prev, 0xEA: Vol-, 0xE9: Vol+
                        if b'\xcd' in hid_bytes or b'\xb6' in hid_bytes or b'\xea' in hid_bytes:
                            self._trigger_action('left')
                        elif b'\xb5' in hid_bytes or b'\xe9' in hid_bytes:
                            self._trigger_action('right')
            return 0

        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _message_loop(self):
        h_inst = kernel32.GetModuleHandleW(None)
        cls_name = f"SonyWideTapListener_{id(self)}"

        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wnd_proc_ref
        cls.hInstance = h_inst
        cls.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(cls))

        # 创建隐藏顶层弹窗（WS_POPUP，确保能够接收系统 ShellHook 与广播）
        WS_POPUP = 0x80000000
        self.hwnd = user32.CreateWindowExW(
            0, cls_name, "WideTapReceiverWindow",
            WS_POPUP, 0, 0, 0, 0,
            None, None, h_inst, None
        )

        # 注册 ShellHook 接收全局应用指令
        self.wm_shellhook = user32.RegisterWindowMessageW("SHELLHOOK")
        if self.hwnd:
            user32.RegisterShellHookWindow(self.hwnd)

            # 注册 Raw Input Devices (Consumer Audio 0x000C + Keyboard 0x0001)
            RIDEV_INPUTSINK = 0x00000100
            rid = (RAWINPUTDEVICE * 2)()
            # 1. Consumer Control (媒体按键/耳周敲击)
            rid[0].usUsagePage = 0x000C
            rid[0].usUsage = 0x0001
            rid[0].dwFlags = RIDEV_INPUTSINK
            rid[0].hwndTarget = self.hwnd

            # 2. Keyboard (键盘媒体虚拟键)
            rid[1].usUsagePage = 0x0001
            rid[1].usUsage = 0x0006
            rid[1].dwFlags = RIDEV_INPUTSINK
            rid[1].hwndTarget = self.hwnd

            user32.RegisterRawInputDevices(rid, 2, ctypes.sizeof(RAWINPUTDEVICE))

        # 安装低级键盘钩子 (WH_KEYBOARD_LL) 形成多重容错
        self.h_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._ll_proc_ref,
            h_inst,
            0
        )

        self.thread_id = kernel32.GetCurrentThreadId()
        self.ready_event.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 清理资源
        if self.h_hook:
            user32.UnhookWindowsHookEx(self.h_hook)
            self.h_hook = None

        if self.hwnd:
            user32.DeregisterShellHookWindow(self.hwnd)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.keepalive.start()
        self.ready_event.clear()
        self.thread = threading.Thread(target=self._message_loop, daemon=True)
        self.thread.start()
        self.ready_event.wait(timeout=2.0)

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.keepalive.stop()
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        if self.thread:
            self.thread.join(timeout=1.0)
            self.thread = None

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
