@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到 .venv，请先双击 install.bat 安装
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
python app.py --open
pause
