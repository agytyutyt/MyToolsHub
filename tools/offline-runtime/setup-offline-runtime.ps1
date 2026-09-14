# ============================================================================
# JZToolsHub 离线运行组件安装（Chrome / LibreOffice）
#
# 用途：无外网的目标机上，用随包分发的安装包把两个「应用之外但运行需要」的
#       组件装好/解包好。install.ps1 在安装完成后会自动调用本脚本；也可单独运行
#       （修环境、补装单个组件、或在只做组件的机器上跑）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File setup-offline-runtime.ps1
#   ... -SkipChrome                 只处理 LibreOffice
#   ... -SkipLibreOffice            只处理 Chrome
#   ... -Force                      已安装/已解包也重做
#   ... -DryRun                     只报告将要做什么，不实际执行
#   ... -RuntimeDir D:\path         指定组件目录（默认本脚本所在目录）
#
# 两个组件的行为差异（很重要）：
#   * Chrome      —— 全机安装，**需要管理员权限**。无管理员时脚本不报错，
#                    而是打印手动安装指引（Chrome 不是应用运行的必要条件，
#                    仅影响目标机的浏览体验/浏览器基线）。
#   * LibreOffice —— 优先解压随包的「裁剪核心包」libreoffice-core.zip（约 166 MB，
#                    解出约 558 MB）：只保留 .doc→.docx 与 .xls→.xlsx 需要的那套，
#                    裁掉了词典/界面语言包/图标主题/字体等死重。
#                    包内只有原始 MSI 时回退 msiexec /a（管理安装，解全量约 1.5 GB）。
#                    两条路都**不需要管理员、不写注册表**，解出的目录可直接运行；
#                    应用会自动探测其中的 soffice.exe（零配置）。
#
# 退出码：0 = 无硬失败；1 = 有组件尝试安装但失败（需人工处理）。
# ============================================================================

param(
    [string]$RuntimeDir = "",
    [switch]$SkipChrome,
    [switch]$SkipLibreOffice,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RuntimeDir)) {
    if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        $RuntimeDir = (Get-Location).Path
    } else {
        $RuntimeDir = $PSScriptRoot
    }
}

function Say {
    param([string]$Text)
    Write-Host $Text
}

function Test-IsAdmin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $pr = New-Object Security.Principal.WindowsPrincipal($id)
        return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Get-ComponentMsi {
    param([string]$SubDir)
    $dir = Join-Path $RuntimeDir $SubDir
    if (-not (Test-Path $dir)) { return $null }
    $f = Get-ChildItem -LiteralPath $dir -File -Filter "*.msi" -ErrorAction SilentlyContinue |
         Sort-Object Name | Select-Object -First 1
    if ($f) { return $f.FullName }
    return $null
}

# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------
function Get-ChromeVersion {
    $keys = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome"
    )
    foreach ($k in $keys) {
        $p = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
        if ($p -and $p.DisplayVersion) { return $p.DisplayVersion }
    }
    return $null
}

