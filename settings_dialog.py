"""
Sony LinkBuds 免手操作与手势灵敏度调节界面 (Settings Dialog)
- 悬停点击停留时间滑块 (0.3s ~ 2.5s)
- 左右歪头点击触发角度滑块 (6° ~ 20°)
- 耳机耳周广域点击 (Wide Area Tap) 快捷开关
- 实时同步参数并即时持久化到 config.json
- 内置实时左/右键双色测试反馈区，方便用户调整后直接验证手感
"""

import sys
import tkinter as tk
from tkinter import ttk

_settings_window_instance = None

class SettingsDialog:
    def __init__(self, config_manager, gesture_engine, tap_listener=None, on_update_callback=None):
        self.config = config_manager
        self.gesture = gesture_engine
        self.tap_listener = tap_listener
        self.on_update = on_update_callback

        self.root = None
        self.dwell_val_label = None
        self.tilt_val_label = None
        self.test_btn = None
        self.test_click_count = 0

        # 连接引擎动作回调
        self.prev_engine_action = self.gesture.on_action
        self.gesture.on_action = self._on_gesture_action

    def _on_gesture_action(self, action_name):
        if self.prev_engine_action:
            self.prev_engine_action(action_name)
        if self.root and self.test_btn:
            try:
                self.root.after(0, lambda: self._show_test_feedback(action_name))
            except Exception:
                pass

    def show(self):
        global _settings_window_instance
        if _settings_window_instance and _settings_window_instance.root:
            try:
                _settings_window_instance.root.lift()
                _settings_window_instance.root.focus_force()
                return
            except Exception:
                pass

        _settings_window_instance = self
        self._build_ui()

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Sony LinkBuds - 免手操作与手势灵敏度调节")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        # 窗口居中
        width, height = 520, 560
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        # 字体
        font_title = ("Microsoft YaHei UI", 12, "bold")
        font_label = ("Microsoft YaHei UI", 10)
        font_small = ("Microsoft YaHei UI", 9)
        font_badge = ("Consolas", 10, "bold")

        main_frame = ttk.Frame(self.root, padding=18)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 标题栏
        header = ttk.Label(main_frame, text="🎚️ 免手操作参数与手势调节", font=font_title)
        header.pack(anchor="w", pady=(0, 2))
        sub_header = ttk.Label(
            main_frame,
            text="专为手部活动不便及残障人士设计的无障碍辅助配置",
            font=font_small,
            foreground="#555555"
        )
        sub_header.pack(anchor="w", pady=(0, 12))

        # --- 1. 悬停点击停留时间 ---
        dwell_frame = ttk.LabelFrame(main_frame, text=" 1. 悬停点击 (Dwell Click) ", padding=10)
        dwell_frame.pack(fill=tk.X, pady=(0, 10))

        self.dwell_enable_var = tk.BooleanVar(value=self.config.get("enable_dwell_click", True))
        dwell_enable_check = ttk.Checkbutton(
            dwell_frame,
            text="开启悬停点击 (光标静止自动左键单击)",
            variable=self.dwell_enable_var,
            command=self._on_dwell_enable_toggle
        )
        dwell_enable_check.pack(anchor="w", pady=(0, 4))

        cur_dwell = self.config.get("dwell_time", 0.9)
        self.dwell_var = tk.DoubleVar(value=cur_dwell)

        d_header_frame = ttk.Frame(dwell_frame)
        d_header_frame.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(d_header_frame, text="停留触发时间：", font=font_label).pack(side=tk.LEFT)
        self.dwell_val_label = ttk.Label(
            d_header_frame,
            text=f"{cur_dwell:.1f} 秒",
            font=font_badge,
            foreground="#0066cc"
        )
        self.dwell_val_label.pack(side=tk.RIGHT)

        self.dwell_scale = ttk.Scale(
            dwell_frame,
            from_=0.3,
            to=2.5,
            orient=tk.HORIZONTAL,
            variable=self.dwell_var,
            command=self._on_dwell_slider_change
        )
        self.dwell_scale.pack(fill=tk.X, pady=2)

        ttk.Label(
            dwell_frame,
            text="说明：光标悬停在静止区域达到设定时间后自动触发单击 (推荐 0.7~1.0 秒)",
            font=font_small,
            foreground="#666666"
        ).pack(anchor="w", pady=(2, 0))

        # --- 2. 左右歪头手势点击 ---
        tilt_frame = ttk.LabelFrame(main_frame, text=" 2. 左右歪头手势点击 (Head Tilt Gesture) ", padding=10)
        tilt_frame.pack(fill=tk.X, pady=(0, 10))

        self.tilt_enable_var = tk.BooleanVar(value=self.config.get("enable_tilt_click", True))
        tilt_enable_check = ttk.Checkbutton(
            tilt_frame,
            text="启用左右歪头点击 (左歪头=左键，右歪头=右键)",
            variable=self.tilt_enable_var,
            command=self._on_tilt_enable_toggle
        )
        tilt_enable_check.pack(anchor="w", pady=(0, 4))

        cur_tilt = self.config.get("tilt_threshold", 10.0)
        self.tilt_var = tk.DoubleVar(value=cur_tilt)

        t_header_frame = ttk.Frame(tilt_frame)
        t_header_frame.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(t_header_frame, text="歪头触发角度阈值：", font=font_label).pack(side=tk.LEFT)
        self.tilt_val_label = ttk.Label(
            t_header_frame,
            text=f"{int(round(cur_tilt))}°",
            font=font_badge,
            foreground="#0066cc"
        )
        self.tilt_val_label.pack(side=tk.RIGHT)

        self.tilt_scale = ttk.Scale(
            tilt_frame,
            from_=6,
            to=20,
            orient=tk.HORIZONTAL,
            variable=self.tilt_var,
            command=self._on_tilt_slider_change
        )
        self.tilt_scale.pack(fill=tk.X, pady=2)

        ttk.Label(
            tilt_frame,
            text="说明：面向屏幕微偏头部即可触发点击，歪头期间光标自动原地定格防跑偏",
            font=font_small,
            foreground="#666666"
        ).pack(anchor="w", pady=(2, 0))

        # --- 3. 耳机耳周广域点击 ---
        tap_frame = ttk.LabelFrame(main_frame, text=" 3. 耳机耳周广域点击 (Wide Area Tap) ", padding=10)
        tap_frame.pack(fill=tk.X, pady=(0, 12))

        self.tap_enable_var = tk.BooleanVar(value=self.config.get("enable_wide_area_tap", True))
        tap_enable_check = ttk.Checkbutton(
            tap_frame,
            text="启用耳周广域点击 (右耳双击=左键，右耳三击=右键)",
            variable=self.tap_enable_var,
            command=self._on_tap_enable_toggle
        )
        tap_enable_check.pack(anchor="w", pady=(0, 2))

        ttk.Label(
            tap_frame,
            text="说明：右耳双击触发左键，右耳三击触发右键 (免手机App设置；自动屏蔽系统音乐切歌)",
            font=font_small,
            foreground="#666666"
        ).pack(anchor="w")

        # --- 4. 实时测试交互区 ---
        test_frame = ttk.Frame(main_frame)
        test_frame.pack(fill=tk.X, pady=(0, 14))

        self.test_btn = tk.Button(
            test_frame,
            text="🎯 点击测试区 (在此处测试悬停、歪头或耳周敲击，累计: 0 次)",
            font=font_label,
            bg="#f0f2f5",
            activebackground="#d1e7dd",
            relief=tk.GROOVE,
            pady=8,
            command=self._on_test_button_clicked
        )
        self.test_btn.bind("<Button-3>", lambda e: self._show_test_feedback("右键单击"))
        self.test_btn.pack(fill=tk.X)

        # --- 5. 底部按钮 ---
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X)

        reset_btn = ttk.Button(btn_frame, text="恢复默认值", command=self._reset_defaults)
        reset_btn.pack(side=tk.LEFT)

        close_btn = ttk.Button(btn_frame, text="完成并关闭", command=self._close_window)
        close_btn.pack(side=tk.RIGHT)

        self.root.protocol("WM_DELETE_WINDOW", self._close_window)
        self.root.mainloop()

    def _on_dwell_enable_toggle(self):
        val = bool(self.dwell_enable_var.get())
        self.gesture.enable_dwell_click = val
        self.config.set("enable_dwell_click", val)
        if self.on_update:
            self.on_update()

    def _on_dwell_slider_change(self, val):
        sec = round(float(val), 1)
        if self.dwell_val_label:
            self.dwell_val_label.config(text=f"{sec:.1f} 秒")
        self.gesture.set_dwell_time(sec)
        self.config.set("dwell_time", sec)
        if self.on_update:
            self.on_update()

    def _on_tilt_enable_toggle(self):
        val = bool(self.tilt_enable_var.get())
        self.gesture.enable_tilt_click = val
        self.config.set("enable_tilt_click", val)
        if self.on_update:
            self.on_update()

    def _on_tilt_slider_change(self, val):
        deg = float(round(float(val)))
        if self.tilt_val_label:
            self.tilt_val_label.config(text=f"{int(deg)}°")
        self.gesture.set_tilt_threshold(deg)
        self.config.set("tilt_threshold", deg)
        if self.on_update:
            self.on_update()

    def _on_tap_enable_toggle(self):
        val = bool(self.tap_enable_var.get())
        if self.tap_listener:
            self.tap_listener.set_enabled(val)
        self.config.set("enable_wide_area_tap", val)
        if self.on_update:
            self.on_update()

    def _on_test_button_clicked(self):
        self._show_test_feedback("左键单击")

    def _show_test_feedback(self, action_name):
        self.test_click_count += 1
        is_right = "右键" in action_name
        flash_bg = "#cfe2ff" if is_right else "#d1e7dd"
        action_desc = "右键单击" if is_right else "左键单击"

        if self.test_btn:
            self.test_btn.config(
                text=f"🎯 成功识别【{action_desc}】！累计触发: {self.test_click_count} 次",
                bg=flash_bg
            )
            # 350ms 后还原背景色
            if self.root:
                self.root.after(350, lambda: self.test_btn and self.test_btn.config(bg="#f0f2f5"))

    def _reset_defaults(self):
        self.dwell_enable_var.set(True)
        self._on_dwell_enable_toggle()
        self.dwell_var.set(0.9)
        self._on_dwell_slider_change(0.9)

        self.tilt_enable_var.set(True)
        self._on_tilt_enable_toggle()
        self.tilt_var.set(10.0)
        self._on_tilt_slider_change(10.0)

        self.tap_enable_var.set(True)
        self._on_tap_enable_toggle()

    def _close_window(self):
        global _settings_window_instance
        _settings_window_instance = None
        self.gesture.on_action = self.prev_engine_action
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None
