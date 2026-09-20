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
    [string]$Requires = "",           # 依赖的其它组件 id（逗号分隔；缺省取 UNITS 表）——写进清单供安装器校验
    [string]$Affects = "",            # 兼容保留（提示文案已改由插件声明，不再写进依赖包清单）
    [string]$Version = "",            # 缺省取首个可导入包的实际版本
    [string]$From = "",
    [string]$OutDir = "",
    [string]$MinApp = "",             # 写入 min_app_version（缺省留空=不限制）
    [switch]$NoVcRuntime,            # 不随组件分发 VC++ 运行时（默认：含 C 扩展的组件自动带）
    [switch]$Force,
    [switch]$NoZip
)

# VC++ 2015-2022 运行时（C 扩展的硬前提）——与 LibreOffice 组件包同一份清单与做法。
# 为什么必须随组件分发：主包 _internal 只带 VCRUNTIME140*.dll，**不带 msvcp140***；
# 目标机若没装 VC++ 运行库，cv2/lxml 等 .pyd 会在加载阶段失败（现象极隐蔽：
# "DLL load failed"、开发机上因 System32 兜底而测不出来——LibreOffice 已踩过同一个坑）。
$VcRuntimeDlls = @(
    "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll",
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
    "concrt140.dll", "vccorlib140.dll"
)

# 预置表：常用组件的默认载荷与文案（新组件可用 -Packages/-Provides/-Affects 覆盖，无需改脚本）
# 依赖单元表（平铺同一层级：一个依赖一个包，不做"簇"分组）。
#   key  = 载荷目录名（也是文件名里的 id）；value.payload = 该依赖 + 它独占的传递依赖。
#   共享底层（如 numpy 被 cv2 与插件直接使用）不并入别的单元，由插件在 requires 里显式声明——
#   同一份依赖在目标机上只存在一份，不重复也不冲突。
#   提示文案不在这里：缺哪个依赖、影响什么、怎么修，由插件自己声明
#   （manifest.requires[].hint，见《插件设计规范.md》U-8），框架只原样展示。
$UNITS = @{
    "numpy"    = @{ payload = @("numpy", "numpy.libs") }
    # cv2 的 Python 绑定在 import 期就要 numpy（实测 "OpenCV bindings requires numpy"）——
    # 组件间依赖必须显式声明，安装器才能在"只装 cv2"时给出可照做的提示，而不是报"组件损坏"。
    "cv2"      = @{ payload = @("cv2", "cv2.libs"); requires = @("numpy") }
    "openpyxl" = @{ payload = @("openpyxl", "et_xmlfile") }
    "docx"     = @{ payload = @("docx", "lxml", "lxml.libs", "typing_extensions.py") }
    "xlrd"     = @{ payload = @("xlrd") }
    "olefile"  = @{ payload = @("olefile") }
    "pypdf"    = @{ payload = @("pypdf") }
    "qrcode"   = @{ payload = @("qrcode", "colorama") }
    "zfec"     = @{ payload = @("zfec") }
    "zxingcpp" = @{ payload = @("zxingcpp", "zxingcpp.libs") }
    "requests" = @{ payload = @("requests", "urllib3", "certifi", "idna", "charset_normalizer") }
}

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

# ---- 0. 合并预置与参数 ----
$unit = $UNITS[$Id]
if (-not $Name)     { $Name     = $Id }
if (-not $Packages) { if (-not $unit) { Die "未知依赖单元 id=$Id：请用 -Packages 指定载荷目录，或先在 UNITS 表里登记" }
                      $Packages = ($unit.payload -join ",") }
