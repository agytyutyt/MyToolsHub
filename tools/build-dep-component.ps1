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
    [string]$Id = "opencv",           # 组件 id（机器可读，进文件名：JZToolsHub-依赖组件-<id>-v<版本>.zip）
    [string]$Name = "",               # 显示名（缺省取预置表 / 同 id）
    [string]$Packages = "",           # 载荷目录（逗号分隔；缺省取预置表）
    [string]$Provides = "",           # 对外提供的 import 名（缺省=载荷去掉 *.libs 等支撑目录）
    [string]$Affects = "",            # "装了它哪个功能才可用"（缺省取预置表）
    [string]$Version = "",            # 缺省取首个可导入包的实际版本
    [string]$From = "",
    [string]$OutDir = "",
    [string]$MinApp = "",             # 写入 min_app_version（缺省留空=不限制）
    [string]$RegistryFile = "",       # 登记表（缺省 tools\dep-components.json）：包 → 组件的唯一真源
    [switch]$Force,
    [switch]$NoZip
)

# 预置表：常用组件的默认载荷与文案（新组件可用 -Packages/-Provides/-Affects 覆盖，无需改脚本）
$PRESETS = @{
    # numpy 是 cv2 / pandas 的公共底层 → 单独成组件（路径①：避免重复与版本冲突）
    "numpy"  = @{
        name     = "NumPy 依赖组件"
        packages = @("numpy", "numpy.libs")
        provides = @("numpy")
        affects  = "cv2 / pandas 等组件的公共底层（单独安装无直接功能，装了是给别的组件用）"
    }
    "opencv" = @{
        name     = "OpenCV 依赖组件"
        packages = @("cv2", "cv2.libs")
        provides = @("cv2")
        requires = "numpy"
        affects  = "info-transfer / trajectory-convert 的视频码流模式（静态码模式不受影响）"
    }
    "office" = @{
        name     = "Office 文档组件"
        packages = @("openpyxl", "et_xmlfile", "docx", "lxml", "lxml.libs", "typing_extensions.py",
                     "xlrd", "olefile", "pypdf")
        provides = @("openpyxl", "et_xmlfile", "docx", "lxml", "typing_extensions", "xlrd", "olefile", "pypdf")
        affects  = "表格/文档读写：知识库与共享文档预览、战果与轨迹报表、admin 批量导入导出（xlsx；缺失时 admin 降级为仅 CSV）"
    }
    "qr"     = @{
        name     = "二维码编解码组件"
        packages = @("qrcode", "colorama", "zfec", "zxingcpp", "zxingcpp.libs")
        provides = @("qrcode", "colorama", "zfec", "zxingcpp")
        affects  = "信息传输/轨迹转换的出码与校验、QR 视频流解码"
    }
    "llm"    = @{
        name     = "大模型 HTTP 组件"
        packages = @("requests", "certifi", "charset_normalizer", "idna", "urllib3")
        provides = @("requests", "certifi", "charset_normalizer", "idna", "urllib3")
        affects  = "各插件的大模型调用（战果录入、人物关系、过滤器、轨迹速写）"
    }
}

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $Root "deploy\依赖组件" }
if (-not $RegistryFile) { $RegistryFile = Join-Path $Root "tools\dep-components.json" }
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

# ---- 0. 合并预置与参数 ----
$preset = $PRESETS[$Id]
if (-not $Name)     { $Name     = if ($preset) { [string]$preset.name } else { $Id } }
if (-not $Packages) { if (-not $preset) { Die "未预置组件 id=$Id：请用 -Packages 指定载荷目录（逗号分隔）" }
                      $Packages = ($preset.packages -join ",") }
