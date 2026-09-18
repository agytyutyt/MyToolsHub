# ============================================================================
# JZToolsHub 离线组件：LibreOffice 核心包 —— 一键安装 / 卸载 / 自检
#
# 用途：
#   把「LibreOffice 裁剪核心包」（libreoffice-core.zip）解到 JZToolsHub 程序目录下的
#   runtime\libreoffice\，供工具箱的 .doc → .docx 与 .xls → .xlsx 高保真预览通道使用。
#   应用侧会自动探测到它，**无需改任何配置、无需重启服务**。
#
# 用法：
#   安装：  powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1
#   卸载：  powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1 -Uninstall
#   只自检：powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1 -VerifyOnly
#   应用目录不在默认位置：  -InstallDir "D:\JZToolsHub"
#   重新解压（覆盖、并先清空旧树）：  -Force
#   自检不通过但仍要装（知情时）：    -SkipSmoke
#   自检通过后删掉 zip：    -DeleteZip     （默认保留，便于以后重装；删掉可回收约 164MB）
#   把组件目录设为隐藏：    -Hide          （进一步降低可见性，非必需）
#
# 退出码：0 成功 / 1 前置条件不满足 / 2 解压失败 / 3 自检未通过
#
# ★ 出问题先看日志：脚本同目录下的「安装日志-LibreOffice核心.txt」记录了环境、每步结果
#   和 soffice 的原始输出。**自检失败时请把这个文件发给维护者**，它包含定位所需的全部信息。
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
    [string]$LogPath    = "",      # 日志输出路径；留空则写到脚本同目录
    [switch]$Uninstall,            # 卸载：删除 runtime\libreoffice\
    [switch]$VerifyOnly,           # 只对已安装的组件跑一次自检（不解压、不改动任何文件）
    [switch]$Force,                # 已存在时强制重新解压（会先清空旧树）
    [switch]$SkipSmoke,            # 跳过自检（明知环境受限时使用；装完请自行验证预览）
    [switch]$DeleteZip,            # 自检通过后删除 libreoffice-core.zip（回收约 164MB）
    [switch]$Hide,                 # 给 runtime\libreoffice 加隐藏属性
    [int]$SmokeTimeoutSec = 240    # 自检（真实转换一次）的超时秒数
)
$ErrorActionPreference = "Stop"

$AppName   = "JZToolsHub"
$ExeName   = "$AppName.exe"
$RegKey    = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
$ScriptDir = $PSScriptRoot

# ---------------------------------------------------------------------------
# 日志：所有输出既进控制台也进日志文件（自检失败时这个文件就是唯一线索）
# ---------------------------------------------------------------------------
$LogLines = New-Object System.Collections.ArrayList
if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $LogPath = Join-Path $ScriptDir "安装日志-LibreOffice核心.txt"
}

function Say {
    param([string]$m = "")
    [void]$LogLines.Add($m)
    Write-Host $m
}

function Save-Log {
    $enc = New-Object System.Text.UTF8Encoding($true)   # 带 BOM，记事本直接可读
    foreach ($p in @($LogPath, (Join-Path ([System.IO.Path]::GetTempPath()) "安装日志-LibreOffice核心.txt"))) {
        try {
            if (-not (Test-Path (Split-Path -Parent $p))) { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null }
            [System.IO.File]::WriteAllText($p, ($LogLines -join "`r`n"), $enc)
            if ($p -ne $LogPath) { Write-Host "（脚本目录不可写，日志已改写到：$p）" }
            return
        } catch { continue }
    }
}

# 统一的失败出口：先把日志落盘，再退出
function Fail {
    param([int]$Code, [string[]]$Lines)
    foreach ($l in $Lines) { Say $l }
    Say ""
    Say "  日志已保存：$LogPath"
    Say "  自检/安装失败时，请把该文件发给维护者。"
    Save-Log
    exit $Code
}

