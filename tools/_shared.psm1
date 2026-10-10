# _shared.psm1 —— 出包链共用函数（仓库侧构建脚本专用；P2-2「构建脚本单一真源」）
#
# 消费方：build-deploy.ps1 / tools\build-*.ps1 / tools\e2e\build-acceptance-kit.ps1。
# 为什么只服务"仓库侧"：目标机安装器（install.ps1、install-plugin.ps1、
# install-dep-component.ps1、offline-runtime\*.ps1）必须**自包含**——它们要随包分发到
# 没有仓库的机器上，只能各自带着实现（这是"单一真源拷贝进包"的设计，不是漏收口）。
#
# 收口清单（此前 N 份手抄、语义还漂移过）：
#   Say/Warn/Die、Write-Utf8NoBom、Get-RelPath、Get-Sha256、Get-Hash16、
#   Compare-SemVer（★ 统一语义：任一侧非法 → $null，由调用方决定；此前
#   build-plugin-package 那份把非法按 (0,0,0) 处理，与其余三份结论相反）、
#   Get-TempStagingPath、Get-IsoTimestamp、Compress-DirectoryToZip、
#   Get-SpecPackageList（JZToolsHub.spec 的 PACKAGES / DEP_COMPONENT_PACKAGES 解析）、
#   Test-LibreOfficeCoreBundle、Get-PyBaseline、Get-PythonRuntimeInfo。

