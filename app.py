"""
Sony LinkBuds 头控鼠标驱动 (纯后台系统托盘版 - 极速免手与无障碍无感版)
- 专为手部活动不便及残障人士深度打造的全免手鼠标交互方案
- 彻底摒弃易跑偏的点头手势与多余的中心校准，光标定位极其丝滑稳定
- 三大无障碍点击模式：
  1. 悬停点击 (Dwell Click)：光标停留在目标上自动触发左键单击
  2. 左右歪头手势 (Head Tilt Gesture)：微偏头部即可触发（左歪=左键，右歪=右键，自带动作防晃定格）
  3. 耳周广域点击 (Wide Area Tap)：轻叩左侧耳周=左键，轻叩右侧耳周=右键 (自动屏蔽音乐切歌)
- 单实例进程锁：全局互斥量确保永远只有一个驱动实例在运行
- 匿名内核管道直传：0 网络端口，零防火墙弹窗，零网络协议栈开销
"""

import os
import sys
import time
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
from wide_area_tap import WideAreaTapListener
from config_manager import ConfigManager
from settings_dialog import SettingsDialog

VK_F9 = 0x78

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
        
        # 悬停点击与歪头手势配置
        self.gesture.enable_dwell_click = self.config.get("enable_dwell_click", True)
        self.gesture.set_dwell_time(self.config.get("dwell_time", 0.9))
        self.gesture.enable_tilt_click = self.config.get("enable_tilt_click", True)
        self.gesture.set_tilt_threshold(self.config.get("tilt_threshold", 10.0))
        self.gesture.invert_tilt = self.config.get("invert_tilt", False)

        # 耳周广域点击 (Wide Area Tap) 监听器
        self.tap_listener = WideAreaTapListener(
            on_left_click=self.gesture.click_left,
            on_right_click=self.gesture.click_right,
            enabled=self.config.get("enable_wide_area_tap", True)
        )

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
            item("🔄 重新检测/修复耳机连接", self.rebind_tracker),
            Menu.SEPARATOR,
            item("🎚️ 调节免手操作与手势灵敏度...", self.open_settings_dialog),
            item("免手操作设置", Menu(
                item("反转水平 X 轴", self.toggle_invert_x, checked=lambda it: self.gesture.invert_x),
                item("反转垂直 Y 轴", self.toggle_invert_y, checked=lambda it: self.gesture.invert_y),
                Menu.SEPARATOR,
                item("开启悬停点击 (Dwell Click)", self.toggle_dwell, checked=lambda it: self.gesture.enable_dwell_click),
                item("开启左右歪头手势 (左歪=左键，右歪=右键)", self.toggle_tilt, checked=lambda it: self.gesture.enable_tilt_click),
                item("开启耳周广域点击 (左耳=左键，右耳=右键)", self.toggle_wide_tap, checked=lambda it: self.tap_listener.enabled),
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

    def _on_head_motion(self, yaw_rate, pitch_rate, cur_yaw, cur_pitch, cur_roll=0.0):
        if not self.tracking_enabled:
            return
        f_yaw, f_pitch = self.accela.filter(yaw_rate, pitch_rate)
        self.gesture.process_frame(f_yaw, f_pitch, yaw_rate, pitch_rate, cur_roll)

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
            if self.icon:
                try: self.icon.notify("头控鼠标已恢复", "Sony 鼠标驱动")
                except Exception: pass
        else:
            if self.icon:
                try: self.icon.notify("头控鼠标已暂停 (交由手部鼠标控制)", "Sony 鼠标驱动")
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
                self.tap_listener,
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

    def toggle_tilt(self, icon=None, it=None):
        self.gesture.enable_tilt_click = not self.gesture.enable_tilt_click
        self.config.set("enable_tilt_click", self.gesture.enable_tilt_click)

    def toggle_wide_tap(self, icon=None, it=None):
        new_state = not self.tap_listener.enabled
        self.tap_listener.set_enabled(new_state)
        self.config.set("enable_wide_area_tap", new_state)

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
            last_f9 = False
            while True:
                f9_state = (user32.GetAsyncKeyState(VK_F9) & 0x8000) != 0
                if f9_state and not last_f9:
                    self.toggle_tracking()
                last_f9 = f9_state
                time.sleep(0.05)

        t = threading.Thread(target=listener, daemon=True)
        t.start()

    def run(self):
        self._start_hotkey_listener()
        self.tap_listener.start()
        self.tracker.start()
        self.icon.run()

    def quit_service(self, icon=None, it=None):
        self.tap_listener.stop()
        self.tracker.stop()
        if self.icon:
            self.icon.stop()
        sys.exit(0)

if __name__ == "__main__":
    require_admin()
    ensure_single_instance()
    
    service = HeadMouseDriverService()
    service.run()
