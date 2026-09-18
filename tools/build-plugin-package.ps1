# ============================================================================
# JZToolsHub 插件包构建工具（开发侧）
#
# 用途：
#   把 plugins\<id>\ 打成一个「插件独立升级包」（zip），供目标机离线单独升级该插件，
#   不必重出约 122 MB 的整包。包结构 / 字段 / 校验规则见
#   docs\design\插件独立升级方案-设计文档.md §4、§6。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\build-plugin-package.ps1 -Id knowledge-base
#   powershell -ExecutionPolicy Bypass -File tools\build-plugin-package.ps1 -Id base64 -Version 1.0.1
#   ... -FromMin 1.0.0 -FromMax 1.0.1 -MinApp 1.9.0 -Notes <变更说明.md>
#   ... -From D:\某处\plugins\knowledge-base      # 指定取源目录
#   ... -RunTests                                  # 额外跑插件自带的 backend/test_*.py（需要 pytest）
#   ... -SkipChecks                                # 全部校验降级为警告（谨慎；版本防呆也失效）
#   ... -NoZip                                     # 只产出 staging 目录，便于目检
#   ... -Publish \\fileserver\JZToolsHub\插件包      # 出包后直接投递到内网共享盘（含 index.json）
#
# 产物（deploy\ 已 gitignore，与其它部署产物一致）：
#   deploy\插件包\JZToolsHub-插件-<id>-v<版本>.zip
#   deploy\插件包\JZToolsHub-插件-<id>-v<版本>.zip.sha256      ← 介质传递后核对用
#   deploy\插件包\index.json                                   ← 随介质分发的索引（见 §10）
#   tools\plugin-packages.json                                 ← 入库：已发布包登记与 sha256 冻结值
#
# 退出码：0 成功；1 校验失败或出错
# ============================================================================
param(
    [Parameter(Mandatory = $true)][string]$Id,
    [string]$Version = "",            # 缺省取 manifest.json 的 version（仍需大于上一发布版本）
    [string]$From = "",               # 取源目录（缺省：仓库 plugins\<id>）
    [string]$FromMin = "",            # 允许直升的最低目标机版本（写入 upgrade_from_min）
    [string]$FromMax = "",            # 允许直升的最高目标机版本（写入 upgrade_from_max）
    [string]$MinApp = "",             # 要求的主程序最低版本（写入 min_app_version）
    [switch]$RequiresRestart,         # 强制标记"需要重启"（缺省自动判定）
    [switch]$NoRestart,               # 强制标记"不需要重启"
    [string]$Notes = "",              # 人工变更说明文件（缺省用 git 提交记录自动生成）
    [string]$OutDir = "",             # 缺省 <仓库>\deploy\插件包
    [string]$RegistryFile = "",       # 发布登记文件（缺省 <仓库>	ools\plugin-packages.json；沙箱/CI 可另指）
    [string]$Python = "python",       # 打包机解释器（依赖扫描 / 自测用）
    [string]$Publish = "",            # 发布目录（内网共享盘）：投放 zip + .sha256 + index.json（阶段三）
    [switch]$RunTests,
    [switch]$Strict,                  # 自测失败即中止（缺省仅警告）
    [switch]$SkipChecks,
    [switch]$NoZip,
    [switch]$Force                    # 允许同号重打（打上 -ForceRepack 标记并告警）
)
$ErrorActionPreference = "Stop"

$ScriptVer = "1.0.0"
$Root      = Split-Path -Parent $PSScriptRoot          # 仓库根目录
$PythonCmd = $Python
$RulesFile = Join-Path $PSScriptRoot "plugin-payload-rules.json"
if ([string]::IsNullOrWhiteSpace($RegistryFile)) { $RegistryFile = Join-Path $PSScriptRoot "plugin-packages.json" }
if (-not [System.IO.Path]::IsPathRooted($RegistryFile)) { $RegistryFile = Join-Path $Root $RegistryFile }
$InstallerDir = Join-Path $PSScriptRoot "plugin-upgrade"
$SpecFile  = Join-Path $Root "JZToolsHub.spec"
$ToolIds   = @("JZToolsHub-插件")

function Say { param([string]$m = "") Write-Host $m }
function Warn { param([string]$m) Write-Warning $m }
function Die  {
    param([string]$m)
    Write-Host ""
    Write-Host "  [失败] $m" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# ---------------- 基础工具 ----------------
function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        Die "JSON 解析失败：$Path`n         $($_.Exception.Message)"
    }
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

function Test-ExcludedPath {
    param($Rules, [string]$RelPath)
    $parts = $RelPath -split '/'
    # 目录段（除最后一段）
    for ($i = 0; $i -lt $parts.Count - 1; $i++) {
        if ($Rules.exclude_dirs -contains $parts[$i]) { return $true }
    }
    $name = $parts[$parts.Count - 1]
    if ($Rules.exclude_files -contains $name) { return $true }
    foreach ($g in $Rules.exclude_globs) {
        if ($name -like $g) { return $true }
    }
    return $false
}

