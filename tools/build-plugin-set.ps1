# build-plugin-set.ps1 —— 产出「插件集」：主包不含业务插件后，新机首装与批量收敛的介质
#
# 设计依据：docs\design\主体与插件解耦-设计文档.md §4.1 / §4.2
#   · 主包（JZToolsHub-v*.zip）只带核心插件 admin；
#   · 业务插件以「插件包」独立分发（build-plugin-package.ps1 产出）；
#   · 「插件集」= 一组插件包 + index.json + 一键安装脚本，供新机首装 / 多机批量收敛。
#
# 产物（OutDir 缺省 deploy\插件集\）：
#   JZToolsHub-插件集-<名称>-v<日期>.zip
#     ├── index.json            # 各包的 id / version / file / sha256 / requires_restart / min_app_version
#     ├── 安装插件集.bat         # 双击入口（以 -Set . 调用 install-plugin.ps1）
#     ├── install-plugin.ps1    # 目标机安装器（从仓库 tools\plugin-upgrade\ 原样拷贝）
#     ├── 升级说明.md            # 人工可读：包含哪些插件、怎么装、怎么回滚
#     └── JZToolsHub-插件-<id>-v<版本>.zip (+ .sha256) × N
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\build-plugin-set.ps1
#   powershell -ExecutionPolicy Bypass -File tools\build-plugin-set.ps1 -Name 全量 -Ids knowledge-base,file-filter
#   powershell -ExecutionPolicy Bypass -File tools\build-plugin-set.ps1 -Publish \\server\share\JZToolsHub\插件集
param(
    [string]$Name = "初始",                       # 插件集名称（进文件名）
    [string]$From = "",                           # 插件包来源目录（缺省 deploy\插件包）
    [string[]]$Ids = @(),                         # 只收指定插件（缺省：来源目录里每个插件的最新版）
    [string]$OutDir = "",                         # 产物目录（缺省 deploy\插件集）
    [string]$Publish = "",                        # 可选：投递到共享目录
    [switch]$Force,                               # 同名产物已存在时覆盖
    [switch]$NoZip                                # 只组装暂存目录，不打 zip（排障用）
)
$ErrorActionPreference = "Stop"

$Root       = Split-Path -Parent $PSScriptRoot
if (-not $From)   { $From   = Join-Path $Root "deploy\插件包" }
if (-not $OutDir) { $OutDir = Join-Path $Root "deploy\插件集" }
$ToolVer    = "1.0.0"

