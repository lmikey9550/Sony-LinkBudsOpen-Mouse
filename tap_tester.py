"""
Sony LinkBuds 耳机硬件动作与按键专属抓包诊断工具 (纯净免翻译版)
- 彻底剔除电脑键盘所有字母、数字、标点及常规编辑键
- 杜绝一切自作聪明的“头部运动翻译”与“冲击波猜测”
- 原汁原味捕获耳机发送给 Windows 的真实信号：
  1. 蓝牙 AVRCP 媒体按键 (Play/Pause 0xB3, Next 0xB0, Prev 0xB1, Vol 0xAE/0xAF 等)
  2. 系统广播 WM_APPCOMMAND 与 ShellHook
  3. 全局特殊功能键 (F13~F24, LaunchApp 等非打字键)
  4. RawInput 原始 HID Consumer / 电话控制数据包
  5. 25Hz 耳机姿态遥测 (仅做顶部数字显示，绝不产生误判日志)
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

# 严格过滤物理键盘所有的普通打字按键，防止键盘输入干扰
TYPING_VK_BLOCKLIST = set(
    list(range(0x30, 0x3A)) + # 0-9
    list(range(0x41, 0x5B)) + # A-Z
    list(range(0x60, 0x70)) + # 小键盘 Numpad
    list(range(0x70, 0x7C)) + # F1-F12
    [
        0x08, 0x09, 0x0D, 0x1B, 0x20, # Backspace, Tab, Enter, Esc, Space
        0x10, 0x11, 0x12, 0x14,       # Shift, Ctrl, Alt, CapsLock
        0x5B, 0x5C,                   # LWin, RWin
        0x21, 0x22, 0x23, 0x24,       # PageUp, PageDown, End, Home
        0x25, 0x26, 0x27, 0x28,       # Left, Up, Right, Down
        0x2C, 0x2D, 0x2E,             # PrintScreen, Insert, Delete
        0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, # ; = , - . / `
        0xDB, 0xDC, 0xDD, 0xDE        # [ \ ] '
    ]
)

KNOWN_SPECIAL_KEYS = {
    0xAD: "音量静音 (VOLUME_MUTE)",
    0xAE: "音量减 (VOLUME_DOWN)",
    0xAF: "音量加 (VOLUME_UP)",
    0xB0: "下一首 (MEDIA_NEXT_TRACK)",
    0xB1: "上一首 (MEDIA_PREV_TRACK)",
    0xB2: "停止 (MEDIA_STOP)",
    0xB3: "播放/暂停 (MEDIA_PLAY_PAUSE)",
    0xA6: "浏览器后退 (BROWSER_BACK)",
    0xA7: "浏览器前进 (BROWSER_FORWARD)",
    0xA8: "浏览器刷新 (BROWSER_REFRESH)",
    0xA9: "浏览器停止 (BROWSER_STOP)",
    0xAA: "浏览器搜索 (BROWSER_SEARCH)",
    0xAB: "浏览器收藏 (BROWSER_FAVORITES)",
    0xAC: "浏览器主页 (BROWSER_HOME)",
    0xB4: "启动邮件 (LAUNCH_MAIL)",
    0xB5: "媒体选择 (LAUNCH_MEDIA_SELECT)",
    0xB6: "启动应用1 (LAUNCH_APP1)",
    0xB7: "启动应用2 (LAUNCH_APP2)",
    0x5F: "系统睡眠 (SLEEP)"
}
for i in range(13, 25):
    KNOWN_SPECIAL_KEYS[0x70 + i - 1] = f"F{i}"

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

class DedicatedHeadsetSniffer:
    def __init__(self, log_queue, telemetry_callback):
        self.queue = log_queue
        self.telemetry_cb = telemetry_callback
        self.running = False

        self.udp_sock = None
        self.udp_thread = None
        self.win32_thread = None
        self.win32_tid = None
        self.hwnd = None
        self.h_hook = None
        self.wm_shellhook = None

        self.keepalive = SilentAudioKeepalive()

        self.last_yaw = None
        self.last_pitch = None
        self.last_roll = None
        self.last_time = time.perf_counter()

        self._wnd_proc_ref = WNDPROC(self._window_proc)
        self._ll_proc_ref = HOOKPROC(self._ll_proc)

    def log(self, channel, message, level="INFO"):
        t_str = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
        self.queue.put((t_str, channel, message, level))

    def _ll_proc(self, nCode, wParam, lParam):
        if nCode >= 0:
            p = KBDLLHOOKSTRUCT.from_address(lParam)
            vk = p.vkCode
            # 过滤物理普通打字键，其余所有特殊/媒体/扩展键真实原样输出，不加任何脑补修饰
            if vk not in TYPING_VK_BLOCKLIST and wParam in (0x0100, 0x0104):
                name = KNOWN_SPECIAL_KEYS.get(vk, f"未知键 0x{vk:02X}")
                self.log("按键/指令监听 (Hook)", f"收到指令: {name} [vk=0x{vk:02X}, scan=0x{p.scanCode:02X}, flags=0x{p.flags:02X}]", "HIT")
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _window_proc(self, hwnd, msg, wp, lp):
        # 1. 窗口收到 WM_APPCOMMAND
        if msg == 0x0319:
            cmd = ((lp >> 16) & ~0xF000)
            name = APPCOMMAND_NAMES.get(cmd, f"APPCOMMAND_{cmd}")
            self.log("系统多媒体指令 (WM_APPCOMMAND)", f"{name} [cmd={cmd}]", "HIT")
            return 1

        # 2. 窗口收到 ShellHook 广播
        if self.wm_shellhook and msg == self.wm_shellhook:
            if wp == 12: # HSHELL_APPCOMMAND
                cmd = ((lp >> 16) & ~0xF000)
                name = APPCOMMAND_NAMES.get(cmd, f"APPCOMMAND_{cmd}")
                self.log("蓝牙系统广播 (ShellHook)", f"{name} [cmd={cmd}]", "HIT")
                return 1

        # 3. 原始输入 WM_INPUT
        if msg == 0x00FF:
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
                            self.log("原始输入 (WM_INPUT/HID)", f"长度={raw_size.value} 数据: {hex_data}", "HIT")
            except Exception:
                pass
            return 0

        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _win32_loop(self):
        h_inst = kernel32.GetModuleHandleW(None)
        cls_name = f"SonyRawSniffer_{id(self)}"

        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wnd_proc_ref
        cls.hInstance = h_inst
        cls.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(cls))

        self.hwnd = user32.CreateWindowExW(
            0, cls_name, "SonyRawSnifferWin",
            0x80000000, 0, 0, 0, 0,
            None, None, h_inst, None
        )

        self.wm_shellhook = user32.RegisterWindowMessageW("SHELLHOOK")
        if self.hwnd:
            user32.RegisterShellHookWindow(self.hwnd)

            # 注册 RawInput 捕获 Consumer Control 与 Telephony 原始上报
            devs = (RAWINPUTDEVICE * 2)()
            devs[0].usUsagePage = 0x000C # Consumer Control (播放、音量等)
            devs[0].usUsage = 0x0001
            devs[0].dwFlags = 0x00000100 # RIDEV_INPUTSINK
            devs[0].hwndTarget = self.hwnd

            devs[1].usUsagePage = 0x000B # Telephony (通话接听/挂断)
            devs[1].usUsage = 0x0001
            devs[1].dwFlags = 0x00000100 # RIDEV_INPUTSINK
            devs[1].hwndTarget = self.hwnd

            user32.RegisterRawInputDevices(devs, 2, ctypes.sizeof(RAWINPUTDEVICE))

        # 安装低级键盘钩子 (过滤常规打字键)
        self.h_hook = user32.SetWindowsHookExW(
            13, # WH_KEYBOARD_LL
            self._ll_proc_ref,
            h_inst,
            0
        )

        self.win32_tid = kernel32.GetCurrentThreadId()
        self.log("系统状态", "蓝牙指令通道已就绪 (AVRCP / ShellHook / RawInput)", "INFO")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self.h_hook:
            user32.UnhookWindowsHookEx(self.h_hook)
        if self.hwnd:
            user32.DeregisterShellHookWindow(self.hwnd)
            user32.DestroyWindow(self.hwnd)

    def _udp_loop(self):
        """直连 127.0.0.1:4242 仅作为头部姿态数字仪表盘，绝不产生误判日志"""
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.udp_sock.bind(('127.0.0.1', 4242))
            self.udp_sock.settimeout(0.5)
            self.log("姿态传感器", "✅ 成功接入 127.0.0.1:4242 索尼耳机六轴数据总线", "INFO")
        except Exception as e:
            self.log("姿态传感器", f"❌ 绑定 4242 端口失败: {e}", "WARN")
            return

        while self.running:
            try:
                data, _ = self.udp_sock.recvfrom(1024)
                if len(data) >= 48:
                    now = time.perf_counter()
                    dt = max(0.015, min(0.080, now - self.last_time))
                    self.last_time = now

                    x, y, z, yaw, pitch, roll = struct.unpack('<6d', data[:48])

                    if self.last_yaw is not None:
                        dy = yaw - self.last_yaw
                        dp = pitch - self.last_pitch
                        dr = roll - self.last_roll

                        if dy > 180.0: dy -= 360.0
                        elif dy < -180.0: dy += 360.0
                        if dp > 180.0: dp -= 360.0
                        elif dp < -180.0: dp += 360.0
                        if dr > 180.0: dr -= 360.0
                        elif dr < -180.0: dr += 360.0

                        yr = dy / dt
                        pr = dp / dt
                        rr = dr / dt
                        speed = math.hypot(yr, pr, rr)

                        # 纯更新 UI 仪表盘数字，绝对不在这里做任何敲击猜测或日志输出
                        if self.telemetry_cb:
                            self.telemetry_cb(yaw, pitch, roll, speed)

                    self.last_yaw, self.last_pitch, self.last_roll = yaw, pitch, roll
            except socket.timeout:
                continue
            except Exception:
                break

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
        self.root.title("🎧 Sony LinkBuds 耳机动作原始数据诊断工具 (纯净原样版)")
        self.root.geometry("880x640")
        self.root.minsize(750, 500)

        self.topmost_var = tk.BooleanVar(value=True)
        self.root.attributes("-topmost", True)

        self.log_queue = queue.Queue()
        self.sniffer = DedicatedHeadsetSniffer(self.log_queue, self._on_telemetry)

        self._build_ui()
        self.sniffer.start()

        self.root.after(40, self._process_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # 顶部提示
        top_frame = ttk.Frame(self.root, padding=12)
        top_frame.pack(fill=tk.X)

        title = ttk.Label(top_frame, text="🎧 Sony LinkBuds 硬件指令与耳周点击原始监听器", font=("Microsoft YaHei UI", 12, "bold"))
        title.pack(anchor=tk.W)

        desc = ttk.Label(
            top_frame,
            text="★ 已彻底移除所有“头部转动翻译”与“冲击波猜测”，纯粹捕获耳机向 Windows 上报的原生信号。\n"
                 "★ 右耳点击已验证：双连击触发【播放/暂停 0xB3】，三连击触发【下一首 0xB0】。\n"
                 "★ 左耳点击请注意：耳周点击硬件机制必须是快速敲两下(双击)或三下；若仍无反应，请在手机 Sony App 检查左耳功能分配。",
            font=("Microsoft YaHei UI", 9),
            foreground="#333333",
            justify=tk.LEFT
        )
        desc.pack(anchor=tk.W, pady=(4, 6))

        # 实时姿态数据（仅展示数值）
        meter_frame = ttk.LabelFrame(self.root, text="📡 索尼耳机实时头部姿态 (UDP 4242 仅作仪表盘展示)", padding=8)
        meter_frame.pack(fill=tk.X, padx=12, pady=(0, 8))

        m_box = ttk.Frame(meter_frame)
        m_box.pack(fill=tk.X)

        self.lbl_yaw = ttk.Label(m_box, text="偏航角 (Yaw): 0.0°", font=("Consolas", 10, "bold"), width=22)
        self.lbl_yaw.pack(side=tk.LEFT)

        self.lbl_pitch = ttk.Label(m_box, text="俯仰角 (Pitch): 0.0°", font=("Consolas", 10, "bold"), width=22)
        self.lbl_pitch.pack(side=tk.LEFT)

        self.lbl_roll = ttk.Label(m_box, text="翻滚角 (Roll): 0.0°", font=("Consolas", 10, "bold"), width=22)
        self.lbl_roll.pack(side=tk.LEFT)

        self.lbl_speed = ttk.Label(m_box, text="转头速度: 0.0°/s", font=("Consolas", 10), foreground="#666666")
        self.lbl_speed.pack(side=tk.LEFT)

        # 最新捕获指令卡片
        card_box = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        card_box.pack(fill=tk.X)

        self.card_latest = ttk.Label(
            card_box,
            text="⚡ 最近收到的硬件指令: 等待耳机操作...",
            font=("Microsoft YaHei UI", 10, "bold"),
            background="#F0F4F8",
            foreground="#005A9E",
            padding=8,
            relief=tk.GROOVE
        )
        self.card_latest.pack(fill=tk.X)

        # 控制栏
        ctrl = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        ctrl.pack(fill=tk.X)

        btn_copy = ttk.Button(ctrl, text="📋 复制全部诊断数据", command=self.copy_all)
        btn_copy.pack(side=tk.LEFT, padx=(0, 6))

        btn_clear = ttk.Button(ctrl, text="🗑️ 清空日志", command=self.clear_logs)
        btn_clear.pack(side=tk.LEFT, padx=(0, 6))

        btn_save = ttk.Button(ctrl, text="💾 保存诊断报告", command=self.save_logs)
        btn_save.pack(side=tk.LEFT, padx=(0, 12))

        chk_top = ttk.Checkbutton(ctrl, text="窗口始终置顶", variable=self.topmost_var, command=self.toggle_topmost)
        chk_top.pack(side=tk.RIGHT)

        # 日志输出文本框
        log_frame = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.text = tk.Text(log_frame, wrap=tk.WORD, font=("Consolas", 10), bg="#1E1E1E", fg="#D4D4D4", insertbackground="white")
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.text.tag_config("TIME", foreground="#777777")
        self.text.tag_config("HIT", foreground="#38E888", font=("Consolas", 10, "bold"))
        self.text.tag_config("WARN", foreground="#E8A038")
        self.text.tag_config("INFO", foreground="#4DAAF8")

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost_var.get())

    def _on_telemetry(self, yaw, pitch, roll, speed):
        self.lbl_yaw.config(text=f"偏航角 (Yaw): {yaw:+6.1f}°")
        self.lbl_pitch.config(text=f"俯仰角 (Pitch): {pitch:+6.1f}°")
        self.lbl_roll.config(text=f"翻滚角 (Roll): {roll:+6.1f}°")
        self.lbl_speed.config(text=f"转头速度: {speed:5.1f}°/s")

    def _process_queue(self):
        while not self.log_queue.empty():
            t_str, channel, msg, level = self.log_queue.get_nowait()
            self.text.insert(tk.END, f"[{t_str}] ", "TIME")
            self.text.insert(tk.END, f"[{channel}] ", level)
            self.text.insert(tk.END, f"{msg}\n")
            self.text.see(tk.END)

            if level == "HIT":
                self.card_latest.config(
                    text=f"⚡ 最新捕获: {msg}",
                    background="#D4EDDA",
                    foreground="#155724"
                )

        self.root.after(40, self._process_queue)

    def copy_all(self):
        content = self.text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showinfo("提示", "当前日志为空")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        messagebox.showinfo("成功", "诊断数据已复制到剪贴板，请直接粘贴发送！")

    def clear_logs(self):
        self.text.delete("1.0", tk.END)

    def save_logs(self):
        content = self.text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showinfo("提示", "当前日志为空")
            return
        fp = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
            initialfile="SonyLinkBuds_Diagnostic_Report.txt"
        )
        if fp:
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(content)
                messagebox.showinfo("成功", f"诊断报告已保存到:\n{fp}")
            except Exception as e:
                messagebox.showerror("错误", f"保存失败: {e}")

    def _on_close(self):
        self.sniffer.stop()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = TapTesterApp(root)
    root.mainloop()
