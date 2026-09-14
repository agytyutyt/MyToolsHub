@echo off
setlocal
title JZToolsHub 离线组件安装
echo ================================================
echo   JZToolsHub 离线运行组件安装（Chrome / LibreOffice）
echo ================================================
echo.
echo 说明：
echo   - Chrome      全机安装，需要管理员权限
echo   - LibreOffice 便携解包，无需管理员
echo.
echo 若要安装 Chrome，请关闭本窗口，右键本文件选择「以管理员身份运行」。
echo.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-offline-runtime.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo [完成] 离线组件处理完成。
) else (
    echo [失败] 存在失败项（退出码 %RC%），请查看上方日志。
)
echo.
pause
exit /b %RC%
