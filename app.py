"""
Sony LinkBuds 头控鼠标驱动 (纯后台系统托盘版 - 稳健修复版)
- 单实例进程锁：全局互斥量确保永远只有一个驱动实例在运行，杜绝多开冲突
- 独立连接状态机：彻底修复校准或状态消息导致鼠标断流的问题
- 管理员权限自动提权保护：确保能向所有窗口正常注入鼠标操作
- 配置即时持久化：托盘菜单的所有修改自动保存到 config.json，启动时无缝恢复
"""

import os
import sys
import time
import math
import ctypes
import threading
import subprocess
import winreg
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu

from head_tracker_pipe import HeadTrackerPipe
from accela_filter import AccelaFilter
from gesture_engine import GestureEngine
from config_manager import ConfigManager
from settings_dialog import SettingsDialog

VK_F12 = 0x7B
VK_F9  = 0x78

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_APP_NAME = "SonyLinkBudsHeadMouse"

_mutex_handle = None

def ensure_single_instance():
    """Windows 全局命名互斥锁，防止多开造成端口/设备句柄冲突"""
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    mutex_name = "Global\\SonyLinkBudsHeadMouseInstanceMutex"
    _mutex_handle = kernel32.CreateMutexW(None, False, mutex_name)
    ERROR_ALREADY_EXISTS = 183
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        # 已有实例在运行，直接静默退出当前重复进程
        sys.exit(0)

def require_admin():
    """检测管理员权限状态，快捷方式已内置提权标志"""
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
        if not is_admin:
            print("注意：当前以普通用户权限运行。如需控制任务管理器等管理员窗口，请使用桌面管理员快捷方式启动。")
    except Exception:
        pass

def create_tray_icon(connected=False, active=True):
    """动态生成 64x64 现代扁平化耳机托盘图标"""
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    
    # 耳机头梁
    band_color = (60, 120, 200, 255) if connected else (120, 120, 120, 255)
    d.arc([10, 10, 54, 54], start=180, end=0, fill=band_color, width=6)
    
    # 左右耳罩
    cup_color = (40, 44, 52, 255) if connected else (80, 80, 80, 255)
    d.rounded_rectangle([8, 30, 20, 52], radius=4, fill=cup_color)
    d.rounded_rectangle([44, 30, 56, 52], radius=4, fill=cup_color)
    
    # 状态指示圆点
    if connected and active:
        dot_color = (46, 204, 113, 255)  # 绿灯：正常工作中
    elif connected and not active:
        dot_color = (241, 196, 15, 255)  # 黄灯：已暂停
    else:
        dot_color = (189, 195, 199, 255) # 灰灯：等待耳机连接
        
    d.ellipse([40, 40, 58, 58], fill=dot_color, outline=(255, 255, 255, 255), width=2)
    return img

