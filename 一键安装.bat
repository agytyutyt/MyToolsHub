@echo off
setlocal
title JZToolsHub 一键安装 / 更新

echo ================================================
echo   JZToolsHub 一键安装 / 更新
echo ================================================
echo.
echo 正在执行安装/更新，请稍候...
echo.

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [完成] JZToolsHub 已安装 / 更新成功。
) else (
    echo [失败] 安装 / 更新过程出错，请查看上方日志（退出码 %RC%）。
)
echo.
pause
exit /b %RC%