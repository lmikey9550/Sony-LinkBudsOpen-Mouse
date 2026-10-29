"""
手势识别与 Windows 原生鼠标驱动引擎：
- 停顿点击 (Dwell Click)：光标静止自动点击，免按键
- 点头手势 (Nod Gesture)：快速轻微点头触发点击
- 动态角加速度门控与历史位置回溯 (Anti-Drift Lock & Snapback)：彻底解决点头带跑光标问题
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

        # 停顿点击 (Dwell Click) 配置
        self.enable_dwell_click = True
        self.dwell_time = 0.9          # 停顿触发时间 (秒)
        self.dwell_radius = 15         # 判定静止的像素容差半径
        
        # 点头手势 (Nod Click) 配置
        self.enable_nod_click = True
        self.nod_sensitivity = 6       # 点头灵敏度 1~10 (默认 6)
        self.enable_nod_lock = True    # 点头防跑偏锁死

        # 点头物理判定参数 (毫秒与度数)
        # 灵敏度 1~10 动态换算所需下潜角度与回弹角度
        self.req_drop = 2.69           # 默认下潜角度阈值 (度)
        self.req_rebound = 1.39        # 默认回弹角度阈值 (度)
        self._update_thresholds()

        # 动态加速度与突发高速度抑制门限
        # 正常头部瞄准通常只有 2°~6°/s，角加速度 < 80°/s²
        # 点头动作瞬间角速度可达 25°~60°/s，角加速度 > 250°/s²
        self.speed_suppress_threshold = 12.0  # 超过 12°/s 视为突发动作，立即锁死 dy
        self.accel_suppress_threshold = 160.0 # 超过 160°/s² 视为急剧动作，立即锁死 dy

        # 历史轨迹环形缓冲：保存 (时间戳, 屏幕x, 屏幕y, 俯仰角pitch, 俯仰角速度pitch_rate)
        # 深度为 25 帧，相当于过去 500ms 的完整轨迹
        self.motion_history = deque(maxlen=25)

        # 状态机变量
        self.last_frame_time = 0.0
        self.last_pitch_rate = 0.0
        self.last_nod_time = 0.0
        self.nod_freeze_until = 0.0

        # 停顿点击状态机
        self.dwell_anchor_pos = (0, 0)
        self.dwell_start_time = None
        self.dwell_triggered = False

        # Windows 系统指针速度联动 (1~20)
        self.last_speed_check = 0
        self.system_speed_multiplier = 1.0

    def _update_thresholds(self):
        """根据灵敏度等级 1~10 换算所需下潜度数与回弹度数
        等级 1 (大幅防误触): 下潜 3.8°, 回弹 2.0°
        等级 6 (默认舒适): 下潜 2.7°, 回弹 1.4°
        等级 10 (轻点即按): 下潜 1.8°, 回弹 0.9°
        """
        lvl = max(1, min(10, int(round(self.nod_sensitivity))))
        self.req_drop = 3.8 - (lvl - 1) * (2.0 / 9.0)
        self.req_rebound = 2.0 - (lvl - 1) * (1.1 / 9.0)

    def set_nod_sensitivity(self, level):
        """设置点头灵敏度 1~10"""
        self.nod_sensitivity = level
        self._update_thresholds()

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

    def process_frame(self, filtered_yaw, filtered_pitch, raw_pitch_rate, cur_pitch):
        now = time.time()
        cur_x, cur_y = self.get_cursor_pos()

        # 计算垂直角加速度 (deg/s²)
        dt = max(0.02, min(0.06, now - self.last_frame_time)) if self.last_frame_time else 0.04
        self.last_frame_time = now

        pitch_accel = abs(raw_pitch_rate - self.last_pitch_rate) / dt
        self.last_pitch_rate = raw_pitch_rate

        # 压入历史轨迹
        self.motion_history.append((now, cur_x, cur_y, cur_pitch, raw_pitch_rate))

        # --- 1. 点击后的稳定冷却期 ---
        is_frozen = False
        if now < self.nod_freeze_until:
            is_frozen = True

        # --- 2. 点头手势判定 (毫秒窗口 + 真实度数极值判定) ---
        if self.enable_nod_click and not is_frozen:
            clicked, snap_x, snap_y = self._check_nod_gesture(now)
            if clicked:
                self.last_nod_time = now
                self.motion_history.clear()

                # 核心防跑偏：强制将光标拉回下潜发生前（200~300ms 前）的稳定坐标
                if self.enable_nod_lock and (snap_x, snap_y) != (0, 0):
                    self.user32.SetCursorPos(snap_x, snap_y)

                # 原位精确触发点击
                self.click_left()

                # 点击后提供 250ms 冷却死区，抵消头部回正运动
                self.nod_freeze_until = now + 0.25
                is_frozen = True

        # --- 3. 动态加速度与高角速度移动抑制 (防止点头带跑光标) ---
        # 如果垂直角速度很大，或角加速度突变剧烈，直接压制位移为 0！
        suppress_y = False
        suppress_x = False
        if self.enable_nod_lock and not is_frozen:
            if abs(raw_pitch_rate) > self.speed_suppress_threshold or pitch_accel > self.accel_suppress_threshold:
                suppress_y = True
                # 若是向下低头且速度较猛，同时锁死 X 轴微晃
                if raw_pitch_rate < -8.0:
                    suppress_x = True

        # --- 4. 驱动光标移动 (非手势及平稳动作时正常响应) ---
        if not is_frozen:
            sign_x = -1.0 if self.invert_x else 1.0
            sign_y = 1.0 if self.invert_y else -1.0
            sys_scale = self.get_system_mouse_speed()

            dx = 0.0 if suppress_x else (sign_x * filtered_yaw * 0.35 * sys_scale)
            dy = 0.0 if suppress_y else (sign_y * filtered_pitch * 0.35 * sys_scale)
            self.move_mouse(dx, dy)

        # --- 5. 停顿点击 (Dwell Click) 判定 ---
        if self.enable_dwell_click and not is_frozen and not suppress_y:
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

    def _check_nod_gesture(self, now):
        """基于物理毫秒窗口 (120ms~380ms) 与角度差 (度数) 判定点头手势"""
        if now - self.last_nod_time < 0.6:
            return False, 0, 0

        # 取最近 380ms 时间窗口内的数据
        window = [item for item in self.motion_history if (now - item[0]) <= 0.38]
        if len(window) < 4:
            return False, 0, 0

        pitches = [item[3] for item in window]
        min_p = min(pitches)
        min_idx = pitches.index(min_p)

        # 谷底必须在中间（排除单纯低头或单纯抬头的单向运动）
        if min_idx < 1 or min_idx >= len(pitches) - 1:
            return False, 0, 0

        # 起点角度（动作开始前）与终点角度（回弹后）
        start_p = pitches[0]
        end_p = pitches[-1]

        drop = start_p - min_p        # 低头下潜角度 (度)
        rebound = end_p - min_p      # 抬头回弹角度 (度)

        # 同时满足下潜度数与回弹度数要求
        if drop >= self.req_drop and rebound >= self.req_rebound:
            # 找到下潜发生前（即 window[0]）光标所在的绝对坐标作为原点锚点！
            snap_x, snap_y = window[0][1], window[0][2]
            return True, snap_x, snap_y

        return False, 0, 0
