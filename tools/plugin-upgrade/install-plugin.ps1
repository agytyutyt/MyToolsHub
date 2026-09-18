# ============================================================================
# JZToolsHub 插件安装 / 升级 / 回滚 / 卸载（目标机侧，随插件包分发）
#
# 用途：
#   把「插件独立升级包」装到已部署的 JZToolsHub 上。全程离线、免管理员、
#   可校验、可回退：先只读校验（版本 / 哈希 / 框架版本），再备份，最后才替换。
#   设计依据：docs\design\插件独立升级方案-设计文档.md §5。
#
# 用法：
#   安装 / 升级：  powershell -ExecutionPolicy Bypass -File install-plugin.ps1
#                 （包即本脚本所在目录；也可 -Package <zip|目录> 指定）
#   体检：        ... -List             列出各插件版本 / 状态登记 / 备份 / 数据占用
#   只演算：      ... -DryRun           打印将发生的增/删/改与重启判定，不落盘
#   回滚：        ... -Rollback <id>    回到最近一次升级前（或 -BackupFile 指定备份）
#   卸载：        ... -Uninstall <id>   停用注册条目 → 可选备份数据 → 删插件目录
#
# 常用开关：
#   -InstallDir <dir>   程序目录（缺省：注册表 InstallLocation → %LOCALAPPDATA%\JZToolsHub）
#   -DataRoot <dir>     数据根目录（缺省：%USERPROFILE%\.jztoolshub.json 指针 → 程序目录备份指针 → 默认值）
#   -ExpectedSha256 <h> 包本体哈希（与旁挂 .sha256 文件核对，防介质损坏）
#   -Force              越过"同版本/降级/跳版/无法判定框架版本"限制（仍强制校验文件哈希）
#   -NoStart            升级完成后不自动启动服务（缺省仅当它本来在运行时才启动）
#   -Hot                不停服务（仅当包声明 requires_restart=false；随后需 Ctrl+F5）
#   -PurgeUnknown       删除"新旧清单都没有"的未知文件（缺省保留并列出）
#   -UpdateEntry        升级时也用包内 tools_entry 更新注册条目的文案（缺省忽略）
#   -PurgeEntry         卸载时删除注册条目（缺省仅置 enabled:false）
#   -BackupData         卸载时把数据目录打包备份（默认只提示，不打包）
#   -PruneBackups <n>   备份保留份数（缺省 3；0 = 不清理）
#
# ★ 三条底线（改动前请先读设计文档 §2.3）：
#   ① 数据零触碰：只写程序目录 plugins\<id>\、数据根 backups\ 与状态文件；
#      从不读写数据根 plugins\<id>\ 下该插件的用户数据。
#   ② 先校验后写入：所有拒绝判定都在第一次写操作之前完成。
#   ③ 可回退：备份失败即中止，绝不在没有备份的情况下替换代码。
# ============================================================================
param(
    [string]$Package = "",
    [switch]$List,
    [switch]$DryRun,
    [string]$Rollback = "",
    [string]$Uninstall = "",
    [switch]$Force,
    [string]$ExpectedSha256 = "",
    [string]$InstallDir = "",
    [string]$DataRoot = "",           # 数据根目录（缺省按 .jztoolshub.json 指针自动解析）
    [switch]$NoStart,
    [switch]$Hot,
    [switch]$PurgeUnknown,
    [switch]$UpdateEntry,
    [switch]$PurgeEntry,
    [switch]$BackupData,
    [string]$BackupFile = "",
    [int]$PruneBackups = 3
)
$ErrorActionPreference = "Stop"

$ScriptVer = "1.0.0"
$AppName   = "JZToolsHub"
$ExeName   = "JZToolsHub.exe"
$RegKey    = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
$TempWork  = $null
$StoppedByUs = $false

