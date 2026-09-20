# build-dep-component.ps1 —— 产出「依赖组件包」（cv2 + numpy，不随主包）
#
# 设计依据：docs\design\主体与插件解耦-设计文档.md §3.4 / T25 第 1 步
#   主包只带框架必需依赖；cv2(opencv-python) 与 numpy 压缩后约 60 MB，且只服务
#   info-transfer / trajectory-convert 的**视频码流模式**——改为独立组件按需安装。
#
# 取源（-From，缺省自动按序尝试）：
#   1) deploy\JZToolsHub\_internal  —— 最近一次主包构建的产物（版本与主包一致，推荐）
#   2) 当前 Python 的 site-packages —— 开发机直接取（-From 可显式指定）
#
# 产物（OutDir 缺省 deploy\依赖组件\）：
#   JZToolsHub-依赖组件-<名称>-v<版本>.zip
#     ├── dep-component.json      # 包清单：id/名称/版本/提供的包与版本/适用主程序版本
#     ├── manifest.json           # 装到 runtime\pylibs\ 的组件清单（主体与后台读它）
#     ├── 安装依赖组件.bat         # 双击入口
#     ├── install-dep-component.ps1（从仓库 tools\dep-component\ 原样拷贝）
#     ├── 说明.md
#     └── payload\pylibs\{cv2,numpy,numpy.libs}\…
param(
    [string]$Name = "OpenCV",
    [string]$Version = "",            # 缺省取 cv2 的实际版本
    [string]$From = "",
    [string]$OutDir = "",
    [string]$MinApp = "",             # 写入 min_app_version（缺省留空=不限制）
    [switch]$Force,
    [switch]$NoZip
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $Root "deploy\依赖组件" }
$ToolVer = "1.0.0"