if (-not $Requires -and $unit -and $unit.requires) { $Requires = ($unit.requires -join ",") }
$requireList = @()
if ($Requires) { $requireList = @($Requires -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
if (-not $Provides) { $Provides = $Id }        # 单元只"提供"它自己；闭包里的依赖不声明为 provides
$pkgList = @($Packages -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($pkgList.Count -eq 0) { Die "-Packages 为空（载荷目录清单缺失）" }
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
$pyVer = (& python -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null | Select-Object -First 1)
if (-not $pyVer) { $pyVer = "unknown" }
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
    python     = $pyVer
    platform   = "win-amd64"
    components = @([ordered]@{
        id       = $Id
        name     = $Name
        version  = $Version
        packages = $verMap
        modules  = $mods
        provides = @($provideList)
        affects  = $Affects
        python   = $pyVer
        platform = "win-amd64"
        vc_runtime = @($vcBundled)
        requires_components = @($requireList)
    })
}
# ---- VC++ 运行时：组件含 C 扩展（.pyd/.dll）时自动随包（-NoVcRuntime 可关） ----
$vcBundled = @()
$hasNative = @(Get-ChildItem -LiteralPath $pylibs -Recurse -File -Include *.pyd, *.dll -ErrorAction SilentlyContinue).Count -gt 0
if ($hasNative -and -not $NoVcRuntime) {
    $sys32 = Join-Path $env:WINDIR "System32"
    foreach ($dll in $VcRuntimeDlls) {
        $src = Join-Path $sys32 $dll
        if (Test-Path -LiteralPath $src) {
            Copy-Item -LiteralPath $src -Destination $pylibs -Force
            $vcBundled += $dll
        } else {
            Warn "构建机 System32 缺少 $dll（未随组件分发）"
        }
    }
    Say ("    + VC++ 运行时   " + $vcBundled.Count + " 个 DLL（C 扩展硬前提，目标机免装运行库）")
} elseif (-not $hasNative) {
    Say "    （纯 Python 组件：无需 VC++ 运行时）"
}

# 清单在 VC 运行时打包**之后**定稿：vc_runtime 必须反映实际随包的 DLL（写在打包前会恒为空），
# 并登记本组件的文件清单（相对 pylibs 的路径）——安装器据此按组件精确清理旧文件，
# 而不是清空整个 pylibs（cv2 与 numpy 必须共存）。
# 清理载荷里的构建机字节码：__pycache__/*.pyc 体积大、且只对构建机的 Python 版本有效
# （目标机解释器版本不同会直接忽略，留着纯属浪费体积，还会进文件清单）。
$pyc = @(Get-ChildItem -LiteralPath $pylibs -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue)
foreach ($d in $pyc) { Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
# 注意：-Include 与 -LiteralPath 同用时会被忽略（实测把整个载荷都匹配上并删光），
# 故这里用 Where-Object 显式按扩展名过滤。
$pycFiles = @(Get-ChildItem -LiteralPath $pylibs -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { @(".pyc", ".pyo") -contains $_.Extension })
foreach ($f in $pycFiles) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue }
if ($pyc.Count -gt 0 -or $pycFiles.Count -gt 0) {
    Say ("    已剔除构建机字节码：" + $pyc.Count + " 个 __pycache__ 目录 / " + $pycFiles.Count + " 个 .pyc")
}

$manifest.components[0].vc_runtime = @($vcBundled)
$manifest.components[0].files = @(
    Get-ChildItem -LiteralPath $pylibs -Recurse -File | Sort-Object FullName |
    ForEach-Object { $_.FullName.Substring($pylibs.Length).TrimStart([char]92) } |
    Where-Object { $_ -ne "manifest.json" }
)
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
    # ABI 契约：组件里的 C 扩展只能被同版本同架构的解释器加载（主包换 Python 版本必须重出组件）
    python          = $pyVer
    platform        = "win-amd64"
    vc_runtime      = @($vcBundled)
    # 组件间依赖（如 cv2 → numpy）：安装器据此在"只装 cv2"时给出可照做的提示
    requires_components = @($requireList)
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
$notes += "# 依赖包：$Name v$Version"
$notes += ""
$notes += "包含：" + (($verMap.GetEnumerator() | ForEach-Object { "$($_.Key) $($_.Value)" }) -join "、") + "（合计约 $([math]::Round($totalBytes/1MB,1)) MB）"
$notes += ""
$notes += "## 为什么要单独装"
$notes += ""
$notes += "本组件只服务：" + $(if ($Affects) { "**$Affects**" } else { "（见插件 README）" }) + "，"
if ($requireList.Count -gt 0) {
    $notes += "**前置**：本组件需要先安装依赖组件 **" + ($requireList -join "、") + "**" +
              "（它的运行时依赖不并入本包，避免同一份库被重复分发）；"
    $notes += "只装本组件时安装器会自检失败并提示缺哪个组件——按提示先装即可。"
}
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
$zipPath = Join-Path $OutDir ("JZToolsHub-依赖-{0}-v{1}.zip" -f $Id, $Version)
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
