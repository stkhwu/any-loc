@echo off
REM ============================================================
REM  AnyLoc — 开发模式 (DEV)
REM  改界面：改完 web/ 里的文件，浏览器自动刷新，无需打包。
REM
REM  两种用法：
REM   * 普通双击        = 只开网页，快速调界面（连不了真机）
REM   * 右键“以管理员身份运行” = 网页热重载 + 真机隧道，能实测定位
REM
REM  这里【故意】保留控制台窗口。打包出来的 AnyLoc.exe 是无控制台的窗口程序
REM  （日志在 %LOCALAPPDATA%\AnyLoc\logs\anyloc.log），但开发模式要的就是
REM  能直接看到日志、能按 Ctrl+C 停。--dev 走的是浏览器 + 热重载，根本不加载
REM  原生窗口那套（gui.py / pywebview），所以别把这里也改成无控制台。
REM ============================================================
title AnyLoc DEV
REM this script lives in scripts/, so cd up to the project root first
cd /d "%~dp0.."

REM 检测是否管理员
net session >nul 2>&1
if %errorlevel%==0 (
  echo [管理员模式] 热重载 + 真机隧道，可实测定位。
) else (
  echo [普通模式] 只开网页，改界面用。
  echo.
  echo   想连真机实测定位？请关掉这个窗口，
  echo   改为【右键 dev.bat ^> 以管理员身份运行】。
)
echo.
python src\launcher.py --dev
pause