function Compare-SemVer {
    # 返回 -1 / 0 / 1；非法版本号按 (0,0,0) 处理并告警
    param([string]$A, [string]$B)
    $pa = @(0, 0, 0); $pb = @(0, 0, 0)
    if ($A -match '^(\d+)\.(\d+)\.(\d+)$') { $pa = @([int]$matches[1], [int]$matches[2], [int]$matches[3]) }
    if ($B -match '^(\d+)\.(\d+)\.(\d+)$') { $pb = @([int]$matches[1], [int]$matches[2], [int]$matches[3]) }
    for ($i = 0; $i -lt 3; $i++) {
        if ($pa[$i] -gt $pb[$i]) { return 1 }
        if ($pa[$i] -lt $pb[$i]) { return -1 }
    }
    return 0
}

# ---------------- 读取规则与登记 ----------------
if (-not (Test-Path -LiteralPath $RulesFile)) { Die "缺少规则文件：$RulesFile" }
$Rules = Read-JsonFile $RulesFile
if ($null -eq $Rules) { Die "规则文件不可用：$RulesFile" }

$Registry = Read-JsonFile $RegistryFile
if ($null -eq $Registry) {
    $Registry = [pscustomobject]@{ schema = 1; packages = [pscustomobject]@{} }
}

# ---------------- C-1：id 三处一致 ----------------
Say "================================================"
Say "  JZToolsHub 插件包构建（build-plugin-package.ps1 $ScriptVer）"
Say "================================================"
Say ""
if ($Id -notmatch '^[A-Za-z0-9_-]+$') { Die "插件 id 非法（只允许 [A-Za-z0-9_-]）：$Id" }

if ([string]::IsNullOrWhiteSpace($From)) {
    $srcDir = Join-Path (Join-Path $Root "plugins") $Id
} else {
    $srcDir = $From
}
if (-not (Test-Path -LiteralPath $srcDir)) { Die "取源目录不存在：$srcDir" }
$srcDir = (Get-Item -LiteralPath $srcDir).FullName

$manifestPath = Join-Path $srcDir "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) { Die "取源目录缺少 manifest.json：$srcDir" }
$manifest = Read-JsonFile $manifestPath
if (($manifest.id) -ne $Id) {
    Die "id 不一致（规范 M-3）：参数 -Id=$Id，manifest.json.id=$($manifest.id)。"
}
if ((Split-Path -Leaf $srcDir) -ne $Id) {
    Die "目录名与 id 不一致（规范 S-2/M-3）：目录 $(Split-Path -Leaf $srcDir)，id=$Id。"
}
Say "  插件      ：$Id（$srcDir）"

# 注册条目一致性（存在时）
$toolsCfg = Read-JsonFile (Join-Path $Root "config\tools.json")
if ($toolsCfg) {
    $entry = @($toolsCfg.tools | Where-Object { $_.id -eq $Id })
    if ($entry.Count -eq 1) { Say "  注册条目  ：config\tools.json 命中（name=$($entry[0].name)）" }
    elseif ($entry.Count -eq 0) { Warn "config\tools.json 中没有该插件的注册条目（全新插件的包须带 tools_entry）" }
    else { Die "config\tools.json 中 id=$Id 的注册条目重复（规范 R-1）" }
}

# ---------------- C-2：版本 ----------------
$manVer = $manifest.version
if ([string]::IsNullOrWhiteSpace($manVer)) { Die "manifest.json 缺少 version（规范 V-1）" }
if ($manVer -notmatch '^\d+\.\d+\.\d+$') { Die "manifest.json.version 不是语义化版本：$manVer" }
if ([string]::IsNullOrWhiteSpace($Version)) { $Version = $manVer }
if ($Version -ne $manVer) {
    Die "版本不一致：-Version=$Version，manifest.json.version=$manVer（两处必须相等，见 §4.5）"
}

$prevEntry = $null
$cmp = 1                                  # 无上一版时按"递增"处理
if ($Registry.packages -and ($Registry.packages.PSObject.Properties.Name -contains $Id)) {
    $prevEntry = $Registry.packages.$Id
}
if ($prevEntry) {
    $cmp = Compare-SemVer $Version $prevEntry.version
    if ($cmp -le 0 -and -not $Force) {
        Die ("版本未递增：本次 $Version，上一发布 $($prevEntry.version)。`n" +
             "         请递增 plugins\$Id\manifest.json 的 version（规范 V-1，语义化：修订=修复/次版本=兼容增强/主版本=不兼容），`n" +
             "         或加 -Force 强制同号重打（会打上 force_repack 标记）。")
    }
    if ($cmp -le 0) { Warn "同号/降级重打（-Force）：$Version <= $($prevEntry.version)" }
}
$forceRepack = [bool]($prevEntry -and $cmp -le 0 -and $Force)
Say "  版本      ：$Version（上一发布：$(if ($prevEntry) { $prevEntry.version } else { '（无）' })）"