function Write-EnvHeader {
    Say "================================================"
    Say "  JZToolsHub 离线组件：LibreOffice 核心包"
    Say "================================================"
    Say ""
    Say "时间     ：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    $os = try { (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).Caption } catch { "未知" }
    Say "系统     ：$os  $([Environment]::OSVersion.Version)  64位进程=$([Environment]::Is64BitProcess)"
    Say "PowerShell：$($PSVersionTable.PSVersion)"
    Say "当前用户 ：$env:USERNAME"
    Say "TEMP     ：$([System.IO.Path]::GetTempPath())"
    Say "脚本目录 ：$ScriptDir"
    Say ""
}

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
            $n = 0
            foreach ($e in $zip.Entries) {
                if ([string]::IsNullOrEmpty($e.Name)) { continue }   # 目录项，跳过
                $target = Join-Path $DestDir ($e.FullName -replace '/', '\')
                $parent = Split-Path -Parent $target
                if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
                $n++
            }
            Say "  （逐条解压完成：$n 个文件）"
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

# ---------------- 辅助：可写 / 剩余空间 ----------------
function Test-DirWritable {
    param([string]$Dir)
    try {
        if (-not (Test-Path $Dir)) { New-Item -ItemType Directory -Force -Path $Dir | Out-Null }
        $f = Join-Path $Dir ".jz-wtest-$(Get-Random)"
        [System.IO.File]::WriteAllText($f, "x")
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
        return $true
    } catch { return $false }
}

function Get-FreeSpaceMB {
    param([string]$Dir)
    try {
        $p = $Dir
        while ($p -and -not (Test-Path $p)) { $p = Split-Path -Parent $p }
        $root = [System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $p).Path)
        $d = New-Object System.IO.DriveInfo $root
        return [int]($d.AvailableFreeSpace / 1MB)
    } catch { return -1 }
}

function Get-PathSha256Short {
    param([string]$Path)
    try { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.Substring(0, 16).ToLower() }
    catch { return "(无法计算)" }
}

# ---------------- 自检：用全新 profile 真实转换一次 ----------------
# 为什么必须用**全新** profile：应用每次转换都用唯一临时 profile（等于每次首启动）。
# 复用已初始化好的 profile 时，缺 presets 等问题的树看起来是正常的 —— 那是假阳性。
# 另外：不能只看退出码，必须检查输出文件真的存在（见 vendor/xhr 的三条铁律）。
function Get-ProbeWorkDir {
    param([string]$AppDir)
    $tag = "jz-lo-probe-" + [Guid]::NewGuid().ToString("N")
    $cands = @(
        (Join-Path ([System.IO.Path]::GetTempPath()) $tag),
        (Join-Path $AppDir "runtime\.probe-$tag"),
        (Join-Path $env:LOCALAPPDATA "Temp\$tag")
    )
    foreach ($c in $cands) {
        if (-not $c) { continue }
        try {
            New-Item -ItemType Directory -Force -Path $c | Out-Null
            $f = Join-Path $c ".wtest"
            [System.IO.File]::WriteAllText($f, "x")
            Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
            return $c
        } catch { continue }
    }
    return $null
}

