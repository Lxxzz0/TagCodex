@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [错误] 没找到 Python。请先安装 Python 3.10 以上：https://www.python.org/downloads/
  echo        安装时勾选 "Add python.exe to PATH"，装好后重新双击 install.bat
  pause
  exit /b 1
)
%PY% tools\setup.py %*
pause
