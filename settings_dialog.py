"""
Sony LinkBuds 手势与灵敏度调节界面 (Settings Dialog)
- 悬停点击停留时间滑块 (0.3s ~ 2.5s)
- 点头点击识别灵敏度滑块 (等级 1 ~ 10)
- 实时同步参数并即时持久化到 config.json
- 内置实时点击测试区，方便用户调整后直接验证手感
"""

import sys
import tkinter as tk
from tkinter import ttk

_settings_window_instance = None

class SettingsDialog:
    def __init__(self, config_manager, gesture_engine, on_update_callback=None):
        self.config = config_manager
        self.gesture = gesture_engine
        self.on_update = on_update_callback

        self.root = None
        self.dwell_val_label = None
        self.nod_val_label = None
        self.test_btn = None
        self.test_click_count = 0

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
        self.root.title("Sony LinkBuds - 免手操作灵敏度调节")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        # 窗口居中显示
        width, height = 460, 420
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        # 字体与配色
        font_title = ("Microsoft YaHei UI", 12, "bold")
        font_label = ("Microsoft YaHei UI", 10)
        font_small = ("Microsoft YaHei UI", 9)
        font_badge = ("Consolas", 10, "bold")

        main_frame = ttk.Frame(self.root, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 标题栏
        header = ttk.Label(main_frame, text="🎚️ 免手操作参数调节", font=font_title)
        header.pack(anchor="w", pady=(0, 15))

        # --- 1. 悬停点击停留时间 ---
        dwell_frame = ttk.LabelFrame(main_frame, text=" 悬停点击 (Dwell Click) ", padding=12)
        dwell_frame.pack(fill=tk.X, pady=(0, 12))

        cur_dwell = self.config.get("dwell_time", 0.9)
        self.dwell_var = tk.DoubleVar(value=cur_dwell)

        d_header_frame = ttk.Frame(dwell_frame)
        d_header_frame.pack(fill=tk.X, pady=(0, 4))
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
        self.dwell_scale.pack(fill=tk.X, pady=4)

        ttk.Label(
            dwell_frame,
            text="说明：光标悬停在静止区域达到该时间后自动左键单击 (推荐 0.7~1.0 秒)",
            font=font_small,
            foreground="#666666"
        ).pack(anchor="w", pady=(2, 0))

        # --- 2. 点头识别灵敏度 ---
        nod_frame = ttk.LabelFrame(main_frame, text=" 点头点击 (Nod Gesture) ", padding=12)
        nod_frame.pack(fill=tk.X, pady=(0, 15))

        cur_nod = self.config.get("nod_sensitivity", 6)
        self.nod_var = tk.DoubleVar(value=cur_nod)

        n_header_frame = ttk.Frame(nod_frame)
        n_header_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(n_header_frame, text="点头识别灵敏度：", font=font_label).pack(side=tk.LEFT)
        self.nod_val_label = ttk.Label(
            n_header_frame,
            text=f"等级 {int(round(cur_nod))} / 10",
            font=font_badge,
            foreground="#0066cc"
        )
        self.nod_val_label.pack(side=tk.RIGHT)

        self.nod_scale = ttk.Scale(
            nod_frame,
            from_=1,
            to=10,
            orient=tk.HORIZONTAL,
            variable=self.nod_var,
            command=self._on_nod_slider_change
        )
        self.nod_scale.pack(fill=tk.X, pady=4)

        ttk.Label(
            nod_frame,
            text="说明：头部快速轻微点头后仰触发左键单击，数值越高动作幅度要求越小",
            font=font_small,
            foreground="#666666"
        ).pack(anchor="w", pady=(2, 0))

        self.nod_lock_var = tk.BooleanVar(value=self.config.get("enable_nod_lock", True))
        self.nod_lock_check = ttk.Checkbutton(
            nod_frame,
            text="🔒 启用点头防跑偏锁定 (点头时原地定格光标，点击原位锚点)",
            variable=self.nod_lock_var,
            command=self._on_nod_lock_toggle
        )
        self.nod_lock_check.pack(anchor="w", pady=(6, 0))

        # --- 3. 实时测试交互区 ---
        test_frame = ttk.Frame(main_frame)
        test_frame.pack(fill=tk.X, pady=(0, 15))

        self.test_btn = tk.Button(
            test_frame,
            text="🎯 点击测试区 (在此处测试悬停或点头，累计点击: 0 次)",
            font=font_label,
            bg="#f0f2f5",
            activebackground="#d1e7dd",
            relief=tk.GROOVE,
            pady=8,
            command=self._on_test_click
        )
        self.test_btn.pack(fill=tk.X)

        # --- 4. 底部按钮 ---
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X)

        reset_btn = ttk.Button(btn_frame, text="恢复默认值", command=self._reset_defaults)
        reset_btn.pack(side=tk.LEFT)

        close_btn = ttk.Button(btn_frame, text="完成并关闭", command=self._close_window)
        close_btn.pack(side=tk.RIGHT)

        self.root.protocol("WM_DELETE_WINDOW", self._close_window)
        self.root.mainloop()

    def _on_dwell_slider_change(self, val):
        sec = round(float(val), 1)
        if self.dwell_val_label:
            self.dwell_val_label.config(text=f"{sec:.1f} 秒")
        
        self.gesture.set_dwell_time(sec)
        self.config.set("dwell_time", sec)
        if self.on_update:
            self.on_update()

    def _on_nod_slider_change(self, val):
        lvl = int(round(float(val)))
        if self.nod_val_label:
            self.nod_val_label.config(text=f"等级 {lvl} / 10")
        
        self.gesture.set_nod_sensitivity(lvl)
        self.config.set("nod_sensitivity", lvl)
        if self.on_update:
            self.on_update()

    def _on_nod_lock_toggle(self):
        val = bool(self.nod_lock_var.get())
        self.gesture.enable_nod_lock = val
        self.config.set("enable_nod_lock", val)
        if self.on_update:
            self.on_update()

    def _on_test_click(self):
        self.test_click_count += 1
        if self.test_btn:
            self.test_btn.config(
                text=f"🎯 成功触发点击！累计已识别: {self.test_click_count} 次",
                bg="#d1e7dd"
            )
            # 300ms 后还原背景色
            self.root.after(300, lambda: self.test_btn and self.test_btn.config(bg="#f0f2f5"))

    def _reset_defaults(self):
        self.dwell_var.set(0.9)
        self._on_dwell_slider_change(0.9)

        self.nod_var.set(6)
        self._on_nod_slider_change(6)

        self.nod_lock_var.set(True)
        self._on_nod_lock_toggle()

    def _close_window(self):
        global _settings_window_instance
        _settings_window_instance = None
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None
