@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] 准备虚拟环境…
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo 创建虚拟环境失败，请确认已安装 Python 3.10+
        pause
        exit /b 1
    )
)

echo [2/3] 安装依赖…
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 (
    echo 依赖安装失败
    pause
    exit /b 1
)

echo [3/3] 打包…
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "WIFI直连" ^
    --icon "assets\icon.ico" ^
    --add-data "web;web" ^
    --hidden-import waitress ^
    --hidden-import qrcode.image.pil ^
    run.py

if errorlevel 1 (
    echo 打包失败
    pause
    exit /b 1
)

echo.
echo 完成：dist\WIFI直连.exe
pause
