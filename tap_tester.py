"""
Sony LinkBuds 耳机动作全频段实时抓包监听器 (完全零过滤版)
- 抓取 Windows 系统的所有输入通道：
  1. 低级键盘钩子 (WH_KEYBOARD_LL): 捕获所有按键 (0x00 ~ 0xFF)，含 Win键、F功能键、特殊键、媒体键，支持 KeyDown 与 KeyUp
  2. 低级鼠标钩子 (WH_MOUSE_LL): 捕获可能产生的鼠标原生按键 (左键、右键、中键、侧键)
  3. 蓝牙/多媒体指令 (WM_APPCOMMAND & ShellHook): 捕获所有 AVRCP 媒体广播
  4. 原始 HID 报文 (WM_INPUT): 捕获 Consumer Control 与 通话控制数据
  5. 实时同步写入日志文件 live_tap.log
"""

import os
import sys
import time
import math
import queue
import socket
import struct
import ctypes
from ctypes import wintypes
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

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

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ('pt_x', ctypes.c_long),
        ('pt_y', ctypes.c_long),
        ('mouseData', wintypes.DWORD),
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

VK_NAMES = {
    0x01: "鼠标左键", 0x02: "鼠标右键", 0x04: "鼠标中键", 0x05: "鼠标侧键1", 0x06: "鼠标侧键2",
    0x0D: "回车(ENTER)", 0x1B: "ESC", 0x20: "空格(SPACE)",
    0x5B: "左Win键", 0x5C: "右Win键",
    0x5F: "系统睡眠",
    0xA6: "浏览器后退", 0xA7: "浏览器前进", 0xA8: "浏览器刷新", 0xA9: "浏览器停止", 0xAA: "浏览器搜索",
    0xAB: "浏览器收藏", 0xAC: "浏览器主页",
    0xAD: "音量静音 (VOLUME_MUTE)", 0xAE: "音量减 (VOLUME_DOWN)", 0xAF: "音量加 (VOLUME_UP)",
    0xB0: "下一首 (MEDIA_NEXT_TRACK)", 0xB1: "上一首 (MEDIA_PREV_TRACK)",
    0xB2: "停止 (MEDIA_STOP)", 0xB3: "播放/暂停 (MEDIA_PLAY_PAUSE)",
    0xB4: "启动邮件", 0xB5: "启动媒体", 0xB6: "启动应用1", 0xB7: "启动应用2"
}
for i in range(1, 25):
    VK_NAMES[0x70 + i - 1] = f"F{i}"

APPCOMMAND_NAMES = {
    8: "APPCOMMAND_VOLUME_MUTE (静音)",
    9: "APPCOMMAND_VOLUME_DOWN (音量减)",
    10: "APPCOMMAND_VOLUME_UP (音量加)",
    11: "APPCOMMAND_MEDIA_NEXTTRACK (下一首)",
    12: "APPCOMMAND_MEDIA_PREVIOUSTRACK (上一首)",
    13: "APPCOMMAND_MEDIA_STOP (停止)",
    14: "APPCOMMAND_MEDIA_PLAY_PAUSE (播放/暂停)",
    46: "APPCOMMAND_MEDIA_PLAY (播放)",
    47: "APPCOMMAND_MEDIA_PAUSE (暂停)"
}

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_tap.log")