# 杀掉本次自检进程树（只动我们启动的那个 PID，不碰目标机自己的 LibreOffice）
function Stop-ProbeTree {
    param([System.Diagnostics.Process]$Proc, [string]$LoRoot)
    if ($Proc -and -not $Proc.HasExited) {
        try {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = "taskkill"; $psi.Arguments = "/PID $($Proc.Id) /T /F"
            $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
            $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
            [void][System.Diagnostics.Process]::Start($psi).WaitForExit(15000)
        } catch { try { $Proc.Kill() } catch { } }
    }
    # soffice.exe 可能已退出而 soffice.bin 仍在跑：只清理本组件目录下启动的那些
    try {
        Get-CimInstance Win32_Process -Filter "Name='soffice.bin'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($LoRoot, 'OrdinalIgnoreCase') } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch { }
}

function Invoke-SmokeTest {
    param([string]$Soffice, [string]$AppDir, [int]$TimeoutSec)
    $root = Split-Path -Parent (Split-Path -Parent $Soffice)   # …\libreoffice
    $work = Get-ProbeWorkDir -AppDir $AppDir
    if (-not $work) {
        return @{ ok = $false; kind = "noworkdir"; rc = "";
                  msg = "无法创建可写的临时目录（%TEMP% 与程序目录都不可写）" }
    }
    # 失败时保留现场（profile 里可能留有 crash 数据），成功则清理
    $keepWork = $false
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
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError  = $true
        try { $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
        try { $psi.StandardErrorEncoding  = [System.Text.Encoding]::UTF8 } catch { }

        Say "  [自检] 命令行：`"$Soffice`" $($psi.Arguments)"
        Say "  [自检] 工作目录：$work"
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $p = [System.Diagnostics.Process]::Start($psi)
        } catch {
            $keepWork = $true
            return @{ ok = $false; kind = "startfail"; rc = ""; work = $work; out = ""; err = "";
                      msg = "无法启动 soffice.exe：$($_.Exception.Message)`n" +
                            "         常见原因：杀毒 / 安全软件拦截、AppLocker 或「受控文件夹访问」策略。" }
        }
        $tOut = $p.StandardOutput.ReadToEndAsync()
        $tErr = $p.StandardError.ReadToEndAsync()
        $exited = $p.WaitForExit($TimeoutSec * 1000)
        if (-not $exited) {
            Stop-ProbeTree -Proc $p -LoRoot $root
            $keepWork = $true
            return @{ ok = $false; kind = "timeout"; rc = ""; work = $work; out = ""; err = "";
                      msg = "转换超时（>${TimeoutSec}s）" }
        }
        $sw.Stop()
        $sOut = try { $tOut.Result } catch { "" }
        $sErr = try { $tErr.Result } catch { "" }
        $rcCode = try { $p.ExitCode } catch { "?" }
        $produced = Join-Path $outDir "probe.xlsx"
        $made = @()
        if (Test-Path $outDir) { $made = @(Get-ChildItem -LiteralPath $outDir -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name) }
        $profMade = (Test-Path (Join-Path $profile "user"))
        Say ("  [自检] 退出码={0}  耗时={1:N1}s  产物={2}  profile已初始化={3}" -f $rcCode, $sw.Elapsed.TotalSeconds, ($(if ($made) { $made -join ',' } else { '（无）' })), $profMade)
        if ($sOut.Trim()) { Say "  [自检] soffice stdout：$($sOut.Trim())" }
        if ($sErr.Trim()) { Say "  [自检] soffice stderr：$($sErr.Trim())" }

        if ((Test-Path $produced) -and ((Get-Item $produced).Length -gt 0)) {
            return @{ ok = $true; kind = "ok"; rc = $rcCode; work = $work; out = $sOut; err = $sErr;
                      msg = "转换成功（退出码 $rcCode，$(('{0:N1}' -f $sw.Elapsed.TotalSeconds)) 秒）" }
        }
        $keepWork = $true
        return @{ ok = $false; kind = "nooutput"; rc = $rcCode; work = $work; out = $sOut; err = $sErr;
                  made = $made; profMade = $profMade;
                  msg = "未产出 xlsx（退出码 $rcCode）" }
    } finally {
        if ((-not $keepWork) -and (-not $env:JZ_KEEP_PROBE)) {
            Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# 把自检失败翻译成「人话 + 下一步怎么办」
function Explain-ProbeFailure {
    param([hashtable]$Probe)
    $tips = New-Object System.Collections.ArrayList

    switch ($Probe.kind) {
        "noworkdir" {
            [void]$tips.Add("  可能原因：%TEMP% 不可写（企业策略把 TEMP 指到了只读位置），程序目录也不可写。")
            [void]$tips.Add("  处理：检查环境变量 TEMP / TMP 是否指向可写目录；或用管理员身份重试。")
        }
        "startfail" {
            [void]$tips.Add("  可能原因：安全软件拦截了 soffice.exe 的启动（360 / 火绒 / Defender ASR / AppLocker）。")
            [void]$tips.Add("  处理：把 <程序目录>\runtime\libreoffice\program\soffice.exe 加入杀软白名单后重试；")
            [void]$tips.Add("        或临时关闭实时防护重跑一次（装完再打开）。")
        }
        "timeout" {
            [void]$tips.Add("  可能原因：首次启动要初始化 profile，杀软实时扫描 2800+ 个文件会很慢；也可能磁盘繁忙。")
            [void]$tips.Add("  处理：加长超时重试，例如 -SmokeTimeoutSec 600；仍不行则加 -SkipSmoke 先装，")
            [void]$tips.Add("        再启动工具箱实测 .doc / .xls 预览（自检只是前置体检，不是运行前提）。")
        }
        "nooutput" {
            if ($Probe.profMade -eq $false) {
                [void]$tips.Add("  profile 目录都没被创建 → soffice 基本没跑起来：多半是杀软拦截或缺运行库。")
            }
            if ($Probe.rc -eq 77) {
                [void]$tips.Add("  退出码 77 = profile 引导失败（典型是核心包里的 presets\ 缺失）。")
                [void]$tips.Add("  处理：本次组件包可能不完整，请重新解压/重新获取组件包后加 -Force 重装。")
            } elseif ([string]$Probe.rc -eq "-1073741515") {
                [void]$tips.Add("  退出码 -1073741515 (0xC0000135) = 缺少运行时 DLL（VC++ 运行库）；")
                [void]$tips.Add("        目标机需安装 Microsoft Visual C++ 2015-2022 可再发行组件（x64）。")
            } elseif ([string]$Probe.rc -eq "-1073741819") {
                [void]$tips.Add("  退出码 -1073741819 (0xC0000005) = 访问冲突，常由安全软件注入或系统组件异常引起。")
            } else {
                [void]$tips.Add("  请把上面 soffice 的 stdout / stderr 原文连同本日志一起发给维护者。")
            }
            if ($Probe.made -and $Probe.made.Count -gt 0) {
                [void]$tips.Add("  注意：输出目录里出现了其它文件（$($Probe.made -join ', ')）→ 转换其实部分成功了。")
            }
        }
    }
    [void]$tips.Add("  另外：确认程序目录所在盘剩余空间充足（解压需约 600 MB），且旧组件树未被残缺覆盖。")
    return $tips
}

# ============================================================================
#  公共前置：定位程序目录
# ============================================================================
$AppDir     = Resolve-AppDir
$RuntimeDir = Join-Path $AppDir "runtime"
$LoDir      = Join-Path $RuntimeDir "libreoffice"
$Marker     = Join-Path $RuntimeDir "libreoffice-core.installed.json"

if ($Uninstall) {
    Write-EnvHeader
    Say "==> 卸载 LibreOffice 核心组件"
    Say "    应用目录：$AppDir"
    if (Test-Path $LoDir) {
        # 先确认没有残留进程占用（soffice 均已 headless 退出，这里只是兜底）
        Get-Process -Name "soffice", "soffice.bin" -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        try {
            Remove-Item -LiteralPath $LoDir -Recurse -Force
            Say "    已删除：$LoDir"
        } catch {
            Fail 1 @("  [失败] 删除失败：$($_.Exception.Message)",
                     "         可能有进程占用，重启后重试，或以管理员身份运行。")
        }
    } else {
        Say "    未发现已安装的组件（$LoDir 不存在）"
    }
    if (Test-Path $Marker) { Remove-Item -LiteralPath $Marker -Force; Say "    已删除安装标记：$Marker" }
    Say "==> 卸载完成。工具箱本体不受影响（仅 .doc/.xls 高保真预览通道降级）。"
    Save-Log
    exit 0
}

# ============================================================================
#  -VerifyOnly：只对已安装组件跑一次自检
# ============================================================================
if ($VerifyOnly) {
    Write-EnvHeader
    Say "==> 仅自检（不改动任何文件）"
    Say "    应用目录：$AppDir"
    $so = Find-SofficeUnder $LoDir
    if (-not $so) {
        Fail 1 @("  [失败] 未发现已安装的组件：$LoDir\program\soffice.exe 不存在。",
                 "         请先运行「安装LibreOffice核心组件.bat」。")
    }
    Say "    soffice ：$so"
    Say ""
    $probe = Invoke-SmokeTest -Soffice $so -AppDir $AppDir -TimeoutSec $SmokeTimeoutSec
    if ($probe.ok) {
        Say ""
        Say "  [通过] $($probe.msg)"
        Save-Log
        exit 0
    }
    $lines = @("", "  [失败] 自检未通过：$($probe.msg)", "")
    $lines += @(Explain-ProbeFailure -Probe $probe)
    $lines += @("", "  临时目录：$($probe.work)", "（本次未删除，便于排查；可自行删除）")
    $env:JZ_KEEP_PROBE = "1"   # 走到这里说明要排查，保留现场
    Fail 3 $lines
}

# ============================================================================
#  安装
# ============================================================================
Write-EnvHeader

if (-not (Test-Path (Join-Path $AppDir $ExeName))) {
    Fail 1 @("  [失败] 未找到 JZToolsHub 程序目录：$AppDir",
             "         该目录下没有 $ExeName。请先安装 JZToolsHub 工具箱，",
             "         或用 -InstallDir 指定程序目录后重试。")
}
Say "  应用目录：$AppDir"

$zipPath = Resolve-CoreZip -AppDir $AppDir
if (-not $zipPath) {
    Fail 1 @("  [失败] 未找到 libreoffice-core.zip。",
             "         请确认本脚本与核心包在同一目录（正常解压「离线组件包」即如此），",
             "         或用 -CoreZip 指定路径。")
}
$zipLen = (Get-Item $zipPath).Length
Say ("  核心包  ：{0}（{1} MB，sha256 {2}…）" -f $zipPath, [math]::Round($zipLen / 1MB, 1), (Get-PathSha256Short $zipPath))

# 核心包可读性预检：截断的下载会在解压到一半才炸，提前报出来
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    $za = [IO.Compression.ZipFile]::OpenRead($zipPath)
    $entryCount = $za.Entries.Count
    $za.Dispose()
    Say "  包内条目：$entryCount"
    if ($entryCount -lt 2000) {
        Fail 1 @("  [失败] 核心包条目数异常（$entryCount，正常约 2800）。",
                 "         组件包可能下载/解压不完整，请重新获取后重试。")
    }
} catch {
    Fail 1 @("  [失败] 核心包无法打开：$($_.Exception.Message)",
             "         文件可能不完整或已损坏，请重新获取组件包。")
}

# 目标目录可写 + 剩余空间预检（企业环境下 Program Files 需要提权）
if (-not (Test-DirWritable $RuntimeDir)) {
    Fail 1 @("  [失败] 程序目录不可写：$RuntimeDir",
             "         原因：工具箱装在需要管理员权限的目录（如 C:\Program Files）下。",
             "         处理：右键「安装LibreOffice核心组件.bat」→「以管理员身份运行」后重试。")
}
$freeMb = Get-FreeSpaceMB -Dir $AppDir
if ($freeMb -ge 0 -and $freeMb -lt 800) {
    Fail 1 @("  [失败] 磁盘剩余空间不足：$freeMb MB（解压需约 600 MB，另需临时空间）。",
             "         请清理空间后重试。")
}
Say ("  剩余空间：{0} MB" -f $(if ($freeMb -ge 0) { $freeMb } else { "未知" }))

$existing = Find-SofficeUnder $LoDir
if ($existing -and (-not $Force)) {
    Say ""
    Say "  检测到已安装：$existing"
    Say "  （默认只做校验；要强制重新解压请加 -Force —— 会先清空旧树再解压）"
    $didExtract = $false
} else {
    Say ""
    Say "==> 解压到 runtime\libreoffice\ ..."
    if ($Force -and (Test-Path $LoDir)) {
        Say "    （-Force：先清空旧树，避免残缺文件残留）"
        try { Remove-Item -LiteralPath $LoDir -Recurse -Force } catch { Say "    [提示] 清空旧树时出现问题（继续）：$($_.Exception.Message)" }
    }
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        # 核心包内根目录就是 libreoffice\，因此解压目标是 runtime\（不是 runtime\libreoffice\）
        # 用 $null = 吞掉返回值，否则 True 会被打到输出里
        $null = Expand-PayloadZip -ZipPath $zipPath -DestDir $RuntimeDir
    } catch {
        if (Test-Path $LoDir) { Remove-Item -LiteralPath $LoDir -Recurse -Force -ErrorAction SilentlyContinue }
        Fail 2 @("  [失败] 解压出错：$($_.Exception.Message)",
                 "         已回滚半成品。若提示拒绝访问，请以管理员身份运行。")
    }
    $sw.Stop()
    Say ("  解压完成，用时 {0:N1} 秒" -f $sw.Elapsed.TotalSeconds)
    $didExtract = $true
}

