# ============================================================================
# reset-clean-machine.ps1 —— 把测试机还原成「干净机」，以便重复做干净机验收
#
# 为什么需要：干净机验收（见 docs\guide\干净机器部署验收手册.md）依赖"这台机器上什么都没装"。
# 一旦装过一次（程序目录 / 数据根 / 注册表 / 依赖组件 / 插件），第二轮测的就不是干净环境，
# 缺依赖、缺运行库这类问题会被上一轮的残留掩盖（假绿）。本脚本把本产品在机器上留下的
# 东西清干净，并**核查是否真的干净**，让验收可重复。
#
# 用法（在仓库里执行；目标机不需要 Python）：
#   powershell -ExecutionPolicy Bypass -File tools\e2e\reset-clean-machine.ps1              # 全清（默认）
#   ... -Scope Deps                # 只清依赖组件（保留程序与用户数据）——反复测"装依赖"用
#   ... -KeepData                  # 全清但保留用户数据根
#   ... -KeepRegistry -KeepShortcuts   # 保留注册表项 / 快捷方式（绿色部署或只想清目录时）
#   ... -DryRun                    # 只报告要删什么，不动手
#   ... -VerifyOnly                # 只做干净度核查，不删任何东西
#   ... -Yes                       # 不询问（脚本化/双击场景）
#   ... -InstallDir D:\JZToolsHub -DataRoot D:\jzdata    # 非默认位置
#   ... -RemoveVCRuntime           # ★ 连系统级 VC++ 2015-2022 运行库一起卸掉（见下）
#
# ★ -RemoveVCRuntime 说明：组件自带的 VC 运行时 DLL 在 runtime\pylibs 里，随依赖组件一起删；
#   但"这台机器装过 VC++ 可再发行组件包"属于**系统状态**，会让"缺 VC 运行库"这类验收测不出来
#   （验收手册 §1 第 5 项要求 msvcp140.dll 不在 System32）。该开关会：
#     ① 优先调用官方卸载（msiexec /x <ProductCode> /qn /norestart）；
#     ② 卸不掉（无卸载项/失败）时退化为删除 System32 下 10 个运行时 DLL + 清注册表标记键；
#     ③ 逐项报告结果，需要重启才生效的会说明。
#   仅用于**测试机**：删掉系统运行库会影响这台机器上其它依赖它的软件。需要管理员权限。
#
# 退出码：0 = 已清理且核查通过；1 = 有残留（输出里逐条列出）
# ============================================================================
param(
    [ValidateSet("Full", "Deps")]
    [string]$Scope = "Full",
    [string]$InstallDir = "",
    [string]$DataRoot = "",
    [switch]$KeepData,
    [switch]$KeepRegistry,
    [switch]$KeepShortcuts,
    [switch]$RemoveVCRuntime,   # 连**系统级** VC++ 2015-2022 运行库一起卸（仅测试机；需管理员）
    [switch]$IncludeX86,        # 同时处理 x86 版（SysWOW64 / WOW6432Node）
    [switch]$DryRun,
    [switch]$VerifyOnly,
    [switch]$Yes
)
$ErrorActionPreference = "Stop"

$AppName = "JZToolsHub"
$ExeName = "$AppName.exe"
$RegKey  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
$Pointer = Join-Path $env:USERPROFILE ".jztoolshub.json"
$script:Leftovers = @()

function Say  { param([string]$m = "") Write-Host $m }
function Head { param([string]$m) Write-Host ""; Write-Host ("== " + $m) -ForegroundColor Cyan }
function Warn { param([string]$m) Write-Host ("  [注意] " + $m) -ForegroundColor Yellow }
function Left { param([string]$m) $script:Leftovers += $m; Write-Host ("  [残留] " + $m) -ForegroundColor Red }