function Install-ChromeComponent {
    $msi = Get-ComponentMsi "chrome"
    if (-not $msi) {
        Say "  [跳过] 未找到 chrome\*.msi（组件可能未被下载，见 runtime\README.md）"
        return "missing"
    }
    $have = Get-ChromeVersion
    if ($have -and -not $Force) {
        Say "  [跳过] 已安装 Chrome $have（用 -Force 可重装）"
        return "present"
    }

    if (-not (Test-IsAdmin)) {
        Say "  [需管理员] Chrome 为全机安装，当前会话无管理员权限。"
        Say "             请右键「安装离线组件.bat」→ 以管理员身份运行；或手动双击："
        Say "             $msi"
        return "need-admin"
    }
    if ($DryRun) {
        Say "  [DryRun] 将执行：msiexec /i `"$msi`" /qn /norestart"
        return "dryrun"
    }

    Say "  正在静默安装 Chrome…（$([IO.Path]::GetFileName($msi))）"
    $rc = Invoke-Msi -Arguments @("/i", $msi, "/qn", "/norestart")
    if ($rc -eq 0 -or $rc -eq 3010) {
        $v = Get-ChromeVersion
        Say "  [完成] Chrome 已安装（版本 $v，退出码 $rc）"
        return "installed"
    }
    Say "  [失败] msiexec 退出码 $rc，请检查安装包是否完整或改用管理员手动安装"
    return "failed"
}

# ---------------------------------------------------------------------------
# LibreOffice（便携解包）
# ---------------------------------------------------------------------------
function Find-SofficeUnder {
    param([string]$Root)
    if (-not (Test-Path $Root)) { return $null }
    $direct = Join-Path $Root "program\soffice.exe"
    if (Test-Path $direct) { return $direct }
    $hit = Get-ChildItem -LiteralPath $Root -Recurse -File -Filter "soffice.exe" -ErrorAction SilentlyContinue |
           Sort-Object FullName | Select-Object -First 1
    if ($hit) { return $hit.FullName }
    return $null
}

# 解压随包 zip（三级策略；.NET Framework 上 ZipFile.ExtractToDirectory 只有
# (源,目标) 与 (源,目标,Encoding) 两个重载，**没有** bool 覆盖重载，别传 $true）
function Expand-PayloadZip {
    param([string]$ZipPath, [string]$DestDir)
    try { Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop } catch { }

    # ① 最快：整包解压（要求目标文件都不存在 —— 正常的首次安装即是如此）
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
        } finally {
            $zip.Dispose()
        }
        return $true
    } catch {
        Say "  [提示] 逐条解压不可用（$($_.Exception.Message)），回退 Expand-Archive"
    }

    # ③ 兜底：Expand-Archive（慢，但兼容性最好）
    try {
        Expand-Archive -LiteralPath $ZipPath -DestinationPath $DestDir -Force
        return $true
    } catch {
        Say "  [失败] 解压失败：$($_.Exception.Message)"
        return $false
    }
}

function Install-LibreOfficeComponent {
    $loDir    = Join-Path $RuntimeDir "libreoffice"
    $coreZip  = Join-Path $loDir "libreoffice-core.zip"
    $coreMeta = Join-Path $loDir "libreoffice-core.json"

    $found = Find-SofficeUnder $loDir
    if ($found -and -not $Force) {
        Say "  [跳过] 便携 LibreOffice 已就绪：$found"
        return "present"
    }

    # ---- 路径 A（优先）：随包的裁剪核心包 ----
    if (Test-Path $coreZip) {
        $zipMb = [math]::Round((Get-Item $coreZip).Length / 1MB, 1)
        if (Test-Path $coreMeta) {
            try {
                $m = Get-Content $coreMeta -Raw -Encoding UTF8 | ConvertFrom-Json
                $unpMb = [math]::Round($m.artifact.unpacked_bytes / 1MB, 1)
                Say "  [核心包] $($m.source.file) 的裁剪版：$zipMb MB → 解出 $unpMb MB / $($m.artifact.unpacked_files) 文件"
            } catch {
                Say "  [核心包] $zipMb MB（元数据解析失败，不阻断安装）"
            }
        } else {
            Say "  [核心包] $zipMb MB"
        }
        if ($DryRun) {
            Say "  [DryRun] 将解压 $coreZip 到 $RuntimeDir（包内路径以 libreoffice\ 打头）"
            return "dryrun"
        }
        Say "  正在解压到：$RuntimeDir"
        # 核心包内路径以 libreoffice\ 打头 → 解到 RuntimeDir 即得 runtime\libreoffice\program\soffice.exe
        if (-not (Expand-PayloadZip -ZipPath $coreZip -DestDir $RuntimeDir)) {
            return "failed"
        }
        $found = Find-SofficeUnder $loDir
        if (-not $found) {
            Say "  [失败] 解压完成但未找到 soffice.exe，请人工检查 $loDir"
            return "failed"
        }
        Say "  [完成] 便携 LibreOffice 就绪：$found"
        Say "         应用会自动探测该路径，无需在插件配置里指定 soffice_path。"
        Say "         确认可用后可删除 $coreZip 释放约 $zipMb MB（部署包内仍有它，可随时重解）。"
        return "installed"
    }

    # ---- 路径 B（回退）：只有原始 MSI 时走管理安装，解全量约 1.5 GB ----
    $msi = Get-ComponentMsi "libreoffice"
    if (-not $msi) {
        Say "  [跳过] 未找到 libreoffice\*.msi / libreoffice-core.zip（组件可能未被下载，见 runtime\README.md）"
        return "missing"
    }
    if ($DryRun) {
        Say "  [DryRun] 将执行：msiexec /a `"$msi`" /qn /norestart TARGETDIR=`"$loDir`""
        return "dryrun"
    }

    Say "  正在解包 LibreOffice 到：$loDir"
    Say "  （管理安装模式：不写注册表、不需要管理员，解出的目录可直接运行）"
    New-Item -ItemType Directory -Force -Path $loDir | Out-Null
    $rc = Invoke-Msi -Arguments @("/a", $msi, "/qn", "/norestart", "TARGETDIR=$loDir")
    if ($rc -ne 0 -and $rc -ne 3010) {
        Say "  [失败] msiexec 退出码 $rc"
        return "failed"
    }

    $found = Find-SofficeUnder $loDir
    if (-not $found) {
        Say "  [失败] 解包完成但未找到 soffice.exe，请人工检查 $loDir"
        return "failed"
    }
    Say "  [完成] 便携 LibreOffice 就绪：$found"
    Say "         应用会自动探测该路径，无需在插件配置里指定 soffice_path。"
    return "installed"
}

