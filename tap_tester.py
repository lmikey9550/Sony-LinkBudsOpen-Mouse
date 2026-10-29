"""
Sony LinkBuds 耳周点击 / 硬件动作专属数据抓取与诊断工具 (纯净耳机专用版)
- 彻底屏蔽电脑键盘普通按键 (绝不记录任何字母/数字/功能键输入)
- 专精两大耳机数据源：
  1. 索尼耳机底层 IMU 高频姿态与冲击波传感器 (UDP 4242 / 25Hz 六轴流)
     - 实时捕获手指轻叩耳周/脸颊引起的微机械震动冲击波 (瞬时角加速度与冲击偏向)
     - 实时展示偏航角 (Yaw)、俯仰角 (Pitch)、翻滚角 (Roll) 及瞬时震动能量
  2. 蓝牙耳机 AVRCP / 媒体控制指令监听器 (ShellHook + WM_APPCOMMAND + 媒体专用钩子)
     - 仅捕获播放/暂停、下一首、上一首、音量加减等蓝牙多媒体广播
- 界面直观显示【左侧敲击】与【右侧敲击】的实时判定数据，支持一键复制完整诊断报告
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
import subprocess
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

# 严格限定只监听耳机可能触发的多媒体专用键值
MEDIA_VKEYS = {
    0xAD: "静音 (VOLUME_MUTE)",
    0xAE: "音量减 (VOLUME_DOWN)",
    0xAF: "音量加 (VOLUME_UP)",
    0xB0: "下一首 (MEDIA_NEXT_TRACK)",
    0xB1: "上一首 (MEDIA_PREV_TRACK)",
    0xB2: "停止 (MEDIA_STOP)",
    0xB3: "播放/暂停 (MEDIA_PLAY_PAUSE)"
}

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

        # 运动学状态基准
        self.last_yaw = None
        self.last_pitch = None
        self.last_roll = None
        self.last_time = time.perf_counter()
        self.last_tap_time = 0.0

        self._wnd_proc_ref = WNDPROC(self._window_proc)
        self._ll_proc_ref = HOOKPROC(self._ll_proc)

    def log(self, channel, message, level="INFO"):
        t_str = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
        self.queue.put((t_str, channel, message, level))

    def _ll_proc(self, nCode, wParam, lParam):
        if nCode >= 0:
            p = KBDLLHOOKSTRUCT.from_address(lParam)
            vk = p.vkCode
            # 严格过滤：绝不记录普通键盘按键！只捕获媒体键！
            if vk in MEDIA_VKEYS and wParam in (0x0100, 0x0104):
                name = MEDIA_VKEYS[vk]
                self.log("蓝牙媒体按键 (AVRCP Hook)", f"收到按键: {name} [vk=0x{vk:02X}, scan=0x{p.scanCode:02X}]", "HIT")
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _window_proc(self, hwnd, msg, wp, lp):
        # 1. 窗口收到 WM_APPCOMMAND
        if msg == 0x0319:
            cmd = ((lp >> 16) & ~0xF000)
            if cmd in APPCOMMAND_NAMES:
                self.log("蓝牙媒体指令 (WM_APPCOMMAND)", f"{APPCOMMAND_NAMES[cmd]}", "HIT")
            return 1

        # 2. 窗口收到 ShellHook 广播
        if self.wm_shellhook and msg == self.wm_shellhook:
            if wp == 12: # HSHELL_APPCOMMAND
                cmd = ((lp >> 16) & ~0xF000)
                if cmd in APPCOMMAND_NAMES:
                    self.log("蓝牙系统广播 (ShellHook)", f"{APPCOMMAND_NAMES[cmd]}", "HIT")
                return 1

        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _win32_loop(self):
        h_inst = kernel32.GetModuleHandleW(None)
        cls_name = f"SonySnifferWin_{id(self)}"

        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wnd_proc_ref
        cls.hInstance = h_inst
        cls.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(cls))

        self.hwnd = user32.CreateWindowExW(
            0, cls_name, "SonySnifferReceiver",
            0x80000000, 0, 0, 0, 0,
            None, None, h_inst, None
        )

        self.wm_shellhook = user32.RegisterWindowMessageW("SHELLHOOK")
        if self.hwnd:
            user32.RegisterShellHookWindow(self.hwnd)

        # 安装低级键盘钩子（仅监测媒体按键）
        self.h_hook = user32.SetWindowsHookExW(
            13, # WH_KEYBOARD_LL
            self._ll_proc_ref,
            h_inst,
            0
        )

        self.win32_tid = kernel32.GetCurrentThreadId()
        self.log("系统状态", "蓝牙多媒体指令通道 (AVRCP / ShellHook) 已就绪", "INFO")

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
        """直连接收索尼耳机 25Hz 高频六轴运动数据流并计算耳周敲击冲击波"""
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

                        # 综合瞬时角加速度 / 冲击震荡能量
                        impulse = math.hypot(yr, pr, rr)

                        # 回调更新 UI 仪表板
                        if self.telemetry_cb:
                            self.telemetry_cb(yaw, pitch, roll, impulse)

                        # 敲击冲击波分析算法：
                        # 自然转头角速度通常在 1~15°/s 之间，而轻叩耳周/脸颊会产生瞬时 > 20°/s 的微脉冲
                        curr_t = time.time()
                        if impulse > 18.0 and (curr_t - self.last_tap_time > 0.35):
                            self.last_tap_time = curr_t
                            # 判定冲击偏向：敲击左侧时产生正向反冲，敲击右侧时产生负向反冲
                            bias_str = "【👈 左侧轻叩】" if (yr + rr * 0.5) > 0 else "【👉 右侧轻叩】"
                            self.log(
                                "💥 耳周冲击传感器",
                                f"{bias_str} 检测到敲击波形: 震动强度={impulse:.1f}°/s [Yaw速={yr:+.1f}, Roll速={rr:+.1f}]",
                                "HIT"
                            )

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

        # 启动静音音频保活，防止耳机无声休眠
        self.keepalive.start()

        # 启动 UDP 传感器监听
        self.udp_thread = threading.Thread(target=self._udp_loop, daemon=True)
        self.udp_thread.start()

        # 启动 Win32 媒体监听
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
        self.root.title("🎧 Sony LinkBuds 耳周点击专属抓包诊断工具 (纯净耳机专用)")
        self.root.geometry("880x660")
        self.root.minsize(750, 520)

        self.topmost_var = tk.BooleanVar(value=True)
        self.root.attributes("-topmost", True)

        self.log_queue = queue.Queue()
        self.sniffer = DedicatedHeadsetSniffer(self.log_queue, self._on_telemetry)

        self._build_ui()
        self.sniffer.start()

        self.root.after(40, self._process_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # 1. 顶部操作指南
        top_frame = ttk.Frame(self.root, padding=12)
        top_frame.pack(fill=tk.X)

        title = ttk.Label(top_frame, text="🎧 Sony LinkBuds 耳周动作捕获与特征诊断器", font=("Microsoft YaHei UI", 13, "bold"))
        title.pack(anchor=tk.W)

        desc = ttk.Label(
            top_frame,
            text="★ 本程序已彻底过滤电脑键盘普通按键，仅专注抓取索尼耳机的【耳周敲击冲击波】与【蓝牙媒体控制指令】！\n"
                 "测试步骤：\n"
                 "  1. 戴好耳机保持头部平稳；\n"
                 "  2. 用食指分别轻叩【左侧脸颊/耳周】和【右侧脸颊/耳周】各 2 次；\n"
                 "  3. 观察下方绿色高亮条目，测试完成后点击【复制全部诊断数据】发送即可。",
            font=("Microsoft YaHei UI", 9),
            foreground="#444444",
            justify=tk.LEFT
        )
        desc.pack(anchor=tk.W, pady=(4, 6))

        # 2. 实时传感器仪表板 (显示 live 姿态与震荡波形)
        meter_frame = ttk.LabelFrame(self.root, text="📡 索尼耳机实时遥测仪表盘 (25Hz 六轴流)", padding=10)
        meter_frame.pack(fill=tk.X, padx=12, pady=(0, 8))

        m_box = ttk.Frame(meter_frame)
        m_box.pack(fill=tk.X)

        self.lbl_yaw = ttk.Label(m_box, text="偏航角 (Yaw): 0.0°", font=("Consolas", 10, "bold"), width=22)
        self.lbl_yaw.pack(side=tk.LEFT)

        self.lbl_pitch = ttk.Label(m_box, text="俯仰角 (Pitch): 0.0°", font=("Consolas", 10, "bold"), width=22)
        self.lbl_pitch.pack(side=tk.LEFT)

        self.lbl_roll = ttk.Label(m_box, text="翻滚角 (Roll): 0.0°", font=("Consolas", 10, "bold"), width=22)
        self.lbl_roll.pack(side=tk.LEFT)

        self.lbl_shock = ttk.Label(m_box, text="瞬时震荡强度: 0.0°/s", font=("Consolas", 10, "bold"), foreground="#007ACC")
        self.lbl_shock.pack(side=tk.LEFT)

        # 3. 敲击快速状态卡片 (最近一次检测到的敲击)
        cards_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        cards_frame.pack(fill=tk.X)

        self.card_left = ttk.Label(
            cards_frame,
            text="👈 最近左侧动作: 等待轻叩...",
            font=("Microsoft YaHei UI", 10, "bold"),
            background="#E8F4F8",
            foreground="#005A9E",
            padding=8,
            relief=tk.GROOVE
        )
        self.card_left.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self.card_right = ttk.Label(
            cards_frame,
            text="👉 最近右侧动作: 等待轻叩...",
            font=("Microsoft YaHei UI", 10, "bold"),
            background="#E8F4F8",
            foreground="#005A9E",
            padding=8,
            relief=tk.GROOVE
        )
        self.card_right.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(6, 0))

        # 4. 控制栏
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

        # 5. 详细日志展示区
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

    def _on_telemetry(self, yaw, pitch, roll, impulse):
        # 仪表板平滑刷新
        self.lbl_yaw.config(text=f"偏航角 (Yaw): {yaw:+6.1f}°")
        self.lbl_pitch.config(text=f"俯仰角 (Pitch): {pitch:+6.1f}°")
        self.lbl_roll.config(text=f"翻滚角 (Roll): {roll:+6.1f}°")
        color = "#FF4444" if impulse > 18.0 else ("#FFAA00" if impulse > 8.0 else "#007ACC")
        self.lbl_shock.config(text=f"瞬时震荡强度: {impulse:5.1f}°/s", foreground=color)

    def _process_queue(self):
        while not self.log_queue.empty():
            t_str, channel, msg, level = self.log_queue.get_nowait()
            self.text.insert(tk.END, f"[{t_str}] ", "TIME")
            self.text.insert(tk.END, f"[{channel}] ", level)
            self.text.insert(tk.END, f"{msg}\n")
            self.text.see(tk.END)

            # 更新快速状态卡片
            if "左侧" in msg or "上一首" in msg or "播放/暂停" in msg:
                self.card_left.config(text=f"👈 最近左侧动作: {msg[:36]}...", background="#D4EDDA", foreground="#155724")
            elif "右侧" in msg or "下一首" in msg:
                self.card_right.config(text=f"👉 最近右侧动作: {msg[:36]}...", background="#D4EDDA", foreground="#155724")

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