class UniversalSniffer:
    def __init__(self, log_queue, telemetry_callback):
        self.queue = log_queue
        self.telemetry_cb = telemetry_callback
        self.running = False

        self.udp_sock = None
        self.udp_thread = None
        self.win32_thread = None
        self.win32_tid = None
        self.hwnd = None
        self.h_hook_kb = None
        self.h_hook_mouse = None
        self.wm_shellhook = None

        self.keepalive = SilentAudioKeepalive()

        self._wnd_proc_ref = WNDPROC(self._window_proc)
        self._ll_kb_ref = HOOKPROC(self._ll_kb_proc)
        self._ll_mouse_ref = HOOKPROC(self._ll_mouse_proc)

    def log(self, channel, message, level="INFO"):
        t_str = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
        line = f"[{t_str}] [{channel}] {message}\n"
        self.queue.put((t_str, channel, message, level))
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _ll_kb_proc(self, nCode, wParam, lParam):
        if nCode >= 0:
            p = KBDLLHOOKSTRUCT.from_address(lParam)
            vk = p.vkCode
            # 零过滤：捕获全部虚拟按键 (0x00 ~ 0xFF)
            act = "按下(Down)" if wParam in (0x0100, 0x0104) else "松开(Up)"
            name = VK_NAMES.get(vk, f"按键0x{vk:02X}")
            self.log("键盘总线 (Hook)", f"【{act}】{name} [vk=0x{vk:02X}, scan=0x{p.scanCode:02X}, flags=0x{p.flags:02X}]", "HIT")
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _ll_mouse_proc(self, nCode, wParam, lParam):
        if nCode >= 0:
            # 捕获物理鼠标按键动作 (跳过纯移动 WM_MOUSEMOVE 0x0200)
            if wParam in (0x0201, 0x0202, 0x0204, 0x0205, 0x0207, 0x0208, 0x020B, 0x020C):
                m_names = {
                    0x0201: "左键按下", 0x0202: "左键抬起",
                    0x0204: "右键按下", 0x0205: "右键抬起",
                    0x0207: "中键按下", 0x0208: "中键抬起",
                    0x020B: "侧键按下", 0x020C: "侧键抬起"
                }
                m_str = m_names.get(wParam, f"鼠标消息0x{wParam:04X}")
                self.log("鼠标总线 (Hook)", f"收到: {m_str}", "HIT")
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _window_proc(self, hwnd, msg, wp, lp):
        if msg == 0x0319: # WM_APPCOMMAND
            cmd = ((lp >> 16) & ~0xF000)
            name = APPCOMMAND_NAMES.get(cmd, f"APPCOMMAND_{cmd}")
            self.log("多媒体广播 (WM_APPCOMMAND)", f"收到: {name} [cmd={cmd}]", "HIT")
            return 1

        if self.wm_shellhook and msg == self.wm_shellhook:
            if wp == 12: # HSHELL_APPCOMMAND
                cmd = ((lp >> 16) & ~0xF000)
                name = APPCOMMAND_NAMES.get(cmd, f"APPCOMMAND_{cmd}")
                self.log("系统广播 (ShellHook)", f"收到: {name} [cmd={cmd}]", "HIT")
                return 1

        if msg == 0x00FF: # WM_INPUT
            try:
                raw_size = wintypes.UINT()
                user32.GetRawInputData(lp, 0x10000003, None, ctypes.byref(raw_size), ctypes.sizeof(RAWINPUTHEADER))
                if raw_size.value > 0:
                    buf = (ctypes.c_byte * raw_size.value)()
                    user32.GetRawInputData(lp, 0x10000003, buf, ctypes.byref(raw_size), ctypes.sizeof(RAWINPUTHEADER))
                    header = RAWINPUTHEADER.from_buffer_copy(buf)
                    if header.dwType == 2: # RIM_TYPEHID
                        hex_data = ' '.join(f'{b & 0xFF:02X}' for b in buf[ctypes.sizeof(RAWINPUTHEADER):])
                        if hex_data.strip():
                            self.log("原始HID流 (WM_INPUT)", f"长度={raw_size.value} 字节: {hex_data}", "HIT")
            except Exception:
                pass
            return 0

        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _win32_loop(self):
        h_inst = kernel32.GetModuleHandleW(None)
        cls_name = f"SonySnifferAll_{id(self)}"

        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wnd_proc_ref
        cls.hInstance = h_inst
        cls.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(cls))

        self.hwnd = user32.CreateWindowExW(
            0, cls_name, "SonySnifferAllWin",
            0x80000000, 0, 0, 0, 0,
            None, None, h_inst, None
        )

        self.wm_shellhook = user32.RegisterWindowMessageW("SHELLHOOK")
        if self.hwnd:
            user32.RegisterShellHookWindow(self.hwnd)

            devs = (RAWINPUTDEVICE * 2)()
            devs[0].usUsagePage = 0x000C
            devs[0].usUsage = 0x0001
            devs[0].dwFlags = 0x00000100
            devs[0].hwndTarget = self.hwnd

            devs[1].usUsagePage = 0x000B
            devs[1].usUsage = 0x0001
            devs[1].dwFlags = 0x00000100
            devs[1].hwndTarget = self.hwnd

            user32.RegisterRawInputDevices(devs, 2, ctypes.sizeof(RAWINPUTDEVICE))

        self.h_hook_kb = user32.SetWindowsHookExW(13, self._ll_kb_ref, h_inst, 0)
        self.h_hook_mouse = user32.SetWindowsHookExW(14, self._ll_mouse_ref, h_inst, 0)

        self.win32_tid = kernel32.GetCurrentThreadId()
        self.log("监听器状态", "全频段实时监听已启动 (已监听所有键盘/鼠标/蓝牙AVRCP/HID通道)", "INFO")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self.h_hook_kb: user32.UnhookWindowsHookEx(self.h_hook_kb)
        if self.h_hook_mouse: user32.UnhookWindowsHookEx(self.h_hook_mouse)
        if self.hwnd:
            user32.DeregisterShellHookWindow(self.hwnd)
            user32.DestroyWindow(self.hwnd)

    def _udp_loop(self):
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.udp_sock.bind(('127.0.0.1', 4242))
            self.udp_sock.settimeout(0.5)
        except Exception:
            return

        while self.running:
            try:
                data, _ = self.udp_sock.recvfrom(1024)
                if len(data) >= 48:
                    x, y, z, yaw, pitch, roll = struct.unpack('<6d', data[:48])
                    if self.telemetry_cb:
                        self.telemetry_cb(yaw, pitch, roll)
            except Exception:
                continue

        if self.udp_sock:
            try: self.udp_sock.close()
            except Exception: pass

    def start(self):
        if self.running: return
        self.running = True
        self.keepalive.start()
        self.udp_thread = threading.Thread(target=self._udp_loop, daemon=True)
        self.udp_thread.start()
        self.win32_thread = threading.Thread(target=self._win32_loop, daemon=True)
        self.win32_thread.start()

    def stop(self):
        self.running = False
        self.keepalive.stop()
        if self.win32_tid:
            user32.PostThreadMessageW(self.win32_tid, 0x0012, 0, 0)


class TapTesterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("🎧 Sony LinkBuds 全协议实时抓包器 (零过滤/全信号直显)")
        self.root.geometry("900x620")
        self.root.minsize(760, 480)

        self.topmost_var = tk.BooleanVar(value=True)
        self.root.attributes("-topmost", True)

        self.log_queue = queue.Queue()
        self.sniffer = UniversalSniffer(self.log_queue, self._on_telemetry)

        self._build_ui()
        self.sniffer.start()

        self.root.after(30, self._process_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill=tk.X)

        title = ttk.Label(top_frame, text="🟢 实时抓包模式已就绪 (无任何过滤，捕获全系统事件)", font=("Microsoft YaHei UI", 12, "bold"))
        title.pack(anchor=tk.W)

        desc = ttk.Label(
            top_frame,
            text="★ 请在不要触碰电脑键盘的情况下，用手指在【左耳周 / 左脸颊】快速连续双击 2 次。\n"
                 "★ 只要耳机向电脑发送了任何按键、鼠标信号、蓝牙多媒体指令或原始HID字节，下方均会立即高亮显示！",
            font=("Microsoft YaHei UI", 9),
            foreground="#222222"
        )
        desc.pack(anchor=tk.W, pady=(2, 4))

        meter_frame = ttk.LabelFrame(self.root, text="📡 头部姿态 (UDP 4242)", padding=6)
        meter_frame.pack(fill=tk.X, padx=10, pady=(0, 6))

        m_box = ttk.Frame(meter_frame)
        m_box.pack(fill=tk.X)
        self.lbl_yaw = ttk.Label(m_box, text="Yaw: 0.0°", font=("Consolas", 10, "bold"), width=18)
        self.lbl_yaw.pack(side=tk.LEFT)
        self.lbl_pitch = ttk.Label(m_box, text="Pitch: 0.0°", font=("Consolas", 10, "bold"), width=18)
        self.lbl_pitch.pack(side=tk.LEFT)
        self.lbl_roll = ttk.Label(m_box, text="Roll: 0.0°", font=("Consolas", 10, "bold"), width=18)
        self.lbl_roll.pack(side=tk.LEFT)

        self.lbl_status = ttk.Label(
            self.root,
            text="⚡ 等待左侧双击动作...",
            font=("Microsoft YaHei UI", 11, "bold"),
            background="#FFF3CD",
            foreground="#856404",
            padding=8,
            relief=tk.GROOVE
        )
        self.lbl_status.pack(fill=tk.X, padx=10, pady=(0, 6))

        ctrl = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        ctrl.pack(fill=tk.X)
        btn_copy = ttk.Button(ctrl, text="📋 复制全部日志", command=self.copy_all)
        btn_copy.pack(side=tk.LEFT, padx=(0, 6))
        btn_clear = ttk.Button(ctrl, text="🗑️ 清空日志", command=self.clear_logs)
        btn_clear.pack(side=tk.LEFT, padx=(0, 6))
        chk_top = ttk.Checkbutton(ctrl, text="置顶显示", variable=self.topmost_var, command=self.toggle_topmost)
        chk_top.pack(side=tk.RIGHT)

        log_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.text = tk.Text(log_frame, wrap=tk.WORD, font=("Consolas", 10), bg="#1E1E1E", fg="#D4D4D4", insertbackground="white")
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.text.tag_config("TIME", foreground="#888888")
        self.text.tag_config("HIT", foreground="#00FF66", font=("Consolas", 10, "bold"))
        self.text.tag_config("INFO", foreground="#4DAAF8")

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost_var.get())

    def _on_telemetry(self, yaw, pitch, roll):
        self.lbl_yaw.config(text=f"Yaw: {yaw:+6.1f}°")
        self.lbl_pitch.config(text=f"Pitch: {pitch:+6.1f}°")
        self.lbl_roll.config(text=f"Roll: {roll:+6.1f}°")

    def _process_queue(self):
        while not self.log_queue.empty():
            t_str, channel, msg, level = self.log_queue.get_nowait()
            self.text.insert(tk.END, f"[{t_str}] ", "TIME")
            self.text.insert(tk.END, f"[{channel}] ", level)
            self.text.insert(tk.END, f"{msg}\n")
            self.text.see(tk.END)

            if level == "HIT":
                self.lbl_status.config(
                    text=f"🟢 捕获到信号: {msg}",
                    background="#D4EDDA",
                    foreground="#155724"
                )

        self.root.after(30, self._process_queue)

    def copy_all(self):
        content = self.text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showinfo("提示", "当前日志为空")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        messagebox.showinfo("成功", "已复制到剪贴板！")

    def clear_logs(self):
        self.text.delete("1.0", tk.END)

    def _on_close(self):
        self.sniffer.stop()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = TapTesterApp(root)
    root.mainloop()
