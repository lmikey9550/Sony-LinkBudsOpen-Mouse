"""
一键在桌面创建带管理员权限的快捷方式 (优先指向已打包的 SonyLinkBudsHeadMouse.exe)
"""

import os
import sys
import subprocess

def create_desktop_shortcut():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    lnk_path = os.path.join(desktop, "Sony LinkBuds 头控鼠标.lnk")

    exe_target = os.path.join(base_dir, "SonyLinkBudsHeadMouse.exe")
    dist_target = os.path.join(base_dir, "dist", "SonyLinkBudsHeadMouse.exe")

    if os.path.exists(exe_target):
        target_path = exe_target
        target_args = ""
    elif os.path.exists(dist_target):
        target_path = dist_target
        target_args = ""
    else:
        pythonw = os.path.join(base_dir, ".venv", "Scripts", "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        target_path = pythonw
        target_args = "app.py"

    ps_script = f"""
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut('{lnk_path}')
$shortcut.TargetPath = '{target_path}'
$shortcut.Arguments = '{target_args}'
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

    print(f"[OK] Desktop shortcut updated to: {target_path}")

if __name__ == "__main__":
    create_desktop_shortcut()
