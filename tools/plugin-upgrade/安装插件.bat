@echo off
setlocal
title JZToolsHub 插件包 - 安装 / 升级

echo ================================================
echo   JZToolsHub 插件包：安装 / 升级
echo ================================================
echo.
echo 本包用于给已安装的工具箱单独升级一个插件（不必重装整包）。
echo 安装过程：先只读校验（版本 / 哈希 / 框架版本）^→ 备份旧版 ^→ 再替换代码。
echo 不写注册表、不建快捷方式，插件的用户数据不会被改动。
echo.
echo 常用命令（在本目录执行）：
echo    体检        install-plugin.ps1 加 -List
echo    只演算      install-plugin.ps1 加 -DryRun
echo    回滚        install-plugin.ps1 加 -Rollback 插件id
echo    卸载        install-plugin.ps1 加 -Uninstall 插件id
echo.

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-plugin.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [完成] 插件已就绪。若为前端更新，请在浏览器按 Ctrl+F5 强刷一次。
) else (
    if "%RC%"=="2" (
        echo [注意] 安装已完成，但冒烟自检未通过；请查看上方日志与数据根目录 logs\。
    ) else (
        echo [失败] 安装未完成；目标机未做改动（或已按备份自动保持原样），请查看上方日志（退出码 %RC%）。
    )
)
echo.
pause
exit /b %RC%