$soffice = Find-SofficeUnder $LoDir
if (-not $soffice) {
    Fail 1 @("  [失败] 解压后仍找不到 program\soffice.exe，组件包可能不完整。",
             "         请重新获取组件包后加 -Force 重试。")
}
Say "  soffice ：$soffice"

# ---- 自检：全新 profile 真实转换一次 ----
if ($SkipSmoke) {
    Say ""
    Say "==> 自检：已按 -SkipSmoke 跳过"
    Say "    ⚠ 未做前置体检。请启动工具箱打开一个 .doc / .xls 确认预览正常；"
    Say "      发现问题可随时用 -VerifyOnly 单独自检。"
} else {
    Say ""
    Say "==> 自检（全新临时 profile 真实转换一次，首次可能需 30~60 秒）..."
    $probe = Invoke-SmokeTest -Soffice $soffice -AppDir $AppDir -TimeoutSec $SmokeTimeoutSec

    # 自愈：本次没重新解压、却自检不过 → 旧树可疑，强制重解压再验一次
    if ((-not $probe.ok) -and (-not $didExtract)) {
        Say ""
        Say "  [自检] 未通过，且本次未重新解压 → 旧组件树可疑，强制重新解压后再验一次..."
        try { Remove-Item -LiteralPath $LoDir -Recurse -Force } catch { }
        New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
        $null = Expand-PayloadZip -ZipPath $zipPath -DestDir $RuntimeDir
        $soffice = Find-SofficeUnder $LoDir
        if (-not $soffice) {
            Fail 2 @("  [失败] 重新解压后找不到 soffice.exe，组件包可能不完整。")
        }
        Say "  重新解压完成：$soffice"
        $probe = Invoke-SmokeTest -Soffice $soffice -AppDir $AppDir -TimeoutSec $SmokeTimeoutSec
    }

    if (-not $probe.ok) {
        $lines = @("", "  [失败] 自检未通过：$($probe.msg)", "")
        $lines += @(Explain-ProbeFailure -Probe $probe)
        $lines += @("",
                    "  组件文件已解压到：$LoDir（未被删除）。",
                    "  两条路可选：",
                    "    ① 先别管自检，直接启动工具箱打开一个 .doc / .xls 看预览是否正常；",
                    "       若正常，说明自检是环境误报（例如杀软只在首次拦），可继续使用。",
                    "    ② 按上面的提示处理后，运行 -VerifyOnly 单独重验（不重新解压，几十秒出结果）。")
        $env:JZ_KEEP_PROBE = "1"
        Fail 3 $lines
    }
    Say "  [通过] $($probe.msg)"
}

# ---- 安装标记（供卸载/审计；不含任何敏感信息） ----
$markerObj = @{
    app           = $AppName
    component     = "libreoffice-core"
    installed_at  = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    core_zip      = (Split-Path -Leaf $zipPath)
    core_zip_sha  = Get-PathSha256Short $zipPath
    soffice       = $soffice
    smoke_ok      = (-not $SkipSmoke)
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
Say "    重跑自检  ：powershell -ExecutionPolicy Bypass -File install-libreoffice-core.ps1 -VerifyOnly"
Save-Log
exit 0
