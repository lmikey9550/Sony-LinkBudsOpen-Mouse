@echo off
cd /d "%~dp0"
title Sony LinkBuds Head Mouse

if exist "SonyLinkBudsHeadMouse.exe" (
    start "" "SonyLinkBudsHeadMouse.exe"
    exit /b
)

if exist "dist\SonyLinkBudsHeadMouse.exe" (
    start "" "dist\SonyLinkBudsHeadMouse.exe"
    exit /b
)

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" app.py
    exit /b
)

where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw app.py
    exit /b
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    start "" python app.py
    exit /b
)

echo [ERROR] 无法找到可执行程序或 Python 运行环境！
echo 请直接双击运行 SonyLinkBudsHeadMouse.exe，或安装 Python 后重试。
pause