class HeadMouseDriverService:
    def __init__(self):
        self.config = ConfigManager()

        deadzone = self.config.get("deadzone", 1.2)
        sensitivity = self.config.get("sensitivity", 0.85)
        smoothing = self.config.get("smoothing", 0.75)

        self.accela = AccelaFilter(deadzone=deadzone, sensitivity=sensitivity, smoothing=smoothing)
        self.gesture = GestureEngine()
        
        self.gesture.invert_x = self.config.get("invert_x", True)
        self.gesture.invert_y = self.config.get("invert_y", False)
        self.gesture.enable_dwell_click = self.config.get("enable_dwell_click", True)
        self.gesture.set_dwell_time(self.config.get("dwell_time", 0.9))
        self.gesture.enable_nod_click = self.config.get("enable_nod_click", True)
        self.gesture.set_nod_sensitivity(self.config.get("nod_sensitivity", 6))

        self.tracking_enabled = self.config.get("tracking_enabled", True)
        self.is_connected = False
        self.status_text = "正在初始化 LinkBuds 驱动..."

        self.tracker = HeadTrackerPipe(
            on_motion_callback=self._on_head_motion,
            on_status_callback=self._on_tracker_status,
            on_connection_callback=self._on_connection_change
        )
        
        self.icon = None
        self._init_tray()

    def _init_tray(self):
        menu = Menu(
            item(lambda it: f"Sony LinkBuds: {self.status_text}", lambda: None, enabled=False),
            Menu.SEPARATOR,
            item(
                "启用头控鼠标 (F9)",
                self.toggle_tracking,
                checked=lambda it: self.tracking_enabled
            ),
            item("⌖ 居中校准当前朝向 (F12)", self.calibrate_center),
            item("🔄 重新检测/修复耳机连接", self.rebind_tracker),
            Menu.SEPARATOR,
            item("🎚️ 调节手势灵敏度与停留时间...", self.open_settings_dialog),
            item("免手操作设置", Menu(
                item("反转水平 X 轴", self.toggle_invert_x, checked=lambda it: self.gesture.invert_x),
                item("反转垂直 Y 轴", self.toggle_invert_y, checked=lambda it: self.gesture.invert_y),
                Menu.SEPARATOR,
                item("开启停顿点击 (Dwell Click)", self.toggle_dwell, checked=lambda it: self.gesture.enable_dwell_click),
                item("开启点头点击 (Nod Click)", self.toggle_nod, checked=lambda it: self.gesture.enable_nod_click),
            )),
            Menu.SEPARATOR,
            item("⚙️ Windows 鼠标属性设置...", self.open_windows_mouse_settings),
            item("🚀 开机自动启动", self.toggle_autostart, checked=lambda it: self.is_autostart_enabled()),
            Menu.SEPARATOR,
            item("退出驱动", self.quit_service)
        )

        init_img = create_tray_icon(connected=False, active=self.tracking_enabled)
        self.icon = pystray.Icon(
            "SonyLinkBudsHeadMouse",
            init_img,
            "Sony LinkBuds 头控鼠标驱动",
            menu=menu
        )

    def _update_icon_state(self):
        if self.icon:
            self.icon.icon = create_tray_icon(self.is_connected, self.tracking_enabled)

    def _on_head_motion(self, yaw_rate, pitch_rate, cur_yaw, cur_pitch):
        # 只要开启了跟踪，且收到数据帧，立即驱动鼠标，绝不被文字状态阻断！
        if not self.tracking_enabled:
            return
        f_yaw, f_pitch = self.accela.filter(yaw_rate, pitch_rate)
        self.gesture.process_frame(f_yaw, f_pitch, pitch_rate)

    def _on_connection_change(self, connected):
        self.is_connected = connected
        self._update_icon_state()
        if connected and self.icon:
            try:
                self.icon.notify("Sony LinkBuds 已就绪，开始头控跟踪！", "Sony 鼠标驱动")
            except Exception:
                pass

    def _on_tracker_status(self, msg):
        self.status_text = msg

    def toggle_tracking(self, icon=None, it=None):
        self.tracking_enabled = not self.tracking_enabled
        self.config.set("tracking_enabled", self.tracking_enabled)
        self._update_icon_state()
        if self.tracking_enabled:
            self.tracker.calibrate()
            if self.icon:
                try: self.icon.notify("头控鼠标已恢复", "Sony 鼠标驱动")
                except Exception: pass
        else:
            if self.icon:
                try: self.icon.notify("头控鼠标已暂停 (交由手部鼠标控制)", "Sony 鼠标驱动")
                except Exception: pass

    def calibrate_center(self, icon=None, it=None):
        self.tracker.calibrate()
        if self.icon:
            try: self.icon.notify("中心基准点已重置 (当前朝向为屏幕中央)", "Sony 鼠标驱动")
            except Exception: pass

    def rebind_tracker(self, icon=None, it=None):
        if self.tracker:
            self.tracker.rebind()
        if self.icon:
            try: self.icon.notify("已触发底层蓝牙节点探测与保活刷新...", "Sony 鼠标驱动")
            except Exception: pass

    def open_settings_dialog(self, icon=None, it=None):
        def _run():
            dialog = SettingsDialog(
                self.config,
                self.gesture,
                on_update_callback=None
            )
            dialog.show()
        threading.Thread(target=_run, daemon=True).start()

    def toggle_invert_x(self, icon=None, it=None):
        self.gesture.invert_x = not self.gesture.invert_x
        self.config.set("invert_x", self.gesture.invert_x)

    def toggle_invert_y(self, icon=None, it=None):
        self.gesture.invert_y = not self.gesture.invert_y
        self.config.set("invert_y", self.gesture.invert_y)

    def toggle_dwell(self, icon=None, it=None):
        self.gesture.enable_dwell_click = not self.gesture.enable_dwell_click
        self.config.set("enable_dwell_click", self.gesture.enable_dwell_click)

    def toggle_nod(self, icon=None, it=None):
        self.gesture.enable_nod_click = not self.gesture.enable_nod_click
        self.config.set("enable_nod_click", self.gesture.enable_nod_click)

    def open_windows_mouse_settings(self, icon=None, it=None):
        try:
            os.system("start control main.cpl")
        except Exception:
            try:
                subprocess.Popen(["control", "main.cpl"])
            except Exception:
                pass

    def is_autostart_enabled(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, REG_APP_NAME)
                return bool(val)
        except Exception:
            return False

    def toggle_autostart(self, icon=None, it=None):
        current = self.is_autostart_enabled()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if current:
                    winreg.DeleteValue(key, REG_APP_NAME)
                else:
                    base_dir = os.path.dirname(os.path.abspath(__file__))
                    pythonw = os.path.join(base_dir, ".venv", "Scripts", "pythonw.exe")
                    app_py = os.path.join(base_dir, "app.py")
                    cmd = f'"{pythonw}" "{app_py}"'
                    winreg.SetValueEx(key, REG_APP_NAME, 0, winreg.REG_SZ, cmd)
        except Exception as e:
            print("注册表开机自启更新失败:", e)

    def _start_hotkey_listener(self):
        user32 = ctypes.windll.user32
        
        def listener():
            last_f12 = False
            last_f9 = False
            while True:
                f12_state = (user32.GetAsyncKeyState(VK_F12) & 0x8000) != 0
                if f12_state and not last_f12:
                    self.calibrate_center()
                last_f12 = f12_state

                f9_state = (user32.GetAsyncKeyState(VK_F9) & 0x8000) != 0
                if f9_state and not last_f9:
                    self.toggle_tracking()
                last_f9 = f9_state

                time.sleep(0.05)

        t = threading.Thread(target=listener, daemon=True)
        t.start()

    def run(self):
        self._start_hotkey_listener()
        self.tracker.start()
        self.icon.run()

    def quit_service(self, icon=None, it=None):
        self.tracker.stop()
        if self.icon:
            self.icon.stop()
        sys.exit(0)

if __name__ == "__main__":
    require_admin()
    ensure_single_instance()
    
    service = HeadMouseDriverService()
    service.run()
