# JZToolsHub 打包部署脚本
# 产物：deploy\JZToolsHub\ —— 后端单目录可执行程序（JZToolsHub.exe + _internal/）
#       + 前端源码（static/、plugins/）+ 配置（config/）+ 一键启动脚本（start.bat）
#       + 一键安装/更新（一键安装.bat + install.ps1）+ 一键卸载（一键卸载.bat）+ 版本号（version.json）
# 用法：powershell -ExecutionPolicy Bypass -File build-deploy.ps1 [-Python "python"] [-DeployName "JZToolsHub"] [-Version "1.3.6"]
#       默认用 PATH 上的 python 打包、输出到 deploy\JZToolsHub\；
#       可用 -Python 指定其他解释器（如 Python 3.8：C:\...\Python38\python.exe）、
#       用 -DeployName 指定不同的部署目录名（如 py38 版输出到 deploy\JZToolsHub-py38）、
#       用 -Version 指定版本号（写入 version.json，一键安装脚本据此判断更新）。
param(
    [string]$Python = "python",
    [string]$DeployName = "JZToolsHub",
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"

$Root    = $PSScriptRoot
$Dist    = Join-Path $Root "dist"
$Deploy  = Join-Path $Root "deploy"
$AppDir  = Join-Path $Deploy $DeployName
$WorkDir = Join-Path $Root "build"

# 版本号：-Version 未指定时读取上一版部署的 version.json（默认 1.0.0）
if (-not $Version) {
    $oldVer = Join-Path $AppDir "version.json"
    if (Test-Path $oldVer) {
        try { $Version = (Get-Content $oldVer -Raw | ConvertFrom-Json).app } catch {}
    }
    if (-not $Version) { $Version = "1.0.0" }
}

# 1. 清理旧产物（只清理本次目标，保留其他 DeployName 的旧版本共存）
if (Test-Path $Dist)  { Remove-Item -Recurse -Force $Dist }
if (Test-Path $AppDir){ Remove-Item -Recurse -Force $AppDir }
if (Test-Path $WorkDir){ Remove-Item -Recurse -Force $WorkDir }

# 2. PyInstaller 打包后端（单目录：exe + _internal/）
Write-Host "==> PyInstaller 打包后端（$Python）..."
& $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $WorkDir (Join-Path $Root "JZToolsHub.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（exit $LASTEXITCODE）" }

# 3. 组装部署目录
Write-Host "==> 组装部署目录：$AppDir"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null

# 3.1 exe + _internal
Copy-Item -Recurse -Force (Join-Path $Dist "JZToolsHub\*") $AppDir

# 3.2 前端与插件源码（可修改）、配置模板、文档
Copy-Item -Recurse -Force (Join-Path $Root "static")  $AppDir
Copy-Item -Recurse -Force (Join-Path $Root "plugins") $AppDir
Copy-Item -Recurse -Force (Join-Path $Root "docs")    $AppDir
Copy-Item -Force (Join-Path $Root "README.md") $AppDir
Copy-Item -Force (Join-Path $Root "HANDOFF.md") $AppDir

# config 仅复制 tools.json 模板（admin.json / .admin_key 属密钥，首启自动生成）
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "config") | Out-Null
Copy-Item -Force (Join-Path $Root "config\tools.json") (Join-Path $AppDir "config")

# 清理插件目录中的运行时数据 / 密钥 / 缓存（全新部署由程序自动重建）
$pluginDir = Join-Path $AppDir "plugins"
if (Test-Path $pluginDir) {
  Get-ChildItem -Recurse -Directory $pluginDir |
    Where-Object { $_.Name -in @("data", ".task_cache", "__pycache__") } |
    Remove-Item -Recurse -Force
  Get-ChildItem -Recurse -File $pluginDir |
    Where-Object { $_.Name -like "*.pyc" -or $_.Name -eq "config.json" } |
    Remove-Item -Force
}

# 3.3 运行期目录
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "logs") | Out-Null

# 3.4 一键安装 / 卸载脚本 + 版本号
Write-Host "==> 写入一键安装/卸载脚本与 version.json（版本 $Version）..."
foreach ($f in @("install.ps1", "一键安装.bat", "一键卸载.bat")) {
    $srcF = Join-Path $Root $f
    if (Test-Path $srcF) { Copy-Item -Force $srcF $AppDir }
}
$verObj = @{ app = $Version; schema = 1 }
[System.IO.File]::WriteAllText(
    (Join-Path $AppDir "version.json"),
    ($verObj | ConvertTo-Json),
    (New-Object System.Text.UTF8Encoding($false))
)

# 4. 一键启动脚本
Write-Host "==> 生成 start.bat..."
$startBat = @'
@echo off
title JZToolsHub
echo 正在启动 JZToolsHub...（无窗口运行，托盘图标常驻右下角）
echo 浏览器访问 http://localhost:5000 ；退出服务请右键托盘图标选择「退出服务」
start "" /min JZToolsHub.exe
exit
'@
Set-Content -Path (Join-Path $AppDir "start.bat") -Value $startBat -Encoding Default

# 5.（可选）生成版本 zip：deploy\JZToolsHub-v<版本>.zip
if (Test-Path (Join-Path $AppDir "JZToolsHub.exe")) {
    $zip = Join-Path $Deploy "JZToolsHub-v$Version.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $AppDir "*") -DestinationPath $zip
    Write-Host "==> 已生成安装包：$zip"
}

Write-Host ""
Write-Host "==> 部署完成：$AppDir"
Write-Host "    全新安装 / 更新：双击「一键安装.bat」（自动停止旧服务、复制程序、同步配置模板到用户数据根目录）"
Write-Host "    完全卸载（含用户数据）：双击「一键卸载.bat」"
Write-Host "    双击 start.bat 一键启动；前端源码在 static\ 与 plugins\<id>\frontend\，改完重启即生效。"
