# JZToolsHub 打包部署脚本
# 产物：deploy\JZToolsHub\ —— 后端单目录可执行程序（JZToolsHub.exe + _internal/）
#       + 前端源码（static/、plugins/）+ 配置（config/）+ 一键启动脚本（start.bat）
# 用法：powershell -ExecutionPolicy Bypass -File build-deploy.ps1
$ErrorActionPreference = "Stop"

$Root    = $PSScriptRoot
$Dist    = Join-Path $Root "dist"
$Deploy  = Join-Path $Root "deploy"
$AppDir  = Join-Path $Deploy "JZToolsHub"
$WorkDir = Join-Path $Root "build"

# 1. 清理旧产物
if (Test-Path $Dist)  { Remove-Item -Recurse -Force $Dist }
if (Test-Path $Deploy){ Remove-Item -Recurse -Force $Deploy }
if (Test-Path $WorkDir){ Remove-Item -Recurse -Force $WorkDir }

# 2. PyInstaller 打包后端（单目录：exe + _internal/）
Write-Host "==> PyInstaller 打包后端..."
python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $WorkDir (Join-Path $Root "JZToolsHub.spec")
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

# 4. 一键启动脚本
Write-Host "==> 生成 start.bat..."
$startBat = @"
@echo off
cd /d %~dp0
echo [JZToolsHub] 正在启动服务，浏览器访问 http://localhost:5000  （Ctrl+C 停止）
echo.
JZToolsHub.exe
pause
"@
Set-Content -Path (Join-Path $AppDir "start.bat") -Value $startBat -Encoding Default

Write-Host ""
Write-Host "==> 部署完成：$AppDir"
Write-Host "    双击 start.bat 一键启动；前端源码在 static\ 与 plugins\<id>\frontend\，改完重启即生效。"