function Say { param([string]$m = "") Write-Host $m }
function Warn { param([string]$m) Write-Warning $m }
function Die {
    param([string]$m)
    Write-Host ""
    Write-Host "  [失败] $m" -ForegroundColor Red
    exit 1
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

# 相对路径（PS 5.1 没有 [IO.Path]::GetRelativePath，手写一个）
function Get-RelPath {
    param([string]$Base, [string]$Full)
    $b = [System.IO.Path]::GetFullPath($Base).TrimEnd('\', '/')
    $f = [System.IO.Path]::GetFullPath($Full)
    if ($f.StartsWith($b, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $f.Substring($b.Length).TrimStart('\', '/') -replace '\\', '/'
    }
    return $f -replace '\\', '/'
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

# 16 位内容指纹（MD5 前 16 位；与 jztools_data._file_hash16 同语义，用于"内容是否变过"）
function Get-Hash16 {
    param([string]$Path)
    $md5 = [System.Security.Cryptography.MD5]::Create()
    try {
        $fs = [System.IO.File]::OpenRead($Path)
        try { return ([BitConverter]::ToString($md5.ComputeHash($fs)) -replace '-', '').ToLower().Substring(0, 16) }
        finally { $fs.Dispose() }
    } finally { $md5.Dispose() }
}

# 语义化版本比较：返回 1 / 0 / -1；任一侧不是 主.次.修订 开头 → $null（调用方决定口径）。
# ★ 统一语义（P2-2）：非法一侧一律 $null——需要"非法按 0.0.0"的调用方自行转换，
#   不要在本函数里替调用方拍板（此前两套语义并存，同一比较两处结论不同）。
function Compare-SemVer {
    param([string]$A, [string]$B)
    $pa = $null; $pb = $null
    if ($A -match '^(\d+)\.(\d+)\.(\d+)') { $pa = @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3]) }
    if ($B -match '^(\d+)\.(\d+)\.(\d+)') { $pb = @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3]) }
    if ($null -eq $pa -or $null -eq $pb) { return $null }
    for ($i = 0; $i -lt 3; $i++) {
        if ($pa[$i] -gt $pb[$i]) { return 1 }
        if ($pa[$i] -lt $pb[$i]) { return -1 }
    }
    return 0
}

function Get-TempStagingPath {
    param([string]$Prefix)
    return (Join-Path ([System.IO.Path]::GetTempPath()) ($Prefix + [Guid]::NewGuid().ToString("N").Substring(0, 8)))
}

function Get-IsoTimestamp { return (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz") }

# 目录 → zip。替代 Compress-Archive：PS 5.1 的 Compress-Archive 对非 ASCII 条目名
# 不写 UTF-8 标志位（bit 11），Windows 资源管理器 / 7-Zip / Python 会按系统代码页解码
# 成乱码；.NET ZipFile 默认 UTF-8 并按需置标志位。主包 zip 与插件包链路由此统一机制。
# ★ 不要传 entryNameEncoding：显式传编码时 .NET **反而不会**写 UTF-8 标志位（实测教训）。
# -IncludeBaseDirectory 对应「zip 内保留顶层目录名」（离线组件包用）。
function Compress-DirectoryToZip {
    param(
        [Parameter(Mandatory = $true)][string]$SourceDir,
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [ValidateSet("Fastest", "Optimal")][string]$Level = "Fastest",
        [switch]$IncludeBaseDirectory
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    $dir = Split-Path -Parent $ZipPath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $lvl = [System.IO.Compression.CompressionLevel]::Fastest
    if ($Level -eq "Optimal") { $lvl = [System.IO.Compression.CompressionLevel]::Optimal }
    [System.IO.Compression.ZipFile]::CreateFromDirectory($SourceDir, $ZipPath, $lvl, [bool]$IncludeBaseDirectory)
    return (Get-Item -LiteralPath $ZipPath).Length
}

# JZToolsHub.spec 里的包清单（出包链唯一真源，不另存抄本）：
#   PACKAGES                = 随主包分发的第三方库
#   DEP_COMPONENT_PACKAGES  = 由「依赖组件包」按需提供的库（主包 excludes）
# 解析口径与 gen-installed-deps.py 的 read_spec_whitelist 一致：行内注释去掉、取引号内包名。
# ★ 行首锚定（(?m)^）——PACKAGES 不能把 DEP_COMPONENT_PACKAGES 的前缀段误命中。
function Get-SpecPackageList {
    param([string]$Name)
    $spec = Join-Path (Split-Path -Parent $PSScriptRoot) "JZToolsHub.spec"
    if (-not (Test-Path -LiteralPath $spec)) { return @() }
    $src = Get-Content -LiteralPath $spec -Raw -Encoding UTF8
    $m = [regex]::Match($src, "(?sm)^\s*$Name\s*=\s*\[(.*?)\]")
    if (-not $m.Success) { return @() }
    return @([regex]::Matches($m.Groups[1].Value, '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value })
}
function Get-SpecPackages { return @(Get-SpecPackageList "PACKAGES") }
function Get-SpecDepComponentPackages { return @(Get-SpecPackageList "DEP_COMPONENT_PACKAGES") }

# LibreOffice 裁剪核心包出厂核验：zip 体积 + sha256 必须与 meta 一致；
# 给定 -SourceDir 时再校验源 MSI 的溯源 sha256（换了 MSI 却没重建核心包 → 拦下）。
# 返回解析后的 meta 对象（调用方继续用 artifact / source 字段）。双份实现于此收口
# （build-deploy.ps1 整包随带分支 ↔ build-offline-component.ps1 独立组件包）。
function Test-LibreOfficeCoreBundle {
    param(
        [Parameter(Mandatory = $true)][string]$CoreZip,
        [Parameter(Mandatory = $true)][string]$CoreMeta,
        [string]$SourceDir = ""
    )
    if (-not (Test-Path -LiteralPath $CoreZip)) {
        throw "缺少 libreoffice-core.zip：$CoreZip（先跑：python tools\fetch-offline-bundle.py --core）"
    }
    if (-not (Test-Path -LiteralPath $CoreMeta)) {
        throw "包内有 libreoffice-core.zip 但缺 libreoffice-core.json，无法校验溯源。请重跑：python tools\build-libreoffice-core.py --force"
    }
    $cm = Get-Content -LiteralPath $CoreMeta -Raw -Encoding UTF8 | ConvertFrom-Json
    if ((Get-Item -LiteralPath $CoreZip).Length -ne [int64]$cm.artifact.size) {
        throw ("libreoffice-core.zip 体积与元数据不符（{0} vs {1}）；请重跑：python tools\build-libreoffice-core.py --force" -f (Get-Item -LiteralPath $CoreZip).Length, [int64]$cm.artifact.size)
    }
    if ((Get-FileHash -LiteralPath $CoreZip -Algorithm SHA256).Hash.ToLower() -ne ([string]$cm.artifact.sha256).ToLower()) {
        throw "libreoffice-core.zip 的 sha256 与元数据不符；请重跑：python tools\build-libreoffice-core.py --force"
    }
    if ($SourceDir) {
        $srcMsiPath = Join-Path $SourceDir ([string]$cm.source.file)
        if ((Test-Path -LiteralPath $srcMsiPath) -and
            ((Get-FileHash -LiteralPath $srcMsiPath -Algorithm SHA256).Hash.ToLower() -ne ([string]$cm.source.sha256).ToLower())) {
            throw "runtime\libreoffice\$($cm.source.file) 与核心包的溯源 sha256 不符（换了 MSI 却没重建核心包）；请重跑：python tools\build-libreoffice-core.py --force"
        }
    }
    return $cm
}

# 打包/组件解释器基线（与 Windows 10+ 目标机配套；见 docs/eval/Python版本选型评估.md）
function Get-PyBaseline { return "3.14" }

# 取解释器可执行路径与版本：返回 @{ Exe; Ver; Short; Baseline; MatchesBaseline }
function Get-PythonRuntimeInfo {
    param([string]$Python = "python")
    $exe = (Get-Command $Python -ErrorAction SilentlyContinue).Source
    if (-not $exe) { $exe = $Python }
    $ver = ""
    try { $ver = (& $Python -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null | Select-Object -First 1) } catch {}
    if (-not $ver) { $ver = "unknown" }
    $short = if ($ver -match '^(\d+\.\d+)') { $Matches[1] } else { "unknown" }
    $baseline = Get-PyBaseline
    return @{ Exe = $exe; Ver = $ver; Short = $short; Baseline = $baseline; MatchesBaseline = ($short -eq $baseline) }
}

Export-ModuleMember -Function Say, Warn, Die, Write-Utf8NoBom, Get-RelPath, Get-Sha256, Get-Hash16,
    Compare-SemVer, Get-TempStagingPath, Get-IsoTimestamp, Compress-DirectoryToZip,
    Get-SpecPackageList, Get-SpecPackages, Get-SpecDepComponentPackages,
    Test-LibreOfficeCoreBundle, Get-PyBaseline, Get-PythonRuntimeInfo
