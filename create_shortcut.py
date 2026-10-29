"""
一键在桌面创建带管理员权限的快捷方式 (无需第三方依赖，基于 Windows 原生 PowerShell COM)
"""

import os
import sys
import subprocess

def create_desktop_shortcut():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    lnk_path = os.path.join(desktop, "Sony LinkBuds 头控鼠标.lnk")

    pythonw = os.path.join(base_dir, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")

    ps_script = f"""
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut('{lnk_path}')
$shortcut.TargetPath = '{pythonw}'
$shortcut.Arguments = 'app.py'
$shortcut.WorkingDirectory = '{base_dir}'
$shortcut.Description = 'Sony LinkBuds Open 头控鼠标免手驱动'
$shortcut.Save()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=True)

    # 设置 RunAsAdministrator 标志位 (字节 0x15 的 0x20 位)
    if os.path.exists(lnk_path):
        with open(lnk_path, "r+b") as f:
            data = bytearray(f.read())
            if len(data) > 0x15:
                data[0x15] |= 0x20
                f.seek(0)
                f.write(data)

    print(f"[OK] Desktop shortcut created: {lnk_path}")

if __name__ == "__main__":
    create_desktop_shortcut()
