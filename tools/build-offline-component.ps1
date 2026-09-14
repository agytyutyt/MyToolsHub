# JZToolsHub 离线组件包构建脚本
#
# 主程序包默认不再携带离线运行组件（build-deploy.ps1 默认瘦身）。本脚本把组件单独
# 打成「一键安装包」，由目标机按需安装 —— 这样既缩小分发体积，也让组件能独立升级/卸载。
#
# 产物（deploy\ 下）：
#   JZToolsHub-离线组件-LibreOffice核心-<版本>.zip   默认；目标机解压后双击「安装LibreOffice核心组件.bat」
#   JZToolsHub-离线组件-Chrome-<版本>.zip            仅 -Component Chrome / All；需管理员安装
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\build-offline-component.ps1
#   powershell -ExecutionPolicy Bypass -File tools\build-offline-component.ps1 -Component All
#   powershell -ExecutionPolicy Bypass -File tools\build-offline-component.ps1 -StageOnly   # 只组装不打 zip
#
# 前置：组件本体先由 tools\fetch-offline-bundle.py（--core）准备好，位于仓库 runtime\ 下。
param(
    [ValidateSet("LibreOffice", "Chrome", "All")]
    [string]$Component = "LibreOffice",
    [string]$DeployName = "JZToolsHub",
    [switch]$StageOnly
)
$ErrorActionPreference = "Stop"

$Root    = Split-Path -Parent $PSScriptRoot          # tools\ → 仓库根
$RtSrc   = Join-Path $Root "runtime"
$RtTools = Join-Path $Root "tools\offline-runtime"
$Deploy  = Join-Path $Root "deploy"
$Work    = Join-Path $Root "build\offline-component"

function Say { param([string]$m = "") Write-Host $m }

function Assert-File {
    param([string]$Path, [string]$Hint)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "缺少文件：$Path`n  $Hint"
    }
}

# 用 GBK 写文本，保证目标机记事本与 cmd 都能正常显示（与 .bat 编码一致）
function Write-TextGbk {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text.Replace("`r`n", "`n").Replace("`n", "`r`n"),
                                   [System.Text.Encoding]::GetEncoding(936))
}

# LibreOffice 版本号：优先取核心包元数据里的源 MSI 文件名
function Get-LoVersion {
    $meta = Join-Path $RtSrc "libreoffice\libreoffice-core.json"
    if (Test-Path -LiteralPath $meta) {
        try {
            $m = Get-Content $meta -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([string]$m.source.file -match '([0-9]+\.[0-9]+\.[0-9]+)') { return $matches[1] }
        } catch { }
    }
    return "unknown"
}

function New-StageDir {
    param([string]$Name)
    $p = Join-Path $Work $Name
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $p | Out-Null
    return $p
}

# 打成 zip：zip 内保留顶层目录名，便于目标机解压后一眼看出是什么
function Compress-Stage {
    param([string]$StageRoot, [string]$OutZip)
    if (Test-Path -LiteralPath $OutZip) { Remove-Item -LiteralPath $OutZip -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path $OutZip) | Out-Null
    Compress-Archive -Path $StageRoot -DestinationPath $OutZip -CompressionLevel Fastest
    return (Get-Item -LiteralPath $OutZip).Length
}

