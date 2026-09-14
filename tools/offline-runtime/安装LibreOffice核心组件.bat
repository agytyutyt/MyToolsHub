@echo off
setlocal
title JZToolsHub 离线组件 - 安装 LibreOffice 核心包

echo ================================================
echo   JZToolsHub 离线组件：安装 LibreOffice 核心包
echo ================================================
echo.
echo 本组件让工具箱支持 .doc / .xls 的高保真预览。
echo 安装过程只解压文件：不写注册表、不建快捷方式、
echo 不改文件关联与默认应用，Microsoft Office / WPS 照常使用。
echo.

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-libreoffice-core.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [完成] LibreOffice 核心组件已就绪，可直接使用。
) else (
    echo [失败] 安装未完成，请查看上方日志（退出码 %RC%）。
)
echo.
pause
exit /b %RC%
