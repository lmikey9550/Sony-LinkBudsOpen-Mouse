"""
Sony LinkBuds 纯内部系统驱动层 (Native In-Process HID Driver)
- 彻底抛弃 UDP 网络套接字 (0 本地环回端口占用，零防火墙干扰)
- 彻底抛弃外部 exe 子进程 (单进程轻量内存直通，零子进程残留)
- 通过 Windows 底层 hid.dll 原生 API (CreateFile / ReadFile) 直接读取耳机 14 字节姿态报文
- 内置静音 A2DP 保活引擎，彻底破解索尼耳机 2.5 秒无音频休眠的固件特性
"""

import time
import struct
import threading
import hid

from silent_keepalive import SilentAudioKeepalive

# Sony 官方厂商代码与 AOSP Head Tracker 传感器规范
SONY_VID = 0x054C
USAGE_PAGE_SENSOR = 0x0020
USAGE_HEAD_TRACKER = 0x00E1

class HeadTrackerHID:
    def __init__(self, on_motion_callback=None, on_status_callback=None, on_connection_callback=None):
        self.on_motion = on_motion_callback
        self.on_status = on_status_callback
        self.on_connection = on_connection_callback

        self.device = None
        self.running = False
        self.thread = None

        # 蓝牙 A2DP 静音保活引擎 (防止索尼耳机 2.5 秒休眠)
        self.keepalive = SilentAudioKeepalive()

        # 姿态状态
        self.last_yaw = None
        self.last_pitch = None
        self.last_time = time.perf_counter()

        self.bias_yaw = 0.0
        self.bias_pitch = 0.0

        self.current_yaw = 0.0
        self.current_pitch = 0.0
        self.current_roll = 0.0

        self.is_connected = False

    def calibrate(self):
        """居中校准：以当前朝向为零点基准"""
        self.bias_yaw = self.current_yaw
        self.bias_pitch = self.current_pitch
        if self.on_status:
            self.on_status("已校准：当前朝向已设为中心基准点")

    def _find_target_hid_device(self):
        """直接扫描 Windows 系统底层 HID 设备树"""
        for d in hid.enumerate():
            vid = d.get('vendor_id', 0)
            up = d.get('usage_page', 0)
            u = d.get('usage', 0)

            # 精准匹配 Sony 且为 Sensor Head Tracker 节点
            if vid == SONY_VID and (up == USAGE_PAGE_SENSOR or u == USAGE_HEAD_TRACKER):
                return d
            if up == USAGE_PAGE_SENSOR and u == USAGE_HEAD_TRACKER:
                return d
        return None

    def _try_activate_streaming(self, dev):
        """向耳机发送 AOSP Head Tracker 唤醒指令 (Reporting State: All Events)"""
        try:
            # 尝试发送标准 Feature Report 1 (设置 All Events 0x0841 与 Full Power 0x0851)
            # 在某些 Windows 蓝牙协议栈下系统会自动激活，发送失败亦可安全忽略
            feat_packet = bytes([0x01, 0x41, 0x08, 0x51, 0x08])
            dev.send_feature_report(feat_packet)
        except Exception:
            pass

    def start(self):
        if self.running:
            return
        self.running = True

        # 启动静音保活，阻止索尼耳机固件休眠
        self.keepalive.start()

        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.keepalive.stop()

        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None

    def _worker_loop(self):
        """纯内部数据轮询线程：直接从 Windows hid.dll 内核驱动管道读取"""
        last_search_time = 0

        while self.running:
            # 1. 如果尚未连接，尝试打开设备
            if self.device is None:
                now_sec = time.time()
                if now_sec - last_search_time > 1.0:
                    last_search_time = now_sec
                    d_info = self._find_target_hid_device()
                    if d_info:
                        try:
                            dev = hid.device()
                            dev.open_path(d_info['path'])
                            dev.set_nonblocking(False)
                            self._try_activate_streaming(dev)

                            self.device = dev
                            self.last_yaw = None
                            self.last_pitch = None
                            self.last_time = time.perf_counter()

                            self.is_connected = True
                            if self.on_connection:
                                self.on_connection(True)
                            if self.on_status:
                                self.on_status("✅ 已直连 Sony LinkBuds (纯内部驱动已激活)")
                        except Exception as e:
                            self.device = None
                    else:
                        if self.is_connected:
                            self.is_connected = False
                            if self.on_connection:
                                self.on_connection(False)
                        if self.on_status:
                            self.on_status("等待连接 Sony LinkBuds (请戴上耳机/开启蓝牙)...")
                time.sleep(0.3)
                continue

            # 2. 已连接状态：直接读取 14 字节原始硬件报文
            try:
                # 阻塞读取 64 字节缓冲区，超时 80ms
                raw = self.device.read(64, timeout_ms=80)
                if not raw or len(raw) < 7:
                    continue

                raw_bytes = bytes(raw)

                # LinkBuds 报文结构：
                # Byte 0: Report ID (0x01)
                # Byte 1..2: rx (俯仰角 Pitch, int16)
                # Byte 3..4: ry (翻滚角 Roll, int16)
                # Byte 5..6: rz (偏航角 Yaw, int16)
                v1, v2, v3 = struct.unpack_from('<hhh', raw_bytes, offset=1)

                # AOSP 标准换算为角度制 [-180°, +180°]
                pitch_deg = v1 * 180.0 / 32768.0
                roll_deg  = v2 * 180.0 / 32768.0
                yaw_deg   = v3 * 180.0 / 32768.0

                self.current_yaw = yaw_deg
                self.current_pitch = pitch_deg
                self.current_roll = roll_deg

                now_time = time.perf_counter()
                dt = max(1e-4, min(0.1, now_time - self.last_time))
                self.last_time = now_time

                if self.last_yaw is not None:
                    d_yaw = yaw_deg - self.last_yaw
                    d_pitch = pitch_deg - self.last_pitch

                    # 处理 ±180° 跨界回绕
                    if d_yaw > 180.0:   d_yaw -= 360.0
                    elif d_yaw < -180.0: d_yaw += 360.0
                    if d_pitch > 180.0:   d_pitch -= 360.0
                    elif d_pitch < -180.0: d_pitch += 360.0

                    # 实时角速度 (度/秒)
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

            except Exception as e:
                # 硬件异常断开 (如耳机放入电池仓或关闭蓝牙)
                try:
                    self.device.close()
                except Exception:
                    pass
                self.device = None
                self.is_connected = False
                if self.on_connection:
                    self.on_connection(False)
                if self.on_status:
                    self.on_status("LinkBuds 已断开，等待重新佩戴...")
                time.sleep(0.5)

# 兼容别名
SonyDriverBridge = HeadTrackerHID