# ============================================================================
#  LibreOffice 核心组件包
# ============================================================================
function Build-LibreOfficeCore {
    $loVer  = Get-LoVersion
    $coreZip = Join-Path $RtSrc "libreoffice\libreoffice-core.zip"
    $coreMeta = Join-Path $RtSrc "libreoffice\libreoffice-core.json"
    Assert-File $coreZip  "先运行：python tools\fetch-offline-bundle.py --core（会顺带生成裁剪核心包）"
    Assert-File $coreMeta "核心包元数据缺失；重跑：python tools\build-libreoffice-core.py --force"

    # 出厂前核验：zip 体积与 sha256 必须与元数据一致，避免分发出去的包本身是坏的
    $meta = Get-Content $coreMeta -Raw -Encoding UTF8 | ConvertFrom-Json
    $sz = (Get-Item -LiteralPath $coreZip).Length
    if ($sz -ne [int64]$meta.artifact.size) {
        throw "libreoffice-core.zip 体积与元数据不符（$sz vs $($meta.artifact.size)）；请重建核心包。"
    }
    $sha = (Get-FileHash -LiteralPath $coreZip -Algorithm SHA256).Hash.ToLower()
    if ($sha -ne ([string]$meta.artifact.sha256).ToLower()) {
        throw "libreoffice-core.zip 的 sha256 与元数据不符；请重建核心包。"
    }

    Say "==> 组装 LibreOffice 核心组件包（$loVer）"
    $pkgName = "LibreOffice核心组件"
    $stage = New-StageDir $pkgName
    Copy-Item -LiteralPath $coreZip  -Destination $stage -Force
    Copy-Item -LiteralPath $coreMeta -Destination $stage -Force
    foreach ($f in @("install-libreoffice-core.ps1", "安装LibreOffice核心组件.bat", "卸载LibreOffice核心组件.bat")) {
        Assert-File (Join-Path $RtTools $f) "离线组件脚本缺失：tools\offline-runtime\$f"
        Copy-Item -LiteralPath (Join-Path $RtTools $f) -Destination $stage -Force
    }

    $zipMb = [math]::Round($sz / 1MB, 1)
    $readme = @"
JZToolsHub 离线组件：LibreOffice 核心包 $loVer
================================================================

用途
  让 JZToolsHub 支持 .doc 与 .xls 的原样式高保真预览。
  主程序包已不再携带本组件，需要时在目标机上单独安装。

安装
  1) 把本压缩包解压到任意目录（目标机上即可）；
  2) 双击「安装LibreOffice核心组件.bat」；
  3) 看到「[完成]」即安装成功。免管理员权限。

对目标机的影响：几乎没有（这是本组件能放心安装的前提）
  - 只解压文件：不写注册表、不注册文件关联、不创建开始菜单/桌面快捷方式，
    「程序和功能」里也不会出现条目；
  - 目标机的默认应用与文件图标完全不变，Microsoft Office / WPS 照常使用；
  - 调用时一律 headless + 唯一临时 profile，不弹窗口，也不会碰目标机已有的
    LibreOffice 配置（若目标机另装了完整版 LibreOffice，两者各自独立）；
  - 安装位置：<JZToolsHub 程序目录>\runtime\libreoffice\（默认在
    %LOCALAPPDATA%\JZToolsHub 下，普通用户平时看不到）。

卸载
  双击「卸载LibreOffice核心组件.bat」；或直接删除上面那个 runtime\libreoffice 目录。
  卸载后工具箱其余功能不受影响，仅 .doc / .xls 的预览降级为简化渲染。

空间
  解压后约 558 MB；本压缩包内约 $zipMb MB。
  确认功能正常后，可删除解压目录里的 libreoffice-core.zip 回收 $zipMb MB。

故障排查
  - 提示"未找到 JZToolsHub 程序目录"：先安装工具箱，或加参数指定：
      powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1 -InstallDir "D:\JZToolsHub"
  - 自检失败（超时/未产出文件）：检查杀毒软件是否拦截了 soffice.exe，然后加 -Force 重装。
  - 安装后预览仍走简化通道：确认工具箱已重启，并访问 /api/knowledge-base/status
    查看 office_render / soffice 字段。
"@
    Write-TextGbk (Join-Path $stage "使用说明.txt") $readme

    if ($StageOnly) {
        Say "    已组装（未打 zip）：$stage"
        return $null
    }
    $out = Join-Path $Deploy ("JZToolsHub-离线组件-LibreOffice核心-$loVer.zip")
    $len = Compress-Stage -StageRoot $stage -OutZip $out
    Say ("    已生成：{0}（{1} MB）" -f $out, [math]::Round($len / 1MB, 1))
    return $out
}

