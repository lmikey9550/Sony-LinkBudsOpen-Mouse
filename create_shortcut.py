"""
一键在桌面创建带管理员权限的快捷方式
"""

import os
import sys

def create_desktop_shortcut():
    try:
        import win32com.client
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pywin32"])
        import win32com.client

    base_dir = os.path.dirname(os.path.abspath(__file__))
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    lnk_path = os.path.join(desktop, "Sony LinkBuds 头控鼠标.lnk")

    pythonw = os.path.join(base_dir, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(lnk_path)
    shortcut.TargetPath = pythonw
    shortcut.Arguments = "app.py"
    shortcut.WorkingDirectory = base_dir
    shortcut.Description = "Sony LinkBuds Open 头控鼠标后台驱动"
    shortcut.Save()

    # 设置 RunAsAdministrator 标志位 (字节 0x15 的 0x20 位)
    with open(lnk_path, "r+b") as f:
        data = bytearray(f.read())
        if len(data) > 0x15:
            data[0x15] |= 0x20
            f.seek(0)
            f.write(data)

    print(f"✅ 快捷方式已创建至桌面：{lnk_path}")

if __name__ == "__main__":
    create_desktop_shortcut()
