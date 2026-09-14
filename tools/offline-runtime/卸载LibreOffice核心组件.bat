@echo off
setlocal
title JZToolsHub 离线组件 - 卸载 LibreOffice 核心包

echo ================================================
echo   JZToolsHub 离线组件：卸载 LibreOffice 核心包
echo ================================================
echo.
echo 将删除程序目录下的 runtime\libreoffice\（解包后约 558MB）。
echo 工具箱本体不受影响，仅 .doc / .xls 高保真预览通道降级。
echo.
set /p CONFIRM=确认卸载？输入 Y 后回车继续，其他键取消：
if /i not "%CONFIRM%"=="Y" (
    echo.
    echo 已取消，未做任何改动。
    pause
    exit /b 0
)

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-libreoffice-core.ps1" -Uninstall
set "RC=%ERRORLEVEL%"

echo.
echo 卸载流程结束，退出码 %RC%。
pause
exit /b %RC%
