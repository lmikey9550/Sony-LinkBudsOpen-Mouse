"""
手势识别与 Windows 原生鼠标驱动引擎：
- 停顿点击 (Dwell Click)：光标静止自动点击，免按键
- 点头手势 (Nod Gesture)：快速轻微点头触发点击
- Windows user32.dll 原生硬件级鼠标注入
"""

import time
import ctypes
import math
from collections import deque

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
SPI_GETMOUSESPEED = 0x0070

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class GestureEngine:
    def __init__(self, on_action_callback=None):
        self.user32 = ctypes.windll.user32
        self.on_action = on_action_callback

        # 方向配置 (默认反转 X 轴，修正耳机左右转动方向)
        self.invert_x = True
        self.invert_y = False

        # 开关配置
        self.enable_dwell_click = True
        self.dwell_time = 0.9          # 停顿触发时间 (秒)
        self.dwell_radius = 15         # 判定静止的像素容差半径
        
        self.enable_nod_click = True
        self.nod_sensitivity = 6       # 点头灵敏度 1~10
        self.nod_threshold = 34.0      # 点头角速度阈值 (度/秒)
        self.enable_nod_lock = True    # 点头防跑偏锁死：点头时定格光标并在原处精确落点点击
        
        # 点头手势防跑偏状态机
        self.nod_in_progress = False
        self.nod_start_time = 0.0
        self.nod_anchor_pos = (0, 0)
        self.nod_freeze_until = 0.0

        # 停顿点击状态机
        self.dwell_anchor_pos = (0, 0)
        self.dwell_start_time = None
        self.dwell_triggered = False

        # 点头手势检测滑动窗口
        self.pitch_history = deque(maxlen=30)
        self.last_nod_time = 0

        # Windows 系统指针速度联动 (1~20)
        self.last_speed_check = 0
        self.system_speed_multiplier = 1.0

    def set_nod_sensitivity(self, level):
        """设置点头灵敏度 1~10 (数值越高动作幅度越小，越容易触发)"""
        level = max(1, min(10, int(round(level))))
        self.nod_sensitivity = level
        # 1 对应 55°/s (大幅动作防误触)，10 对应 18°/s (极轻点头即响应)
        self.nod_threshold = 55.0 - (level - 1) * (37.0 / 9.0)

    def set_dwell_time(self, seconds):
        """设置停留点击触发时间 (秒)"""
        self.dwell_time = max(0.3, min(3.0, float(seconds)))

    def get_system_mouse_speed(self):
        """动态同步 Windows 系统控制面板中的鼠标指针速度"""
        now = time.time()
        if now - self.last_speed_check > 2.0:
            self.last_speed_check = now
            spd = ctypes.c_int(10)
            if self.user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0, ctypes.byref(spd), 0):
                self.system_speed_multiplier = max(0.1, spd.value / 10.0)
        return self.system_speed_multiplier

    def get_cursor_pos(self):
        pt = POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def click_left(self):
        """执行原生鼠标左键点击"""
        self.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        self.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        if self.on_action:
            self.on_action("触发【左键单击】")

    def click_right(self):
        """执行原生鼠标右键点击"""
        self.user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        self.user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        if self.on_action:
            self.on_action("触发【右键单击】")

    def move_mouse(self, dx, dy):
        """向 Windows 总线发送相对位移指令"""
        idx = int(round(dx))
        idy = int(round(dy))
        if idx != 0 or idy != 0:
            self.user32.mouse_event(MOUSEEVENTF_MOVE, idx, idy, 0, 0)

    def process_frame(self, filtered_yaw, filtered_pitch, raw_pitch_rate):
        now = time.time()
        cur_x, cur_y = self.get_cursor_pos()

        # --- 1. 点头手势检测与光标防跑偏锁定 ---
        is_nod_frozen = False
        if self.enable_nod_click:
            self.pitch_history.append((now, raw_pitch_rate))

            # A. 点击后的稳定冷却期：头部回弹阶段完全忽略位移
            if now < self.nod_freeze_until:
                is_nod_frozen = True

            # B. 点头动作进入检测：当俯仰角速度出现大幅下潜，立即锚定当前光标并冻结移动
            onset_thresh = self.nod_threshold * 0.42
            if self.enable_nod_lock:
                if not self.nod_in_progress and raw_pitch_rate < -onset_thresh:
                    self.nod_in_progress = True
                    self.nod_start_time = now
                    self.nod_anchor_pos = (cur_x, cur_y)
                    is_nod_frozen = True
                elif self.nod_in_progress:
                    # 超时保护：若超过 0.35 秒未形成有效反弹，判定为单纯低头看东西，解除锁定
                    if now - self.nod_start_time > 0.35:
                        self.nod_in_progress = False
                    else:
                        is_nod_frozen = True

            # C. 完整点头手势判定（下潜峰值 + 回弹峰值）
            if now - self.last_nod_time > 0.6:
                if self._detect_nod():
                    self.last_nod_time = now
                    self.pitch_history.clear()
                    self.nod_in_progress = False

                    # 核心防跑偏：强制将光标拉回点头触发前锁定的原点位置，实现精准狙击点击！
                    if self.enable_nod_lock and self.nod_anchor_pos != (0, 0):
                        target_x, target_y = self.nod_anchor_pos
                        self.user32.SetCursorPos(target_x, target_y)

                    self.click_left()

                    # 触发点击后进入 0.22 秒保护期，抵消头部回正运动
                    self.nod_freeze_until = now + 0.22
                    is_nod_frozen = True

        # --- 2. 移动鼠标光标 (非点头动作期间正常响应) ---
        if not is_nod_frozen:
            sign_x = -1.0 if self.invert_x else 1.0
            sign_y = 1.0 if self.invert_y else -1.0
            sys_scale = self.get_system_mouse_speed()

            dx = sign_x * filtered_yaw * 0.35 * sys_scale
            dy = sign_y * filtered_pitch * 0.35 * sys_scale
            self.move_mouse(dx, dy)

        # --- 3. 停顿点击 (Dwell Click) 判定 ---
        if self.enable_dwell_click and not is_nod_frozen:
            anchor_x, anchor_y = self.dwell_anchor_pos
            dist = math.hypot(cur_x - anchor_x, cur_y - anchor_y)

            # 只有当角速度很小且光标位移很小时才算静止
            if dist > self.dwell_radius or (abs(filtered_yaw) > 1.5 or abs(filtered_pitch) > 1.5):
                self.dwell_anchor_pos = (cur_x, cur_y)
                self.dwell_start_time = now
                self.dwell_triggered = False
            else:
                if self.dwell_start_time and not self.dwell_triggered:
                    elapsed = now - self.dwell_start_time
                    if elapsed >= self.dwell_time:
                        self.click_left()
                        self.dwell_triggered = True

    def _detect_nod(self):
        if len(self.pitch_history) < 5:
            return False

        rates = [r for _, r in self.pitch_history]
        min_rate = min(rates)
        max_rate = max(rates)

        # 显著的下潜 (向下转动) 与向上反弹动作
        if min_rate < -self.nod_threshold and max_rate > (self.nod_threshold * 0.35):
            return True
        return False
