"""
Sony LinkBuds / XM 系列耳机高精度头控驱动模块 (自动唤醒修复 + UDP/管道双冗余)
- 兼容 Sony LinkBuds Open, LinkBuds S, WF-1000XM5, WH-1000XM5 等全部支持 Android Head Tracker 的索尼设备
- 内置自动 Repair 修复引擎：开机/冷启动自动执行 Repair 唤醒 Bluetooth HID 子设备并绑定通用驱动，彻底无需手动打开 sony-head-tracker 界面点 Repair
- 通过本地环回 UDP 4242 端口高效接收 48 字节 IEEE-754 姿态数据包，辅以 stdout 管道流双重容错保底
- 内置 A2DP 静音保活引擎，彻底解决索尼固件 2.5 秒无音频休眠的难题
- 支持托盘一键无缝“唤醒/修复耳机头控节点 (Repair Tracker)”
"""

import os
import re
import sys
import time
import socket
import struct
import ctypes
import threading
import subprocess

from silent_keepalive import SilentAudioKeepalive

UDP_IP = "127.0.0.1"
UDP_PORT = 4242

# 格式示例：YPR    1.08   -2.94   -1.07   23.2 pps
YPR_PATTERN = re.compile(
    r'YPR\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)'
)

class HeadTrackerPipe:
    def __init__(self, on_motion_callback=None, on_status_callback=None, on_connection_callback=None):
        self.on_motion = on_motion_callback
        self.on_status = on_status_callback
        self.on_connection = on_connection_callback

        self.process = None
        self.running = False
        self.device_name = "Sony 耳机"

        # 线程与网络句柄
        self.udp_thread = None
        self.stdout_thread = None
        self.watchdog_thread = None
        self.udp_sock = None

        # 蓝牙 A2DP 静音保活引擎 (防止索尼耳机 2.5 秒休眠)
        self.keepalive = SilentAudioKeepalive()

        # 姿态基准与状态
        self.last_yaw = None
        self.last_pitch = None
        self.last_time = time.perf_counter()

        self.bias_yaw = 0.0
        self.bias_pitch = 0.0

        self.current_yaw = 0.0
        self.current_pitch = 0.0
        self.current_roll = 0.0

        self.is_connected = False
        self.last_sample_time = 0.0
        self.last_repair_time = 0.0

        # 定位底层的独立驱动引擎 (兼顾源码运行与 PyInstaller 打包环境)
        self.exe_path = self._find_tracker_exe()

    def _find_tracker_exe(self):
        candidates = []
        # 1. PyInstaller 运行时解压临时目录
        if hasattr(sys, '_MEIPASS'):
            candidates.append(os.path.join(sys._MEIPASS, "bin", "sony-head-tracker.exe"))
            candidates.append(os.path.join(sys._MEIPASS, "sony-head-tracker.exe"))

        # 2. 独立打包后的主可执行程序所在目录
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            candidates.append(os.path.join(exe_dir, "bin", "sony-head-tracker.exe"))
            candidates.append(os.path.join(exe_dir, "sony-head-tracker.exe"))

        # 3. 源码开发目录
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(base_dir, "bin", "sony-head-tracker.exe"))
        candidates.append(os.path.join(base_dir, "sony-head-tracker.exe"))

        for p in candidates:
            if os.path.exists(p):
                return p
        return candidates[0]

    def calibrate(self):
        """居中校准：以当前朝向为零点基准"""
        self.bias_yaw = self.current_yaw
        self.bias_pitch = self.current_pitch
        if self.on_status:
            self.on_status("已校准：当前朝向已设为中心基准点")

    def _repair_tracker(self, interactive=False):
        """执行底层驱动的一键修复/节点唤醒 (完全等效于 sony-head-tracker 界面的 Repair Tracker 按钮)"""
        now = time.time()
        # 限制修复调用频率，避免 10 秒内重复触发
        if now - self.last_repair_time < 10.0 and not interactive:
            return
        self.last_repair_time = now

        if self.on_status:
            self.on_status("正在激活/修复 Sony 耳机头控 HID 节点 (Repair)...")

        try:
            self._kill_subprocess()

            is_admin = False
            try:
                is_admin = ctypes.windll.shell32.IsUserAnAdmin()
            except Exception:
                pass

            if is_admin:
                # 已具备管理员权限，直接以 --elevated --no-launch 静默无黑框极速修复
                subprocess.run(
                    [self.exe_path, "repair", "--elevated", "--no-launch"],
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                    timeout=15,
                    capture_output=True
                )
            else:
                # 非管理员权限时通过 Windows ShellExecuteW runas 唤起 UAC 授权弹窗
                ctypes.windll.shell32.ShellExecuteW(
                    None,
                    "runas",
                    self.exe_path,
                    "repair --elevated --no-launch",
                    None,
                    1 if interactive else 0
                )
                time.sleep(5.0)
        except Exception as e:
            print("执行 Repair 异常:", e)

        # 刷新静音音频通道唤醒蓝牙 A2DP 链路
        self.keepalive.refresh()

        if self.on_status:
            self.on_status("HID 节点已刷新，正在直连...")

    def rebind(self):
        """托盘菜单手动点击“唤醒/修复耳机头控节点”"""
        def _do_manual_rebind():
            self._repair_tracker(interactive=True)
            self._start_subprocess()

        threading.Thread(target=_do_manual_rebind, daemon=True).start()

    def _is_tracker_accessible(self):
        """快速探测当前 Windows 是否已正确暴露 Android Head Tracker HID 设备"""
        try:
            res = subprocess.run(
                [self.exe_path, "probe"],
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                capture_output=True,
                text=True,
                timeout=4
            )
            return res.returncode == 0
        except Exception:
            return False

    def start(self):
        if self.running:
            return
        self.running = True

        # 清理可能残留的孤儿进程
        self._clean_orphaned_trackers()

        # 启动静音保活，阻止索尼耳机固件休眠
        self.keepalive.start()

        # 启动 UDP 接收监听
        self._start_udp_receiver()

        # 启动初始化线程：探测设备状态并在必要时自动执行 repair
        init_thread = threading.Thread(target=self._startup_sequence, daemon=True)
        init_thread.start()

        # 启动看门狗守护线程
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def _startup_sequence(self):
        """自愈启动序列：检查节点可达性，若未就绪则自动执行静默修复后直连"""
        if self.on_status:
            self.on_status("正在检测 Sony 耳机头控状态...")

        if not self._is_tracker_accessible():
            # 首次启动或冷启动时设备未唤醒，自动触发一次修复
            self._repair_tracker(interactive=False)

        # 启动驱动桥接子进程
        self._start_subprocess()

    def stop(self):
        self.running = False
        self.keepalive.stop()
        self._stop_udp_receiver()
        self._kill_subprocess()
        self._clean_orphaned_trackers()

    def _clean_orphaned_trackers(self):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "sony-head-tracker.exe"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
        except Exception:
            pass

    def _start_udp_receiver(self):
        self._stop_udp_receiver()
        try:
            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_sock.bind((UDP_IP, UDP_PORT))
            self.udp_sock.settimeout(0.5)
            self.udp_thread = threading.Thread(target=self._udp_listener_loop, daemon=True)
            self.udp_thread.start()
        except Exception as e:
            if self.on_status:
                self.on_status(f"❌ 绑定 UDP 接收端口 {UDP_PORT} 失败: {e}")

    def _stop_udp_receiver(self):
        if self.udp_sock:
            try:
                self.udp_sock.close()
            except Exception:
                pass
            self.udp_sock = None

    def _udp_listener_loop(self):
        """UDP 接收循环：从 127.0.0.1:4242 接收 48 字节 IEEE-754 双精度姿态数据"""
        while self.running:
            sock = self.udp_sock
            if not sock:
                break
            try:
                data, _ = sock.recvfrom(1024)
                if len(data) >= 48:
                    x, y, z, yaw, pitch, roll = struct.unpack('<6d', data[:48])
                    self._handle_motion_sample(yaw, pitch, roll)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                continue

    def _start_subprocess(self):
        self._kill_subprocess()
        if not os.path.exists(self.exe_path):
            if self.on_status:
                self.on_status("❌ 未找到底层驱动引擎，请检查 bin 目录")
            return

        cmd = [self.exe_path, "bridge", "--port", str(UDP_PORT)]
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding='utf-8',
                errors='ignore',
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )

            self.stdout_thread = threading.Thread(target=self._stdout_reader_loop, daemon=True)
            self.stdout_thread.start()
        except Exception as e:
            if self.on_status:
                self.on_status(f"❌ 启动驱动子进程失败: {e}")

    def _kill_subprocess(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.kill()
            except Exception:
                pass
            self.process = None

    def _stdout_reader_loop(self):
        """读取子进程输出，实时识别耳机状态、设备型号并支持 stdout 数据流冗余解析"""
        proc = self.process
        if not proc or not proc.stdout:
            return

        try:
            for line in iter(proc.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue
                self._handle_status_line(line)
        except Exception:
            pass

    def _handle_status_line(self, line):
        # 1. 识别已成功连接的设备名称
        if "Tracking " in line:
            # 格式 1: Tracking headset: LinkBuds Open
            # 格式 2: Tracking LinkBuds Open (80:99:E7:85:66:84)...
            model = line.split("Tracking ")[-1].strip()
            if "headset:" in model:
                model = model.split("headset:")[-1].strip()
            if "(" in model:
                model = model.split("(")[0].strip()
            self.device_name = model if model else "Sony 耳机"
            self.is_connected = True
            if self.on_connection:
                self.on_connection(True)
            if self.on_status:
                self.on_status(f"✅ 已直连 {self.device_name}")
            return

        # 2. 识别未找到设备
        if "No Android Head Tracker" in line:
            if self.is_connected:
                self.is_connected = False
                if self.on_connection:
                    self.on_connection(False)
            if self.on_status:
                self.on_status("等待连接 Sony 耳机 (请戴上耳机/开启蓝牙)...")
            return

        # 3. 冗余处理标准输出的 YPR 姿态数据 (若 UDP 未收到采样时作为无缝保底)
        m = YPR_PATTERN.search(line)
        if m:
            now = time.time()
            if now - self.last_sample_time > 0.035:
                yaw = float(m.group(1))
                pitch = float(m.group(2))
                roll = float(m.group(3))
                self._handle_motion_sample(yaw, pitch, roll)

    def _watchdog_loop(self):
        """监测连接活性与子进程存活状态，断开时自动尝试重连与唤醒"""
        consecutive_failures = 0
        while self.running:
            time.sleep(3.0)
            if not self.running:
                break
            now = time.time()

            # 1. 检查子进程是否已退出 (未连接耳机时 bridge 进程退出为正常现象)
            if self.process and self.process.poll() is not None:
                consecutive_failures += 1
                if self.is_connected:
                    self.is_connected = False
                    if self.on_connection:
                        self.on_connection(False)
                    if self.on_status:
                        self.on_status("等待连接 Sony 耳机 (请戴上耳机/开启蓝牙)...")

                # 如果连续多次启动失败（可能系统驱动节点丢失），自动触发一次自愈修复
                if consecutive_failures >= 3:
                    consecutive_failures = 0
                    self._repair_tracker(interactive=False)

                time.sleep(2.0)
                if self.running:
                    self._start_subprocess()
                continue
            else:
                consecutive_failures = 0

            # 2. 如果子进程仍在运行但超过 3.5 秒未收到姿态数据包
            if self.is_connected and (now - self.last_sample_time > 3.5):
                self.is_connected = False
                if self.on_connection:
                    self.on_connection(False)
                if self.on_status:
                    self.on_status("等待连接 Sony 耳机 (请戴上耳机/开启蓝牙)...")
                self.keepalive.refresh()

    def _handle_motion_sample(self, yaw_deg, pitch_deg, roll_deg):
        now_time = time.perf_counter()
        self.last_sample_time = time.time()

        if not self.is_connected:
            self.is_connected = True
            if self.on_connection:
                self.on_connection(True)
            if self.on_status:
                self.on_status(f"✅ 已直连 {self.device_name}")

        self.current_yaw = yaw_deg
        self.current_pitch = pitch_deg
        self.current_roll = roll_deg

        # Sony 硬件采样率为 ~25Hz (0.040s)
        # 防止微秒级 dt 造成瞬时角速度畸变，限制安全 dt 区间
        raw_dt = now_time - self.last_time
        if raw_dt < 0.010:
            dt = 0.040
        else:
            dt = min(0.080, max(0.020, raw_dt))
        self.last_time = now_time

        if self.last_yaw is not None:
            d_yaw = yaw_deg - self.last_yaw
            d_pitch = pitch_deg - self.last_pitch

            # 处理 ±180° 跨界回绕
            if d_yaw > 180.0:    d_yaw -= 360.0
            elif d_yaw < -180.0:  d_yaw += 360.0
            if d_pitch > 180.0:   d_pitch -= 360.0
            elif d_pitch < -180.0: d_pitch += 360.0

            yaw_rate = d_yaw / dt
            pitch_rate = d_pitch / dt

            if self.on_motion:
                self.on_motion(
                    yaw_rate, pitch_rate,
                    yaw_deg, pitch_deg, roll_deg
                )

        self.last_yaw = yaw_deg
        self.last_pitch = pitch_deg
