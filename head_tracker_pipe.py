"""
Sony LinkBuds / XM 系列耳机高精度头控驱动模块 (UDP 4242 + 进程守护)
- 兼容 Sony LinkBuds Open, LinkBuds S, WF-1000XM5, WH-1000XM5 等全部支持 Android Head Tracker 的索尼设备
- 底层调度 sony-head-tracker.exe bridge --port 4242 采集 25Hz 高频六轴姿态数据
- 通过本地环回 UDP 4242 端口高效接收 48 字节 IEEE-754 姿态数据包 (x, y, z, yaw, pitch, roll)
- 内置 A2DP 静音保活引擎，防止索尼耳机 2.5 秒休眠
- 实时监控底层驱动输出并动态更新连接状态，支持故障自愈与平滑重连
"""

import os
import sys
import time
import socket
import struct
import threading
import subprocess

from silent_keepalive import SilentAudioKeepalive

UDP_IP = "127.0.0.1"
UDP_PORT = 4242

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

    def rebind(self):
        """调用底层蓝牙服务重新探测与修复功能 (针对系统冷启动或蓝牙断连恢复)"""
        def _do_rebind():
            if self.on_status:
                self.on_status("正在扫描并重新探测 Sony 蓝牙 HID 节点...")
            try:
                self._kill_subprocess()
                # 自动重新扫描绑定，不带硬编码过滤，兼容所有型号
                subprocess.run(
                    [self.exe_path, "bluetooth-rebind"],
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                    timeout=15,
                    capture_output=True
                )
            except Exception:
                pass

            # 刷新静音音频通道唤醒蓝牙 A2DP 链路
            self.keepalive.refresh()

            if self.on_status:
                self.on_status("蓝牙节点已刷新，正在重新连接...")

            # 重启子进程以快速接管
            self._start_subprocess()

        threading.Thread(target=_do_rebind, daemon=True).start()

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

        # 启动驱动桥接子进程
        self._start_subprocess()

        # 启动看门狗守护线程
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

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
        """读取子进程输出，实时识别耳机状态与设备型号"""
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
        if "Tracking " in line:
            # 示例: Tracking LinkBuds Open (80:99:E7:85:66:84)...
            model = line.split("Tracking ")[-1].strip()
            if "(" in model:
                model = model.split("(")[0].strip()
            self.device_name = model if model else "Sony 耳机"
            self.is_connected = True
            if self.on_connection:
                self.on_connection(True)
            if self.on_status:
                self.on_status(f"✅ 已直连 {self.device_name}")

        elif "No Android Head Tracker" in line:
            if self.is_connected:
                self.is_connected = False
                if self.on_connection:
                    self.on_connection(False)
            if self.on_status:
                self.on_status("等待连接 Sony 耳机 (请戴上耳机/开启蓝牙)...")

    def _watchdog_loop(self):
        """监测连接活性与子进程存活状态，断开时自动尝试重连"""
        while self.running:
            time.sleep(3.0)
            if not self.running:
                break
            now = time.time()

            # 1. 检查子进程是否已退出 (未连接耳机时 bridge 进程退出为正常现象)
            if self.process and self.process.poll() is not None:
                if self.is_connected:
                    self.is_connected = False
                    if self.on_connection:
                        self.on_connection(False)
                    if self.on_status:
                        self.on_status("等待连接 Sony 耳机 (请戴上耳机/开启蓝牙)...")
                # 稍作等待后重启子进程尝试重新探测
                time.sleep(2.0)
                if self.running:
                    self._start_subprocess()
                continue

            # 2. 如果子进程仍在运行但超过 3.5 秒未收到 UDP 数据包
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