if (-not $Affects)  { $Affects  = if ($preset) { [string]$preset.affects } else { "" } }
if (-not $Provides -and $preset -and $preset.provides) { $Provides = ($preset.provides -join ",") }   # ★ 预置的 provides 必须读，否则会退回"从载荷推导"（.py 载荷名会被当成 import 名）
$pkgList = @($Packages -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($pkgList.Count -eq 0) { Die "-Packages 为空" }
if (-not $Provides) {
    # 支撑目录（如 numpy.libs）不是 import 名，不进 provides
    $Provides = (@($pkgList | Where-Object { $_ -notmatch '\.libs$' -and $_ -notmatch '^_' }) -join ",")
}
$provideList = @($Provides -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })

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
# import 名 → 发行包名（如 cv2 → opencv-python；docx → python-docx）。
# ★ 必须经 packages_distributions 映射：直接按 import 名查版本会得到 unknown（实测踩到）。
function Get-DistName {
    param([string]$ImportName)
    try {
        $d = & python -c "import importlib.metadata as m;d=m.packages_distributions().get('$ImportName') or [];print(d[0] if d else '$ImportName')" 2>$null | Select-Object -First 1
        if ($d) { return $d.Trim() }
    } catch {}
    return $ImportName
}
function Get-DistVersion {
    param([string]$DistName)
    try {
        $v = & python -c "import importlib.metadata as m;print(m.version('$DistName'))" 2>$null | Select-Object -First 1
        if ($v) { return $v.Trim() }
    } catch {}
    return ""
}
$mods = [ordered]@{}      # import 名 → 发行包名
$verMap = [ordered]@{}    # 发行包名 → 版本（供组件清单 packages 字段）
foreach ($m in $provideList) {
    $dist = Get-DistName $m
    $mods[$m] = $dist
    $v = Get-DistVersion $dist
    if (-not $v) { $v = "unknown" }
    $verMap[$dist] = $v
}
if (-not $Version) { $Version = [string]($verMap.Values | Select-Object -First 1) }
Say "  组件     ：$Id（$Name）"
Say "  组件版本 ：$Version"
Say ("  载荷     ：" + ($pkgList -join ", "))
Say ("  提供     ：" + ($provideList -join ", ") + "（" + (($verMap.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "，") + "）")
if ($verMap.Values -contains "unknown") { Die "有包查不到版本（import 名 → 发行包名映射失败）：$($verMap.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })" }

# ---- 3. 组装 ----
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("jz-dep-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
$pylibs = Join-Path $staging "payload\pylibs"
New-Item -ItemType Directory -Force -Path $pylibs | Out-Null
$totalBytes = 0
foreach ($it in $pkgList) {
    $src = Join-Path $srcDir $it
    if (-not (Test-Path -LiteralPath $src)) {
        if ($it -match '\.libs$') { Warn "取源里没有 $it（部分构建无该支撑目录，可忽略）"; continue }
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
    id         = $Id
    name       = $Name
    version    = $Version
    built_at   = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
    components = @([ordered]@{
        id       = $Id
        name     = $Name
        version  = $Version
        packages = $verMap
        modules  = $mods
        provides = @($provideList)
        affects  = $Affects
        requires = $(if ($preset.requires) { [string]$preset.requires } else { "" })
    })
}
Write-Utf8NoBom (Join-Path $pylibs "manifest.json") (($manifest | ConvertTo-Json -Depth 8) + "`n")
Write-Utf8NoBom (Join-Path $staging "manifest.json") (($manifest | ConvertTo-Json -Depth 8) + "`n")

$pkgMeta = [ordered]@{
    schema          = 1
    kind            = "dep-component-package"
    id              = $Id
    name            = $Name
    version         = $Version
    provides        = @($provideList)
    affects         = $Affects
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
$notes += "包含：" + (($verMap.GetEnumerator() | ForEach-Object { "$($_.Key) $($_.Value)" }) -join "、") + "（合计约 $([math]::Round($totalBytes/1MB,1)) MB）"
$notes += ""
$notes += "## 为什么要单独装"
$notes += ""
$notes += "本组件只服务：" + $(if ($Affects) { "**$Affects**" } else { "（见插件 README）" }) + "，"
if ($preset -and $preset.requires) { $notes += ""; $notes += "**前置**：需先安装 `依赖组件-$(($preset.requires))`（本组件的运行时依赖不在包内）。" }
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
# 文件名对齐插件包约定：JZToolsHub-依赖组件-<id>-v<版本>.zip（id 机器可读、可容纳多个组件）
$zipPath = Join-Path $OutDir ("JZToolsHub-依赖组件-{0}-v{1}.zip" -f $Id, $Version)
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
# ---- 登记表（入库）：包 → 组件的唯一真源，主体据此把"缺哪个包"翻译成"装哪个组件" ----
$regObj = $null
if (Test-Path -LiteralPath $RegistryFile) {
    try { $regObj = Get-Content -LiteralPath $RegistryFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
}
if (-not $regObj) { $regObj = [pscustomobject]@{ schema = 1; components = [pscustomobject]@{} } }
if (-not $regObj.components) { $regObj | Add-Member -NotePropertyName components -NotePropertyValue ([pscustomobject]@{}) -Force }
$entry = [ordered]@{
    id         = $Id
    name       = $Name
    version    = $Version
    file       = (Split-Path -Leaf $zipPath)
    sha256     = $sha
    size       = [long](Get-Item -LiteralPath $zipPath).Length
    provides   = @($provideList)
    packages   = $verMap
    requires   = $(if ($preset.requires) { [string]$preset.requires } else { "" })
    affects    = $Affects
    built_at   = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
}
$regObj.components | Add-Member -NotePropertyName $Id -NotePropertyValue ([pscustomobject]$entry) -Force
Write-Utf8NoBom $RegistryFile (($regObj | ConvertTo-Json -Depth 10) + "`n")
Say "    登记（入库）：$RegistryFile"

Say ""
Say "==> 构建完成。"
