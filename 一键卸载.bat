@echo off
setlocal
title JZToolsHub 一键卸载

echo ================================================
echo   JZToolsHub 一键卸载
echo ================================================
echo.
echo [警告] 卸载将停止服务，并删除：
echo   - 项目文件（安装目录）
echo   - 用户数据根目录（默认 %USERPROFILE%\.jztoolshub）
echo     即：账号、台账、文档、公告、日志与大模型配置
echo.
set /p CONFIRM=确认卸载并删除全部数据？(Y=确认 / N=取消)：
if /i not "%CONFIRM%"=="Y" (
    echo.
    echo 已取消卸载。
    pause
    exit /b 0
)

echo.
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Uninstall
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [完成] JZToolsHub 已卸载。
) else (
    echo [失败] 卸载过程出错，请查看上方日志（退出码 %RC%）。
)
echo.
pause
exit /b %RC%