function Say { param([string]$m = "") Write-Host $m }
function Warn { param([string]$m) Write-Warning $m }
function Fail {
    param([string]$m, [int]$Code = 1)
    Write-Host ""
    Write-Host "  [失败] $m" -ForegroundColor Red
    # 失败前先把我们自己停掉的服务恢复起来，别把目标机留在"服务停着"的状态
    if ($script:StoppedByUs -and -not $NoStart) {
        try { Start-JZApp -AppDir $script:AppDir } catch { }
        $script:StoppedByUs = $false
    }
    Say ""
    exit $Code
}
function Cleanup {
    if ($script:TempWork -and (Test-Path -LiteralPath $script:TempWork)) {
        Remove-Item -LiteralPath $script:TempWork -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------------- 基础工具 ----------------
function Read-Json {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}
function Write-JsonUtf8NoBom {
    param([string]$Path, $Obj)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $json = $Obj | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
}
function Get-Sha256File {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}
# 16 位内容指纹（MD5 前 16 位；与 build-plugin-package.ps1 / jztools_data._file_hash16 同语义）
function Get-Hash16OfFile {
    param([string]$Path)
    $md5 = [System.Security.Cryptography.MD5]::Create()
    try {
        $fs = [System.IO.File]::OpenRead($Path)
        try { return ([BitConverter]::ToString($md5.ComputeHash($fs)) -replace '-', '').ToLower().Substring(0, 16) }
        finally { $fs.Dispose() }
    } finally { $md5.Dispose() }
}
function Get-RelPath {
    param([string]$Base, [string]$Full)
    $b = [System.IO.Path]::GetFullPath($Base).TrimEnd('\', '/')
    $f = [System.IO.Path]::GetFullPath($Full)
    if ($f.StartsWith($b, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $f.Substring($b.Length).TrimStart('\', '/') -replace '\\', '/'
    }
    return $f -replace '\\', '/'
}
function Compare-SemVer {
    param([string]$A, [string]$B)
    $pa = @(0, 0, 0); $pb = @(0, 0, 0); $oka = $false; $okb = $false
    if ($A -match '^(\d+)\.(\d+)\.(\d+)') { $pa = @([int]$matches[1], [int]$matches[2], [int]$matches[3]); $oka = $true }
    if ($B -match '^(\d+)\.(\d+)\.(\d+)') { $pb = @([int]$matches[1], [int]$matches[2], [int]$matches[3]); $okb = $true }
    if (-not $oka -or -not $okb) { return $null }   # 任一不合法 → 交由调用方决定
    for ($i = 0; $i -lt 3; $i++) {
        if ($pa[$i] -gt $pb[$i]) { return 1 }
        if ($pa[$i] -lt $pb[$i]) { return -1 }
    }
    return 0
}

# ---------------- 目录解析 ----------------
function Resolve-AppDir {
    if (-not [string]::IsNullOrWhiteSpace($script:InstallDir)) {
        return [System.IO.Path]::GetFullPath($script:InstallDir)
    }
    $reg = Get-ItemProperty -Path $RegKey -ErrorAction SilentlyContinue
    if ($reg -and $reg.InstallLocation -and (Test-Path -LiteralPath $reg.InstallLocation)) { return [string]$reg.InstallLocation }
    return (Join-Path $env:LOCALAPPDATA $AppName)
}
function Resolve-DataRoot {
    param([string]$AppDir)
    # 显式指定优先（自定义数据根，或沙箱/演练环境）
    if (-not [string]::IsNullOrWhiteSpace($script:DataRoot)) {
        return [System.IO.Path]::GetFullPath($script:DataRoot)
    }
    # 主指针：%USERPROFILE%\.jztoolshub.json（与 install.ps1 同口径）
    $ptr = Join-Path $env:USERPROFILE ".jztoolshub.json"
    if (Test-Path -LiteralPath $ptr) {
        $o = Read-Json $ptr
        if ($o -and $o.data_root) { return [string]$o.data_root }
    }
    $bkp = Join-Path $AppDir "config\data_root.json"
    if (Test-Path -LiteralPath $bkp) {
        $o = Read-Json $bkp
        if ($o -and $o.data_root) { return [string]$o.data_root }
    }
    return (Join-Path $env:USERPROFILE ".jztoolshub")
}

# ---------------- 服务控制 ----------------
function Test-ServiceRunning { return ($null -ne (Get-Process -Name $AppName -ErrorAction SilentlyContinue)) }
function Stop-JZService {
    $p = Get-Process -Name $AppName -ErrorAction SilentlyContinue
    if ($p) {
        $p | Stop-Process -Force
        Start-Sleep -Milliseconds 800
        $script:StoppedByUs = $true
        Say "  已停止服务进程（PID $($p.Id -join ', ')）"
    }
}
function Start-JZApp {
    param([string]$AppDir)
    Start-Process -FilePath (Join-Path $AppDir $ExeName) -WorkingDirectory $AppDir -WindowStyle Minimized
    $script:StoppedByUs = $false
    Say "  已启动服务（托盘图标常驻右下角）"
}
function Get-SiteBaseUrl {
    $host_ = $env:JZTOOLS_HOST
    if ([string]::IsNullOrWhiteSpace($host_) -or $host_ -eq "0.0.0.0" -or $host_ -eq "::") { $host_ = "127.0.0.1" }
    $port = 5000
    if ($env:JZTOOLS_PORT) { try { $port = [int]$env:JZTOOLS_PORT } catch { } }
    return "http://${host_}:$port"
}
function Wait-HttpOk {
    param([string]$Url, [int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 800
    }
    return $false
}
function Test-HttpOkOnce {
    param([string]$Url)
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

# ---------------- zip：安全校验 / 解压 / 列表 / 打包 ----------------
function Test-ZipPathsSafe {
    param([string]$ZipPath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($e in $zip.Entries) {
            $n = $e.FullName
            if ([string]::IsNullOrEmpty($n)) { continue }
            if ($n -match '^[A-Za-z]:' -or $n.StartsWith('/') -or $n.StartsWith('\') -or $n -match '(^|/)\.\.(/|$)' -or $n.Contains(':')) {
                return $false
            }
        }
        return $true
    } finally { $zip.Dispose() }
}
function Get-ZipEntryList {
    param([string]$ZipPath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $out = New-Object System.Collections.ArrayList
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($e in $zip.Entries) {
            if ([string]::IsNullOrEmpty($e.Name)) { continue }   # 目录项
            [void]$out.Add(($e.FullName -replace '\\', '/'))
        }
    } finally { $zip.Dispose() }
    return $out
}
# 解压（三级策略，移植自 install-libreoffice-core.ps1：整包解压 → 逐条覆盖 → Expand-Archive）
function Expand-PayloadZip {
    param([string]$ZipPath, [string]$DestDir)
    try { Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop } catch { }
    try {
        [IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $DestDir)
        return $true
    } catch { }
    try {
        $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
        try {
            foreach ($e in $zip.Entries) {
                if ([string]::IsNullOrEmpty($e.Name)) { continue }
                $target = Join-Path $DestDir ($e.FullName -replace '/', '\')
                $parent = Split-Path -Parent $target
                if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
            }
        } finally { $zip.Dispose() }
        return $true
    } catch {
        Expand-Archive -LiteralPath $ZipPath -DestinationPath $DestDir -Force
        return $true
    }
}
# 打包目录为 zip，条目统一带 Prefix（如 plugins\knowledge-base\...），便于整目录回滚
function Compress-DirToZip {
    param([string]$SourceDir, [string]$ZipPath, [string]$Prefix)
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    $zip = [IO.Compression.ZipFile]::Open($ZipPath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        Get-ChildItem -LiteralPath $SourceDir -Recurse -File -Force | ForEach-Object {
            $rel = Get-RelPath $SourceDir $_.FullName
            $entry = ($Prefix.TrimEnd('/') + '/' + $rel)
            [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $entry, [IO.Compression.CompressionLevel]::Optimal)
        }
    } finally { $zip.Dispose() }
}

# ---------------- 状态文件（数据根 config\.app_state.json） ----------------
function Get-AppState {
    param([string]$DataRoot)
    $p = Join-Path $DataRoot "config\.app_state.json"
    $o = Read-Json $p
    if (-not $o) { $o = [pscustomobject]@{ last_app = $null } }
    if (-not ($o.PSObject.Properties.Name -contains "plugins") -or -not $o.plugins) {
        $o | Add-Member -NotePropertyName plugins -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    return $o
}
function Save-AppState {
    param([string]$DataRoot, $State)
    $p = Join-Path $DataRoot "config\.app_state.json"
    Write-JsonUtf8NoBom $p $State
}
function Get-PluginState {
    param($State, [string]$Id)
    if ($State.plugins -and ($State.plugins.PSObject.Properties.Name -contains $Id)) { return $State.plugins.$Id }
    return $null
}
function Set-PluginState {
    param($State, [string]$Id, $Value)
    $State.plugins | Add-Member -NotePropertyName $Id -NotePropertyValue $Value -Force
}
function Remove-PluginState {
    param($State, [string]$Id)
    if ($State.plugins -and ($State.plugins.PSObject.Properties.Name -contains $Id)) {
        $State.plugins.PSObject.Properties.Remove($Id)
    }
}

# ---------------- 注册条目（数据根 config\tools.json） ----------------
function Merge-ToolsEntry {
    param([string]$DataRoot, $Entry, [switch]$Update)
    $p = Join-Path $DataRoot "config\tools.json"
    $cfg = Read-Json $p
    if (-not $cfg) { return "no-tools-json" }
    $tools = @($cfg.tools)
    $idx = -1
    for ($i = 0; $i -lt $tools.Count; $i++) { if ($tools[$i].id -eq $Entry.id) { $idx = $i; break } }
    if ($idx -ge 0) {
        if (-not $Update) { return "skip-exists" }
        foreach ($k in @("name", "description", "category")) {
            if ($Entry.PSObject.Properties.Name -contains $k) {
                $tools[$idx] | Add-Member -NotePropertyName $k -NotePropertyValue $Entry.$k -Force
            }
        }
        $cfg | Add-Member -NotePropertyName tools -NotePropertyValue $tools -Force
        Write-JsonUtf8NoBom $p $cfg
        return "updated"
    }
    $cfg | Add-Member -NotePropertyName tools -NotePropertyValue ($tools + $Entry) -Force
    Write-JsonUtf8NoBom $p $cfg
    return "added"
}
function Set-ToolsEntryEnabled {
    param([string]$DataRoot, [string]$Id, [bool]$Enabled)
    $p = Join-Path $DataRoot "config\tools.json"
    $cfg = Read-Json $p
    if (-not $cfg) { return $false }
    $tools = @($cfg.tools)
    for ($i = 0; $i -lt $tools.Count; $i++) {
        if ($tools[$i].id -eq $Id) {
            $tools[$i] | Add-Member -NotePropertyName enabled -NotePropertyValue $Enabled -Force
            $cfg | Add-Member -NotePropertyName tools -NotePropertyValue $tools -Force
            Write-JsonUtf8NoBom $p $cfg
            return $true
        }
    }
    return $false
}
function Remove-ToolsEntry {
    param([string]$DataRoot, [string]$Id)
    $p = Join-Path $DataRoot "config\tools.json"
    $cfg = Read-Json $p
    if (-not $cfg) { return $false }
    $tools = @($cfg.tools | Where-Object { $_.id -ne $Id })
    $cfg | Add-Member -NotePropertyName tools -NotePropertyValue $tools -Force
    Write-JsonUtf8NoBom $p $cfg
    return $true
}

# ---------------- 体检（-List） ----------------
function Show-PluginList {
    param([string]$AppDir, [string]$DataRoot)
    $pluginsDir = Join-Path $AppDir "plugins"
    $state = Get-AppState $DataRoot
    Say ""
    Say "== JZToolsHub 插件体检 =="
    Say "   程序目录：$AppDir"
    Say "   数据根  ：$DataRoot"
    $appVer = (Read-Json (Join-Path $AppDir "version.json"))
    if ($appVer -and $appVer.app) { Say "   主程序版本：$($appVer.app)" }
    Say ""
    Say ("   {0,-22} {1,-10} {2,-10} {3,-8} {4,-9} {5}" -f "插件 id", "代码版本", "登记版本", "需重启", "备份", "数据占用")
    Say ("   " + ("-" * 78))
    $rows = @(Get-ChildItem -LiteralPath $pluginsDir -Directory -ErrorAction SilentlyContinue | Sort-Object Name)
    foreach ($d in $rows) {
        $man = Read-Json (Join-Path $d.FullName "manifest.json")
        $codeVer = "-"
        if ($man -and $man.version) { $codeVer = [string]$man.version }
        $st = Get-PluginState $state $d.Name
        $regVer = "-"
        if ($st -and $st.version) { $regVer = [string]$st.version }
        $restart = ""
        if ($st -and $st.PSObject.Properties.Name -contains "restart_pending" -and $st.restart_pending) { $restart = "是" }
        $bdir = Join-Path (Join-Path $DataRoot "backups\plugins") $d.Name
        $bcount = 0; $bsize = 0
        if (Test-Path -LiteralPath $bdir) {
            $bf = @(Get-ChildItem -LiteralPath $bdir -File -ErrorAction SilentlyContinue)
            $bcount = $bf.Count
            if ($bcount -gt 0) { $bsize = ($bf | Measure-Object -Property Length -Sum).Sum }
        }
        $ddir = Join-Path (Join-Path $DataRoot "plugins") $d.Name
        $dsize = 0
        if (Test-Path -LiteralPath $ddir) {
            $df = @(Get-ChildItem -LiteralPath $ddir -Recurse -File -Force -ErrorAction SilentlyContinue)
            if ($df.Count -gt 0) { $dsize = ($df | Measure-Object -Property Length -Sum).Sum }
        }
        $flag = ""
        if ($regVer -ne "-" -and $codeVer -ne "-" -and $regVer -ne $codeVer) { $flag = " ←登记与代码不一致" }
        Say ("   {0,-22} {1,-10} {2,-10} {3,-8} {4,-9} {5}{6}" -f `
                $d.Name, $codeVer, $regVer, $restart, "$bcount 份 $([math]::Round($bsize / 1MB, 1))MB", "$([math]::Round($dsize / 1MB, 1))MB", $flag)
    }
    $orphan = @()
    if ($state.plugins) {
        foreach ($p in $state.plugins.PSObject.Properties) {
            if (-not (Test-Path -LiteralPath (Join-Path $pluginsDir $p.Name))) { $orphan += $p.Name }
        }
    }
    if ($orphan.Count -gt 0) {
        Say ""
        Warn "以下插件有状态登记但程序目录已无代码：$($orphan -join ', ')"
    }
    Say ""
}

# ============================================================================
#  主流程
# ============================================================================
Say "================================================"
Say "  JZToolsHub 插件安装器（install-plugin.ps1 v$ScriptVer）"
Say "================================================"
Say ""

if (-not $List -and -not $Rollback -and -not $Uninstall) {
    Say "  插件包：$(if ($Package) { $Package } else { $PSScriptRoot })"
}

# ---- 定位程序目录 ----
$AppDir = Resolve-AppDir
if (-not (Test-Path -LiteralPath (Join-Path $AppDir $ExeName))) {
    Fail ("未找到 JZToolsHub 程序目录：$AppDir`n" +
          "         该目录下没有 $ExeName。请先安装工具箱，或用 -InstallDir <dir> 指定程序目录。")
}
$DataRoot = Resolve-DataRoot $AppDir
Say "  程序目录：$AppDir"
Say "  数据根  ：$DataRoot"

# ---- 可写性探测（R-7：装到 Program Files 等受保护位置时尽早报错） ----
$probe = Join-Path (Join-Path $AppDir "plugins") (".jz-write-probe-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
try {
    New-Item -ItemType File -Path $probe -Force | Out-Null
    Remove-Item -LiteralPath $probe -Force
} catch {
    Fail "程序目录不可写：$AppDir`n         请以管理员身份运行，或改用默认安装目录（%LOCALAPPDATA%\JZToolsHub）。"
}

# ---- 体检 ----
if ($List) {
    Show-PluginList -AppDir $AppDir -DataRoot $DataRoot
    exit 0
}

# ============================================================================
#  回滚
# ============================================================================
if ($Rollback) {
    $id = $Rollback
    if ($id -notmatch '^[A-Za-z0-9_-]+$') { Fail "插件 id 非法：$id" }
    $bdir = Join-Path (Join-Path $DataRoot "backups\plugins") $id
    $zipPath = $BackupFile
    if (-not $zipPath) {
        if (-not (Test-Path -LiteralPath $bdir)) { Fail "该插件没有任何备份：$bdir" }
        $latest = Get-ChildItem -LiteralPath $bdir -File -Filter "*.zip" -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -notlike "data-*" } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $latest) { Fail "该插件没有任何代码备份（zip）：$bdir" }
        $zipPath = $latest.FullName
    }
    Say ""
    Say "==> 回滚插件 $id"
    Say "    备份：$zipPath"

    if (-not (Test-ZipPathsSafe $zipPath)) { Fail "备份 zip 含不安全路径（.. / 绝对路径），已拒绝。" }
    $entries = @(Get-ZipEntryList $zipPath)
    $manEntry = $entries | Where-Object { $_ -eq "plugins/$id/manifest.json" }
    if (-not $manEntry) { Fail "备份 zip 结构不符（缺少 plugins/$id/manifest.json）：$zipPath" }
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("jz-rollback-" + $id + "-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    Expand-PayloadZip -ZipPath $zipPath -DestDir $tmp | Out-Null
    $man = Read-Json (Join-Path (Join-Path (Join-Path $tmp "plugins") $id) "manifest.json")
    $restoreVer = "-"
    if ($man -and $man.version) { $restoreVer = [string]$man.version }
    Say "    将恢复版本：$restoreVer"
    if ($DryRun) {
        Say ""
        Say "  [-DryRun] 仅演算：将把 plugins\$id\ 替换为该备份内容，然后（若服务在运行）重启。"
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        exit 0
    }

    $wasRunning = Test-ServiceRunning
    if ($wasRunning) { Stop-JZService }

    # 先把"当前状态"备份一份，保证回滚动作本身也可回退
    $curMan = Read-Json (Join-Path (Join-Path (Join-Path $AppDir "plugins") $id) "manifest.json")
    $curVer = "unknown"
    if ($curMan -and $curMan.version) { $curVer = [string]$curMan.version }
    $curDir = Join-Path (Join-Path $AppDir "plugins") $id
    if (Test-Path -LiteralPath $curDir) {
        if (-not (Test-Path -LiteralPath $bdir)) { New-Item -ItemType Directory -Force -Path $bdir | Out-Null }
        $preZip = Join-Path $bdir ("$id-$curVer-pre-rollback-$(Get-Date -Format 'yyyyMMdd-HHmmss').zip")
        Compress-DirToZip -SourceDir $curDir -ZipPath $preZip -Prefix "plugins/$id"
        Say "    已备份当前版本（回滚动作本身可回退）：$preZip"
    }

    if (Test-Path -LiteralPath $curDir) {
        # 回滚是"整目录替换"：当前目录里不在备份中的文件会被丢弃 —— 先列出来，避免静默丢失
        $zipSet = @{}
        foreach ($e in @(Get-ZipEntryList $zipPath)) { $zipSet[$e] = $true }
        $dropped = @()
        Get-ChildItem -LiteralPath $curDir -Recurse -File -Force | ForEach-Object {
            $rel = Get-RelPath $AppDir $_.FullName
            if (-not $zipSet.ContainsKey($rel)) { $dropped += $rel }
        }
        if ($dropped.Count -gt 0) {
            Warn "回滚将丢弃以下文件（当前目录独有、不在该备份中）：$($dropped.Count) 个"
            foreach ($d in $dropped) { Write-Host "        $d" }
        }
        Remove-Item -LiteralPath $curDir -Recurse -Force
    }
    Expand-PayloadZip -ZipPath $zipPath -DestDir $AppDir | Out-Null
    Say "    已恢复 plugins\$id\ ← $restoreVer"

    $state = Get-AppState $DataRoot
    $old = Get-PluginState $state $id
    $newState = [ordered]@{
        version      = $restoreVer
        installed_at = (Get-Date -Format "o")
        installed_by = "install-plugin.ps1/$ScriptVer (rollback)"
        backup       = $zipPath
        templates    = [pscustomobject]@{}
    }
    if ($old -and $old.code_sha256) { $newState["code_sha256"] = $old.code_sha256 }
    $newState["installed_files"] = @(Get-ZipEntryList $zipPath | Sort-Object)
    Set-PluginState $state $id ([pscustomobject]$newState)
    Save-AppState $DataRoot $state
    Say "    已更新状态登记：plugins.$id = $restoreVer"

    if ($wasRunning -and -not $NoStart) { Start-JZApp -AppDir $AppDir }
    Say ""
    Say "==> 回滚完成。浏览器请 Ctrl+F5 强刷一次。"
    exit 0
}

# ============================================================================
#  卸载
# ============================================================================
if ($Uninstall) {
    $id = $Uninstall
    if ($id -notmatch '^[A-Za-z0-9_-]+$') { Fail "插件 id 非法：$id" }
    $pluginDir = Join-Path (Join-Path $AppDir "plugins") $id
    Say ""
    Say "==> 卸载插件 $id"
    if (-not (Test-Path -LiteralPath $pluginDir)) {
        Warn "程序目录中不存在 plugins\$id\（可能已卸载）；仍会检查注册条目与状态登记。"
    }
    if ($DryRun) {
        Say "  [-DryRun] 仅演算：注册条目 $(if ($PurgeEntry) { '删除' } else { '置 enabled=false' })；数据目录 $(if ($BackupData) { '打包备份' } else { '不动（仅提示）' })；程序目录 $(if (Test-Path -LiteralPath $pluginDir) { '删除' } else { '不存在' })。"
        exit 0
    }
    $wasRunning = Test-ServiceRunning
    if ($wasRunning) { Stop-JZService }

    # ① 注册条目：先停用/删除（规范 §11：先删注册条目，避免悬空引用）
    if ($PurgeEntry) {
        if (Remove-ToolsEntry -DataRoot $DataRoot -Id $id) { Say "  已删除注册条目（tools.json）" }
    } else {
        if (Set-ToolsEntryEnabled -DataRoot $DataRoot -Id $id -Enabled $false) { Say "  已停用注册条目（tools.json → enabled:false；数据与代码保留）" }
        else { Warn "tools.json 中未找到该插件的注册条目（跳过）" }
    }

    # ② 数据：默认不动；-BackupData 时只读打包一份
    $dataDir = Join-Path (Join-Path $DataRoot "plugins") $id
    if (Test-Path -LiteralPath $dataDir) {
        if ($BackupData) {
            $bdir = Join-Path (Join-Path $DataRoot "backups\plugins") $id
            if (-not (Test-Path -LiteralPath $bdir)) { New-Item -ItemType Directory -Force -Path $bdir | Out-Null }
            $dataZip = Join-Path $bdir ("data-$id-$(Get-Date -Format 'yyyyMMdd-HHmmss').zip")
            Compress-DirToZip -SourceDir $dataDir -ZipPath $dataZip -Prefix "plugins/$id"
            Say "  已备份用户数据：$dataZip"
        } else {
            Say "  用户数据保持原位（默认不动）：$dataDir"
            Say "  如需一并备份，请重跑并加 -BackupData；如需彻底清理，请人工删除上述目录。"
        }
    }

    # ③ 程序目录
    if (Test-Path -LiteralPath $pluginDir) {
        Remove-Item -LiteralPath $pluginDir -Recurse -Force
        Say "  已删除插件代码：$pluginDir"
    }

    $state = Get-AppState $DataRoot
    Remove-PluginState $state $id
    Save-AppState $DataRoot $state
    Say "  已清除状态登记：plugins.$id"

    if ($wasRunning -and -not $NoStart) { Start-JZApp -AppDir $AppDir }
    Say ""
    Say "==> 卸载完成。用户数据$(if ($BackupData) { '已备份' } else { '仍保留在数据根目录' })。"
    exit 0
}

# ============================================================================
#  安装 / 升级
# ============================================================================
# ---- 定位包 ----
$pkgRoot = $null
$zipPath = $null
if ([string]::IsNullOrWhiteSpace($Package)) { $Package = $PSScriptRoot }
if (Test-Path -LiteralPath $Package -PathType Container) {
    $pkgRoot = (Get-Item -LiteralPath $Package).FullName
} elseif (Test-Path -LiteralPath $Package) {
    $zipPath = (Get-Item -LiteralPath $Package).FullName
    if (-not (Test-ZipPathsSafe $zipPath)) { Fail "包 zip 含不安全路径（.. / 绝对路径），已拒绝：$zipPath" }
    if ($ExpectedSha256) {
        $h = Get-Sha256File $zipPath
        if ($h -ne $ExpectedSha256.ToLower()) { Fail "包本体哈希不符：`n         期望 $($ExpectedSha256.ToLower())`n         实际 $h`n         介质可能损坏，请重新拷贝。" }
        Say "  包哈希校验通过：$h"
    }
    $TempWork = Join-Path ([System.IO.Path]::GetTempPath()) ("jz-plugin-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 6))
    New-Item -ItemType Directory -Force -Path $TempWork | Out-Null
    Expand-PayloadZip -ZipPath $zipPath -DestDir $TempWork | Out-Null
    $pkgRoot = $TempWork
} else {
    Fail "未找到插件包：$Package"
}

$metaPath = Join-Path $pkgRoot "plugin-package.json"
$meta = Read-Json $metaPath
if (-not $meta) { Fail "包内缺少或无法解析 plugin-package.json（这不是一个插件包）：$metaPath" }
if ([int]$meta.schema -ne 1) { Fail "包清单 schema 不受支持：$($meta.schema)（本安装器支持 1）" }
if ($meta.kind -ne "plugin-upgrade") { Fail "包类型不是 plugin-upgrade：$($meta.kind)" }
$id = [string]$meta.id
if ($id -notmatch '^[A-Za-z0-9_-]+$') { Fail "包清单 id 非法：$id" }
$pluginDir = Join-Path (Join-Path $AppDir "plugins") $id
$payloadPlugin = Join-Path (Join-Path (Join-Path $pkgRoot "payload") "plugins") $id
$manPath = Join-Path $payloadPlugin "manifest.json"
if (-not (Test-Path -LiteralPath $manPath)) { Fail "包结构不符：缺少 payload\plugins\$id\manifest.json" }
$man = Read-Json $manPath
if (($man.id) -ne $id) { Fail "id 不一致：包清单 $id，manifest.json $($man.id)" }
if (($man.version) -ne $meta.version) { Fail "版本不一致：包清单 $($meta.version)，manifest.json $($man.version)" }

Say ""
Say "==> 安装/升级插件：$id → $($meta.version)"
Say "    包清单：schema=$($meta.schema) 需重启=$(if ($meta.requires_restart) { '是' } else { '否' }) $(if ($meta.min_app_version) { "min_app=$($meta.min_app_version)" })"
if ($meta.built_from -and $meta.built_from.commit) {
    Say "    构建溯源：commit=$($meta.built_from.commit)$(if ($meta.built_from.dirty) { '（工作区有未提交改动）' })"
}

# ---- SHA256SUMS 逐文件校验（底线二：全部通过才允许写盘） ----
$sumsPath = Join-Path $pkgRoot "SHA256SUMS"
if (-not (Test-Path -LiteralPath $sumsPath)) { Fail "包内缺少 SHA256SUMS（规范 S-6）" }
$newFiles = New-Object System.Collections.ArrayList
$badHash = New-Object System.Collections.ArrayList
$sumLines = Get-Content -LiteralPath $sumsPath -Encoding UTF8
foreach ($line in $sumLines) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    if ($line -notmatch '^([0-9a-fA-F]{64})\s\s(.+)$') { Fail "SHA256SUMS 行格式不合法：$line" }
    $expect = $matches[1].ToLower()
    $rel = ($matches[2] -replace '\\', '/')
    if (-not $rel.StartsWith("plugins/$id/")) { Fail "SHA256SUMS 含包外路径（应为 plugins/$id/ 开头）：$rel" }
    $src = Join-Path (Join-Path $pkgRoot "payload") ($rel -replace '/', '\')
    if (-not (Test-Path -LiteralPath $src)) { $badHash += "$rel（包内缺失）"; continue }
    $h = Get-Sha256File $src
    if ($h -ne $expect) { $badHash += "$rel（期望 $expect，实际 $h）" }
    [void]$newFiles.Add($rel)
}
if ($badHash.Count -gt 0) {
    Fail ("文件哈希校验未通过（介质损坏或包被篡改），未做任何改动：`n         " + ($badHash -join "`n         "))
}
Say ("    哈希校验：$($newFiles.Count) 个文件全部通过")

# ---- 本机现状与版本规则（唯一的拒绝点，全部发生在写操作之前） ----
$installedVer = $null
$installedMan = Read-Json (Join-Path $pluginDir "manifest.json")
if ($installedMan -and $installedMan.version) { $installedVer = [string]$installedMan.version }
$appVer = $null
$appVerObj = Read-Json (Join-Path $AppDir "version.json")
if ($appVerObj -and $appVerObj.app) { $appVer = [string]$appVerObj.app }
$state = Get-AppState $DataRoot
$stateEntry = Get-PluginState $state $id

Say "    目标机现状：已装版本 $(if ($installedVer) { $installedVer } else { '（未安装 → 全新安装）' })；主程序版本 $(if ($appVer) { $appVer } else { '（未读到 version.json）' })"

if ($stateEntry -and $stateEntry.version -and $installedVer -and ($stateEntry.version -ne $installedVer)) {
    Warn "状态登记（$($stateEntry.version)）与程序目录实际版本（$installedVer）不一致：目录可能被手工改动过，按实际版本执行判定。"
}

# 框架最低版本
if ($meta.min_app_version) {
    if (-not $appVer) {
        if (-not $Force) { Fail "无法读取主程序版本（缺 version.json.app），而本包声明了 min_app_version=$($meta.min_app_version)。确认无碍请加 -Force。" }
        Warn "无法读取主程序版本，已按 -Force 跳过 min_app_version 校验"
    } else {
        $c = Compare-SemVer $appVer $meta.min_app_version
        if ($null -eq $c) { Warn "版本号无法比较（app=$appVer，min=$($meta.min_app_version)），跳过该项校验" }
        elseif ($c -lt 0) { Fail "主程序版本过低：本机 $appVer < 本包要求的 $($meta.min_app_version)。`n         请先用整包安装脚本（一键安装.bat）升级主程序。" }
    }
}

# 全新安装 / 升级 / 拒绝
$isFresh = -not (Test-Path -LiteralPath $pluginDir)
$sameVer = $false
if ($installedVer) {
    $c = Compare-SemVer $meta.version $installedVer
    if ($null -eq $c) { Warn "版本号无法比较（包 $($meta.version)，已装 $installedVer），跳过版本规则" }
    elseif ($c -eq 0) { $sameVer = $true }
    elseif ($c -lt 0) {
        if (-not $Force) { Fail "拒绝降级：包版本 $($meta.version) 低于已装版本 $installedVer。`n         确需降级请加 -Force（数据兼容性由插件负责，规范 V-3/V-7）。" }
        Warn "降级安装（-Force）：$($meta.version) < $installedVer"
    }
}
if ($isFresh) {
    if (-not $meta.tools_entry) {
        if (-not $Force) {
            Fail ("全新安装但包内没有 tools_entry（注册条目），装上去也不会出现在工具列表中。`n" +
                  "         请用带注册条目的包重新构建（构建工具会自动带上 config\tools.json 中的条目），或加 -Force 仅落盘代码、稍后手工登记。")
        }
        Warn "包内无 tools_entry，已按 -Force 继续；请在 config\tools.json 手工登记该插件后方可使用。"
    }
} else {
    if ($sameVer -and -not $Force) { Fail "同版本重装：包与已装版本都是 $installedVer。`n         确需重装请加 -Force（仍会校验哈希并备份）。" }
    if ($meta.upgrade_from_min) {
        $c = Compare-SemVer $installedVer $meta.upgrade_from_min
        if ($null -ne $c -and $c -lt 0 -and -not $Force) {
            Fail "不允许跳版升级：已装 $installedVer < 本包最低起升版本 $($meta.upgrade_from_min)。`n         请先升级到中间版本（见包发布记录），或加 -Force 强行越过。"
        }
    }
    if ($meta.upgrade_from_max) {
        $c = Compare-SemVer $installedVer $meta.upgrade_from_max
        if ($null -ne $c -and $c -gt 0 -and -not $Force) {
            Fail "已装版本 $installedVer 高于本包声明的最高起升版本 $($meta.upgrade_from_max)（属降级路径）。`n         确需如此请加 -Force。"
        }
    }
}
# ---- 重启判定：以目标机事实为准，而不是只信包里的声明 ----
# 包里的 requires_restart 是"构建期相对上一版包"的判定；到了目标机还要看它当前装的是什么：
# 只要 backend/** 与磁盘现状不同（新增/变更/将删除）就必须重启（规范 R-4）；
# manifest.json 每次请求都会重读、前端磁盘直读 → 这两类改动不需要重启。
$newSetForRestart = @{}
foreach ($f in $newFiles) { $newSetForRestart[$f] = $true }
$restartNeeded = [bool]$meta.requires_restart
$restartReason = $(if ($meta.requires_restart) { "包声明（含后端改动）" } else { "" })
if (-not $restartNeeded) {
    if (-not (Test-Path -LiteralPath $pluginDir)) {
        $hasBackend = ($newFiles | Where-Object { $_ -match "^plugins/$id/backend/" } | Measure-Object).Count -gt 0
        if ($hasBackend) { $restartNeeded = $true; $restartReason = "全新安装且含后端（首次注册路由）" }
    } else {
        foreach ($rel in $newFiles) {
            if ($rel -notmatch "^plugins/$id/backend/") { continue }
            $src = Join-Path (Join-Path $pkgRoot "payload") ($rel -replace '/', '\')
            $dst = Join-Path $AppDir ($rel -replace '/', '\')
            if (-not (Test-Path -LiteralPath $dst)) { $restartNeeded = $true; $restartReason = "新增后端文件 $rel"; break }
            if ((Get-Hash16OfFile $src) -ne (Get-Hash16OfFile $dst)) { $restartNeeded = $true; $restartReason = "后端文件有变化 $rel"; break }
        }
        if (-not $restartNeeded) {
            Get-ChildItem -LiteralPath $pluginDir -Recurse -File -Force | ForEach-Object {
                if ($restartNeeded) { return }
                $rel = Get-RelPath $AppDir $_.FullName
                if ($rel -match "^plugins/$id/backend/" -and -not $newSetForRestart.ContainsKey($rel)) {
                    $restartNeeded = $true; $restartReason = "将删除后端文件 $rel"
                }
            }
        }
    }
}
if ($Hot -and $restartNeeded) {
    Fail "目标机需要重启后端才能生效（$restartReason），不允许 -Hot 热替换。`n         请去掉 -Hot（安装器会停服→替换→启动，约 2 秒）。"
}

$dataDir = Join-Path (Join-Path $DataRoot "plugins") $id
$backupDir = Join-Path (Join-Path $DataRoot "backups\plugins") $id

# ---- 演算模式：到此为止，不落盘 ----
if ($DryRun) {
    Say ""
    Say "  [-DryRun] 仅演算，未做任何改动："
    Say "    ① 备份旧版：$(if ($installedVer) { "$backupDir\$id-$installedVer-<时间戳>.zip" } else { "$backupDir\tools.json-<时间戳>.bak（全新安装，仅备份注册表）" })"
    $baseSet = @{}
    $hasBaseline = $false
    if ($stateEntry -and ($stateEntry.PSObject.Properties.Name -contains "installed_files") -and $stateEntry.installed_files) {
        foreach ($f in $stateEntry.installed_files) { $baseSet[[string]$f] = $true }
        $hasBaseline = $true
    }
    $stalePyPrefix = "plugins/$id/backend/"
    $unchanged = 0; $changed = 0; $deleted = @(); $unknown = @()
    if (Test-Path -LiteralPath $pluginDir) {
        Get-ChildItem -LiteralPath $pluginDir -Recurse -File -Force | ForEach-Object {
            $rel = Get-RelPath $AppDir $_.FullName
            if ($newFiles -contains $rel) {
                $src = Join-Path (Join-Path $pkgRoot "payload") ($rel -replace '/', '\')
                if ((Get-Hash16OfFile $src) -eq (Get-Hash16OfFile $_.FullName)) { $unchanged++ } else { $changed++ }
            } elseif ($hasBaseline) {
                if ($baseSet.ContainsKey($rel)) { $deleted += $rel } else { $unknown += $rel }
            } elseif ($rel.StartsWith($stalePyPrefix) -and $rel.EndsWith(".py")) {
                $deleted += $rel
            } else {
                $unknown += $rel
            }
        }
    } else {
        $changed = $newFiles.Count
    }
    Say "    ② 替换代码：新增/修改 $changed 个，内容未变 $unchanged 个，删除 $($deleted.Count) 个"
    foreach ($d in $deleted) { Say "        删 $d" }
    if ($unknown.Count -gt 0) {
        Say "    ③ 未知文件（新旧清单都没有，默认保留）：$($unknown.Count) 个"
        foreach ($u in $unknown) { Say "        留 $u" }
    }
    Say "    ④ 重启判定：$(if ($restartNeeded) { "需要（$restartReason）" } else { '不需要（纯前端改动，Ctrl+F5 即可）' })"
    Say "    ⑤ 数据：数据根 plugins\$id\ 全程只读，不触碰。"
    Say ""
    Say "  去掉 -DryRun 即按上述演算执行。"
    if ($TempWork) { Cleanup }
    exit 0
}

# ---- 停服务（记录 was_running，只恢复自己停掉的服务） ----
$wasRunning = Test-ServiceRunning
if ($wasRunning) {
    if ($Hot) {
        Warn "热替换（-Hot）：不停服务、直接覆盖前端文件。替换完成后请让用户 Ctrl+F5 强刷。"
    } else {
        Stop-JZService
    }
} else {
    Say "  服务当前未运行（升级后也不会自动启动它）"
}

# ---- 备份（底线三） ----
if (-not (Test-Path -LiteralPath $backupDir)) { New-Item -ItemType Directory -Force -Path $backupDir | Out-Null }
$backupPath = $null
if ($installedVer) {
    $backupPath = Join-Path $backupDir ("$id-$installedVer-$(Get-Date -Format 'yyyyMMdd-HHmmss').zip")
    Compress-DirToZip -SourceDir $pluginDir -ZipPath $backupPath -Prefix "plugins/$id"
    if (-not (Test-Path -LiteralPath $backupPath)) { Fail "备份失败（未生成 $backupPath），已中止，未做任何替换。" }
    Say ("  已备份旧版：$backupPath（{0} MB）" -f [math]::Round((Get-Item -LiteralPath $backupPath).Length / 1MB, 2))
} else {
    $toolsJson = Join-Path $DataRoot "config\tools.json"
    if (Test-Path -LiteralPath $toolsJson) {
        $bak = Join-Path $backupDir ("tools.json-$(Get-Date -Format 'yyyyMMdd-HHmmss').bak")
        Copy-Item -LiteralPath $toolsJson -Destination $bak -Force
        Say "  全新安装：已备份注册表 $bak"
    }
}

# ---- 替换代码（三分法：只删"旧版有、新版无"的；未知文件默认保留） ----
# 基线口径（重要）：用**状态登记里"上次装进去的文件清单"**，而不是本次备份快照——
# 备份快照拍的是磁盘现状（含用户/第三方放进来的文件），拿它当基线会把用户文件误判成
# "旧版文件"删掉。状态里没有清单时（插件此前只经整包安装过）改用**保守口径**：
# 只清理 backend\*.py 残留（旧模块被 importlib 扫到会出怪问题），其余文件一律保留。
# 与 Python 侧 plugins/admin/backend/plugin_admin.py 的规则逐条一致（沙箱测试两侧都有断言）。
$baseSet = @{}
$hasBaseline = $false
$baseSource = "无登记：只清理 backend\*.py 残留，其它文件一律保留"
if ($stateEntry -and ($stateEntry.PSObject.Properties.Name -contains "installed_files") -and $stateEntry.installed_files) {
    foreach ($f in $stateEntry.installed_files) { $baseSet[[string]$f] = $true }
    $hasBaseline = $true
    $baseSource = "状态登记的已装文件清单（$($baseSet.Count) 项）"
}
$newSet = @{}
foreach ($f in $newFiles) { $newSet[$f] = $true }
$stalePyPrefix = "plugins/$id/backend/"

$deleted = @()
$unknown = @()
if (Test-Path -LiteralPath $pluginDir) {
    Get-ChildItem -LiteralPath $pluginDir -Recurse -File -Force | ForEach-Object {
        $rel = Get-RelPath $AppDir $_.FullName
        if ($newSet.ContainsKey($rel)) { return }
        if ($hasBaseline) {
            if ($baseSet.ContainsKey($rel)) { $deleted += $_.FullName }
            else { $unknown += $rel }
        } elseif ($rel.StartsWith($stalePyPrefix) -and $rel.EndsWith(".py")) {
            $deleted += $_.FullName
        } else {
            $unknown += $rel
        }
    }
}
foreach ($f in $deleted) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
# 清理因删除而空掉的目录（仅限插件目录内）
if (Test-Path -LiteralPath $pluginDir) {
    Get-ChildItem -LiteralPath $pluginDir -Recurse -Directory -Force |
        Sort-Object { $_.FullName.Length } -Descending | ForEach-Object {
            if (@(Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue).Count -eq 0) {
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
        }
}
if ($unknown.Count -gt 0) {
    Warn "以下文件不在新旧任何版本清单内（疑似人工放入），已保留："
    foreach ($u in $unknown) { Write-Host "        $u" }
    if ($PurgeUnknown) {
        foreach ($u in $unknown) {
            $p = Join-Path $AppDir ($u -replace '/', '\')
            if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue }
        }
        Warn "已按 -PurgeUnknown 删除上述 $($unknown.Count) 个文件。"
    }
}

$written = 0
foreach ($rel in $newFiles) {
    $src = Join-Path (Join-Path $pkgRoot "payload") ($rel -replace '/', '\')
    $dst = Join-Path $AppDir ($rel -replace '/', '\')
    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $written++
}
Say "  已写入代码：$written 个文件（删除 $($deleted.Count) 个旧文件；基线：$baseSource）→ $pluginDir"

# ---- 注册条目（仅全新安装；-UpdateEntry 可更新文案） ----
if ($meta.tools_entry) {
    $r = Merge-ToolsEntry -DataRoot $DataRoot -Entry $meta.tools_entry -Update:$UpdateEntry
    switch ($r) {
        "added"         { Say "  已登记注册条目到 tools.json（enabled=$($meta.tools_entry.enabled)）" }
        "updated"       { Say "  已按 -UpdateEntry 更新注册条目文案（保留启停/排序）" }
        "skip-exists"   { Say "  注册条目已存在：保留现场的名称/描述/启停/排序（-UpdateEntry 可更新文案）" }
        "no-tools-json" { Warn "未找到数据根 config\tools.json，跳过注册条目合并" }
    }
}

# ---- 状态登记 ----
$newStateEntry = [ordered]@{
    version      = $meta.version
    installed_at = (Get-Date -Format "o")
    installed_by = "install-plugin.ps1/$ScriptVer"
    package_hash = $(if ($zipPath) { Get-Sha256File $zipPath } else { $null })
    backup       = $backupPath
    templates    = [pscustomobject]@{}
    data_dir     = "plugins/$id"      # 提示用：用户数据所在（本安装器从不写它）
    installed_files = @($newFiles | Sort-Object)   # 下次升级的三分法基线（见阶段 D）
}
if ($meta.code_sha256) { $newStateEntry["code_sha256"] = $meta.code_sha256 }
Set-PluginState $state $id ([pscustomobject]$newStateEntry)
Save-AppState $DataRoot $state
Say "  已登记状态：plugins.$id = $($meta.version)（config\.app_state.json）"

# ---- 备份轮转 ----
if ($PruneBackups -gt 0) {
    $all = @(Get-ChildItem -LiteralPath $backupDir -File -Filter "*.zip" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
    if ($all.Count -gt $PruneBackups) {
        $drop = $all[$PruneBackups..($all.Count - 1)]
        foreach ($d in $drop) { Remove-Item -LiteralPath $d.FullName -Force -ErrorAction SilentlyContinue }
        Say "  备份轮转：保留最近 $PruneBackups 份，清理 $($drop.Count) 份"
    }
}

# ---- 启动与冒烟 ----
$smokeOk = $null
$statusOk = $null
if ($wasRunning -and -not $NoStart) {
    Start-JZApp -AppDir $AppDir
    $baseUrl = Get-SiteBaseUrl
    Say "  冒烟探测：等待 $baseUrl/api/tools 可访问（最多 60 秒）..."
    $smokeOk = Wait-HttpOk -Url "$baseUrl/api/tools" -TimeoutSec 60
    if ($smokeOk) {
        $tools = $null
        try { $tools = Invoke-RestMethod -Uri "$baseUrl/api/tools" -TimeoutSec 10 } catch { }
        $listed = $null
        if ($tools) {
            $arr = @()
            if ($tools.tools) { $arr = @($tools.tools) }
            $listed = ($arr | Where-Object { $_.id -eq $id } | Measure-Object).Count -gt 0
        }
        if ($listed -eq $false) { Warn "冒烟：/api/tools 可访问，但列表中未见 $id（注册条目 enabled=false 属正常）" }
        $statusOk = Test-HttpOkOnce -Url "$baseUrl/api/$id/status"
        if ($statusOk) { Say "  冒烟：插件自检 /api/$id/status 返回 200（通过）" }
        else { Say "  冒烟：该插件未提供 /status 或暂不可用（不阻断；如异常请查日志）" }
    }
} elseif ($wasRunning -and $NoStart) {
    Warn "已按 -NoStart 跳过启动：服务当前处于停止状态，请手工双击 start.bat 启动。"
}

# ---- 结论 ----
Say ""
Say "================================================"
Say "  安装/升级完成：$id"
Say "    版本      ：$(if ($installedVer) { $installedVer } else { '（全新安装）' }) → $($meta.version)"
Say "    文件      ：写入 $written 个，删除 $($deleted.Count) 个$(if ($unknown.Count -gt 0) { "，保留未知 $($unknown.Count) 个" })"
Say "    备份      ：$(if ($backupPath) { $backupPath } else { '（全新安装，无旧代码备份）' })"
Say "    重启      ：$(if ($restartNeeded) { "需要（$restartReason）" } else { '不需要（纯前端改动；Ctrl+F5 强刷即可）' })"
if ($smokeOk -eq $false) { Say "    [注意] 冒烟探测未通过：服务可能未能在 60 秒内就绪，请查数据根 logs\ 或稍后手工启动。" }
Say ""
Say "    回滚本插件：powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Rollback $id"
Say "    浏览器请 Ctrl+F5 强刷一次（前端强缓存，规范 F-2）。"
Say "================================================"
if ($TempWork) { Cleanup }
if ($smokeOk -eq $false) { exit 2 }
exit 0