# ---------------- 暂存目录与 payload 拷贝（排除运行态数据） ----------------
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("jz-pkg-" + $Id + "-" + $ts)
$payloadRoot = Join-Path $staging "payload"
$payloadPlugin = Join-Path (Join-Path $payloadRoot "plugins") $Id
New-Item -ItemType Directory -Force -Path $payloadPlugin | Out-Null

$excluded = New-Object System.Collections.ArrayList
$copied = New-Object System.Collections.ArrayList
Get-ChildItem -LiteralPath $srcDir -Recurse -Force | ForEach-Object {
    $rel = Get-RelPath $srcDir $_.FullName
    if ($_.PSIsContainer) { return }
    if (Test-ExcludedPath $Rules $rel) { [void]$excluded.Add($rel); return }
    $dst = Join-Path $payloadPlugin ($rel -replace '/', '\')
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
    [void]$copied.Add($rel)
}
Write-Host ("  payload   ：{0} 个文件（剔除 {1} 个运行态/构建产物）" -f $copied.Count, $excluded.Count)

# ---------------- C-5：运行态数据零夹带（拷贝阶段已剔除，这里复核） ----------------
$violations = @($excluded | Where-Object { $_ -match '(^|/)(\.env)$' -or $_ -match 'config\.json$' -or $_ -match 'config\.local\.json$' })
if ($violations.Count -gt 0) {
    $msg = "payload 混入本机配置文件（含密钥风险）：`n         " + ($violations -join "`n         ")
    if ($SkipChecks) { Warn $msg } else { Die $msg }
}
if ($excluded.Count -gt 0) {
    Say "  已剔除    ："
    foreach ($e in $excluded) { Say "    - $e" }
}

# ---------------- C-6：结构与清单 ----------------
$entry = "index.html"
if ($manifest.entry) { $entry = [string]$manifest.entry }
$entryPath = Join-Path (Join-Path $payloadPlugin "frontend") ($entry -replace '/', '\')
if (-not (Test-Path -LiteralPath $entryPath)) { Die "manifest.entry 指向的文件不存在：frontend/$entry" }
if (Test-Path -LiteralPath (Join-Path $payloadPlugin "backend")) {
    $reqFiles = @("backend\__init__.py", "backend\routes.py")
    foreach ($rf in $reqFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $payloadPlugin $rf))) { Die "缺少后端必须文件：$rf（规范 §4.1）" }
    }
    $routesText = Get-Content -LiteralPath (Join-Path $payloadPlugin "backend\routes.py") -Raw -Encoding UTF8
    if ($routesText -notmatch 'def\s+register\s*\(\s*app') { Die "backend\routes.py 未导出 register(app)（规范 §8.2）" }
}
foreach ($req in $Rules.required) {
    if (-not (Test-Path -LiteralPath (Join-Path $payloadPlugin $req))) { Warn "缺少建议文件：$req（规范 S-5）" }
}