# ============================================================================
#  Chrome 组件包（全机安装，需管理员）
# ============================================================================
function Build-Chrome {
    $manifest = Join-Path $RtSrc "manifest.json"
    Assert-File $manifest "先运行：python tools\fetch-offline-bundle.py"
    $mf = Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    $chrome = $mf.components | Where-Object { $_.id -eq "chrome" } | Select-Object -First 1
    if (-not $chrome) { throw "runtime\manifest.json 里没有 chrome 组件定义。" }
    $msiSrc = Join-Path (Join-Path $RtSrc $chrome.subdir) $chrome.file
    Assert-File $msiSrc "先运行：python tools\fetch-offline-bundle.py（下载 Chrome 企业版 MSI）"

    Say "==> 组装 Chrome 组件包"
    $stage = New-StageDir "Chrome离线组件"
    $sub = Join-Path $stage $chrome.subdir
    New-Item -ItemType Directory -Force -Path $sub | Out-Null
    Copy-Item -LiteralPath $msiSrc -Destination $sub -Force
    Copy-Item -LiteralPath $manifest -Destination $stage -Force
    foreach ($f in @("setup-offline-runtime.ps1", "README.md")) {
        if (Test-Path -LiteralPath (Join-Path $RtTools $f)) {
            Copy-Item -LiteralPath (Join-Path $RtTools $f) -Destination $stage -Force
        }
    }
    # Chrome 专属入口：跳过 LibreOffice（本包不含它）
    $bat = @"
@echo off
setlocal
title JZToolsHub 离线组件 - 安装 Google Chrome

echo ================================================
echo   JZToolsHub 离线组件：安装 Google Chrome 企业版
echo ================================================
echo.
echo Chrome 为全机安装，需要管理员权限。
echo 若提示权限不足：请右键本文件 →「以管理员身份运行」。
echo.

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-offline-runtime.ps1" -RuntimeDir "%~dp0" -SkipLibreOffice
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (echo [完成] Chrome 组件处理结束。) else (echo [失败] 退出码 %RC%。)
echo.
pause
exit /b %RC%
"@
    Write-TextGbk (Join-Path $stage "安装Chrome浏览器.bat") $bat

    $msiMb = [math]::Round((Get-Item -LiteralPath $msiSrc).Length / 1MB, 1)
    Write-TextGbk (Join-Path $stage "使用说明.txt") @"
JZToolsHub 离线组件：Google Chrome 企业版（$($chrome.version)）
================================================================

用途
  目标机没有可用浏览器时，离线安装 Chrome。主程序包已不再携带本组件。
  Chrome 是「浏览器基线 >= 72」的推荐浏览器；目标机已有 Chrome / Edge 时无需安装。

安装
  右键「安装Chrome浏览器.bat」→「以管理员身份运行」（全机安装需要管理员）。
  如目标机已有 Chrome，脚本会检测到并跳过；要强制重装加 -Force。

卸载
  Chrome 由 Windows 正常安装，请在「设置 → 应用」或「程序和功能」里卸载。
  本组件不提供卸载入口，也不会改动你的默认浏览器。

体积
  本包内约 $msiMb MB（MSI 已是压缩格式）。
"@

    if ($StageOnly) {
        Say "    已组装（未打 zip）：$stage"
        return $null
    }
    $out = Join-Path $Deploy ("JZToolsHub-离线组件-Chrome-$($chrome.version).zip")
    $len = Compress-Stage -StageRoot $stage -OutZip $out
    Say ("    已生成：{0}（{1} MB）" -f $out, [math]::Round($len / 1MB, 1))
    return $out
}

# ============================================================================
#  主流程
# ============================================================================
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$made = New-Object System.Collections.ArrayList

if ($Component -eq "LibreOffice" -or $Component -eq "All") {
    $p = Build-LibreOfficeCore
    if ($p) { [void]$made.Add($p) }
    Say ""
}
if ($Component -eq "Chrome" -or $Component -eq "All") {
    $p = Build-Chrome
    if ($p) { [void]$made.Add($p) }
    Say ""
}

if ($made.Count -eq 0) {
    Say "==> 组装完成（未生成 zip，见 build\offline-component\）。"
} else {
    Say "==> 完成。组件包在 deploy\ 下："
    foreach ($p in $made) { Say "    $p" }
    Say ""
    Say "    目标机：解压对应组件包 → 双击其中的「安装*.bat」即可。"
}
