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
    $msvcp = Test-Path -LiteralPath (Join-Path $env:WINDIR "System32\msvcp140.dll")
    $hasPy = [bool](Get-Command python -ErrorAction SilentlyContinue)
    Say ("  {0} msvcp140.dll 在系统里：{1}" -f $(if ($msvcp) { "[i]" } else { "[OK]" }), $msvcp)
    if ($msvcp) { Warn "系统已有 VC++ 运行库 → 本机测不出「缺 VC 运行时」这类问题（要换机器或接受这一项测不到）" }
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

# ============================================================================
#  5. 清理后核查 + 下一轮提示
# ============================================================================
$clean = Invoke-CleanCheck -Mode $Scope
if ($clean) {
    Say ""
    Say "  下一轮验收（详见 docs\guide\干净机器部署验收手册.md）："
    Say "    1) 解压 JZToolsHub-v<版本>.zip → 双击「一键安装.bat」"
    Say "    2) 解压插件集 → 双击「安装插件集.bat」（或登录后台用「一键扫描安装」）"
    Say "    3) 依赖组件按需装（cv2 需同时装 numpy）→ 双击包内「安装依赖组件.bat」"
}
exit $(if ($clean) { 0 } else { 1 })