function Say  { param([string]$m = "") Write-Host $m }
function Warn { param([string]$m) Write-Warning $m }
function Die  { param([string]$m) Write-Host ""; Write-Host "  [失败] $m" -ForegroundColor Red; exit 1 }
function Write-Utf8NoBom { param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

Say ""
Say "================================================"
Say "  JZToolsHub 插件集构建（build-plugin-set.ps1 $ToolVer）"
Say "================================================"
Say ""
Say "  名称    ：$Name"
Say "  来源    ：$From"
Say "  产物目录：$OutDir"

if (-not (Test-Path -LiteralPath $From)) {
    Die ("插件包来源目录不存在：$From`n" +
         "         先用 tools\build-plugin-package.ps1 -Id <插件id> 出包，或用 -From 指定目录。")
}

# ---------------- 1. 选包：每个插件取最新版本 ----------------
$zips = @(Get-ChildItem -LiteralPath $From -Filter "JZToolsHub-插件-*.zip" -File -ErrorAction SilentlyContinue)
if ($zips.Count -eq 0) { Die "来源目录里没有插件包（JZToolsHub-插件-*.zip）：$From" }

function Get-PkgMeta {
    param([string]$ZipPath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    try {
        $z = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
        try {
            $e = $z.Entries | Where-Object { $_.FullName -eq "plugin-package.json" } | Select-Object -First 1
            if (-not $e) { return $null }
            $sr = New-Object System.IO.StreamReader($e.Open())
            try { return ($sr.ReadToEnd() | ConvertFrom-Json) } finally { $sr.Close() }
        } finally { $z.Dispose() }
    } catch { return $null }
}
function Compare-Ver {
    param([string]$A, [string]$B)
    if ($A -notmatch '^(\d+)\.(\d+)\.(\d+)') { return $null }
    $pa = @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
    if ($B -notmatch '^(\d+)\.(\d+)\.(\d+)') { return $null }
    $pb = @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
    for ($i = 0; $i -lt 3; $i++) { if ($pa[$i] -gt $pb[$i]) { return 1 } elseif ($pa[$i] -lt $pb[$i]) { return -1 } }
    return 0
}

$best = @{}
foreach ($z in $zips) {
    $meta = Get-PkgMeta $z.FullName
    if (-not $meta -or -not $meta.id -or -not $meta.version) {
        Warn "跳过无法解析包清单的 zip：$($z.Name)"
        continue
    }
    if ($Ids.Count -gt 0 -and ($Ids -notcontains [string]$meta.id)) { continue }
    $cur = $best[[string]$meta.id]
    if (-not $cur) { $best[[string]$meta.id] = @{ meta = $meta; zip = $z } ; continue }
    $c = Compare-Ver ([string]$meta.version) ([string]$cur.meta.version)
    if ($null -eq $c -or $c -gt 0) { $best[[string]$meta.id] = @{ meta = $meta; zip = $z } }
}
if ($best.Count -eq 0) { Die "没有可用的插件包（-Ids 过滤后为空？）：$From" }

# ---------------- 2. 组装暂存目录 ----------------
$stamp = Get-Date -Format "yyyyMMdd"
$setName = "JZToolsHub-插件集-$Name-v$stamp"
$zipPath = Join-Path $OutDir "$setName.zip"
if ((Test-Path -LiteralPath $zipPath) -and -not $Force) {
    Die "产物已存在：$zipPath`n         换 -Name，或加 -Force 覆盖（同名重打会覆盖旧产物）。"
}
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("jz-set-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $staging | Out-Null

Say ""
Say "==> 收录插件（每个插件取最新版）"
$entries = @()
foreach ($id in ($best.Keys | Sort-Object)) {
    $it = $best[$id]
    $zipSrc = $it.zip.FullName
    $sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipSrc).Hash.ToLower()
    Copy-Item -LiteralPath $zipSrc -Destination $staging -Force
    $shaSide = "$zipSrc.sha256"
    if (Test-Path -LiteralPath $shaSide) { Copy-Item -LiteralPath $shaSide -Destination $staging -Force }
    $entries += [pscustomobject][ordered]@{
        id               = [string]$it.meta.id
        version          = [string]$it.meta.version
        api_version      = [int]($it.meta.api_version)
        file             = $it.zip.Name
        sha256           = $sha
        size             = [long]$it.zip.Length
        requires_restart = [bool]$it.meta.requires_restart
        min_app_version  = [string]$it.meta.min_app_version
    }
    Say ("  {0,-18} v{1,-10} {2,8:N2} MB" -f $it.meta.id, $it.meta.version, ($it.zip.Length / 1MB))
}

$indexObj = [ordered]@{
    schema     = 1
    kind       = "plugin-set"
    name       = $Name
    built_at   = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
    built_from = [ordered]@{ commit = (& git -C $Root rev-parse --short HEAD 2>$null | Select-Object -First 1); source = $From }
    packages   = @($entries)
}
Write-Utf8NoBom (Join-Path $staging "index.json") (($indexObj | ConvertTo-Json -Depth 8) + "`n")

# 目标机安装器（原样拷贝：与插件包内那份同源，只改仓库这一份）
$installer = Join-Path $Root "tools\plugin-upgrade\install-plugin.ps1"
if (-not (Test-Path -LiteralPath $installer)) { Die "缺少安装器：$installer" }
Copy-Item -LiteralPath $installer -Destination $staging -Force

# 双击入口（GBK/CRLF，与既有 bat 一致）
$bat = @"
@echo off
chcp 65001 >nul
title JZToolsHub 插件集安装
echo 正在安装插件集「$Name」（$($entries.Count) 个插件）...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-plugin.ps1" -Set "%~dp0"
echo.
pause
"@
[System.IO.File]::WriteAllText((Join-Path $staging "安装插件集.bat"), $bat, [System.Text.Encoding]::GetEncoding("GBK"))

$notes = @()
$notes += "# 插件集：$Name（v$stamp）"
$notes += ""
$notes += "本插件集包含 $($entries.Count) 个插件包。安装方式二选一："
$notes += ""
$notes += "1. 双击 `「安装插件集.bat」`（推荐）：逐个校验并安装，已是最新版的会自动跳过，"
$notes += "   全部成功且服务原本在运行时统一重启一次。"
$notes += "2. 命令行：`powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Set .`"
$notes += ""
$notes += "## 包含的插件"
$notes += ""
foreach ($e in $entries) {
    $notes += ("- **{0}** v{1}（{2}；含后端改动需重启：{3}）" -f $e.id, $e.version, $e.file, $(if ($e.requires_restart) { "是" } else { "否" }))
}
$notes += ""
$notes += "## 注意"
$notes += ""
$notes += "- 依赖不满足**不阻断安装**：装完后该插件会被主体标记为「不可运行」并暂不加载，补齐依赖重启后自动恢复。"
$notes += "- 回滚：`install-plugin.ps1 -Rollback <插件id>`（取最近一份备份）；卸载：`-Uninstall <插件id>`。"
$notes += "- 主体（含核心插件 admin）由主包安装，**不在本插件集内**。"
Write-Utf8NoBom (Join-Path $staging "升级说明.md") ($notes -join "`n")

# ---------------- 3. 打包 ----------------
if ($NoZip) {
    Say ""
    Say "==> [-NoZip] 已组装暂存目录：$staging"
    exit 0
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
# ★ 用 .NET ZipFile 而非 Compress-Archive：后者在 PS 5.1 下按系统代码页写条目名，
#   中文文件名（JZToolsHub-插件-<id>-v<版本>.zip）会被别的解压工具读成乱码，
#   导致 -Set 找不到包。这里显式用 UTF-8 写条目名（Windows 资源管理器 / .NET / Python 均可正确读取）。
Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $staging, $zipPath, [System.IO.Compression.CompressionLevel]::Fastest, $false,
    (New-Object System.Text.UTF8Encoding($false)))
$setSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLower()
Write-Utf8NoBom "$zipPath.sha256" ("{0}  {1}`n" -f $setSha, (Split-Path -Leaf $zipPath))

Say ""
Say "==> 已产出插件集：$zipPath"
Say ("    体积 {0:N2} MB；sha256 {1}" -f ((Get-Item -LiteralPath $zipPath).Length / 1MB), $setSha)
Say "    旁挂哈希：$zipPath.sha256"
Say "    目标机操作：解压 → 双击「安装插件集.bat」"

if ($Publish) {
    if (-not (Test-Path -LiteralPath $Publish)) { New-Item -ItemType Directory -Force -Path $Publish | Out-Null }
    Copy-Item -LiteralPath $zipPath -Destination $Publish -Force
    Copy-Item -LiteralPath "$zipPath.sha256" -Destination $Publish -Force
    Say ""
    Say "==> 已投递到：$Publish"
}

Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
Say ""
Say "==> 构建完成。"
