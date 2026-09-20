# install-dep-component.ps1 —— 目标机安装「依赖组件包」（免管理员、纯解压、可卸载、有自检）
#
# 设计依据：docs\design\主体与插件解耦-设计文档.md §3.4 / T25 第 1 步
#   主包不再携带 cv2(opencv-python) 与 numpy（压缩后约 60 MB，只服务 info-transfer /
#   trajectory-convert 的**视频码流模式**）。它们改由本组件包按需安装到
#   <程序目录>\runtime\pylibs\，主体启动时注入 sys.path（app.py:_setup_dep_components）。
#
# 用法（在本脚本所在目录执行）：
#   powershell -ExecutionPolicy Bypass -File install-dep-component.ps1            # 安装/升级
#   powershell -ExecutionPolicy Bypass -File install-dep-component.ps1 -Check     # 只体检
#   powershell -ExecutionPolicy Bypass -File install-dep-component.ps1 -Uninstall # 卸载（删 pylibs）
#   -InstallDir <dir>  指定程序目录（缺省自动探测）   -Force  已存在也覆盖
param(
    [string]$Package = "",
    [switch]$Check,
    [switch]$Uninstall,
    [switch]$Force,
    [string]$InstallDir = ""
)
$ErrorActionPreference = "Stop"
$ScriptVer = "1.0.0"
$AppName = "JZToolsHub"
$ExeName = "$AppName.exe"
$Source = $PSScriptRoot
$RegKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"

function Say { param([string]$m = "") Write-Host $m }
function Warn { param([string]$m) Write-Warning $m }
function Fail { param([string]$m) Write-Host ""; Write-Host "  [失败] $m" -ForegroundColor Red; exit 1 }

function Resolve-AppDir {
    param([string]$Explicit)
    if ($Explicit) { return (Resolve-Path -LiteralPath $Explicit).Path }
    try {
        $v = (Get-ItemProperty -Path $RegKey -ErrorAction Stop).InstallLocation
        if ($v -and (Test-Path -LiteralPath (Join-Path $v $ExeName))) { return $v }
    } catch {}
    $def = Join-Path $env:LOCALAPPDATA $AppName
    if (Test-Path -LiteralPath (Join-Path $def $ExeName)) { return $def }
    # 脚本所在目录的上一级也常见（组件包解压到程序目录内）
    $up = Split-Path -Parent $Source
    if (Test-Path -LiteralPath (Join-Path $up $ExeName)) { return $up }
    return $def
}

$AppDir = Resolve-AppDir -Explicit $InstallDir
$Pylibs = Join-Path $AppDir "runtime\pylibs"
Say ""
Say "================================================"
Say "  JZToolsHub 依赖组件安装器 v$ScriptVer"
Say "================================================"
Say "  程序目录：$AppDir"
Say "  安装位置：$Pylibs"

if (-not (Test-Path -LiteralPath (Join-Path $AppDir $ExeName))) {
    Fail ("未找到 $ExeName：$AppDir`n         请先安装工具箱，或用 -InstallDir <dir> 指定程序目录。")
}

# ---- 体检 / 自检：调用 exe 自身的 --check-deps（真实 import 验证，不是"文件在不在"） ----
function Invoke-SelfCheck {
    # 冻结 exe 是 GUI 子系统（无控制台）→ 自检结果写 JSON 文件后读回（不靠 stdout）
    $exe = Join-Path $AppDir $ExeName
    if (-not (Test-Path -LiteralPath $exe)) { return @{ ok = $false; text = "缺少 $ExeName" } }
    $outFile = Join-Path $env:TEMP ("jz-checkdeps-" + [Guid]::NewGuid().ToString("N").Substring(0, 8) + ".json")
    try {
        $p = Start-Process -FilePath $exe -ArgumentList @("--check-deps", $outFile) -Wait -PassThru -WindowStyle Hidden
        $code = $p.ExitCode
    } catch {
        return @{ ok = $false; text = "自检执行失败：$($_.Exception.Message)" }
    }
    $text = ""
    if (Test-Path -LiteralPath $outFile) {
        try { $text = (Get-Content -LiteralPath $outFile -Raw -Encoding UTF8).Trim() } catch {}
        Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue
    }
    if (-not $text) { $text = "(自检未产出结果文件；退出码 $code)" }
    return @{ ok = ($code -eq 0); text = $text }
}

