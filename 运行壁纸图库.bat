@echo off
rem HBR 壁纸图库 启动器（便携版，无需固定 Python 安装路径）
setlocal
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0hbr_gallery.pyw"
  if not errorlevel 1 exit /b
)

where pythonw.exe >nul 2>&1
if not errorlevel 1 (
  start "" pythonw.exe "%~dp0hbr_gallery.pyw"
  if not errorlevel 1 exit /b
)
where pyw.exe >nul 2>&1
if not errorlevel 1 (
  start "" pyw.exe -3 "%~dp0hbr_gallery.pyw"
  if not errorlevel 1 exit /b
)

set "PYRUN="
set "PYARGS="
where python.exe >nul 2>&1 && set "PYRUN=python.exe"
if not defined PYRUN (
  where py.exe >nul 2>&1 && (
    set "PYRUN=py.exe"
    set "PYARGS=-3 "
  )
)

if not defined PYRUN (
  echo [错误] 未检测到 Python。请先安装 Python 3.11 或更高版本，
  echo        安装时勾选 "Add Python to PATH"，然后重新双击本文件。
  echo        下载地址: https://www.python.org/downloads/
  pause
  exit /b 1
)

powershell.exe -NoProfile -NonInteractive -Command "Start-Process -FilePath '%PYRUN%' -ArgumentList '%PYARGS%hbr_gallery.pyw' -WindowStyle Hidden -ErrorAction Stop"
if errorlevel 1 (
  echo.
  echo 图库进程无法启动。若提示缺少依赖，请按 README 安装依赖。
  echo    python -m pip install -r "%~dp0requirements.txt"
  pause
)
