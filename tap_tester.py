"""
Sony LinkBuds 耳周点击 / 硬件按键全通道数据抓取与诊断测试工具
- 全通道监听：
  1. 全局底层键盘钩子 (WH_KEYBOARD_LL)：监听所有多媒体键与虚拟键
  2. 原始输入接收器 (Raw Input WM_INPUT)：监听 Consumer Controls (0x0C) 与 Keyboard (0x01)
  3. 系统 Shell 命令 (RegisterShellHookWindow / HSHELL_APPCOMMAND)
  4. 窗口应用命令 (WM_APPCOMMAND)
  5. 索尼耳机底层原始 HID 数据包监听器 (VID 0x054C, PID 0x0EDC)
  6. 256 位虚拟键状态高频异步轮询 (GetAsyncKeyState)
- 界面直观显示每一条捕获到的事件与原始十六进制数据
- 支持一键复制全部日志
"""

import os
import sys
import time
import math
import queue
import ctypes
from ctypes import wintypes
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import hid
except ImportError:
    hid = None

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

# 常用多媒体键字典
VK_NAMES = {
    0xAD: "音量静音 (VK_VOLUME_MUTE)",
    0xAE: "音量减 (VK_VOLUME_DOWN)",
    0xAF: "音量加 (VK_VOLUME_UP)",
    0xB0: "下一首 (VK_MEDIA_NEXT_TRACK)",
    0xB1: "上一首 (VK_MEDIA_PREV_TRACK)",
    0xB2: "停止播放 (VK_MEDIA_STOP)",
    0xB3: "播放/暂停 (VK_MEDIA_PLAY_PAUSE)",
    0xB4: "启动邮件 (VK_LAUNCH_MAIL)",
    0xB5: "启动媒体选择 (VK_LAUNCH_MEDIA_SELECT)",
    0xB6: "启动应用1 (VK_LAUNCH_APP1)",
    0xB7: "启动应用2 (VK_LAUNCH_APP2)",
    0xA6: "浏览器后退 (VK_BROWSER_BACK)",
    0xA7: "浏览器前进 (VK_BROWSER_FORWARD)",
    0xA8: "浏览器刷新 (VK_BROWSER_REFRESH)",
    0x5B: "左 Win 键",
    0x5C: "右 Win 键",
    0x78: "F9 键",
}

APPCOMMAND_NAMES = {
    8: "APPCOMMAND_VOLUME_MUTE (静音)",
    9: "APPCOMMAND_VOLUME_DOWN (音量-)",
    10: "APPCOMMAND_VOLUME_UP (音量+)",
    11: "APPCOMMAND_MEDIA_NEXTTRACK (下一首)",
    12: "APPCOMMAND_MEDIA_PREVIOUSTRACK (上一首)",
    13: "APPCOMMAND_MEDIA_STOP (停止)",
    14: "APPCOMMAND_MEDIA_PLAY_PAUSE (播放/暂停)",
    46: "APPCOMMAND_MEDIA_PLAY (播放)",
    47: "APPCOMMAND_MEDIA_PAUSE (暂停)",
}