# ---------------------------------------------------------------------------
# msiexec 调用（等待真实完成：直接 & 调用时 msiexec 会提前返回）
# ---------------------------------------------------------------------------
function Invoke-Msi {
    param([string[]]$Arguments)
    $msiArgs = @()
    foreach ($a in $Arguments) {
        if ($a -match '\s' -and $a -notmatch '^"') { $msiArgs += ('"' + $a + '"') } else { $msiArgs += $a }
    }
    $proc = Start-Process -FilePath "msiexec.exe" -ArgumentList $msiArgs -Wait -PassThru -NoNewWindow
    return $proc.ExitCode
}

# ============================================================================
#  主流程
# ============================================================================
Say "==> JZToolsHub 离线运行组件安装"
Say "    组件目录：$RuntimeDir"
if ($DryRun) { Say "    [DryRun] 只报告，不实际执行" }
if (-not (Test-IsAdmin)) { Say "    当前会话：非管理员（Chrome 全机安装将需要提权）" }
Say ""

$results = @{}

if (-not $SkipChrome) {
    Say "[1/2] Google Chrome（浏览器基线：Chrome >= 72；企业版 MSI）"
    $results["chrome"] = Install-ChromeComponent
    Say ""
}

if (-not $SkipLibreOffice) {
    Say "[2/2] LibreOffice（知识库 .doc/.xls 高保真通道，便携解包）"
    $results["libreoffice"] = Install-LibreOfficeComponent
    Say ""
}

# ---- 自检汇总 ----
Say "==> 汇总"
$hardFail = $false
foreach ($k in $results.Keys) {
    $v = $results[$k]
    $label = switch ($v) {
        "installed"  { "[已安装]" }
        "present"    { "[已就绪]" }
        "dryrun"     { "[DryRun]" }
        "missing"    { "[缺文件]" }
        "need-admin" { "[待提权]" }
        default      { "[失败]  " }
    }
    Say "    $label $k（$v）"
    if ($v -eq "failed") { $hardFail = $true }
}

$soffice = Find-SofficeUnder (Join-Path $RuntimeDir "libreoffice")
if ($soffice) {
    Say "    soffice 可用：$soffice"
    Say "    应用侧自检：启动后访问 /api/knowledge-base/status 应显示 `"soffice`" 非 null。"
} else {
    Say "    soffice 不可用：知识库 .doc 归一化与 .xls 高保真渲染将降级（其余功能不受影响）。"
}

Say ""
if ($hardFail) {
    Say "==> 存在失败项，请人工处理后重跑本脚本。"
    exit 1
}
Say "==> 离线组件处理完成。"
exit 0
