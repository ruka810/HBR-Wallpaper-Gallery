@echo off
rem HBR 壁纸图库 启动器（便携版，无需固定 Python 安装路径）
setlocal
cd /d "%~dp0"

set "PYRUN="
where pythonw >nul 2>nul && set "PYRUN=pythonw"
if not defined PYRUN ( where python >nul 2>nul && set "PYRUN=python" )
if not defined PYRUN ( where py >nul 2>nul && set "PYRUN=py -3" )

if not defined PYRUN (
  echo [错误] 未检测到 Python。请先安装 Python 3.10 或更高版本，
  echo        安装时勾选 "Add Python to PATH"，然后重新双击本文件。
  echo        下载地址: https://www.python.org/downloads/
  pause
  exit /b 1
)

%PYRUN% "%~dp0hbr_gallery.pyw"
if errorlevel 1 (
  echo.
  echo 程序异常退出。若提示缺少依赖，请在本窗口执行：
  echo    python -m pip install -r "%~dp0requirements.txt"
  pause
)
