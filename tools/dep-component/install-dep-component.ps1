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
    [switch]$Uninstall,               # 卸载**全部**依赖组件（删整个 pylibs）
    [string]$Remove = "",             # 只卸载指定组件（保留其它组件）
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
    Say "==> 卸载依赖组件（删除整个 $Pylibs：**全部**组件）"
    if (Test-Path -LiteralPath $Pylibs) {
        Remove-Item -LiteralPath $Pylibs -Recurse -Force
        Say "  已删除。相关插件的视频码流模式将不可用（静态码模式不受影响）。"
        Say "  只想卸载某一个组件：用 -Remove <组件id>（如 -Remove cv2）。"
    } else {
        Say "  目录不存在，无需卸载。"
    }
    exit 0
}

if ($Remove) {
    Say ""
    Say "==> 卸载单个依赖组件：$Remove（保留其它组件）"
    $mPathR = Join-Path $Pylibs "manifest.json"
    if (-not (Test-Path -LiteralPath $mPathR)) { Fail "本机未安装任何依赖组件（缺 $mPathR）。" }
    $manR = Get-Content -LiteralPath $mPathR -Raw -Encoding UTF8 | ConvertFrom-Json
    $entriesR = @($manR.components)
    $target = @($entriesR | Where-Object { [string]$_.id -eq $Remove } | Select-Object -First 1)
    if (-not $target) {
        Fail ("未安装组件 $Remove。已装：" + ((@($entriesR | ForEach-Object { [string]$_.id })) -join "、"))
    }
    # 按登记的文件清单删（早期组件包没有清单时退化为按 provides 顶层名删）
    $filesR = @($target.files)
    if ($filesR.Count -eq 0) {
        $filesR = @()
        foreach ($pv in @($target.provides)) { $filesR += @($pv, "$pv.libs") }
    }
    $n = 0
    foreach ($rel in $filesR) {
        $abs = Join-Path $Pylibs $rel
        if (Test-Path -LiteralPath $abs) { Remove-Item -LiteralPath $abs -Recurse -Force -ErrorAction SilentlyContinue; $n++ }
    }
    $keepR = @($entriesR | Where-Object { [string]$_.id -ne $Remove })
    if ($keepR.Count -eq 0) {
        Remove-Item -LiteralPath $Pylibs -Recurse -Force
        Say "  已删除 $Remove（$n 项文件）；已无其它组件，目录一并清理。"
    } else {
        $mergedR = [ordered]@{
            schema     = 1
            kind       = "dep-components"
            updated_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
            python     = [string]$manR.python
            platform   = [string]$manR.platform
            components = @($keepR)
        }
        Write-Utf8NoBom $mPathR (($mergedR | ConvertTo-Json -Depth 10) + "`n")
        Say ("  已删除 $Remove（$n 项文件）；保留组件：" + (@($keepR | ForEach-Object { "$($_.id)=$($_.version)" }) -join "，"))
    }
    # 删完自检一次：剩下的组件是否仍可用（例如删 numpy 会让 cv2 不可用）
    $rR = Invoke-SelfCheck
    Say ("  剩余组件自检：" + $(if ($rR.ok) { "通过" } else { "未通过（见下）" }))
    if (-not $rR.ok) { Say $rR.text }
    exit $(if ($rR.ok) { 0 } else { 1 })
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
# ---- 前置校验：组件与主程序的 Python 版本 / 平台必须一致（C 扩展 ABI 硬约束） ----
# 主包换 Python 版本后，旧组件里的 .pyd 会加载失败 → 这里在写盘前就拒绝并给出指引。
if ($meta) {
    $vf = Join-Path $AppDir "version.json"
    $appPy = ""
    if (Test-Path -LiteralPath $vf) {
        try { $appPy = [string]((Get-Content -LiteralPath $vf -Raw -Encoding UTF8 | ConvertFrom-Json).python) } catch {}
    }
    $cPy = [string]$meta.python
    if ($appPy -and $cPy -and $cPy -ne "unknown") {
        $mm = { param($v) ($v -split '\.')[0..1] -join '.' }
        if ((& $mm $appPy) -ne (& $mm $cPy)) {
            Fail ("本组件是为 Python $cPy 构建的，当前主程序内置 Python $appPy —— 版本不匹配。`n" +
                  "         请取与主程序匹配的依赖组件包（用对应版本的主包构建产物），或先升级主包。")
        }
    }
    if ($meta.platform -and $meta.platform -ne "win-amd64") {
        Warn "该组件声明平台为 $($meta.platform)，与当前安装器（win-amd64）可能不匹配。"
    }
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

# ---- 前置：组件间依赖（如 cv2 的 import 就需要 numpy）----
# 组件间依赖不并入同一个包（避免同一份库重复分发），因此必须显式检查并给出可照做的提示。
$installedIds = @()
$mp = Join-Path $Pylibs "manifest.json"
if (Test-Path -LiteralPath $mp) {
    try {
        $om = Get-Content -LiteralPath $mp -Raw -Encoding UTF8 | ConvertFrom-Json
        $installedIds = @(@($om.components) | ForEach-Object { [string]$_.id } | Where-Object { $_ })
    } catch {}
}
$needReq = @()
if ($meta -and $meta.requires_components) { $needReq = @($meta.requires_components) }
$missingReq = @($needReq | Where-Object { $installedIds -notcontains $_ })
if ($missingReq.Count -gt 0) {
    Warn ("本组件还需要依赖组件：" + ($missingReq -join "、") + "`n" +
          "         它不并入本包（避免同一份库重复分发）。请在本组件之后一并安装：" +
          (($missingReq | ForEach-Object { "JZToolsHub-依赖-$_-v*.zip" }) -join "、") + "`n" +
          "         否则本组件自检不会通过（例如 cv2 的 import 期就需要 numpy）。")
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
# ★ pylibs 是**所有**依赖组件的公共目录：本安装器只清理**本组件上一版**的文件，
#   绝不清空整个目录——cv2 与 numpy 必须共存（cv2 的 import 期就需要 numpy），
#   整体清空会让"先装 cv2、再装 numpy"变成"只剩 numpy"。
Say ""
Say "==> 正在安装依赖组件…"
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "runtime") | Out-Null
New-Item -ItemType Directory -Force -Path $Pylibs | Out-Null

# 1) 读旧登记（清理本组件旧文件 + 合并登记都要用）
$oldEntries = @()
$mPath0 = Join-Path $Pylibs "manifest.json"
if (Test-Path -LiteralPath $mPath0) {
    try {
        $oldMan = Get-Content -LiteralPath $mPath0 -Raw -Encoding UTF8 | ConvertFrom-Json
        $oldEntries = @($oldMan.components)
    } catch { $oldEntries = @() }
}
$prev = @($oldEntries | Where-Object { [string]$_.id -eq $Id } | Select-Object -First 1)
if ($prev) {
    $prevFiles = @($prev.files)
    if ($prevFiles.Count -eq 0) {
        # 早期组件包没有文件清单：退化为按 provides 的顶层名清理
        $prevFiles = @()
        foreach ($pv in @($prev.provides)) { $prevFiles += @($pv, "$pv.libs") }
    }
    $removed = 0
    foreach ($rel in $prevFiles) {
        $abs = Join-Path $Pylibs $rel
        if (Test-Path -LiteralPath $abs) {
            Remove-Item -LiteralPath $abs -Recurse -Force -ErrorAction SilentlyContinue
            $removed++
        }
    }
    Say ("    清理上一版（v" + $prev.version + "）文件：$removed 项")
}

# 2) 覆盖式解压本组件载荷（同名覆盖，不动其它组件的文件）
Copy-Item -Path (Join-Path $payload "*") -Destination $Pylibs -Recurse -Force

# 3) 合并登记到 pylibs\manifest.json：同 id 覆盖、其它组件保留
#    （主体 jz_deps.load_installed_deps 与后台「已安装依赖」都读这份清单）
$compManifest = Join-Path $Source "manifest.json"
if (Test-Path -LiteralPath $compManifest) {
    try {
        $newMan = Get-Content -LiteralPath $compManifest -Raw -Encoding UTF8 | ConvertFrom-Json
        $newEntry = @($newMan.components)[0]
        $keep = @($oldEntries | Where-Object { [string]$_.id -ne $Id })
        $merged = [ordered]@{
            schema       = 1
            kind         = "dep-components"
            updated_at   = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
            python       = [string]$newMan.python
            platform     = [string]$newMan.platform
            components   = @($keep) + @($newEntry)
        }
        Write-Utf8NoBom $mPath0 (($merged | ConvertTo-Json -Depth 10) + "`n")
        Say ("    已登记组件：" + (@($merged.components | ForEach-Object { "$($_.id)=$($_.version)" }) -join "，"))
    } catch {
        Warn "组件清单合并失败（已保留原文件）：$($_.Exception.Message)"
        Copy-Item -LiteralPath $compManifest -Destination $mPath0 -Force
    }
}

$after = Invoke-SelfCheck
Say ""
Say "==> 安装结果"
Say $after.text
if (-not $after.ok) {
    # 先分辨"还差另一个组件"（可照做）与"组件自身损坏"（要报维护者）
    $missNow = @()
    try { $missNow = @((($after.text | ConvertFrom-Json).missing_requires)) } catch {}
    if ($missNow.Count -gt 0) {
        Fail ("自检未通过：本组件还需要依赖组件 **" + ($missNow -join "、") + "**（尚未安装）。`n" +
              "         请再解压并运行 " + (($missNow | ForEach-Object { "JZToolsHub-依赖-$_-v*.zip" }) -join "、") +
              " 的安装器（本组件文件已就位，重跑不会丢）；装齐后自检即通过。`n" +
              "         说明：组件间依赖不并入同一个包，避免同一份库被重复分发。")
    }
    Fail ("自检未通过：组件文件已就位但 exe 无法 import 其中的库。`n" +
          "         常见原因：① 本机缺 VC++ 2015-2022 运行库且组件包内未自带（报 DLL load failed）；`n" +
          "                   ② 组件与主程序的 Python 版本不匹配（见组件清单 python 字段）；`n" +
          "                   ③ 组件包不完整。请把上面输出反馈给维护者；回退可执行 -Uninstall。")
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
