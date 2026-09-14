# ============================================================================
# JZToolsHub 离线组件：LibreOffice 核心包 —— 一键安装 / 卸载
#
# 用途：
#   把「LibreOffice 裁剪核心包」（libreoffice-core.zip）解到 JZToolsHub 程序目录下的
#   runtime\libreoffice\，供工具箱的 .doc → .docx 与 .xls → .xlsx 高保真预览通道使用。
#   应用侧会自动探测到它，**无需改任何配置、无需重启服务**。
#
# 用法：
#   安装：  powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1
#   卸载：  powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1 -Uninstall
#   应用目录不在默认位置：  -InstallDir "D:\JZToolsHub"
#   重新解压（覆盖）：      -Force
#   自检通过后删掉 zip：    -DeleteZip     （默认保留，便于以后重装；删掉可回收约 164MB）
#   把组件目录设为隐藏：    -Hide          （进一步降低可见性，非必需）
#
# ★ 对目标机是「隐身」的（这是本组件能放心安装的前提，改动前请先读）：
#   - 只做「解压」一件事：不写注册表、不注册 COM / 文件关联、不创建开始菜单或桌面
#     快捷方式、「程序和功能」里也不会出现条目 → 目标机的默认应用与文件图标完全不变；
#   - 目标机可照常用 Microsoft Office / WPS：我们从不把自己注册为 .doc/.xls 的处理程序，
#     也不写 HKEY_CLASSES_ROOT，两者互不干扰；
#   - 调用时一律 `--headless --norestore --nolockcheck`，并带**唯一临时 profile**
#     （-env:UserInstallation=file:///…）→ 既不弹窗，也绝不触碰目标机已有的 LibreOffice
#     配置（目标机若另装了完整版 LibreOffice，两份各自独立）；
#   - 卸载 = 删掉该目录，不留残余。
# ============================================================================
param(
    [string]$InstallDir = "",      # JZToolsHub 程序目录；留空则自动探测
    [string]$CoreZip    = "",      # 核心包路径；留空则在脚本同目录及上级查找
    [switch]$Uninstall,            # 卸载：删除 runtime\libreoffice\
    [switch]$Force,                # 已存在时强制重新解压（默认只做校验）
    [switch]$DeleteZip,            # 自检通过后删除 libreoffice-core.zip（回收约 164MB）
    [switch]$Hide,                 # 给 runtime\libreoffice 加隐藏属性
    [int]$SmokeTimeoutSec = 180    # 自检（真实转换一次）的超时秒数
)
$ErrorActionPreference = "Stop"

$AppName   = "JZToolsHub"
$ExeName   = "$AppName.exe"
$RegKey    = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
$ScriptDir = $PSScriptRoot

function Say { param([string]$m = "") Write-Host $m }

# ---------------- 定位 JZToolsHub 程序目录 ----------------
# 优先级：-InstallDir > 安装时写入的注册表 InstallLocation > %LOCALAPPDATA%\JZToolsHub
function Resolve-AppDir {
    if (-not [string]::IsNullOrWhiteSpace($InstallDir)) { return $InstallDir }
    $reg = Get-ItemProperty -Path $RegKey -ErrorAction SilentlyContinue
    if ($reg -and $reg.InstallLocation) { return [string]$reg.InstallLocation }
    return (Join-Path $env:LOCALAPPDATA $AppName)
}

# ---------------- 定位核心包 ----------------
function Resolve-CoreZip {
    param([string]$AppDir)
    $cands = New-Object System.Collections.ArrayList
    if (-not [string]::IsNullOrWhiteSpace($CoreZip)) { [void]$cands.Add($CoreZip) }
    [void]$cands.Add((Join-Path $ScriptDir "libreoffice-core.zip"))
    [void]$cands.Add((Join-Path (Split-Path -Parent $ScriptDir) "libreoffice-core.zip"))
    # 从仓库里直接跑时：<仓库>\runtime\libreoffice\libreoffice-core.zip
    [void]$cands.Add((Join-Path $AppDir "runtime\libreoffice\libreoffice-core.zip"))
    foreach ($c in $cands) { if (Test-Path -LiteralPath $c) { return $c } }
    return $null
}