function Get-DirSize {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    try {
        return (Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum
    } catch { return 0 }
}
function Fmt-Size {
    param($Bytes)
    if (-not $Bytes) { return "0 B" }
    if ($Bytes -ge 1GB) { return ("{0:N2} GB" -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ("{0:N1} MB" -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ("{0:N0} KB" -f ($Bytes / 1KB)) }
    return ("{0} B" -f $Bytes)
}

function Resolve-Target {
    # 程序目录：显式参数 > 注册表 InstallLocation > %LOCALAPPDATA%\JZToolsHub（与安装器同口径）
    if ($InstallDir) { return [System.IO.Path]::GetFullPath($InstallDir) }
    try {
        $v = (Get-ItemProperty -Path $RegKey -ErrorAction Stop).InstallLocation
        if ($v -and (Test-Path -LiteralPath (Join-Path $v $ExeName))) { return $v }
    } catch {}
    return (Join-Path $env:LOCALAPPDATA $AppName)
}
function Resolve-DataRoot {
    param([string]$Target)
    if ($DataRoot) { return [System.IO.Path]::GetFullPath($DataRoot) }
    if (Test-Path -LiteralPath $Pointer) {
        try {
            $o = Get-Content -LiteralPath $Pointer -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($o.data_root) { return $o.data_root }
        } catch {}
    }
    $bkp = Join-Path $Target "config\data_root.json"
    if (Test-Path -LiteralPath $bkp) {
        try {
            $o = Get-Content -LiteralPath $bkp -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($o.data_root) { return $o.data_root }
        } catch {}
    }
    return (Join-Path $env:USERPROFILE (".{0}" -f $AppName.ToLower()))
}
# VC++ 2015-2022 x64 运行时 DLL（与依赖组件随包的那 10 个同名单）
$VcRuntimeDlls = @(
    "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll",
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
    "concrt140.dll", "vccorlib140.dll"
)
$VcMarkerX64 = "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
$VcMarkerX86 = "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-VCRuntimeInfo {
    # 探测系统级 VC++ 运行库：标记键版本 + System32/SysWOW64 里还剩几个 DLL + 卸载登记项
    $info = [ordered]@{
        MarkerX64 = (Test-Path $VcMarkerX64)
        MarkerX86 = (Test-Path $VcMarkerX86)
        Version   = ""
        DllsX64   = @()
        DllsX86   = @()
        Entries   = @()
    }
    if ($info.MarkerX64) {
        try { $info.Version = [string](Get-ItemProperty -Path $VcMarkerX64 -ErrorAction Stop).Version } catch {}
    }
    $sys32 = Join-Path $env:WINDIR "System32"
    $syswow = Join-Path $env:WINDIR "SysWOW64"
    $info.DllsX64 = @($VcRuntimeDlls | Where-Object { Test-Path -LiteralPath (Join-Path $sys32 $_) })
    $info.DllsX86 = @($VcRuntimeDlls | Where-Object { Test-Path -LiteralPath (Join-Path $syswow $_) })
    $keys = @()
    $keys += Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue
    $keys += Get-ChildItem "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue
    foreach ($k in $keys) {
        $dn = ""
        try { $dn = [string]$k.GetValue("DisplayName") } catch {}
        if ($dn -match "Visual C\+\+.*Redistributable") {
            $code = $k.PSChildName
            # ★ 只按显示名判断架构：VC++ 的 x64 卸载项**也在** WOW6432Node 下（安装器是 32 位），
            #   看注册表路径会把 x64 误判成 x86（实测踩到）。名称里没有 (xNN) 时按 x64 处理。
            $arch = if ($dn -match "\(x86\)") { "x86" } else { "x64" }
            $quiet = ""
            try { $quiet = [string]$k.GetValue("QuietUninstallString") } catch {}
            $info.Entries += [pscustomobject]@{ Name = $dn; Code = $code; Arch = $arch; Quiet = $quiet }
        }
    }
    return $info
}

function Remove-VCRuntime {
    param([switch]$IncludeX86, [switch]$DryRun)
    Head "系统级 VC++ 运行库"
    $info = Get-VCRuntimeInfo
    Say ("  标记键 x64：{0}{1}" -f $info.MarkerX64, $(if ($info.Version) { "（$($info.Version)）" } else { "" }))
    Say ("  System32 里运行时 DLL：{0}/10" -f $info.DllsX64.Count)
    if ($info.DllsX86.Count -gt 0) { Say ("  SysWOW64 里运行时 DLL：{0}/10（x86 版）" -f $info.DllsX86.Count) }
    foreach ($e in $info.Entries) { Say ("  卸载登记项：{0}（{1}，{2}）" -f $e.Name, $e.Arch, $e.Code) }
    if ($info.DllsX64.Count -eq 0 -and -not $info.MarkerX64 -and $info.Entries.Count -eq 0) {
        Say "  [OK] 未发现系统级 VC++ 运行库——已经是干净的（可验「缺 VC 运行库」类问题）"
        return $true
    }
    if ($DryRun) {
        Say "  [-DryRun] 将执行：优先 msiexec 官方卸载；无卸载项时删除上面列出的 DLL 并清标记键。"
        return $true
    }
    if (-not (Test-Admin)) {
        Warn "需要管理员权限才能卸载系统运行库。请用管理员身份重开 PowerShell 后重跑："
        Warn ("  powershell -ExecutionPolicy Bypass -File `"{0}`" -RemoveVCRuntime" -f $PSCommandPath)
        return $false
    }
    $ok = $true
    # ① 官方卸载（x64 必做；x86 仅在 -IncludeX86 时）
    foreach ($e in $info.Entries) {
        if ($e.Arch -eq "x86" -and -not $IncludeX86) { Say ("  跳过 x86 版（不影响 x64 验收；要一并卸掉加 -IncludeX86）：{0}" -f $e.Name); continue }
        $args = @("/x", $e.Code, "/qn", "/norestart")
        Say ("  正在卸载：{0} …" -f $e.Name)
        try {
            $p = Start-Process -FilePath "msiexec.exe" -ArgumentList $args -Wait -PassThru -WindowStyle Hidden
            if ($p.ExitCode -eq 0 -or $p.ExitCode -eq 3010) {
                Say ("    卸载完成（退出码 {0}{1}）" -f $p.ExitCode, $(if ($p.ExitCode -eq 3010) { "，需重启生效" } else { "" }))
                if ($p.ExitCode -eq 3010) { $ok = $false }
            } else {
                Warn ("    卸载返回退出码 {0}，稍后用兜底方式清理" -f $p.ExitCode)
            }
        } catch {
            Warn ("    调用 msiexec 失败：{0}" -f $_.Exception.Message)
        }
    }
    # ② 兜底：残留 DLL + 标记键（官方卸载后一般已清掉；被占用/无卸载项时才会走到这里）
    Start-Sleep -Milliseconds 500
    $info = Get-VCRuntimeInfo
    $targets = @()
    $targets += @($info.DllsX64 | ForEach-Object { Join-Path (Join-Path $env:WINDIR "System32") $_ })
    if ($IncludeX86) { $targets += @($info.DllsX86 | ForEach-Object { Join-Path (Join-Path $env:WINDIR "SysWOW64") $_ }) }
    $left = 0
    foreach ($f in $targets) {
        try { Remove-Item -LiteralPath $f -Force -ErrorAction Stop; Say ("    已删除：{0}" -f (Split-Path -Leaf $f)) }
        catch { $left++; Warn ("    删除失败（可能被占用，重启后再试）：{0} —— {1}" -f (Split-Path -Leaf $f), $_.Exception.Message) }
    }
    foreach ($mk in @($VcMarkerX64, $(if ($IncludeX86) { $VcMarkerX86 } else { $null }))) {
        if ($mk -and (Test-Path $mk)) {
            try { Remove-Item -Path $mk -Recurse -Force -ErrorAction Stop; Say ("    已清标记键：{0}" -f $mk) } catch { Warn ("    清标记键失败：{0}" -f $mk) }
        }
    }
    $after = Get-VCRuntimeInfo
    Say ""
    if ($after.DllsX64.Count -eq 0 -and -not $after.MarkerX64) {
        Say "  [OK] 系统级 VC++ 运行库已清除——现在可以验「缺 VC 运行库」类问题了" -ForegroundColor Green
        return $true
    }
    Warn ("仍有残留：System32 {0}/10 个 DLL，标记键 {1}（{2}）" -f $after.DllsX64.Count, $after.MarkerX64, $(if ($after.MarkerX64) { "未清" } else { "已清" }))
    if ($left -gt 0) { Warn "有 DLL 被占用：**重启这台机器**后重跑本脚本即可删掉。" }
    return $false
}

function Stop-Service {
    $procs = @(Get-Process -Name $AppName -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { Say "  （服务未在运行）"; return 0 }
    foreach ($p in $procs) { try { $p.Kill(); $p.WaitForExit(5000) | Out-Null } catch {} }
    Start-Sleep -Milliseconds 600
    $still = @(Get-Process -Name $AppName -ErrorAction SilentlyContinue)
    if ($still.Count -gt 0) { Warn "服务进程仍在运行（可能被其它账户启动）：$($still.Count) 个" }
    else { Say ("  已停止服务进程：{0} 个" -f $procs.Count) }
    return $still.Count
}

# ============================================================================
#  1. 定位目标
# ============================================================================
Head "1. 定位（Scope=$Scope）"
$target = Resolve-Target
$data   = Resolve-DataRoot -Target $target
$pylibs = Join-Path $target "runtime\pylibs"
Say "  程序目录：$target"
Say "  数据根  ：$data"
Say "  依赖组件：$pylibs"
if (-not (Test-Path -LiteralPath $target)) { Warn "程序目录不存在（可能已卸载或路径不对）" }

# ============================================================================
#  2. 干净度核查（-VerifyOnly 时只做这一步）
# ============================================================================
function Invoke-CleanCheck {
    param([string]$Mode = "Full")
    Head ("干净度核查（对齐验收手册 §1；Scope={0}）" -f $Mode)
    # 2.1 系统级：这两项决定"能不能验出缺运行库/缺 Python 类问题"
    $vc = Get-VCRuntimeInfo
    $msvcp = $vc.DllsX64 -contains "msvcp140.dll"
    $hasPy = [bool](Get-Command python -ErrorAction SilentlyContinue)
    Say ("  {0} msvcp140.dll 在系统里：{1}" -f $(if ($msvcp) { "[i]" } else { "[OK]" }), $msvcp)
    Say ("  {0} 系统级 VC++ 运行库：{1}" -f $(if ($vc.DllsX64.Count -gt 0 -or $vc.MarkerX64) { "[i]" } else { "[OK]" }),
         $(if ($vc.DllsX64.Count -gt 0 -or $vc.MarkerX64) {
             "已装（System32 有 {0}/10 个 DLL{1}）；要清掉请用 -RemoveVCRuntime（需管理员，仅测试机）" -f $vc.DllsX64.Count, $(if ($vc.Version) { "，$($vc.Version)" } else { "" })
         } else { "未安装——可以验「缺 VC 运行库」类问题" }))
    if ($msvcp) { Warn "系统已有 VC++ 运行库 → 本机测不出「缺 VC 运行时」这类问题（换机器，或加 -RemoveVCRuntime 清掉后再测）" }
    Say ("  {0} 目标机是否装了 Python：{1}" -f $(if ($hasPy) { "[i]" } else { "[OK]" }), $hasPy)

    # 2.2 产品痕迹：目录 / 注册表 / 快捷方式 / 进程
    # ★ 按模式核查：-Scope Deps 只负责依赖组件，程序目录/数据根等是**有意保留**的，
    #   不能算残留（否则退出码会误报 1，脚本没法用于自动化）。
    $depsOnly = ($Mode -eq "Deps")
    if (Test-Path -LiteralPath $pylibs)  { Left "依赖组件仍在：$pylibs" }
    else { Say "  [OK] 依赖组件已清空（runtime\pylibs 不存在）" }
    if ($depsOnly) {
        foreach ($x in @($target, $data, $Pointer)) {
            if (Test-Path -LiteralPath $x) { Say ("  [i] 本模式保留：{0}" -f $x) }
        }
        if (Test-Path $RegKey) { Say "  [i] 本模式保留：注册表卸载项" }
    } else {
        if (Test-Path -LiteralPath $target)  { Left "程序目录仍在：$target" }
        if (Test-Path -LiteralPath $data)    { Left "数据根仍在：$data" }
        if (Test-Path -LiteralPath $Pointer) { Left "数据根指针仍在：$Pointer" }
        if (Test-Path $RegKey)               { Left "注册表卸载项仍在：$RegKey" }
        $links = @((Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"))
        $desktop = [Environment]::GetFolderPath("Desktop")
        if (-not [string]::IsNullOrWhiteSpace($desktop)) { $links += (Join-Path $desktop "$AppName.lnk") }
        foreach ($l in $links) { if (Test-Path -LiteralPath $l) { Left "快捷方式仍在：$l" } }
    }
    if (@(Get-Process -Name $AppName -ErrorAction SilentlyContinue).Count -gt 0) { Left "服务进程仍在运行" }

    # 2.3 常见的"测试残渣"（不影响判定，但会干扰下一轮；只提示不阻断）
    $junk = @()
    $junk += @(Get-ChildItem -LiteralPath $env:TEMP -Filter "jz-checkdeps-*.json" -ErrorAction SilentlyContinue)
    if ($junk.Count -gt 0) { Warn ("临时文件残留 {0} 个（{1}）——不影响验收，可手工删" -f $junk.Count, (Split-Path -Leaf $junk[0].FullName)) }

    Say ""
    if ($script:Leftovers.Count -eq 0) {
        if ($depsOnly) {
            Write-Host "  结论：依赖组件已清空（程序与数据按 -Scope Deps 保留），可重新测「装依赖组件」这一段。" -ForegroundColor Green
        } else {
            Write-Host "  结论：本机已回到「干净机」状态，可直接开始下一轮验收。" -ForegroundColor Green
        }
        return $true
    }
    Write-Host ("  结论：仍有 {0} 处残留（见上），验收前请处理。" -f $script:Leftovers.Count) -ForegroundColor Red
    return $false
}

if ($VerifyOnly) {
    $clean = Invoke-CleanCheck -Mode $Scope
    exit $(if ($clean) { 0 } else { 1 })
}

# ============================================================================
#  3. 确认
# ============================================================================
Head "2. 将删除的内容"
$items = @()
if ($Scope -eq "Deps") {
    $items += @{ Path = $pylibs; Note = "依赖组件（runtime\pylibs：cv2/numpy/openpyxl… 全部）" }
    # 组件包若被解压到**程序目录里**再运行安装器，会在程序目录留下 payload\pylibs\<组件> 这份
    # 解压残留（不在 sys.path 上、不影响运行，但会让人以为"依赖没清掉"）。产品布局不使用
    # payload\ 目录，故可安全清理。
    $payload = Join-Path $target "payload"
    if ((Test-Path -LiteralPath (Join-Path $payload "pylibs")) -or (Test-Path -LiteralPath (Join-Path $payload "plugins"))) {
        $items += @{ Path = $payload; Note = "组件/插件包解压残留（payload\）" }
    }
} else {
    $links = @((Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"))
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not [string]::IsNullOrWhiteSpace($desktop)) { $links += (Join-Path $desktop "$AppName.lnk") }
    $items += @{ Path = $target; Note = "程序目录（含依赖组件、插件、配置模板）" }
    if (-not $KeepShortcuts) {
        foreach ($l in $links) { if (Test-Path -LiteralPath $l) { $items += @{ Path = $l; Note = "快捷方式" } } }
    }
    if (-not $KeepData) {
        $items += @{ Path = $data; Note = "用户数据根（配置/用户/业务数据）" }
        # 指针文件是"机器级"记录：只有在数据根按指针解析时才删它。
        # 显式传了 -DataRoot 说明调用方在自己管数据根（沙箱/演练），不该动机器上的指针。
        if (-not $DataRoot) { $items += @{ Path = $Pointer; Note = "数据根指针文件" } }
    }
    if (-not $KeepRegistry) { $items += @{ Path = $RegKey; Note = "注册表卸载项（HKCU）" } }
}
$total = 0
foreach ($it in $items) {
    if (Test-Path -LiteralPath $it.Path) {
        $sz = if ((Get-Item -LiteralPath $it.Path -Force) -is [System.IO.DirectoryInfo]) { Get-DirSize $it.Path } else { (Get-Item -LiteralPath $it.Path -Force).Length }
        $total += $sz
        Say ("  - {0}（{1}）" -f $it.Note, (Fmt-Size $sz))
        Say ("      {0}" -f $it.Path)
        # 数据根最容易误删（里面有 API Key、上传的业务数据）：列出内容让人确认是不是测试数据
        if ($it.Path -eq $data) {
            $kids = @(Get-ChildItem -LiteralPath $it.Path -Force -ErrorAction SilentlyContinue |
                      Select-Object -First 6 | ForEach-Object { $_.Name })
            if ($kids.Count -gt 0) { Say ("      内容：{0}{1}" -f ($kids -join "、"), $(if (@(Get-ChildItem -LiteralPath $it.Path -Force).Count -gt 6) { " …" } else { "" })) }
            Warn "数据根里有真实配置与业务数据（API Key / 上传文件）——**开发机不要跑全清**；测试机才用。要保留请加 -KeepData"
        }
    } else {
        Say ("  - {0}：不存在，跳过" -f $it.Note)
    }
}
Say ("  合计释放约：{0}" -f (Fmt-Size $total))
if ($Scope -eq "Full" -and $KeepData) { Warn "按 -KeepData 保留用户数据根（下一轮验收建议不要保留）" }
if ($Scope -eq "Deps") { Say "  （-Scope Deps：只清依赖组件，程序与数据保留）" }

if ($RemoveVCRuntime) {
    Say ""
    Remove-VCRuntime -IncludeX86:$IncludeX86 -DryRun | Out-Null
} else {
    $vc = Get-VCRuntimeInfo
    if ($vc.DllsX64.Count -gt 0 -or $vc.MarkerX64) {
        Say ""
        Warn ("系统里仍有 VC++ 运行库（System32 有 {0}/10 个 DLL{1}）——「缺 VC 运行库」这类验收会测不出来；" -f $vc.DllsX64.Count, $(if ($vc.Version) { "，$($vc.Version)" } else { "" }))
        Warn "要连它一起清（仅测试机、需管理员）请加 -RemoveVCRuntime。"
    }
}

if ($DryRun) { Say ""; Say "  [-DryRun] 仅演算，未做任何改动。"; exit 0 }
if (-not $Yes) {
    Say ""
    $ans = Read-Host "  确认执行？(y/N)"
    if ($ans -notmatch '^[yY]') { Say "  已取消。"; exit 0 }
}

# ============================================================================
#  4. 执行
# ============================================================================
Head "3. 执行清理"
$stopped = Stop-Service
foreach ($it in $items) {
    $p = $it.Path
    if (-not (Test-Path -LiteralPath $p)) { continue }
    try {
        if ($p -like "HKCU:*") {
            Remove-Item -Path $p -Recurse -Force -ErrorAction Stop
        } else {
            Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction Stop
        }
        Say ("  已删除：{0}" -f $p)
    } catch {
        Left ("删除失败：{0}（{1}）" -f $p, $_.Exception.Message)
    }
}

# ---- 系统级 VC++ 运行库（显式开关；不属于"本产品留下的东西"，故单独处理） ----
$vcOk = $true
if ($RemoveVCRuntime) {
    $vcOk = Remove-VCRuntime -IncludeX86:$IncludeX86
}

# ============================================================================
#  5. 清理后核查 + 下一轮提示
# ============================================================================
$clean = Invoke-CleanCheck -Mode $Scope
if (-not $vcOk) { $clean = $false; Left "系统级 VC++ 运行库未完全清除（详见上文；被占用的 DLL 重启后重跑即可）" }
if ($clean) {
    Say ""
    Say "  下一轮验收（详见 docs\guide\干净机器部署验收手册.md）："
    Say "    1) 解压 JZToolsHub-v<版本>.zip → 双击「一键安装.bat」"
    Say "    2) 解压插件集 → 双击「安装插件集.bat」（或登录后台用「一键扫描安装」）"
    Say "    3) 依赖组件按需装（cv2 需同时装 numpy）→ 双击包内「安装依赖组件.bat」"
}
exit $(if ($clean) { 0 } else { 1 })