if ($Check) {
    Say ""
    Say "==> 依赖组件体检"
    $r = Invoke-SelfCheck
    Say $r.text
    Say ("  结论：" + $(if ($r.ok) { "已安装且可 import（视频码流模式可用）" } else { "未安装或不可用（视频码流模式将不可用）" }))
    exit $(if ($r.ok) { 0 } else { 1 })
}

if ($Uninstall) {
    Say ""
    Say "==> 卸载依赖组件（仅删除 $Pylibs）"
    if (Test-Path -LiteralPath $Pylibs) {
        Remove-Item -LiteralPath $Pylibs -Recurse -Force
        Say "  已删除。相关插件的视频码流模式将不可用（静态码模式不受影响）。"
    } else {
        Say "  目录不存在，无需卸载。"
    }
    exit 0
}

# ---- 定位载荷：包内 payload\pylibs，或本目录下的 pylibs ----
$payload = Join-Path $Source "payload\pylibs"
if (-not (Test-Path -LiteralPath $payload)) { $payload = Join-Path $Source "pylibs" }
if (-not (Test-Path -LiteralPath $payload)) { Fail "未找到组件载荷（payload\pylibs 或 pylibs）：$Source" }

$meta = $null
$metaPath = Join-Path $Source "dep-component.json"
if (Test-Path -LiteralPath $metaPath) {
    try { $meta = Get-Content -LiteralPath $metaPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
}
if ($meta -and $meta.min_app_version) {
    $vf = Join-Path $AppDir "version.json"
    if (Test-Path -LiteralPath $vf) {
        $appVer = [string]((Get-Content -LiteralPath $vf -Raw -Encoding UTF8 | ConvertFrom-Json).app)
        if ($appVer -and ($appVer -lt [string]$meta.min_app_version)) {
            Fail "本组件要求主程序 ≥ $($meta.min_app_version)（当前 $appVer），请先升级主包。"
        }
    }
}

if ((Test-Path -LiteralPath $Pylibs) -and -not $Force) {
    $cur = Invoke-SelfCheck
    if ($cur.ok) {
        Say ""
        Say "  已安装且自检通过；如需重装请加 -Force。"
        exit 0
    }
    Warn "已存在 $Pylibs 但自检未通过，将覆盖安装。"
}

# ---- 安装（纯解压，免管理员；不写注册表、不建快捷方式） ----
Say ""
Say "==> 正在安装依赖组件…"
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "runtime") | Out-Null
if (Test-Path -LiteralPath $Pylibs) { Remove-Item -LiteralPath $Pylibs -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Pylibs | Out-Null
Copy-Item -Path (Join-Path $payload "*") -Destination $Pylibs -Recurse -Force
# 组件清单落到 pylibs\manifest.json：主体与后台据此显示"已装/未装 + 版本"
$compManifest = Join-Path $Source "manifest.json"
if (Test-Path -LiteralPath $compManifest) { Copy-Item -LiteralPath $compManifest -Destination $Pylibs -Force }

$after = Invoke-SelfCheck
Say ""
Say "==> 安装结果"
Say $after.text
if (-not $after.ok) {
    Fail ("自检未通过：组件文件已就位但 exe 无法 import 其中的库。`n" +
          "         请把上面输出反馈给维护者；如需回退可执行 -Uninstall。")
}
# 生效功能文案取自组件清单（affects），脚本本身与具体组件无关
$affects = ""
$mPath = Join-Path $Pylibs "manifest.json"
if (Test-Path -LiteralPath $mPath) {
    try {
        $mo = Get-Content -LiteralPath $mPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $affects = [string]($mo.components | Select-Object -First 1).affects
    } catch {}
}
Say ""
Say "  完成：本组件已可用$(if ($affects) { '（' + $affects + '）' } else { '' })。"
Say "  卸载：install-dep-component.ps1 -Uninstall"