# ---------------- 解压（三级策略） ----------------
# .NET Framework 的 ZipFile.ExtractToDirectory 只有 (源,目标) 与 (源,目标,Encoding)
# 两个重载，**没有** bool 覆盖重载 —— 传 $true 会被绑到 entryNameEncoding 上抛类型转换错。
function Expand-PayloadZip {
    param([string]$ZipPath, [string]$DestDir)
    try { Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop } catch { }

    # ① 整包解压（最快；要求目标文件都不存在，首次安装即如此）
    try {
        [IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $DestDir)
        return $true
    } catch {
        Say "  [提示] 整包解压不可用（$($_.Exception.Message)），改用逐条解压"
    }
    # ② 逐条解压：可覆盖已存在文件
    try {
        $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
        try {
            foreach ($e in $zip.Entries) {
                if ([string]::IsNullOrEmpty($e.Name)) { continue }   # 目录项，跳过
                $target = Join-Path $DestDir ($e.FullName -replace '/', '\')
                $parent = Split-Path -Parent $target
                if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
            }
        } finally { $zip.Dispose() }
        return $true
    } catch {
        Say "  [提示] 逐条解压不可用（$($_.Exception.Message)），回退 Expand-Archive"
    }
    # ③ 兜底：Expand-Archive（慢，兼容性最好）
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $DestDir -Force
    return $true
}

# ---------------- 在树内查找 soffice.exe ----------------
function Find-SofficeUnder {
    param([string]$Root)
    if (-not (Test-Path $Root)) { return $null }
    $direct = Join-Path $Root "program\soffice.exe"      # 核心包解出的标准位置
    if (Test-Path $direct) { return $direct }
    $hit = Get-ChildItem -LiteralPath $Root -Recurse -File -Filter "soffice.exe" -ErrorAction SilentlyContinue |
           Sort-Object FullName | Select-Object -First 1
    if ($hit) { return $hit.FullName }
    return $null
}

# ---------------- 自检：用全新 profile 真实转换一次 ----------------
# 为什么必须用**全新** profile：应用每次转换都用唯一临时 profile（等于每次首启动）。
# 复用已初始化的 profile 时，缺 presets 等问题的树看起来是正常的 —— 那是假阳性。
# 另外：不能只看退出码，必须检查输出文件真的存在（见 vendor/xhr 的三条铁律）。
function Invoke-SmokeTest {
    param([string]$Soffice, [int]$TimeoutSec)
    $work = Join-Path ([System.IO.Path]::GetTempPath()) ("jz-lo-probe-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $profile = Join-Path $work "profile"
    New-Item -ItemType Directory -Force -Path $profile | Out-Null
    try {
        $csv = Join-Path $work "probe.csv"
        "a,b`r`n1,2`r`n" | Out-File -LiteralPath $csv -Encoding ascii
        $outDir = Join-Path $work "out"
        New-Item -ItemType Directory -Force -Path $outDir | Out-Null
        $profileUrl = "file:///" + ($profile -replace '\\', '/')
        # 用 .NET Process 直接起：与 vendor 引擎同款（免 shell、无窗口、不经 Start-Process 策略）
        $soArgs = @("--headless", "--norestore", "--nolockcheck",
                    "-env:UserInstallation=$profileUrl",
                    "--convert-to", "xlsx", "--outdir", $outDir, $csv)
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName        = $Soffice
        $psi.Arguments       = (($soArgs | ForEach-Object { '"' + $_ + '"' }) -join ' ')
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow  = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit($TimeoutSec * 1000)) {
            try { $p.Kill() } catch { }
            return @{ ok = $false; msg = "转换超时（>${TimeoutSec}s）" }
        }
        $produced = Join-Path $outDir "probe.xlsx"
        if ((Test-Path $produced) -and ((Get-Item $produced).Length -gt 0)) {
            return @{ ok = $true; msg = "转换成功（退出码 $($p.ExitCode)）" }
        }
        return @{ ok = $false; msg = "未产出 xlsx（退出码 $($p.ExitCode)）—— 组件可能不完整" }
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ============================================================================
#  卸载
# ============================================================================
$AppDir = Resolve-AppDir
$RuntimeDir = Join-Path $AppDir "runtime"
$LoDir      = Join-Path $RuntimeDir "libreoffice"
$Marker     = Join-Path $RuntimeDir "libreoffice-core.installed.json"

if ($Uninstall) {
    Say "==> 卸载 LibreOffice 核心组件"
    Say "    应用目录：$AppDir"
    if (Test-Path $LoDir) {
        # 先确认没有残留进程占用（soffice 均已 headless 退出，这里只是兜底）
        Get-Process -Name "soffice", "soffice.bin" -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $LoDir -Recurse -Force
        Say "    已删除：$LoDir"
    } else {
        Say "    未发现已安装的组件（$LoDir 不存在）"
    }
    if (Test-Path $Marker) { Remove-Item -LiteralPath $Marker -Force; Say "    已删除安装标记：$Marker" }
    Say "==> 卸载完成。工具箱本体不受影响（仅 .doc/.xls 高保真预览通道降级）。"
    exit 0
}

# ============================================================================
#  安装
# ============================================================================
Say "================================================"
Say "  JZToolsHub 离线组件：LibreOffice 核心包"
Say "================================================"
Say ""

if (-not (Test-Path (Join-Path $AppDir $ExeName))) {
    Say "  [失败] 未找到 JZToolsHub 程序目录：$AppDir"
    Say "         该目录下没有 $ExeName。请先安装 JZToolsHub 工具箱，"
    Say "         或用 -InstallDir 指定程序目录后重试。"
    exit 1
}
Say "  应用目录：$AppDir"

$zipPath = Resolve-CoreZip -AppDir $AppDir
if (-not $zipPath) {
    Say "  [失败] 未找到 libreoffice-core.zip。"
    Say "         请确认本脚本与核心包在同一目录（正常解压「离线组件包」即如此），"
    Say "         或用 -CoreZip 指定路径。"
    exit 1
}
Say ("  核心包  ：{0}（{1} MB）" -f $zipPath, [math]::Round((Get-Item $zipPath).Length / 1MB, 1))

$existing = Find-SofficeUnder $LoDir
if ($existing -and (-not $Force)) {
    Say ""
    Say "  检测到已安装：$existing"
    Say "  （默认只做校验；要强制重新解压请加 -Force）"
} else {
    Say ""
    Say "==> 解压到 runtime\libreoffice\ ..."
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        # 核心包内根目录就是 libreoffice\，因此解压目标是 runtime\（不是 runtime\libreoffice\）
        # 用 $null = 吞掉返回值，否则 True 会被打到输出里
        $null = Expand-PayloadZip -ZipPath $zipPath -DestDir $RuntimeDir
    } catch {
        Say "  [失败] 解压出错：$($_.Exception.Message)"
        # 回滚半成品，避免留下不完整的树让应用误判为可用
        if (Test-Path $LoDir) { Remove-Item -LiteralPath $LoDir -Recurse -Force -ErrorAction SilentlyContinue }
        exit 1
    }
    $sw.Stop()
    Say ("  解压完成，用时 {0:N1} 秒" -f $sw.Elapsed.TotalSeconds)
}

$soffice = Find-SofficeUnder $LoDir
if (-not $soffice) {
    Say "  [失败] 解压后仍找不到 program\soffice.exe，组件包可能不完整。"
    exit 1
}
Say "  soffice ：$soffice"

# ---- 自检：全新 profile 真实转换一次 ----
Say ""
Say "==> 自检（全新临时 profile 真实转换一次，首次可能需 30~60 秒）..."
$probe = Invoke-SmokeTest -Soffice $soffice -TimeoutSec $SmokeTimeoutSec
if (-not $probe.ok) {
    Say "  [失败] 自检未通过：$($probe.msg)"
    Say "         组件已解压但可能不可用；可重跑本脚本（-Force）或检查杀毒软件拦截。"
    exit 2
}
Say "  [通过] $($probe.msg)"

# ---- 安装标记（供卸载/审计；不含任何敏感信息） ----
$markerObj = @{
    app           = $AppName
    component     = "libreoffice-core"
    installed_at  = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    core_zip      = (Split-Path -Leaf $zipPath)
    soffice       = $soffice
    invisible     = $true   # 不写注册表 / 无快捷方式 / 不注册文件关联
}
[System.IO.File]::WriteAllText($Marker, ($markerObj | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
Say "  已写入安装标记：$Marker"

if ($Hide) {
    try {
        (Get-Item -LiteralPath $LoDir -Force).Attributes = (Get-Item -LiteralPath $LoDir -Force).Attributes -bor [IO.FileAttributes]::Hidden
        Say "  已将组件目录设为隐藏"
    } catch { Say "  [提示] 设置隐藏属性失败（不影响使用）：$($_.Exception.Message)" }
}

if ($DeleteZip) {
    $zipMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)   # 先取体积，删掉后取不到
    try {
        Remove-Item -LiteralPath $zipPath -Force
        Say "  已删除核心包：$zipPath（回收约 $zipMb MB）"
    } catch { Say "  [提示] 删除核心包失败：$($_.Exception.Message)" }
}

Say ""
Say "==> 安装完成。"
Say "    工具箱的 .doc / .xls 高保真预览已可用（无需改配置、无需重启服务）。"
Say ""
Say "    对目标机的影响：无。未写注册表、未建快捷方式、未改文件关联与默认应用，"
Say "    Microsoft Office / WPS 照常使用。"
if (-not $DeleteZip) {
    $zipMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Say ""
    Say "    如已完成验证，可删除核心包回收 $zipMb MB："
    Say "        Remove-Item `"$zipPath`""
    Say "    （重跑本脚本加 -DeleteZip 也会在自检通过后自动删除）"
}
Say ""
Say "    卸载本组件：双击「卸载LibreOffice核心组件.bat」"
exit 0