# ---------------- C-4：依赖白名单（第三方库必须 ∈ 框架 PACKAGES ∪ vendor ∪ 标准库） ----------------
$specText = ""
if (Test-Path -LiteralPath $SpecFile) { $specText = Get-Content -LiteralPath $SpecFile -Raw -Encoding UTF8 }
$fwPkgs = @()
if ($specText -match '(?s)PACKAGES\s*=\s*\[(.*?)\]') {
    $fwPkgs = @([regex]::Matches($matches[1], '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value })
}
if ($fwPkgs.Count -eq 0) { Warn "未能从 JZToolsHub.spec 解析出 PACKAGES 白名单（C-4 将只对照 vendor 与标准库）" }

$depScript = @'
import ast, json, sys
from pathlib import Path

plugin = Path(sys.argv[1])            # 插件目录（非 backend 目录；纯前端插件没有 backend）
fw = set(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
root = Path(sys.argv[3])              # 仓库根：主程序自身的依赖也算"框架已打包"

def top_imports(path):
    """返回该文件里出现的顶层模块名（跳过相对导入）。"""
    mods = set()
    try:
        tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
    except SyntaxError:
        return mods, True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            mods.add(node.module.split('.')[0])
    return mods, False

stdlib = set(getattr(sys, 'stdlib_module_names', ()))
# 主程序自身的第三方依赖（app.py / jztools_data.py 直接 import 的库随 exe 一起打包）
for name in ('app.py', 'jztools_data.py'):
    fp = root / name
    if fp.is_file():
        m, _ = top_imports(fp)
        fw |= {x for x in m if x not in stdlib and not x.startswith('jztools_')}

backend = plugin / 'backend'
if not backend.is_dir():
    print(json.dumps({'no_backend': True, 'own': {}, 'vendor': {}, 'test': {},
                      'syntax': [], 'used_fw': [], 'vendored': []}, ensure_ascii=False))
    sys.exit(0)

local = set()
vendored = []
for p in backend.rglob('*.py'):
    if p.parent == backend:
        local.add(p.stem)
for d in backend.iterdir():
    if d.is_dir() and (d / '__init__.py').exists():
        local.add(d.name)
vend = backend / 'vendor'
if vend.is_dir():
    for d in vend.iterdir():
        if d.is_dir():
            local.add(d.name)
            vendored.append(d.name)
        elif d.suffix == '.py':
            local.add(d.stem)
            vendored.append(d.stem)

imports, syntax = {}, []
for p in backend.rglob('*.py'):
    rel = str(p.relative_to(backend)).replace('\\', '/')
    mods, bad = top_imports(p)
    if bad:
        syntax.append(rel + '（语法错误，无法解析 import）')
        continue
    for m in mods:
        imports.setdefault(m, set()).add(rel)

own, vend_bad, test_bad = {}, {}, {}
used_fw = set()
for m, files in sorted(imports.items()):
    if m in fw:
        used_fw.add(m)
        continue
    if m in stdlib or m in local or m.startswith('jztools_'):
        continue
    # 三档收口：插件自身后端模块 → 失败；vendor/ 与 test_*.py → 警告
    own_files  = [f for f in sorted(files) if not f.startswith('vendor/') and not Path(f).name.startswith('test_')]
    vend_files = [f for f in sorted(files) if f.startswith('vendor/')]
    test_files = [f for f in sorted(files) if Path(f).name.startswith('test_')]
    if own_files:
        own[m] = own_files
    if vend_files:
        vend_bad[m] = vend_files
    if test_files:
        test_bad[m] = test_files
print(json.dumps({'no_backend': False, 'own': own, 'vendor': vend_bad, 'test': test_bad,
                  'syntax': syntax, 'used_fw': sorted(used_fw), 'vendored': sorted(vendored)}, ensure_ascii=False))
'@
# 关键：脚本与白名单都走临时文件，而不是 python -c 内联参数。
#   PS 5.1 把含双引号的参数传给原生命令行时会剥掉引号（$PSNativeCommandArgumentPassing 是 PS 7.3+ 才有），
#   内联脚本里的 f"..." 与 JSON 都会被打坏（实测报 File "<string>", line NN 语法错）。
$depWork = Join-Path ([System.IO.Path]::GetTempPath()) ("jz-depscan-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $depWork | Out-Null
$depFile = Join-Path $depWork "dep_scan.py"
$fwFile = Join-Path $depWork "allowed.json"
$fwJson = "[]"
if ($fwPkgs.Count -gt 0) { $fwJson = $fwPkgs | ConvertTo-Json -Compress }
[System.IO.File]::WriteAllText($depFile, $depScript, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText($fwFile, $fwJson, (New-Object System.Text.UTF8Encoding($false)))
$depJson = & $PythonCmd $depFile $payloadPlugin $fwFile $Root 2>&1 | Select-Object -Last 1
Remove-Item -LiteralPath $depWork -Recurse -Force -ErrorAction SilentlyContinue
if (-not $depJson) { $depJson = "{}" }
$depObj = $null
try { $depObj = $depJson | ConvertFrom-Json } catch { Warn "依赖扫描结果无法解析，跳过 C-4：$depJson" }

$depsVendored = @()
$depsFramework = @()
if ($depObj) {
    if ($depObj.syntax -and @($depObj.syntax).Count -gt 0) {
        $msg = "后端存在语法错误（无法解析 import）：`n         " + (@($depObj.syntax) -join "`n         ")
        if ($SkipChecks) { Warn $msg } else { Die $msg }
    }
    if ($depObj.used_fw) { $depsFramework = @($depObj.used_fw) }
    if ($depObj.vendored) { $depsVendored = @($depObj.vendored) }
    if ($depObj.own.PSObject.Properties.Count -gt 0) {
        $msg = @()
        foreach ($p in $depObj.own.PSObject.Properties) {
            $msg += "  $($p.Name)  ← $($p.Value -join ', ')"
        }
        $text = "插件后端依赖了框架未打包的第三方库（目标机必然 ImportError，规范 U-2）：`n        " + ($msg -join "`n        ") + "`n        修法：① 把该库 vendor 进插件目录（如 backend\vendor\，并在模块内注入 sys.path），或 ② 随主包发布（加进 JZToolsHub.spec 的 PACKAGES）。"
        if ($SkipChecks) { Warn $text } else { Die $text }
    }
    if ($depObj.vendor.PSObject.Properties.Count -gt 0) {
        foreach ($p in $depObj.vendor.PSObject.Properties) {
            Warn "vendor 内部依赖 $($p.Name) 不在框架白名单（$($p.Value -join ', ')）—— 该库若在目标机不可用，请一并 vendor"
        }
    }
    if ($depObj.test -and $depObj.test.PSObject.Properties.Count -gt 0) {
        foreach ($p in $depObj.test.PSObject.Properties) {
            Warn "测试文件引用了框架未打包的库 $($p.Name)（$($p.Value -join ', ')）—— 仅开发环境用（如 pytest），不影响目标机；如介意可从包中移除该测试文件"
        }
    }
}
if ($depObj -and $depObj.no_backend) {
    Say "  依赖      ：纯前端插件（无 backend/），不涉及第三方库"
} else {
    Say ("  依赖      ：框架库 {0} 项{1}；vendor/ 自包含 {2}" -f `
            $depsFramework.Count, $(if ($depsFramework.Count -gt 0) { "（$($depsFramework -join ', ')）" } else { "" }), $depsVendored.Count)
}

# ---------------- 版本与配置模板扫描 ----------------
# 模板命名/目标映射与 jztools_data._TEMPLATE_SYNC 保持一致：
#   plugins/<id>/backend/config.template.json → 数据根 plugins/<id>/config.json
#   （即：去掉 ".template" 标记，落到插件数据目录根；见设计文档 §8.1）
$templates = @()
$tmplSuffix = [string]$Rules.template_suffix          # 缺省 ".template.json"
Get-ChildItem -LiteralPath $payloadPlugin -Recurse -File -Force |
    Where-Object { $_.Name -like "*$tmplSuffix" } | ForEach-Object {
        $rel = Get-RelPath $payloadPlugin $_.FullName
        $base = Split-Path -Leaf $rel
        $dst = $base
        if ($base.Contains(".template.")) { $dst = ($base -replace '\.template\.', '.') }
        else { Warn "模板文件名不含 .template. 标记，目标名按原文件名处理：$rel" }
        $mode = "ensure-keys"
        try {
            $tobj = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($tobj._mode) { $mode = [string]$tobj._mode }
        } catch { }
        $templates += [pscustomobject]@{ src = $rel; dst = $dst; mode = $mode }
    }
if ($templates.Count -gt 0) {
    Say "  配置模板  ：$($templates.Count) 个"
    foreach ($t in $templates) { Say "    - $($t.src) → 数据根 plugins\$Id\$($t.dst)（$($t.mode)）" }
}

# ---------------- C-3：前端缓存戳（?v=N 必须随 JS/CSS 内容变化递增） ----------------
function Get-FrontendAssets {
    param([string]$PayloadPlugin)
    $map = @{}
    $entryRel = "$entry"
    $entryFull = Join-Path (Join-Path $PayloadPlugin "frontend") ($entryRel -replace '/', '\')
    $vMap = @{}
    if (Test-Path -LiteralPath $entryFull) {
        $html = Get-Content -LiteralPath $entryFull -Raw -Encoding UTF8
        foreach ($m in [regex]::Matches($html, '(?i)(?:src|href)\s*=\s*["'']([^"'']+\.(?:js|css))(?:\?v=(\d+))?["'']')) {
            $f = ($m.Groups[1].Value -split '/')[-1]
            $v = $null
            if ($m.Groups[2].Success) { $v = [int]$m.Groups[2].Value }
            $vMap[$f] = $v
        }
    }
    Get-ChildItem -LiteralPath (Join-Path $PayloadPlugin "frontend") -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @(".js", ".css") } | ForEach-Object {
            $rel = Get-RelPath $PayloadPlugin $_.FullName
            $h = Get-Hash16 $_.FullName
            $v = $null
            if ($vMap.ContainsKey($_.Name)) { $v = $vMap[$_.Name] }
            $referenced = $vMap.ContainsKey($_.Name)
            $map[$rel] = [pscustomobject]@{ h = $h; v = $v; referenced = $referenced }
        }
    return $map
}
$assets = Get-FrontendAssets $payloadPlugin
$cacheProblems = @()
$cacheWarns = @()
$entryRelKey = "plugins/$Id/frontend/$entry"
if ($prevEntry -and $prevEntry.assets) {
    foreach ($k in $assets.Keys) {
        $a = $assets[$k]
        if (-not ($prevEntry.assets.PSObject.Properties.Name -contains $k)) { continue }   # 新文件，无需比对
        $b = $prevEntry.assets.$k
        if ($a.h -eq $b.h) { continue }                                                   # 内容没变
        $bv = $null
        if ($b.PSObject.Properties.Name -contains "v") { $bv = $b.v }
        if (($null -ne $a.v) -and ($null -ne $bv)) {
            if ($a.v -le $bv) {
                $cacheProblems += "  $k 内容已变（指纹 $($b.h) → $($a.h)），但入口页 ?v= 未递增：$bv → $($a.v)"
            }
        } elseif ($a.referenced -and ($null -eq $a.v)) {
            $cacheProblems += "  $k 内容已变，但入口页引用未带 ?v=N（规范 F-2）：请在 $entryRelKey 中改为 $((Split-Path -Leaf $k))?v=1"
        } else {
            $cacheWarns += "  $k 内容已变但未在入口页直接引用（可能动态加载）—— 请确认缓存策略"
        }
    }
} elseif (-not $prevEntry -and $assets.Count -gt 0) {
    $cacheWarns += "  首次发布该插件包：?v=N 与上一版无可比对基线（本次记录 baseline，下次生效）"
}
if ($cacheProblems.Count -gt 0) {
    $text = "前端强缓存校验未通过（规范 F-2）：`n      " + ($cacheProblems -join "`n      ") + "`n      理由：/plugin/ 下 .js/.css 下发 1 天强缓存，不递增 ?v=N 用户会长期看到旧文件。"
    if ($SkipChecks) { Warn $text } else { Die $text }
}
foreach ($w in $cacheWarns) { Warn $w }

# ---------------- C-7：体积护栏 ----------------
$payloadFiles = Get-ChildItem -LiteralPath $payloadRoot -Recurse -File -Force
$payloadBytes = ($payloadFiles | Measure-Object -Property Length -Sum).Sum
if ($payloadFiles.Count -gt [int]$Rules.max_files) {
    $msg = "payload 文件数 $($payloadFiles.Count) 超过上限 $($Rules.max_files)（疑似误打包整个仓库）"
    if ($SkipChecks) { Warn $msg } else { Die $msg }
}
if ($payloadBytes -gt [long]$Rules.max_bytes) {
    $msg = "payload 体积 $([math]::Round($payloadBytes / 1MB, 1)) MB 超过上限 $([math]::Round([long]$Rules.max_bytes / 1MB, 1)) MB"
    if ($SkipChecks) { Warn $msg } else { Die $msg }
}

# ---------------- C-9：可选自测 ----------------
if ($RunTests) {
    $tests = @(Get-ChildItem -LiteralPath $srcDir -Recurse -File -Filter "test_*.py" -ErrorAction SilentlyContinue)
    if ($tests.Count -eq 0) {
        Say "  自测      ：未发现 test_*.py，跳过"
    } else {
        Say "  自测      ：pytest $($tests.Count) 个测试文件 ..."
        & $PythonCmd -m pytest $srcDir -q 2>&1 | Select-Object -Last 5 | ForEach-Object { Say "    $_" }
        if ($LASTEXITCODE -ne 0) {
            if ($Strict) { Die "插件自测未通过（-Strict）。" } else { Warn "插件自测未通过（不阻断；加 -Strict 可改为阻断）" }
        }
    }
}

# ---------------- 生成 SHA256SUMS / code_sha256 / requires_restart ----------------
$sumsLines = New-Object System.Collections.ArrayList
$fpLines = New-Object System.Collections.ArrayList
foreach ($f in ($payloadFiles | Sort-Object FullName)) {
    $rel = Get-RelPath $payloadRoot $f.FullName
    $h = Get-Sha256 $f.FullName
    [void]$sumsLines.Add("$h  $rel")
    [void]$fpLines.Add("$rel`0$h")
}
$sumText = ($sumsLines -join "`n") + "`n"
$tmpFp = Join-Path $staging ".code_fingerprint"
Write-Utf8NoBom $tmpFp (($fpLines | Sort-Object) -join "`n")
$codeSha = Get-Sha256 $tmpFp
Remove-Item -LiteralPath $tmpFp -Force

$restart = $true
if ($prevEntry -and $prevEntry.file_hashes) {
    # 有上一版：仅当 backend/** 的内容指纹变化才需要重启（manifest/前端不需重启）
    $restart = $false
    $newRelSet = @{}
    foreach ($f in $payloadFiles) { $newRelSet[(Get-RelPath $payloadRoot $f.FullName)] = $true }
    foreach ($f in $payloadFiles) {
        $rel = Get-RelPath $payloadRoot $f.FullName
        if ($rel -notmatch "^plugins/$Id/backend/") { continue }
        $h = Get-Hash16 $f.FullName
        $prevH = ""
        if ($prevEntry.file_hashes.PSObject.Properties.Name -contains $rel) { $prevH = [string]$prevEntry.file_hashes.$rel }
        if ($h -ne $prevH) { $restart = $true; break }
    }
    # 上一版有、本版已删的后端文件同样算变更
    if (-not $restart) {
        foreach ($p in $prevEntry.file_hashes.PSObject.Properties) {
            if ($p.Name -notmatch "^plugins/$Id/backend/") { continue }
            if (-not $newRelSet.ContainsKey($p.Name)) { $restart = $true; break }
        }
    }
} else {
    $restart = ($payloadFiles | Where-Object { (Get-RelPath $payloadRoot $_.FullName) -match "^plugins/$Id/backend/" } | Measure-Object).Count -gt 0
}
if ($RequiresRestart) { $restart = $true }
if ($NoRestart) { $restart = $false }

# ---------------- 组装包元数据 ----------------
$gitCommit = ""; $gitDirty = $false
try {
    $gitCommit = (& git -C $Root rev-parse --short HEAD 2>$null | Select-Object -First 1)
    $dirty = (& git -C $Root status --porcelain 2>$null | Measure-Object).Count
    $gitDirty = ($dirty -gt 0)
} catch { }

$toolsEntry = $null
if ($toolsCfg) {
    $e = @($toolsCfg.tools | Where-Object { $_.id -eq $Id })
    if ($e.Count -eq 1) { $toolsEntry = $e[0] }
}

$pkgMeta = [ordered]@{
    schema            = 1
    kind              = "plugin-upgrade"
    id                = $Id
    version           = $Version
    requires_restart  = [bool]$restart
    built_at          = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
    built_from        = [ordered]@{ commit = $gitCommit; dirty = [bool]$gitDirty }
    python            = (& $PythonCmd -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null | Select-Object -First 1)
    code_sha256       = $codeSha
    size_bytes        = [long]$payloadBytes
    file_count        = $payloadFiles.Count
    deps              = [ordered]@{
        stdlib_only           = ($depsVendored.Count -eq 0 -and $depsFramework.Count -eq 0)
        vendored              = @($depsVendored)
        framework_packages    = @($depsFramework)
    }
}
if ($FromMin) { $pkgMeta["upgrade_from_min"] = $FromMin }
if ($FromMax) { $pkgMeta["upgrade_from_max"] = $FromMax }
if ($MinApp)  { $pkgMeta["min_app_version"] = $MinApp }
if ($forceRepack) { $pkgMeta["force_repack"] = $true }
if ($templates.Count -gt 0) { $pkgMeta["config_templates"] = $templates }
if ($toolsEntry) { $pkgMeta["tools_entry"] = $toolsEntry }

# 升级说明.md（缺省用 git 提交记录生成；-Notes 可换成人工说明）
# ★ git 的输出是 UTF-8，而 PS 5.1 默认按控制台代码页（GBK）解码子进程 stdout —— 不切编码会得到乱码
#   （实测：升级说明.md 与 index.json 的 changelog 全变"鏇存柊…"）。这里临时把控制台输出编码切到 UTF-8。
$changelog = ""
try {
    $prevOutEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $changelog = (& git -C $Root log -6 --date=short --pretty=format:"- %ad  %s  (%h)" -- "plugins/$Id" 2>$null) -join "`n"
    } finally {
        [Console]::OutputEncoding = $prevOutEnc
    }
} catch { }
if ($Notes) {
    if (-not (Test-Path -LiteralPath $Notes)) { Die "-Notes 指定的文件不存在：$Notes" }
    $changelog = Get-Content -LiteralPath $Notes -Raw -Encoding UTF8
}
if ([string]::IsNullOrWhiteSpace($changelog)) { $changelog = "- （本次未采集到变更记录，请人工补充）" }

$notesText = @"
# 插件升级包：$Id → $Version

- 构建时间：$(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
- 构建来源：$srcDir
- 源码提交：$gitCommit$(if ($gitDirty) { "（工作区有未提交改动）" } else { "" })
- 是否需要重启服务：$(if ($restart) { "是（含后端或清单改动，规范 R-4）" } else { "否（纯前端改动；仍建议 Ctrl+F5 强刷）" })
- payload：$($payloadFiles.Count) 个文件，$([math]::Round($payloadBytes / 1MB, 2)) MB
- 代码指纹：$codeSha

## 变更内容

$changelog

## 安装（目标机）

1. 核对包完整性：``certutil -hashfile <本包.zip> SHA256`` 与同目录 ``.zip.sha256`` 比对
2. 解压本包到任意临时目录
3. 双击「安装插件.bat」（免管理员；脚本会先校验版本/哈希，再备份旧版后才替换）
4. 浏览器 Ctrl+F5 强刷一次

## 回滚

``````
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Rollback $Id
``````

（备份位置：数据根目录 backups\plugins\$Id\；默认保留最近 3 份）

## 依赖说明

$(if ($depsFramework.Count -gt 0) { "- 使用框架已打包的第三方库：$($depsFramework -join ', ')" } else { "- 未使用框架第三方库" })
$(if ($depsVendored.Count -gt 0) { "- 插件自带（vendor/）的第三方库：$($depsVendored -join ', ')" } else { "" })
"@
Write-Utf8NoBom (Join-Path $staging "升级说明.md") $notesText
Write-Utf8NoBom (Join-Path $staging "plugin-package.json") (($pkgMeta | ConvertTo-Json -Depth 10) + "`n")
Write-Utf8NoBom (Join-Path $staging "SHA256SUMS") $sumText

# 安装器与双击入口：从仓库原样拷贝（单一真源，避免各包版本漂移）
foreach ($f in @("install-plugin.ps1", "安装插件.bat")) {
    $p = Join-Path $InstallerDir $f
    if (-not (Test-Path -LiteralPath $p)) { Die "缺少安装器文件：$p" }
    Copy-Item -LiteralPath $p -Destination (Join-Path $staging $f) -Force
}
$instVer = (Select-String -LiteralPath (Join-Path $InstallerDir "install-plugin.ps1") -Pattern '\$ScriptVer\s*=\s*"([^"]+)"' |
            Select-Object -First 1)
if ($instVer) { Say "  安装器    ：install-plugin.ps1 v$($instVer.Matches[0].Groups[1].Value)（随包）" }

# ---------------- 打包 ----------------
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Join-Path (Join-Path $Root "deploy") "插件包" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$zipName = "$($ToolIds[0])-$Id-v$Version.zip"
$zipPath = Join-Path $OutDir $zipName

if ($NoZip) {
    Say ""
    Say "==> [-NoZip] 已产出 staging 目录（未压缩）：$staging"
    Say "    校验结果全部通过；检查无误后可重跑去掉 -NoZip 出包。"
    exit 0
} else {
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($staging, $zipPath, [IO.Compression.CompressionLevel]::Optimal, $false)
    $zipSha = Get-Sha256 $zipPath
    $zipSize = (Get-Item -LiteralPath $zipPath).Length
    Write-Utf8NoBom "$zipPath.sha256" ("$zipSha  $zipName`n")
    Say ""
    Say "==> 已产出插件包：$zipPath"
    Say ("    体积 {0} MB；sha256 {1}" -f [math]::Round($zipSize / 1MB, 2), $zipSha)
}

# ---------------- 登记（入库）+ 发布索引（随介质） ----------------
$fileHashes = [ordered]@{}
foreach ($f in ($payloadFiles | Sort-Object FullName)) {
    $fileHashes[(Get-RelPath $payloadRoot $f.FullName)] = (Get-Hash16 $f.FullName)
}
$assetMap = [ordered]@{}
foreach ($k in ($assets.Keys | Sort-Object)) {
    $assetMap[$k] = [ordered]@{ h = $assets[$k].h; v = $assets[$k].v; referenced = [bool]$assets[$k].referenced }
}

$regEntry = [ordered]@{
    version          = $Version
    file             = $zipName
    sha256           = $zipSha
    size             = [long]$zipSize
    built_at         = $pkgMeta.built_at
    code_sha256      = $codeSha
    file_count       = $payloadFiles.Count
    requires_restart = [bool]$restart
    file_hashes      = $fileHashes
    assets           = $assetMap
}
if ($MinApp) { $regEntry["min_app_version"] = $MinApp }

$regObj = $Registry
if (-not $regObj.packages) { $regObj | Add-Member -NotePropertyName packages -NotePropertyValue ([pscustomobject]@{}) -Force }
$regObj.packages | Add-Member -NotePropertyName $Id -NotePropertyValue ([pscustomobject]$regEntry) -Force
Write-Utf8NoBom $RegistryFile (($regObj | ConvertTo-Json -Depth 12) + "`n")

# 发布索引（deploy\插件包\index.json）：供内网共享盘与后台「检查更新」消费（§10）
$indexPath = Join-Path $OutDir "index.json"
$indexObj = $null
if (Test-Path -LiteralPath $indexPath) {
    try { $indexObj = Get-Content -LiteralPath $indexPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
}
if (-not $indexObj) { $indexObj = [pscustomobject]@{ schema = 1; packages = @() } }
$list = @($indexObj.packages | Where-Object { $_.id -ne $Id -and $null -ne $_.id })
$list += [pscustomobject][ordered]@{
    id               = $Id
    version          = $Version
    file             = $zipName
    sha256           = $zipSha
    size             = [long]$zipSize
    requires_restart = [bool]$restart
    min_app_version  = $MinApp
    published_at     = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz")
    changelog        = ($changelog -split "`n" | Select-Object -First 3) -join "；"
}
[pscustomobject]@{ schema = 1; updated_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"); packages = @($list) } |
    ConvertTo-Json -Depth 8 | ForEach-Object { Write-Utf8NoBom $indexPath ($_ + "`n") }

if ($Publish) {
    if (-not (Test-Path -LiteralPath $Publish)) { New-Item -ItemType Directory -Force -Path $Publish | Out-Null }
    Copy-Item -LiteralPath $zipPath -Destination $Publish -Force
    Copy-Item -LiteralPath "$zipPath.sha256" -Destination $Publish -Force
    Copy-Item -LiteralPath $indexPath -Destination $Publish -Force
    Say ""
    Say "==> 已投递到共享目录：$Publish"
    Say "    内容：$(Split-Path -Leaf $zipPath) + .sha256 + index.json"
    Say "    目标机「插件管理」页填索引路径即可检查更新/批量升级："
    Say "      $(Join-Path $Publish 'index.json')"
}

Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue

Say ""
Say "==> 构建完成。"
Say "    包         ：$zipPath"
Say "    旁挂哈希   ：$zipPath.sha256"
Say "    发布索引   ：$indexPath"
Say "    登记（入库）：$RegistryFile"
Say "    目标机操作 ：解压包 → 双击「安装插件.bat」；回滚用 -Rollback $Id"
Say "    提示       ：包内含 SHA256SUMS，安装器会逐文件校验后才替换。"
exit 0
