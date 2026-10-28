"""
移植自 OpenTrack 的核心算法：
1. Accela 动态非线性滤波 (Adaptive Exponential Smoothing)
   - 微动时：高滤波系数，强力消除心跳、呼吸引起的微颤
   - 大动作时：低滤波系数，迅速响应无拖影
2. 动态死区 (Deadzone) 与非线性响应曲线
"""

import time
import math

class AccelaFilter:
    def __init__(self, deadzone=1.2, sensitivity=0.8, smoothing=0.75, exponent=1.25):
        self.deadzone = deadzone          # 死区角速度阈值 (度/秒)
        self.sensitivity = sensitivity    # 灵敏度增益
        self.smoothing = smoothing        # 平滑强度 (0.0 ~ 0.95)
        self.exponent = exponent          # 曲线非线性指数
        
        self.last_yaw_speed = 0.0
        self.last_pitch_speed = 0.0
        self.last_time = time.perf_counter()

    def update_params(self, deadzone=None, sensitivity=None, smoothing=None):
        if deadzone is not None:
            self.deadzone = float(deadzone)
        if sensitivity is not None:
            self.sensitivity = float(sensitivity)
        if smoothing is not None:
            self.smoothing = float(smoothing)

    def _apply_deadzone_and_curve(self, val):
        sign = 1.0 if val >= 0 else -1.0
        mag = abs(val)
        
        # 处于死区内直接归零，彻底消灭光标静止漂移
        if mag <= self.deadzone:
            return 0.0
        
        # 扣除死区后的有效角速度
        effective = mag - self.deadzone
        # 非线性幂律曲线：微调精准，大角度加速
        curved = math.pow(effective, self.exponent) * self.sensitivity
        return sign * curved

    def filter(self, raw_yaw_speed, raw_pitch_speed):
        now = time.perf_counter()
        dt = max(1e-4, min(0.1, now - self.last_time))
        self.last_time = now

        # 1. 死区与曲线处理
        target_yaw = self._apply_deadzone_and_curve(raw_yaw_speed)
        target_pitch = self._apply_deadzone_and_curve(raw_pitch_speed)

        # 2. Accela 动态自适应滤波：
        diff_yaw = abs(target_yaw - self.last_yaw_speed)
        diff_pitch = abs(target_pitch - self.last_pitch_speed)
        diff = max(diff_yaw, diff_pitch)

        # 动态自适应因子：差异大时迅速趋近 1.0 (无延迟)
        adaptive_factor = min(1.0, diff / 10.0)
        effective_smoothing = self.smoothing * (1.0 - adaptive_factor * 0.75)
        alpha = 1.0 - effective_smoothing

        # 指数平滑更新
        filtered_yaw = self.last_yaw_speed + alpha * (target_yaw - self.last_yaw_speed)
        filtered_pitch = self.last_pitch_speed + alpha * (target_pitch - self.last_pitch_speed)

        self.last_yaw_speed = filtered_yaw
        self.last_pitch_speed = filtered_pitch

        return filtered_yaw, filtered_pitch
