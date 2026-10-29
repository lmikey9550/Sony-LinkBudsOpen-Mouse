"""
手势识别与 Windows 原生鼠标驱动引擎：
- 悬停点击 (Dwell Click)：光标在目标上停顿自动点击，免按键，专为手部活动不便人士设计
- 左右歪头手势点击 (Head Tilt Gesture)：
  * 向左微偏头部 -> 触发【鼠标左键】
  * 向右微偏头部 -> 触发【鼠标右键】
  * 歪头期间硬件级临时锁定光标，彻底杜绝动作晃动带跑光标问题
- 彻底移除了容易导致光标纵向跑偏的点头手势，确保最高操作精度
- Windows user32.dll 原生硬件级鼠标消息注入
"""

import time
import math
import ctypes

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

        # 轴向反转配置 (默认反转 X 轴，修正耳机左右转动方向)
        self.invert_x = True
        self.invert_y = False

        # 悬停点击 (Dwell Click) 配置
        self.enable_dwell_click = True
        self.dwell_time = 0.9          # 停顿触发时间 (秒)
        self.dwell_radius = 16         # 判定静止的像素容差半径

        # 左右歪头手势 (Head Tilt Gesture) 配置
        self.enable_tilt_click = True
        self.tilt_threshold = 10.0     # 触发歪头的侧倾角度 (度，默认 10°)
        self.invert_tilt = False       # 歪头方向反转 (默认 False: 左倾=左键, 右倾=右键)

        # 姿态自适应基准与状态机
        self.baseline_roll = 0.0
        self.tilt_triggered = None     # None, 'left', 'right'
        self.tilt_freeze_until = 0.0   # 动作期间的光标定格锁
        self.last_tilt_time = 0.0

        # 悬停点击状态机
        self.dwell_anchor_pos = (0, 0)
        self.dwell_start_time = None
        self.dwell_triggered = False

        # Windows 系统指针速度动态联动 (1~20)
        self.last_speed_check = 0.0
        self.system_speed_multiplier = 1.0

    def set_dwell_time(self, seconds):
        """设置停留点击触发时间 (秒)"""
        self.dwell_time = max(0.3, min(3.0, float(seconds)))

    def set_tilt_threshold(self, degrees):
        """设置左右歪头触发角度阈值 (度)"""
        self.tilt_threshold = max(5.0, min(25.0, float(degrees)))

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
            self.on_action("左键单击")

    def click_right(self):
        """执行原生鼠标右键点击"""
        self.user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        self.user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        if self.on_action:
            self.on_action("右键单击")

    def move_mouse(self, dx, dy):
        """向 Windows 总线发送相对位移指令"""
        idx = int(round(dx))
        idy = int(round(dy))
        if idx != 0 or idy != 0:
            self.user32.mouse_event(MOUSEEVENTF_MOVE, idx, idy, 0, 0)

    def process_frame(self, filtered_yaw, filtered_pitch, yaw_rate, pitch_rate, cur_roll):
        now = time.time()
        cur_x, cur_y = self.get_cursor_pos()

        # --- 1. 头部侧倾角自适应基准平滑 ---
        # 当头部动作较平稳且角度接近自然居中时，自适应更新用户的自然坐姿零点
        if abs(yaw_rate) < 4.0 and abs(pitch_rate) < 4.0 and abs(cur_roll - self.baseline_roll) < 6.0:
            self.baseline_roll = 0.97 * self.baseline_roll + 0.03 * cur_roll

        # 计算相对偏角
        delta_roll = cur_roll - self.baseline_roll
        if self.invert_tilt:
            delta_roll = -delta_roll

        # --- 2. 左右歪头手势检测 (独立 Roll 轴，不产生垂直位移) ---
        is_frozen = False
        if now < self.tilt_freeze_until:
            is_frozen = True

        if self.enable_tilt_click:
            # 偏角超过阈值：向左歪头
            if delta_roll <= -self.tilt_threshold:
                if self.tilt_triggered is None and (now - self.last_tilt_time > 0.45):
                    self.tilt_triggered = 'left'
                    self.last_tilt_time = now
                    # 关键特性：触发时瞬间定格光标 280ms，彻底抵消歪头造成的晃动
                    self.tilt_freeze_until = now + 0.28
                    is_frozen = True
                    self.click_left()

            # 偏角超过阈值：向右歪头
            elif delta_roll >= self.tilt_threshold:
                if self.tilt_triggered is None and (now - self.last_tilt_time > 0.45):
                    self.tilt_triggered = 'right'
                    self.last_tilt_time = now
                    # 关键特性：触发时瞬间定格光标 280ms，彻底抵消歪头造成的晃动
                    self.tilt_freeze_until = now + 0.28
                    is_frozen = True
                    self.click_right()

            # 头部回正 (回缩到阈值的 40% 以内)，解除触发状态以允许下次点击
            elif abs(delta_roll) < (self.tilt_threshold * 0.4):
                self.tilt_triggered = None

        # --- 3. 光标位移驱动 ---
        if not is_frozen:
            sign_x = -1.0 if self.invert_x else 1.0
            sign_y = 1.0 if self.invert_y else -1.0
            sys_scale = self.get_system_mouse_speed()

            dx = sign_x * filtered_yaw * 0.35 * sys_scale
            dy = sign_y * filtered_pitch * 0.35 * sys_scale
            self.move_mouse(dx, dy)

        # --- 4. 悬停点击 (Dwell Click) 判定 ---
        if self.enable_dwell_click and not is_frozen:
            anchor_x, anchor_y = self.dwell_anchor_pos
            dist = math.hypot(cur_x - anchor_x, cur_y - anchor_y)

            # 只有当光标位移很小且头部转速很小时才算静止悬停
            if dist > self.dwell_radius or (abs(filtered_yaw) > 1.2 or abs(filtered_pitch) > 1.2):
                self.dwell_anchor_pos = (cur_x, cur_y)
                self.dwell_start_time = now
                self.dwell_triggered = False
            else:
                if self.dwell_start_time and not self.dwell_triggered:
                    elapsed = now - self.dwell_start_time
                    if elapsed >= self.dwell_time:
                        self.click_left()
                        self.dwell_triggered = True
