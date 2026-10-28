"""
Sony LinkBuds 内部管道驱动层 (Anonymous Pipe Driver)
- 彻底抛弃 UDP 网络套接字 (0 本地环回端口占用，零防火墙拦截，零网络协议栈开销)
- 基于 Windows 内核匿名管道 (Anonymous Pipe / Standard I/O redirection) 实现底层进程间内存直传
- 驱动子进程以隐藏窗口运行 (CREATE_NO_WINDOW)，纯后台无黑框
- 内置 A2DP 静音保活机制，彻底解决索尼固件 2.5 秒无音频休眠的难题
"""

import os
import re
import sys
import time
import threading
import subprocess

from silent_keepalive import SilentAudioKeepalive

# 正则表达式匹配驱动标准输出中的姿态角度
# 格式示例：YPR    1.08   -2.94   -1.07   23.2 pps
YPR_PATTERN = re.compile(
    rb'YPR\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)'
)
# 备用匹配格式：Yaw   +12.34°   Pitch    -5.67°   Roll    +1.23°
YAW_PATTERN = re.compile(
    rb'Yaw\s+([+-]?\d+(?:\.\d+)?)\D+Pitch\s+([+-]?\d+(?:\.\d+)?)\D+Roll\s+([+-]?\d+(?:\.\d+)?)'
)

class HeadTrackerPipe:
    def __init__(self, on_motion_callback=None, on_status_callback=None, on_connection_callback=None):
        self.on_motion = on_motion_callback
        self.on_status = on_status_callback
        self.on_connection = on_connection_callback

        self.process = None
        self.running = False
        self.reader_thread = None
        self.watchdog_thread = None

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

        # 定位底层的独立驱动引擎
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.exe_path = os.path.join(base_dir, "bin", "sony-head-tracker.exe")
        if not os.path.exists(self.exe_path):
            alt_path = r"C:\Users\lmike\Desktop\sony-head-tracker\sony-head-tracker.exe"
            if os.path.exists(alt_path):
                self.exe_path = alt_path

    def calibrate(self):
        """居中校准：以当前朝向为零点基准"""
        self.bias_yaw = self.current_yaw
        self.bias_pitch = self.current_pitch
        if self.on_status:
            self.on_status("已校准：当前朝向已设为中心基准点")

    def rebind(self):
        """调用底层的蓝牙服务重新探测与修复功能 (针对系统冷启动或蓝牙断连恢复)"""
        def _do_rebind():
            if self.on_status:
                self.on_status("正在重新探测/修复 LinkBuds 蓝牙 HID 节点...")
            try:
                subprocess.run(
                    [self.exe_path, "bluetooth-rebind", "--name", "LinkBuds Open"],
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                    timeout=8,
                    capture_output=True
                )
            except Exception:
                pass
            # 刷新静音音频通道
            self.keepalive.refresh()
            # 重启子进程以快速接管
            self._restart_subprocess()

        threading.Thread(target=_do_rebind, daemon=True).start()

    def start(self):
        if self.running:
            return
        self.running = True

        # 启动静音保活，阻止索尼耳机固件休眠
        self.keepalive.start()

        # 启动驱动桥接子进程
        self._start_subprocess()

        # 启动看门狗线程以处理自动重连
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def stop(self):
        self.running = False
        self.keepalive.stop()
        self._kill_subprocess()

    def _start_subprocess(self):
        self._kill_subprocess()
        if not os.path.exists(self.exe_path):
            if self.on_status:
                self.on_status("❌ 未找到底层驱动引擎，请检查 bin 目录")
            return

        cmd = [self.exe_path, "bridge"]
        try:
            # 内部匿名管道直通：不分配任何控制台窗口，将 stdout 重定向到操作系统匿名管道
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )

            self.reader_thread = threading.Thread(target=self._pipe_reader_loop, daemon=True)
            self.reader_thread.start()
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

    def _restart_subprocess(self):
        if not self.running:
            return
        self._start_subprocess()

    def _watchdog_loop(self):
        """监测连接活性与子进程存活状态，断开时自动尝试重连"""
        while self.running:
            time.sleep(2.0)
            now = time.time()
            if self.process and self.process.poll() is not None:
                # 子进程异常退出，自动重启
                self._restart_subprocess()
                continue

            # 如果超过 3 秒未收到姿态采样，且之前为连接状态，标记为断开/等待
            if self.is_connected and (now - self.last_sample_time > 3.0):
                self.is_connected = False
                if self.on_connection:
                    self.on_connection(False)
                if self.on_status:
                    self.on_status("等待连接 Sony LinkBuds (请戴上耳机/开启蓝牙)...")
                # 尝试刷新一下音频输出通道
                self.keepalive.refresh()

    def _pipe_reader_loop(self):
        """纯内核管道读取循环：通过底层操作系统管道读取标准输出，解析姿态"""
        proc = self.process
        if not proc or not proc.stdout:
            return

        tail = b''
        while self.running and proc.poll() is None:
            try:
                chunk = os.read(proc.stdout.fileno(), 1024)
                if not chunk:
                    break

                data = tail + chunk
                last_end = 0

                # 优先解析 YPR 数据帧 (25Hz 连续流)
                matched_any = False
                for m in YPR_PATTERN.finditer(data):
                    matched_any = True
                    self._handle_motion_sample(
                        float(m.group(1)),
                        float(m.group(2)),
                        float(m.group(3))
                    )
                    last_end = m.end()

                if not matched_any:
                    for m in YAW_PATTERN.finditer(data):
                        matched_any = True
                        self._handle_motion_sample(
                            float(m.group(1)),
                            float(m.group(2)),
                            float(m.group(3))
                        )
                        last_end = m.end()

                # 检查状态和日志信息
                if not matched_any:
                    if b'\n' in data or b'\r' in data:
                        lines = data.replace(b'\r', b'\n').split(b'\n')
                        for line in lines[:-1]:
                            self._handle_status_line(line)
                        tail = lines[-1]
                    else:
                        tail = data[-128:]
                else:
                    tail = data[last_end:]
                    if len(tail) > 256:
                        tail = tail[-128:]

            except Exception:
                break

    def _handle_motion_sample(self, yaw_deg, pitch_deg, roll_deg):
        now_time = time.perf_counter()
        self.last_sample_time = time.time()

        if not self.is_connected:
            self.is_connected = True
            if self.on_connection:
                self.on_connection(True)
            if self.on_status:
                self.on_status("✅ 已直连 Sony LinkBuds (内部管道直通)")

        self.current_yaw = yaw_deg
        self.current_pitch = pitch_deg
        self.current_roll = roll_deg

        dt = max(1e-4, min(0.1, now_time - self.last_time))
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
                    yaw_deg - self.bias_yaw,
                    pitch_deg - self.bias_pitch
                )

        self.last_yaw = yaw_deg
        self.last_pitch = pitch_deg

    def _handle_status_line(self, line_bytes):
        try:
            line_str = line_bytes.decode('utf-8', errors='ignore').strip()
            if not line_str:
                return

            if "Tracking " in line_str:
                model = line_str.split("Tracking ")[-1].strip()
                if self.on_status:
                    self.on_status(f"✅ 正在直连 {model} (内部管道直通)")
            elif "No Android Head Tracker" in line_str:
                if self.is_connected:
                    self.is_connected = False
                    if self.on_connection:
                        self.on_connection(False)
                if self.on_status:
                    self.on_status("等待连接 Sony LinkBuds (请戴上耳机/开启蓝牙)...")
        except Exception:
            pass
