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
if "%RC%"=="0" goto jz_ok
echo [失败] 安装未完成，退出码 %RC%。
echo 详情见本目录下的「安装日志-LibreOffice核心.txt」，请把它发给维护者。
echo.
echo 常用自救命令（在本目录打开命令行执行）：
echo   只自检    powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1 -VerifyOnly
echo   强制重装  powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1 -Force
echo   跳过自检  powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1 -Force -SkipSmoke
echo.
echo 提示：[失败] 不等于装不上 —— 自检失败的常见原因是杀软首次拦截或机器较慢，
echo       组件文件本身可能已经解压好了，可直接启动工具箱试开一个 .doc / .xls。
goto jz_end

:jz_ok
echo [完成] LibreOffice 核心组件已就绪，可直接使用。

:jz_end
echo.
pause
exit /b %RC%
