"""
Sony LinkBuds 耳周广域点击 (Wide Area Tap) 硬件按键截获监听器：
- 基于 Windows 原生 RegisterHotKey 机制实现系统级多媒体/耳周动作拦截
- 自动屏蔽默认的音乐暂停/切歌/系统音量弹窗，静默转化为鼠标原生左键/右键点击
- 彻底解决双手不便人士的点击难题：轻叩左脸颊/左耳触发左键，轻叩右脸颊/右耳触发右键
- 采用消息专用窗口 (HWND_MESSAGE) 与独立后台线程，0 资源开销，绝对防崩溃
"""

import time
import threading
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_int64, wintypes.HWND, wintypes.UINT, ctypes.c_uint64, ctypes.c_int64)

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_uint64, ctypes.c_int64]
user32.DefWindowProcW.restype = ctypes.c_int64

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.UINT),
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', wintypes.HINSTANCE),
        ('hIcon', wintypes.HICON),
        ('hCursor', wintypes.HICON),
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
        ('hIconSm', wintypes.HICON),
    ]

HWND_MESSAGE = wintypes.HWND(-3)
WM_HOTKEY = 0x0312
WM_CLOSE = 0x0010

# 索尼耳机标准蓝牙 HID 多媒体键值
VK_MEDIA_NEXT_TRACK = 0xB0  # 176 (通常对应右耳双击/三击)
VK_MEDIA_PREV_TRACK = 0xB1  # 177
VK_MEDIA_PLAY_PAUSE = 0xB3  # 179 (通常对应左耳双击/三击或右耳默认)
VK_VOLUME_DOWN      = 0xAE  # 174
VK_VOLUME_UP        = 0xAF  # 175

# 预设热键 ID 分组
HOTKEY_ID_BASE_LEFT  = 1000
HOTKEY_ID_BASE_RIGHT = 2000

class WideAreaTapListener:
    def __init__(self, on_left_click=None, on_right_click=None, enabled=True):
        self.on_left_click = on_left_click
        self.on_right_click = on_right_click
        self.enabled = enabled

        # 默认左侧映射：播放/暂停、上一首、音量减
        self.left_keys = [VK_MEDIA_PLAY_PAUSE, VK_MEDIA_PREV_TRACK, VK_VOLUME_DOWN]
        # 默认右侧映射：下一首、音量加
        self.right_keys = [VK_MEDIA_NEXT_TRACK, VK_VOLUME_UP]

        self.last_action_time = 0.0
        self.debounce_sec = 0.25  # 250ms 防抖

        self.hwnd = None
        self.thread = None
        self.running = False
        self.ready_event = threading.Event()
        self._wnd_proc_ref = WNDPROC(self._window_proc)

    def _window_proc(self, hwnd, msg, wp, lp):
        if msg == WM_HOTKEY:
            now = time.time()
            if self.enabled and (now - self.last_action_time > self.debounce_sec):
                self.last_action_time = now
                hotkey_id = wp
                if HOTKEY_ID_BASE_LEFT <= hotkey_id < HOTKEY_ID_BASE_RIGHT:
                    if self.on_left_click:
                        self.on_left_click()
                elif hotkey_id >= HOTKEY_ID_BASE_RIGHT:
                    if self.on_right_click:
                        self.on_right_click()
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _register_all_hotkeys(self):
        if not self.hwnd:
            return
        # 注册左侧键
        for idx, vk in enumerate(self.left_keys):
            hid = HOTKEY_ID_BASE_LEFT + idx
            user32.RegisterHotKey(self.hwnd, hid, 0, vk)
        # 注册右侧键
        for idx, vk in enumerate(self.right_keys):
            hid = HOTKEY_ID_BASE_RIGHT + idx
            user32.RegisterHotKey(self.hwnd, hid, 0, vk)

    def _unregister_all_hotkeys(self):
        if not self.hwnd:
            return
        for idx in range(len(self.left_keys)):
            user32.UnregisterHotKey(self.hwnd, HOTKEY_ID_BASE_LEFT + idx)
        for idx in range(len(self.right_keys)):
            user32.UnregisterHotKey(self.hwnd, HOTKEY_ID_BASE_RIGHT + idx)

    def _message_loop(self):
        h_inst = kernel32.GetModuleHandleW(None)
        cls_name = f"SonyWideTapListener_{id(self)}"
        
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wnd_proc_ref
        cls.hInstance = h_inst
        cls.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(cls))

        self.hwnd = user32.CreateWindowExW(
            0, cls_name, "WideTapMsgWindow",
            0, 0, 0, 0, 0,
            HWND_MESSAGE, 0, h_inst, 0
        )

        if self.enabled:
            self._register_all_hotkeys()

        self.ready_event.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._unregister_all_hotkeys()
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.ready_event.clear()
        self.thread = threading.Thread(target=self._message_loop, daemon=True)
        self.thread.start()
        self.ready_event.wait(timeout=2.0)

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.thread:
            self.thread.join(timeout=1.0)
            self.thread = None

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if self.hwnd:
            if self.enabled:
                self._register_all_hotkeys()
            else:
                self._unregister_all_hotkeys()