class TapDataCollector:
    def __init__(self, log_queue):
        self.queue = log_queue
        self.running = False

        self.win32_thread = None
        self.win32_tid = None
        self.hwnd = None
        self.h_hook = None
        self.wm_shellhook = None

        self.hid_thread = None
        self.poller_thread = None

        self._wnd_proc_ref = WNDPROC(self._window_proc)
        self._ll_proc_ref = HOOKPROC(self._ll_proc)

    def log(self, tag, message, level="INFO"):
        t_str = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
        self.queue.put((t_str, tag, message, level))

    def _ll_proc(self, nCode, wParam, lParam):
        if nCode >= 0:
            p = KBDLLHOOKSTRUCT.from_address(lParam)
            vk = p.vkCode
            action = "按下 (KEYDOWN)" if wParam in (0x0100, 0x0104) else "释放 (KEYUP)"
            name = VK_NAMES.get(vk, f"虚拟键 0x{vk:02X}")
            if wParam in (0x0100, 0x0104):
                # 针对多媒体按键着重高亮
                lvl = "HIT" if vk in (0xB0, 0xB1, 0xB2, 0xB3, 0xAE, 0xAF) else "DEBUG"
                self.log("键盘钩子 (WH_KEYBOARD_LL)", f"{name} {action} [vk=0x{vk:02X}, scan=0x{p.scanCode:02X}, flags=0x{p.flags:02X}]", lvl)
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _window_proc(self, hwnd, msg, wp, lp):
        # 1. 窗口收到 WM_APPCOMMAND
        if msg == 0x0319: # WM_APPCOMMAND
            cmd = ((lp >> 16) & ~0xF000)
            dev = ((lp >> 16) & 0xF000) >> 12
            cmd_name = APPCOMMAND_NAMES.get(cmd, f"未知指令 (cmd={cmd})")
            self.log("应用命令 (WM_APPCOMMAND)", f"{cmd_name} [dev={dev}, lp=0x{lp:08X}]", "HIT")
            return 1

        # 2. 窗口收到 ShellHook 广播
        if self.wm_shellhook and msg == self.wm_shellhook:
            if wp == 12: # HSHELL_APPCOMMAND
                cmd = ((lp >> 16) & ~0xF000)
                cmd_name = APPCOMMAND_NAMES.get(cmd, f"未知应用指令 (cmd={cmd})")
                self.log("系统Shell (HSHELL_APPCOMMAND)", f"{cmd_name} [lp=0x{lp:08X}]", "HIT")
                return 1

        # 3. 窗口收到 Raw Input
        if msg == 0x00FF: # WM_INPUT
            dwSize = wintypes.UINT()
            user32.GetRawInputData(lp, 0x10000003, None, ctypes.byref(dwSize), ctypes.sizeof(RAWINPUTHEADER))
            if dwSize.value > 0:
                buf = (ctypes.c_byte * dwSize.value)()
                if user32.GetRawInputData(lp, 0x10000003, buf, ctypes.byref(dwSize), ctypes.sizeof(RAWINPUTHEADER)) > 0:
                    raw = RAWINPUT.from_buffer(buf)
                    if raw.header.dwType == 1: # RIM_TYPEKEYBOARD
                        vk = raw.keyboard.VKey
                        msg_type = "按下" if raw.keyboard.Message in (0x0100, 0x0104) else "释放"
                        name = VK_NAMES.get(vk, f"键 0x{vk:02X}")
                        if raw.keyboard.Message in (0x0100, 0x0104):
                            lvl = "HIT" if vk in (0xB0, 0xB1, 0xB2, 0xB3, 0xAE, 0xAF) else "DEBUG"
                            self.log("原始输入 (Keyboard)", f"{name} {msg_type} [make=0x{raw.keyboard.MakeCode:02X}, flags=0x{raw.keyboard.Flags:04X}]", lvl)
                    elif raw.header.dwType == 2: # RIM_TYPEHID
                        raw_bytes = bytes(buf[ctypes.sizeof(RAWINPUTHEADER):ctypes.sizeof(RAWINPUTHEADER)+raw.hid.dwSizeHid])
                        hex_str = ' '.join(f'{b:02X}' for b in raw_bytes)
                        self.log("原始输入 (Consumer HID)", f"收到硬件报告: [{hex_str}]", "HIT")
            return 0

        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _win32_message_loop(self):
        h_inst = kernel32.GetModuleHandleW(None)
        cls_name = f"SonyTapTesterWin_{id(self)}"

        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wnd_proc_ref
        cls.hInstance = h_inst
        cls.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(cls))

        self.hwnd = user32.CreateWindowExW(
            0, cls_name, "SonyTapTesterReceiver",
            0x80000000, 0, 0, 0, 0,
            None, None, h_inst, None
        )

        self.wm_shellhook = user32.RegisterWindowMessageW("SHELLHOOK")
        if self.hwnd:
            user32.RegisterShellHookWindow(self.hwnd)

            # 注册 Consumer Controls 与 Keyboard
            RIDEV_INPUTSINK = 0x00000100
            rid = (RAWINPUTDEVICE * 2)()
            rid[0].usUsagePage = 0x000C
            rid[0].usUsage = 0x0001
            rid[0].dwFlags = RIDEV_INPUTSINK
            rid[0].hwndTarget = self.hwnd

            rid[1].usUsagePage = 0x0001
            rid[1].usUsage = 0x0006
            rid[1].dwFlags = RIDEV_INPUTSINK
            rid[1].hwndTarget = self.hwnd

            user32.RegisterRawInputDevices(rid, 2, ctypes.sizeof(RAWINPUTDEVICE))

        self.h_hook = user32.SetWindowsHookExW(
            13, # WH_KEYBOARD_LL
            self._ll_proc_ref,
            h_inst,
            0
        )

        self.win32_tid = kernel32.GetCurrentThreadId()
        self.log("系统状态", "Win32 键盘全局钩子、ShellHook 与原始输入通道全部激活就绪", "INFO")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self.h_hook:
            user32.UnhookWindowsHookEx(self.h_hook)
        if self.hwnd:
            user32.DeregisterShellHookWindow(self.hwnd)
            user32.DestroyWindow(self.hwnd)

    def _hid_reader_loop(self):
        """直连读取索尼 LinkBuds 硬件报告，监测敲击引起的加速度/标志位变化"""
        if not hid:
            self.log("HID引擎", "未检测到 hidapi 库，跳过直连读取", "WARN")
            return

        try:
            dev = hid.device()
            dev.open(0x054C, 0x0EDC)
            dev.set_nonblocking(True)
            self.log("HID引擎", "✅ 已直连打开 Sony LinkBuds HID 节点 (VID 0x054C, PID 0x0EDC)", "INFO")
        except Exception as e:
            self.log("HID引擎", f"未能直接打开 LinkBuds HID 节点 (可能已由驱动独占或未开机): {e}", "WARN")
            return

        last_packet_time = 0.0
        baseline_gyro = None

        while self.running:
            try:
                data = dev.read(64)
                if data:
                    # 采样分析：如果是 14 字节的输入报告 1
                    if len(data) >= 14 and data[0] == 1:
                        # [1, yaw_lo, yaw_hi, pitch_lo, pitch_hi, roll_lo, roll_hi, gx_lo, gx_hi, gy_lo, gy_hi, gz_lo, gz_hi, flags]
                        flags = data[13]
                        # 提取角速度/加速度瞬时突变 (敲击脸颊会产生 > 1500 的瞬间高频震荡)
                        gx = int.from_bytes(data[7:9], byteorder='little', signed=True)
                        gy = int.from_bytes(data[9:11], byteorder='little', signed=True)
                        gz = int.from_bytes(data[11:13], byteorder='little', signed=True)
                        mag = math.hypot(gx, gy, gz)

                        now = time.time()
                        if mag > 3500 and (now - last_packet_time > 0.35):
                            last_packet_time = now
                            hex_data = ' '.join(f'{b:02X}' for b in data[:14])
                            self.log("耳机震动/敲击", f"检测到耳周高能冲击波动: 瞬时震荡强度={int(mag)}, Flags={flags}, 数据=[{hex_data}]", "HIT")

                        if flags != 1:
                            self.log("耳机状态标志", f"检测到非标准状态位改变: Flags={flags}", "HIT")
                time.sleep(0.015)
            except Exception:
                break

        try: dev.close()
        except Exception: pass

    def _async_poller_loop(self):
        """高频轮询所有多媒体按键的异步状态 (GetAsyncKeyState)"""
        target_keys = [0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xA6, 0xA7, 0xA8]
        last_states = {k: False for k in target_keys}

        while self.running:
            for vk in target_keys:
                is_down = (user32.GetAsyncKeyState(vk) & 0x8000) != 0
                if is_down and not last_states[vk]:
                    name = VK_NAMES.get(vk, hex(vk))
                    self.log("异步轮询 (GetAsyncKeyState)", f"捕获按键按下: {name} (0x{vk:02X})", "HIT")
                last_states[vk] = is_down
            time.sleep(0.015)

    def start(self):
        if self.running: return
        self.running = True

        self.win32_thread = threading.Thread(target=self._win32_message_loop, daemon=True)
        self.win32_thread.start()

        self.hid_thread = threading.Thread(target=self._hid_reader_loop, daemon=True)
        self.hid_thread.start()

        self.poller_thread = threading.Thread(target=self._async_poller_loop, daemon=True)
        self.poller_thread.start()

    def stop(self):
        self.running = False
        if self.win32_tid:
            user32.PostThreadMessageW(self.win32_tid, 0x0012, 0, 0)
        self.log("系统状态", "监听器已停止", "INFO")


class TapTesterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("🎧 Sony LinkBuds 耳周点击/按键数据抓取测试工具")
        self.root.geometry("860x620")
        self.root.minsize(700, 500)

        # 始终置顶变量
        self.topmost_var = tk.BooleanVar(value=True)
        self.root.attributes("-topmost", True)

        self.log_queue = queue.Queue()
        self.collector = TapDataCollector(self.log_queue)

        self._build_ui()
        self.collector.start()

        self.root.after(50, self._process_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # 1. 顶部操作指引卡片
        header = ttk.Frame(self.root, padding=12)
        header.pack(fill=tk.X)

        title = ttk.Label(header, text="🎧 Sony LinkBuds 耳周点击/多媒体事件嗅探器", font=("Microsoft YaHei UI", 13, "bold"))
        title.pack(anchor=tk.W)

        desc = ttk.Label(
            header,
            text="使用方法：请戴好耳机，用手指分别轻叩【左侧耳周/脸颊】与【右侧耳周/脸颊】各 2~3 次。\n"
                 "程序已同时开启 全局键盘钩子、Windows 原始输入 (Raw Input)、系统 Shell 广播、异步按键状态 与 索尼 HID 震动传感器全通道监听。\n"
                 "无论敲击产生的是媒体指令、键盘键码还是冲击波形，都会实时在下方刷出高亮日志。测试完毕后点击下方【复制全部日志】发送即可。",
            font=("Microsoft YaHei UI", 9),
            foreground="#555555",
            justify=tk.LEFT
        )
        desc.pack(anchor=tk.W, pady=(4, 6))

        # 2. 控制栏
        ctrl = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        ctrl.pack(fill=tk.X)

        btn_copy = ttk.Button(ctrl, text="📋 复制全部日志", command=self.copy_all)
        btn_copy.pack(side=tk.LEFT, padx=(0, 6))

        btn_clear = ttk.Button(ctrl, text="🗑️ 清空日志", command=self.clear_logs)
        btn_clear.pack(side=tk.LEFT, padx=(0, 6))

        btn_save = ttk.Button(ctrl, text="💾 保存日志到文件", command=self.save_logs)
        btn_save.pack(side=tk.LEFT, padx=(0, 12))

        chk_top = ttk.Checkbutton(ctrl, text="窗口始终置顶", variable=self.topmost_var, command=self.toggle_topmost)
        chk_top.pack(side=tk.RIGHT)

        # 3. 日志文本区域
        log_frame = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.text = tk.Text(log_frame, wrap=tk.WORD, font=("Consolas", 10), bg="#1E1E1E", fg="#D4D4D4", insertbackground="white")
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 颜色样式标签
        self.text.tag_config("TIME", foreground="#888888")
        self.text.tag_config("HIT", foreground="#4EC9B0", font=("Consolas", 10, "bold"))
        self.text.tag_config("WARN", foreground="#CE9178")
        self.text.tag_config("INFO", foreground="#569CD6")
        self.text.tag_config("DEBUG", foreground="#6A9955")

        # 4. 底部状态与测试按钮栏
        bot = ttk.Frame(self.root, padding=8)
        bot.pack(fill=tk.X)

        lbl_tip = ttk.Label(bot, text="💡 提示：如果敲击没有反应，请检查手机端 Sony Sound Connect 应用中的“耳周点击”开关是否处于开启状态。", font=("Microsoft YaHei UI", 9), foreground="#888888")
        lbl_tip.pack(side=tk.LEFT)

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost_var.get())

    def _process_queue(self):
        while not self.log_queue.empty():
            t_str, tag, msg, level = self.log_queue.get_nowait()
            self.text.insert(tk.END, f"[{t_str}] ", "TIME")
            self.text.insert(tk.END, f"[{tag}] ", level)
            self.text.insert(tk.END, f"{msg}\n")
            self.text.see(tk.END)
        self.root.after(50, self._process_queue)

    def copy_all(self):
        content = self.text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showinfo("提示", "当前日志为空")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        messagebox.showinfo("成功", "日志已复制到系统剪贴板，请直接粘贴发送！")

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
            initialfile="SonyLinkBuds_Tap_Log.txt"
        )
        if fp:
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(content)
                messagebox.showinfo("成功", f"日志已成功保存到:\n{fp}")
            except Exception as e:
                messagebox.showerror("错误", f"保存失败: {e}")

    def _on_close(self):
        self.collector.stop()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = TapTesterApp(root)
    root.mainloop()