function Say { param([string]$m = "") Write-Host $m }
function Warn { param([string]$m) Write-Warning $m }
function Die { param([string]$m) Write-Host ""; Write-Host "  [失败] $m" -ForegroundColor Red; exit 1 }
function Write-Utf8NoBom { param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

Say ""
Say "================================================"
Say "  JZToolsHub 依赖组件构建（build-dep-component.ps1 $ToolVer）"
Say "================================================"

# ---- 1. 定位取源 ----
$cands = @()
if ($From) { $cands += $From }
$cands += (Join-Path $Root "deploy\JZToolsHub\_internal")
# 取源探测：本机可能把包装在**用户级 site-packages**（pip install --user），只查 purelib 会漏；
# 最后再用 `import cv2` 反查真实位置兜底（最可靠）。
foreach ($probe in @(
    "import site;print(site.getusersitepackages())",
    "import sysconfig;print(sysconfig.get_paths()['purelib'])",
    "import cv2,os;print(os.path.dirname(os.path.dirname(cv2.__file__)))")) {
    try {
        $sp = & python -c $probe 2>$null | Select-Object -First 1
        if ($sp) { $cands += $sp.Trim() }
    } catch {}
}
$srcDir = ""
foreach ($c in $cands) {
    if ($c -and (Test-Path -LiteralPath (Join-Path $c "cv2"))) { $srcDir = $c; break }
}
if (-not $srcDir) { Die ("未找到包含 cv2 的取源目录。请用 -From 指定（例如 deploy\JZToolsHub\_internal 或 site-packages）。`n         尝试过：$($cands -join '；')") }
Say "  取源    ：$srcDir"

# ---- 2. 读版本（cv2 的发行名是 opencv-python；从 dist-info 或 metadata 取） ----
function Get-DistVersion {
    param([string]$DistName)
    try {
        $v = & python -c "import importlib.metadata as m;print(m.version('$DistName'))" 2>$null | Select-Object -First 1
        if ($v) { return $v.Trim() }
    } catch {}
    return ""
}
$cv2Ver = Get-DistVersion "opencv-python"; if (-not $cv2Ver) { $cv2Ver = "unknown" }
$npVer  = Get-DistVersion "numpy";          if (-not $npVer)  { $npVer  = "unknown" }
if (-not $Version) { $Version = $cv2Ver }
Say "  组件版本：$Version（cv2=$cv2Ver，numpy=$npVer）"

# ---- 3. 组装 ----
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("jz-dep-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
$pylibs = Join-Path $staging "payload\pylibs"
New-Item -ItemType Directory -Force -Path $pylibs | Out-Null
$items = @("cv2", "numpy", "numpy.libs")     # numpy.libs 必须与 numpy 同装同卸（OpenBLAS DLL 链）
$totalBytes = 0
foreach ($it in $items) {
    $src = Join-Path $srcDir $it
    if (-not (Test-Path -LiteralPath $src)) {
        if ($it -eq "numpy.libs") { Warn "取源里没有 numpy.libs（部分 numpy 构建没有该目录，可忽略）"; continue }
        Die "取源缺少 $it：$src"
    }
    Copy-Item -LiteralPath $src -Destination $pylibs -Recurse -Force
    $sz = (Get-ChildItem -LiteralPath $src -Recurse -File | Measure-Object -Property Length -Sum).Sum
    $totalBytes += $sz
    Say ("    + {0,-12} {1,8:N1} MB" -f $it, ($sz / 1MB))
}

$manifest = [ordered]@{
    schema     = 1
    kind       = "dep-component"
    id         = "opencv"
    name       = "OpenCV 依赖组件（opencv-python + numpy）"
    version    = $Version
    built_at   = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
    components = @([ordered]@{
        id       = "opencv"
        name     = "OpenCV 依赖组件"
        version  = $Version
        packages = [ordered]@{ "opencv-python" = $cv2Ver; "numpy" = $npVer }
        modules  = [ordered]@{ "cv2" = "opencv-python"; "numpy" = "numpy" }
        provides = @("cv2", "numpy")
        affects  = "info-transfer / trajectory-convert 的视频码流模式（静态码模式不受影响）"
    })
}
Write-Utf8NoBom (Join-Path $pylibs "manifest.json") (($manifest | ConvertTo-Json -Depth 8) + "`n")
Write-Utf8NoBom (Join-Path $staging "manifest.json") (($manifest | ConvertTo-Json -Depth 8) + "`n")

$pkgMeta = [ordered]@{
    schema          = 1
    kind            = "dep-component-package"
    id              = "opencv"
    name            = $Name
    version         = $Version
    provides        = @("cv2", "numpy")
    built_at        = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
    built_from      = [ordered]@{ commit = (& git -C $Root rev-parse --short HEAD 2>$null | Select-Object -First 1); source = $srcDir }
    size_bytes      = [long]$totalBytes
}
if ($MinApp) { $pkgMeta["min_app_version"] = $MinApp }
Write-Utf8NoBom (Join-Path $staging "dep-component.json") (($pkgMeta | ConvertTo-Json -Depth 8) + "`n")

Copy-Item -LiteralPath (Join-Path $Root "tools\dep-component\install-dep-component.ps1") -Destination $staging -Force
$bat = @"
@echo off
chcp 65001 >nul
title JZToolsHub 依赖组件安装
echo 正在安装依赖组件「$Name」v$Version（opencv-python + numpy）...
echo 用途：信息传输 / 轨迹转换的**视频码流模式**（静态码模式不需要本组件）。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-dep-component.ps1"
echo.
pause
"@
[System.IO.File]::WriteAllText((Join-Path $staging "安装依赖组件.bat"), $bat, [System.Text.Encoding]::GetEncoding("GBK"))

$notes = @()
$notes += "# 依赖组件：$Name v$Version"
$notes += ""
$notes += "包含：opencv-python(cv2) $cv2Ver、numpy $npVer（合计约 $([math]::Round($totalBytes/1MB,1)) MB）"
$notes += ""
$notes += "## 为什么要单独装"
$notes += ""
$notes += "这两个库只服务 **info-transfer / trajectory-convert 的视频码流模式**（生成 mp4），"
$notes += "却占主包约 60 MB。解耦后它们不随主包，改由本组件按需安装——"
$notes += "不装则：插件照常加载，视频码流模式不可用（**静态二维码模式完全不受影响**），"
$notes += "后台「插件管理」会把这些插件标为「降级」并指出缺哪个依赖。"
$notes += ""
$notes += "## 安装"
$notes += ""
$notes += "解压后双击 `安装依赖组件.bat`（免管理员）。安装位置：`<程序目录>\runtime\pylibs\`。"
$notes += "安装器会调用 `JZToolsHub.exe --check-deps` 做真实 import 自检，通过才算成功。"
$notes += ""
$notes += "卸载：`install-dep-component.ps1 -Uninstall`；体检：`-Check`。"
Write-Utf8NoBom (Join-Path $staging "说明.md") ($notes -join "`n")

if ($NoZip) { Say ""; Say "==> [-NoZip] 已组装：$staging"; exit 0 }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$zipPath = Join-Path $OutDir ("JZToolsHub-依赖组件-{0}-v{1}.zip" -f $Name, $Version)
if ((Test-Path -LiteralPath $zipPath) -and -not $Force) { Die "产物已存在：$zipPath（加 -Force 覆盖）" }
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $staging, $zipPath, [System.IO.Compression.CompressionLevel]::Fastest, $false,
    (New-Object System.Text.UTF8Encoding($false)))
$sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLower()
Write-Utf8NoBom "$zipPath.sha256" ("{0}  {1}`n" -f $sha, (Split-Path -Leaf $zipPath))

Say ""
Say "==> 已产出依赖组件包：$zipPath"
Say ("    体积 {0:N1} MB；sha256 {1}" -f ((Get-Item -LiteralPath $zipPath).Length / 1MB), $sha)
Say "    目标机操作：解压 → 双击「安装依赖组件.bat」"
Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
Say ""
Say "==> 构建完成。"
