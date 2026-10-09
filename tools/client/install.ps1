# ============================================================================
# YFJZ_Client 安装 / 更新 / 内核装载 / 体检（目标机侧；审计 D-5、T07 遗留销项）
#
# 用途（三件事，可单跑）：
#   ① 首次安装 / 程序包更新：把本包（或 -Source 指定目录）落到 -InstallDir，
#      **程序更新不触碰 runtime\ 子树**（WebView2 fixed 内核 + embeddable Python；
#      T07/T31 验收口径：覆盖程序目录后内核目录原样未动）。
#   ② 内核装载：探测 runtime\webview2 有效（msedgewebview2.exe + KERNEL_MARKER）→ 有效即跳过；
#      缺失/无效时从「内核分件」装入（分件目录 / zip / 原始 Microsoft CAB 均可）。
#   ③ 体检：-Verify 只读断言（程序文件 / 内核 exe+marker+版本 / 语言裁剪 / worker 运行时）。
#
# 用法：
#   首次安装：powershell -ExecutionPolicy Bypass -File install.ps1 -InstallDir "%LOCALAPPDATA%\YFJZ_Client"
#   程序更新：powershell -ExecutionPolicy Bypass -File install.ps1 -InstallDir <上> -Source <新程序包目录>
#   就地更新：powershell -ExecutionPolicy Bypass -File install.ps1 -Source <新程序包目录>   # 本目录即程序目录
#   装内核：  powershell -ExecutionPolicy Bypass -File install.ps1 -Kernel <内核分件.zip|目录|.cab>
#   体检：    powershell -ExecutionPolicy Bypass -File install.ps1 -Verify
#   只演算：  上述任意命令追加 -DryRun
#
# 常用开关：
#   -Version <v>  期望内核版本（缺省：<程序目录>\build-info.json 的 webview2.version →
#                 脚本旁 client-build-config.json → 空=不校验；含 x 段（100.0.1185.x）按前缀匹配）
#   -Force        内核版本不符 / 同版本重装 也继续（仍校验结构）
#   -NoStart      更新完成后不自动拉起客户端（缺省：本来在运行才拉起）
#   -KeepLangs    保留语言包（缺省 zh-CN,en-US；仅装原始 CAB 时裁剪多语言）
#
# 内核分件来源（-Kernel 缺省时的自动探测）：本包目录下 runtime\webview2（有效即用）、
#   本包同级 运行时\*.zip、本包目录 *.zip（名字含 内核/webview2）。
#
# ★ 三条底线：
#   ① 内核目录只增不删：程序更新只搬「非 runtime\」的程序文件，整棵 runtime\ 原位保留；
#      更新后比对内核指纹（文件数/总字节/KERNEL_MARKER 哈希），不一致即红灯并回滚。
#   ② 先校验后写入：分件结构 / 内核版本 / 语言裁剪全部过检才开始落盘。
#   ③ 可回退：程序更新前把被替换内容整体挪到同级备份目录；失败原样搬回。
#
# 覆盖范围：R 窗口（真机安装与 100 代内核跑壳）用本脚本；内核包获取/裁剪见
#   tools/client/build-webview2-kernel.py（--list / --from-local / --url / --zip）。
#
# 分发落点（为什么真源在 tools/client/）：客户端程序目录由 build-client.py 组装，每次出包会
#   整目录重写（只保留 runtime\ 子树）——放在 deploy\桌面端\YFJZ_Client\ 下的脚本既不可复现、
#   又不进 git（deploy/ 在 .gitignore）。故真源在 tools/client/install.ps1，随包分发时复制到
#   客户端程序包根（脚本放包根 + 不放参数 = 就地安装，与根 install.ps1 同风格）；
#   与 tools/plugin-upgrade/install-plugin.ps1「工具在仓库、安装器随包」先例一致。
# ============================================================================
param(
    [string]$InstallDir = "",          # 目标程序目录（缺省：脚本所在目录——脚本放在包根时=就地安装）
    [string]$Source = "",              # 程序包目录（缺省：脚本所在目录）
    [string]$Kernel = "",              # 内核分件：目录 / zip / 原始 CAB
    [switch]$Verify,                   # 只读体检
    [switch]$DryRun,                   # 只演算，不落盘
    [switch]$Force,                    # 覆盖「版本不符 / 同版本重装 / 目标内核已在」的限制
    [switch]$NoStart,                  # 更新后不自动拉起客户端
    [string]$Version = "",             # 期望内核版本（缺省从 build-info.json / 构建配置读）
    [string]$KeepLangs = "zh-CN,en-US" # 装原始 CAB 时保留的语言包
)
$ErrorActionPreference = "Stop"

$ScriptVer = "1.0.0"
$ExeName   = "YFJZ_Client.exe"
$KernelRel = "runtime\webview2"        # 与 client/client_config.py:webview2_runtime_dir 同口径
$WorkerPy  = "runtime\python\python.exe"
$TempWork  = $null
$StoppedByUs = $false

function Say { param([string]$m = "") Write-Host $m }
function Warn { param([string]$m) Write-Warning $m }
function Fail {
    param([string]$m, [int]$Code = 1)
    Say ""
    Say "  [失败] $m" -ForegroundColor Red
    if ($script:StoppedByUs -and -not $NoStart) {
        try { Start-ClientApp -AppDir $script:AppDir } catch { }
    }
    if ($script:TempWork -and (Test-Path -LiteralPath $script:TempWork)) {
        Remove-Item -LiteralPath $script:TempWork -Recurse -Force -ErrorAction SilentlyContinue
    }
    Say ""
    exit $Code
}

# ---------------- 基础工具 ----------------
function Read-Json {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}
function Get-Sha256File {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}
# 占位/前缀感知的版本匹配：期望 "100.0.1185.x" 时按 "100.0.1185." 前缀；否则全等。
function Test-VersionMatch {
    param([string]$Expected, [string]$Actual)
    if ([string]::IsNullOrWhiteSpace($Expected) -or [string]::IsNullOrWhiteSpace($Actual)) { return $null }
    if ($Expected -match '[^0-9.]') {
        $pre = ($Expected -replace '\.?[^0-9.].*$', '')
        if ([string]::IsNullOrWhiteSpace($pre)) { return $null }
        return $Actual.StartsWith($pre)
    }
    return ($Actual -eq $Expected)
}
function Get-FileVersionOf {
    param([string]$Path)
    try {
        $v = (Get-Item -LiteralPath $Path -ErrorAction Stop).VersionInfo.FileVersion
        if ($v -and $v -match '(\d+\.\d+\.\d+\.\d+)') { return $matches[1] }
    } catch { }
    return ""
}
function Get-MarkerVersion {
    param([string]$KernelDir)
    $m = Join-Path $KernelDir "KERNEL_MARKER"
    if (-not (Test-Path -LiteralPath $m)) { return "" }
    try {
        $t = Get-Content -LiteralPath $m -Raw -Encoding UTF8
        if ($t -match 'kernel-marker-(\S+)') { return $matches[1] }
    } catch { }
    return ""
}
# 内核目录指纹：文件数 / 总字节 / KERNEL_MARKER 的 sha256（程序更新前后比对用）
function Get-KernelFingerprint {
    param([string]$KernelDir)
    if (-not (Test-Path -LiteralPath $KernelDir)) { return "absent" }
    $files = @(Get-ChildItem -LiteralPath $KernelDir -Recurse -File -Force -ErrorAction SilentlyContinue)
    $sum = 0
    if ($files.Count -gt 0) { $sum = ($files | Measure-Object -Property Length -Sum).Sum }
    return ("{0}|{1}|{2}" -f $files.Count, $sum, (Get-Sha256File (Join-Path $KernelDir "KERNEL_MARKER")))
}

# ---------------- 目录 / 版本解析 ----------------
function Resolve-Paths {
    param([string]$Install, [string]$Src)
    if ([string]::IsNullOrWhiteSpace($Src)) { $Src = $PSScriptRoot }
    $Src = [System.IO.Path]::GetFullPath($Src)
    if ([string]::IsNullOrWhiteSpace($Install)) { $Install = $Src }
    $Install = [System.IO.Path]::GetFullPath($Install)
    return @{ Source = $Src; Target = $Install }
}
function Get-ExpectedVersion {
    param([string]$Target, [string]$Src, [string]$Explicit)
    if (-not [string]::IsNullOrWhiteSpace($Explicit)) { return $Explicit }
    foreach ($d in @($Target, $Src, $PSScriptRoot)) {          # 目标 → 源包 → 脚本旁（仓库形态）
        if ([string]::IsNullOrWhiteSpace($d)) { continue }
        $bi = Read-Json (Join-Path $d "build-info.json")
        if ($bi -and $bi.webview2 -and $bi.webview2.version) { return [string]$bi.webview2.version }
        $cfg = Read-Json (Join-Path $d "client-build-config.json")
        if ($cfg -and $cfg.webview2 -and $cfg.webview2.version) { return [string]$cfg.webview2.version }
    }
    return ""
}

# ---------------- 客户端进程 ----------------
function Get-ClientProc {
    $name = [System.IO.Path]::GetFileNameWithoutExtension($ExeName)
    return @(Get-Process -Name $name -ErrorAction SilentlyContinue)
}
function Stop-ClientApp {
    $p = Get-ClientProc
    if ($p.Count -gt 0) {
        $p | Stop-Process -Force
        Start-Sleep -Milliseconds 800
        $script:StoppedByUs = $true
        Say "  已停止客户端进程（PID $($p.Id -join ', ')）"
    }
}
function Start-ClientApp {
    param([string]$AppDir)
    $exe = Join-Path $AppDir $ExeName
    if (-not (Test-Path -LiteralPath $exe)) { return }
    Start-Process -FilePath $exe -WorkingDirectory $AppDir | Out-Null
    $script:StoppedByUs = $false
    Say "  已启动客户端（托盘常驻；渲染内核见数据根 logs 或 client.log）"
}

# ---------------- 内核分件：解包 / 定位 / 裁剪 / 校验 ----------------
function Expand-KernelZip {
    param([string]$ZipPath, [string]$DestDir)
    try { Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop } catch { }
    try {
        $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
        try {
            foreach ($e in $zip.Entries) {
                if ([string]::IsNullOrEmpty($e.Name)) { continue }
                if ($e.FullName -match '(^|/)\.\.(/|$)' -or $e.FullName -match '^[A-Za-z]:') {
                    Fail "分件 zip 含不安全路径（.. / 绝对路径）：$($e.FullName)"
                }
                $target = Join-Path $DestDir ($e.FullName -replace '/', '\')
                $parent = Split-Path -Parent $target
                if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
                [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
            }
        } finally { $zip.Dispose() }
        return
    } catch { }
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $DestDir -Force
}
function Expand-KernelCab {
    param([string]$CabPath, [string]$DestDir)
    $expand = Join-Path $env:SystemRoot "System32\expand.exe"
    $tar    = Join-Path $env:SystemRoot "System32\tar.exe"
    if (Test-Path -LiteralPath $expand) {
        Say "      展开 CAB：expand.exe -F:*"
        & $expand -F:* $CabPath $DestDir | Out-Null
        if ($LASTEXITCODE -eq 0 -and @(Get-ChildItem -LiteralPath $DestDir -Force).Count -gt 0) { return }
        Warn "expand.exe 返回 $LASTEXITCODE / 无产物，改用 tar(bsdtar)…"
    }
    if (Test-Path -LiteralPath $tar) {
        Say "      展开 CAB：tar.exe -xf"
        & $tar -xf $CabPath -C $DestDir
        if ($LASTEXITCODE -eq 0 -and @(Get-ChildItem -LiteralPath $DestDir -Force).Count -gt 0) { return }
    }
    Fail ("CAB 解包失败：$CabPath`n" +
          "         ① 确认包完整（sha256）；② 手工解包：expand.exe -F:* `"<包>`" `"<目录>`"，`n" +
          "         再 -Kernel 指向解出的含 msedgewebview2.exe 的那层目录。")
}
# 在树里找含 msedgewebview2.exe 的内核根（浅层优先；同层多个即报错）
function Find-KernelDir {
    param([string]$Root)
    foreach ($rel in @("", "runtime\webview2", "webview2", "runtime")) {
        $d = if ($rel) { Join-Path $Root $rel } else { $Root }
        if (Test-Path -LiteralPath (Join-Path $d "msedgewebview2.exe")) { return $d }
    }
    $hits = @(Get-ChildItem -LiteralPath $Root -Recurse -Depth 3 -Filter "msedgewebview2.exe" -File -Force -ErrorAction SilentlyContinue)
    if ($hits.Count -eq 0) { return $null }
    $minDepth = ($hits | ForEach-Object { ($_.FullName.Substring($Root.Length).Trim('\').Split('\').Count) } | Measure-Object -Minimum).Minimum
    $top = @($hits | Where-Object { ($_.FullName.Substring($Root.Length).Trim('\').Split('\').Count) -eq $minDepth })
    if ($top.Count -gt 1) {
        Fail "分件内有多处 msedgewebview2.exe（$(($top | ForEach-Object { $_.DirectoryName }) -join '；')），无法判定内核根。"
    }
    return $top[0].DirectoryName
}
function Trim-KernelLanguages {
    param([string]$KernelDir, [string[]]$Keep)
    $keepL = @($Keep | ForEach-Object { $_.ToLower() })
    $removed = 0; $freed = 0; $kept = 0
    foreach ($d in @(Get-ChildItem -LiteralPath $KernelDir -Recurse -Directory -Force -ErrorAction SilentlyContinue)) {
        if ($d.Name.ToLower() -ne "locales") { continue }
        foreach ($f in @(Get-ChildItem -LiteralPath $d.FullName -File -Filter "*.pak" -ErrorAction SilentlyContinue)) {
            $stem = $f.Name.Substring(0, $f.Name.Length - 4).ToLower()
            if ($keepL -contains $stem) { $kept++; continue }
            $freed += $f.Length
            Remove-Item -LiteralPath $f.FullName -Force
            $removed++
        }
    }
    return @{ Removed = $removed; Kept = $kept; Freed = $freed }
}
# 内核版本取证：exe FileVersion → KERNEL_MARKER → <版本>.manifest → 目录名
function Get-KernelVersionOf {
    param([string]$KernelDir)
    $exe = Join-Path $KernelDir "msedgewebview2.exe"
    $v = Get-FileVersionOf $exe
    if ($v) { return @{ Version = $v; From = "exe FileVersion" } }
    $v = Get-MarkerVersion $KernelDir
    if ($v) { return @{ Version = $v; From = "KERNEL_MARKER" } }
    $mf = @(Get-ChildItem -LiteralPath $KernelDir -Filter "*.manifest" -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^(\d+\.\d+\.\d+\.\d+)\.manifest$' })
    if ($mf.Count -gt 0) { return @{ Version = $mf[0].Name -replace '\.manifest$', ''; From = "包内 manifest" } }
    $leaf = Split-Path -Leaf $KernelDir
    if ($leaf -match 'FixedVersionRuntime\.(\d+\.\d+\.\d+\.\d+)') { return @{ Version = $matches[1]; From = "内核目录名" } }
    return @{ Version = ""; From = "" }
}
function Write-KernelMarker {
    param([string]$KernelDir, [string]$Ver, [string]$SrcNote)
    $lines = @(
        "kernel-marker-$Ver",
        "arch=x64",
        "source=$SrcNote",
        "installed_by=install.ps1/$ScriptVer",
        "installed_at=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    )
    [System.IO.File]::WriteAllText((Join-Path $KernelDir "KERNEL_MARKER"),
        ($lines -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
}
# 校验内核目录是否符合 fixed 口径（exe + marker + 版本 + 语言裁剪）
function Test-KernelDir {
    param([string]$KernelDir, [string]$Expected)
    $r = @{ Exists = $false; Exe = $false; Marker = $false; Version = ""; VersionFrom = "";
            VersionOk = $null; Locales = @(); Ok = $false }
    if (-not (Test-Path -LiteralPath $KernelDir)) { return [pscustomobject]$r }
    $r.Exists = $true
    $r.Exe = Test-Path -LiteralPath (Join-Path $KernelDir "msedgewebview2.exe")
    $r.Marker = Test-Path -LiteralPath (Join-Path $KernelDir "KERNEL_MARKER")
    $kv = Get-KernelVersionOf $KernelDir
    $r.Version = $kv.Version; $r.VersionFrom = $kv.From
    $r.VersionOk = Test-VersionMatch $Expected $kv.Version
    foreach ($d in @(Get-ChildItem -LiteralPath $KernelDir -Recurse -Directory -Force -ErrorAction SilentlyContinue)) {
        if ($d.Name.ToLower() -eq "locales") {
            $r.Locales += @(Get-ChildItem -LiteralPath $d.FullName -File -Filter "*.pak" -ErrorAction SilentlyContinue |
                            ForEach-Object { $_.Name })
        }
    }
    $badLang = @($r.Locales | Where-Object { $_.ToLower() -notin @("zh-cn.pak", "en-us.pak") })
    $r.Ok = ($r.Exe -and $r.Marker -and ($r.VersionOk -ne $false) -and $badLang.Count -eq 0)
    return [pscustomobject]$r
}
# 自动探测内核分件（-Kernel 缺省时）：本包目录 → 同级「运行时\」
function Resolve-KernelSource {
    param([string]$PkgDir)
    $c = @(Get-ChildItem -LiteralPath $PkgDir -File -Filter "*.zip" -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -match 'webview2|内核' } | Sort-Object Name -Descending)
    if ($c.Count -gt 0) { return $c[0].FullName }
    $rt = Join-Path (Split-Path -Parent $PkgDir) "运行时"
    if (Test-Path -LiteralPath $rt) {
        $c = @(Get-ChildItem -LiteralPath $rt -File -Filter "*.zip" -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -match 'webview2|内核' } | Sort-Object Name -Descending)
        if ($c.Count -gt 0) { return $c[0].FullName }
    }
    return ""
}

# ---------------- 内核装载 ----------------
function Install-KernelFrom {
    param([string]$Target, [string]$KernelPath, [string]$Expected, [string]$KeepLangs, [switch]$DryRun)
    $kernelDir = Join-Path $Target $KernelRel
    $isCab = $KernelPath.ToLower().EndsWith(".cab")
    $isZip = $KernelPath.ToLower().EndsWith(".zip")
    if (-not (Test-Path -LiteralPath $KernelPath)) { Fail "内核分件不存在：$KernelPath" }

    Say ""
    Say "==> 装载内核分件：$KernelPath"
    if ($DryRun) {
        Say "  [-DryRun] 仅演算："
        Say "    · 解包/复制 → 暂存目录（$(if ($isCab) { '原始 CAB：expand.exe -F:*' } elseif ($isZip) { 'zip 解包' } else { '目录复制' })）"
        if ($isCab) { Say "    · 语言裁剪：Locales 只留 $KeepLangs" }
        Say "    · 断言 msedgewebview2.exe + 写 KERNEL_MARKER（版本取 exe FileVersion/包内 manifest）"
        Say "    · 原子替换：$kernelDir"
        Say "  去掉 -DryRun 即执行。"
        return 0
    }

    $script:TempWork = Join-Path ([System.IO.Path]::GetTempPath()) ("jz-kernel-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 6))
    New-Item -ItemType Directory -Force -Path $script:TempWork | Out-Null
    $stageRoot = Join-Path $script:TempWork "unpack"
    New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null

    if ($isZip) {
        Say "  [1/5] 解包 zip → 暂存"
        Expand-KernelZip -ZipPath $KernelPath -DestDir $stageRoot
        $kernelRoot = Find-KernelDir $stageRoot
    } elseif ($isCab) {
        Say "  [1/5] 展开 CAB → 暂存（多语言包同时裁掉）"
        Expand-KernelCab -CabPath $KernelPath -DestDir $stageRoot
        $kernelRoot = Find-KernelDir $stageRoot
    } else {
        Say "  [1/5] 复制分件目录 → 暂存"
        $srcRoot = Find-KernelDir $KernelPath
        if (-not $srcRoot) { Fail "分件目录内找不到 msedgewebview2.exe：$KernelPath" }
        Copy-Item -LiteralPath $srcRoot -Destination $stageRoot -Recurse -Force
        $kernelRoot = Find-KernelDir $stageRoot
    }
    if (-not $kernelRoot) { Fail "分件内找不到 msedgewebview2.exe（不是有效的 WebView2 fixed 内核分件）：$KernelPath" }
    Say "        内核根：$kernelRoot"

    Say "  [2/5] 语言裁剪（只留 $KeepLangs）"
    $trim = Trim-KernelLanguages -KernelDir $kernelRoot -Keep ($KeepLangs -split ',')
    Say "        删除 $($trim.Removed) 个语言包（$([math]::Round($trim.Freed / 1MB, 1)) MB），保留 $($trim.Kept) 个"

    Say "  [3/5] 结构 / 版本校验"
    if (-not (Test-Path -LiteralPath (Join-Path $kernelRoot "msedgewebview2.exe"))) {
        Fail "产物缺 msedgewebview2.exe：$kernelRoot"
    }
    $kv = Get-KernelVersionOf $kernelRoot
    if ($kv.Version) {
        Say "        内核版本：$($kv.Version)（取自 $($kv.From)）"
    } else {
        Warn "读不到内核版本（exe FileVersion / marker / manifest 都无）——版本核验按「未确认」放行"
    }
    $vm = Test-VersionMatch $Expected $kv.Version
    $expectedEff = $Expected
    if ($vm -eq $false) {
        if (-not $Force) {
            Remove-Item -LiteralPath $script:TempWork -Recurse -Force -ErrorAction SilentlyContinue
            $script:TempWork = $null
            Fail ("内核版本不符：期望 $Expected，分件 $($kv.Version)（尚未落盘，目标未动）。`n" +
                  "         确认要用该分件：-Force（marker 仍写实际版本）；或换匹配的分件。")
        }
        Warn "内核版本不符（期望 $Expected，分件 $($kv.Version)）：按 -Force 继续，marker 写实际版本。"
        $expectedEff = $kv.Version
    }
    if ($vm -eq $true) { Say "        版本核验通过（期望 $Expected）" }

    Say "  [4/5] 写 KERNEL_MARKER 并原子替换"
    if (-not (Test-Path -LiteralPath (Join-Path $kernelRoot "KERNEL_MARKER"))) {
        Write-KernelMarker -KernelDir $kernelRoot -Ver ($kv.Version) -SrcNote $KernelPath
        Say "        已写 KERNEL_MARKER（kernel-marker-$($kv.Version)）"
    } else {
        Say "        分件自带 KERNEL_MARKER（保留）：$(Get-MarkerVersion $kernelRoot)"
    }
    $newDir = "$kernelDir.new-" + (Get-Date -Format "yyyyMMddHHmmss")
    $oldDir = $null
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $kernelDir) | Out-Null
    Move-Item -LiteralPath $kernelRoot -Destination $newDir
    if (Test-Path -LiteralPath $kernelDir) {
        $oldDir = "$kernelDir.old-" + (Get-Date -Format "yyyyMMddHHmmss")
        Move-Item -LiteralPath $kernelDir -Destination $oldDir
    }
    try {
        Move-Item -LiteralPath $newDir -Destination $kernelDir
    } catch {
        if ($oldDir) { Move-Item -LiteralPath $oldDir -Destination $kernelDir -ErrorAction SilentlyContinue }
        Fail "落盘失败（内核目录可能被占用——客户端/资源管理器在跑？）：$($_.Exception.Message)"
    }
    if ($oldDir) { Remove-Item -LiteralPath $oldDir -Recurse -Force -ErrorAction SilentlyContinue }

    Say "  [5/5] 自检（不过即回滚到原内核）"
    $chk = Test-KernelDir -KernelDir $kernelDir -Expected $expectedEff
    if (-not $chk.Ok) {
        Remove-Item -LiteralPath $kernelDir -Recurse -Force -ErrorAction SilentlyContinue
        if ($oldDir) { Move-Item -LiteralPath $oldDir -Destination $kernelDir -ErrorAction SilentlyContinue }
        Fail ("内核自检未过（已回滚到原内核）：exe=$($chk.Exe) marker=$($chk.Marker) 版本=$($chk.Version)" +
              "（期望 $expectedEff）Locales=$($chk.Locales -join ',')")
    }
    if ($oldDir) { Remove-Item -LiteralPath $oldDir -Recurse -Force -ErrorAction SilentlyContinue }
    Say "        msedgewebview2.exe + KERNEL_MARKER 就位；版本 $($chk.Version)；Locales $($chk.Locales -join '/')"
    Remove-Item -LiteralPath $script:TempWork -Recurse -Force -ErrorAction SilentlyContinue
    $script:TempWork = $null
    return 0
}

# ---------------- 程序安装 / 更新（保留 runtime\） ----------------
function Install-Program {
    param([string]$Target, [string]$Src, [switch]$DryRun, [switch]$NoStart)
    $srcExe = Join-Path $Src $ExeName
    if (-not (Test-Path -LiteralPath $srcExe)) {
        Fail ("程序包目录内没有 $ExeName：$Src`n" +
              "         ① 首次安装：-Source 指向解压后的客户端程序包目录（含 $ExeName 与 _internal\）；`n" +
              "         ② 只装内核：用 -Kernel <分件> 并保持 -Source 指向现有程序目录。")
    }
    $autoStart = $false
    $procs = Get-ClientProc
    if ($procs.Count -gt 0) { $autoStart = $true }

    # 程序/运行时界线：
    #   runtime\            → 整棵保留（内核 + embeddable Python，解耦口径）
    #   client.log/pid/port.txt → 客户端运行痕迹，不参与替换也不复制（源包里的可能是旧的）
    #   脚本自身             → 更新时不被挪走（正在运行），随源包复制/覆盖
    $runtimeArtifacts = @("client.log", "client.pid", "port.txt")
    $selfName  = Split-Path -Leaf $PSCommandPath
    $keepDirs  = @("runtime")
    $keepMoves = $runtimeArtifacts + @($selfName)

    $isUpdate = (Test-Path -LiteralPath $Target) -and ((Get-ChildItem -LiteralPath $Target -Force -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0)
    Say ""
    Say "==> 程序$(if ($isUpdate) { '更新' } else { '安装' })：$Src → $Target"
    $kernelDir = Join-Path $Target $KernelRel
    $fpBefore = Get-KernelFingerprint $kernelDir

    $newNames = @(Get-ChildItem -LiteralPath $Src -Force | ForEach-Object { $_.Name })
    $srcRt = Join-Path $Src "runtime"
    $srcHasKernelExe = Test-Path -LiteralPath (Join-Path $srcRt "webview2\msedgewebview2.exe")

    if ($DryRun) {
        Say "  [-DryRun] 仅演算："
        if ($isUpdate) {
            Say "    ① 停客户端（$(if ($autoStart) { '在运行，会先停后拉' } else { '未运行' })）"
            Say "    ② 备份：目标下除 runtime\ 与运行痕迹（$($runtimeArtifacts -join '/')）外的内容 → 同级备份目录"
            Say "    ③ 复制：源下除 runtime\ 与运行痕迹外的所有内容 → 目标（新增/覆盖）"
            if ($srcHasKernelExe) { Say "       [注意] 源包内含 runtime\webview2\msedgewebview2.exe——按解耦口径**不覆盖**目标内核" }
            Say "    ④ 校验内核指纹不变（当前：$fpBefore）"
            Say "    ⑤ 备份轮转（保留最近 2 份）"
        } else {
            Say "    ① 新建目录并按项复制（含 runtime\，源包内内核/运行时随包落地）"
        }
        Say "  去掉 -DryRun 即执行。"
        return 0
    }

    if (-not $isUpdate) {
        New-Item -ItemType Directory -Force -Path $Target | Out-Null
        Say "  [1/2] 首次安装：复制程序文件$(if (Test-Path -LiteralPath $srcRt) { '（含 runtime\）' })"
        foreach ($n in $newNames) {
            if ($runtimeArtifacts -contains $n) { continue }
            Copy-Item -LiteralPath (Join-Path $Src $n) -Destination (Join-Path $Target $n) -Recurse -Force
        }
        if ($srcHasKernelExe) { Say "        随包内核已落地：$kernelDir" }
        Say "  [2/2] 完成（无旧内容，无备份）"
        return 0
    }

    # 版本快照（同版本重装提示）
    $oldBi = Read-Json (Join-Path $Target "build-info.json")
    $newBi = Read-Json (Join-Path $Src "build-info.json")
    $oldVer = if ($oldBi -and $oldBi.product) { [string]$oldBi.product.version } else { "" }
    $newVer = if ($newBi -and $newBi.product) { [string]$newBi.product.version } else { "" }
    if ($oldVer -and $newVer -and $oldVer -eq $newVer -and -not $Force) {
        Fail ("同版本重装：目标与源都是 $oldVer。`n" +
              "         确需重装请加 -Force（仍先备份、仍校验内核目录未动）。")
    }

    Say "  [1/5] 停客户端"
    if ($procs.Count -gt 0) { Stop-ClientApp } else { Say "        客户端未运行" }

    $backupDir = "$Target-backup-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Say "  [2/5] 备份被替换内容 → $backupDir"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $moved = @()
    foreach ($it in @(Get-ChildItem -LiteralPath $Target -Force | Where-Object {
                ($keepDirs -notcontains $_.Name) -and ($keepMoves -notcontains $_.Name) })) {
        Move-Item -LiteralPath $it.FullName -Destination (Join-Path $backupDir $it.Name)
        $moved += $it.Name
    }
    Say "        已挪走 $($moved.Count) 项：$($moved -join '、')"

    Say "  [3/5] 复制新程序文件（整棵 runtime\ 不参与）"
    try {
        foreach ($n in $newNames) {
            if (($keepDirs -contains $n) -or ($runtimeArtifacts -contains $n)) { continue }
            Copy-Item -LiteralPath (Join-Path $Src $n) -Destination (Join-Path $Target $n) -Recurse -Force
        }
        if ($srcHasKernelExe) {
            Warn "源包内含 runtime\webview2（内核）——按内核/程序解耦口径未覆盖目标内核；换内核请用 -Kernel。"
        }
    } catch {
        Say "        复制失败：$($_.Exception.Message)"
        Say "        回滚：删除已复制内容，恢复备份…"
        foreach ($n in $newNames) {
            if (($keepDirs -contains $n) -or ($runtimeArtifacts -contains $n)) { continue }
            if ($moved -contains $n) {
                Remove-Item -LiteralPath (Join-Path $Target $n) -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        foreach ($n in $moved) { Move-Item -LiteralPath (Join-Path $backupDir $n) -Destination (Join-Path $Target $n) }
        Fail "程序更新失败，已回滚到更新前状态。"
    }

    Say "  [4/5] 校验「程序更新不触碰内核目录」"
    $fpAfter = Get-KernelFingerprint $kernelDir
    if ($fpBefore -eq "absent") {
        Say "        目标此前无内核目录（$KernelRel）——跳过指纹比对；用 -Kernel 装内核。"
    } elseif ($fpBefore -eq $fpAfter) {
        Say "        内核指纹未变：$fpAfter（文件数|字节|marker sha256）"
    } else {
        Say "        指纹变化：$fpBefore → $fpAfter"
        Fail ("内核目录被动过（违反程序/内核解耦口径）：$kernelDir`n" +
              "         已保留备份 $backupDir；请排查后再重跑。", 3)
    }

    Say "  [5/5] 备份轮转（保留最近 2 份）"
    $all = @(Get-ChildItem -LiteralPath (Split-Path -Parent $Target) -Directory -ErrorAction SilentlyContinue |
             Where-Object { $_.Name -like ((Split-Path -Leaf $Target) + "-backup-*") } |
             Sort-Object LastWriteTime -Descending)
    if ($all.Count -gt 2) {
        foreach ($b in $all[2..($all.Count - 1)]) {
            Remove-Item -LiteralPath $b.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
        Say "        清理 $($all.Count - 2) 份旧备份"
    }
    Say "        本次备份保留：$backupDir（可人工回退）"

    if ($autoStart -and -not $NoStart) { Start-ClientApp -AppDir $Target }
    return 0
}

# ============================================================================
#  主流程
# ============================================================================
Say "================================================"
Say "  YFJZ_Client 安装器 / 内核装载器（install.ps1 v$ScriptVer）"
Say "================================================"

$paths = Resolve-Paths -Install $InstallDir -Src $Source
$Target = $paths.Target
$Src = $paths.Source
$expected = Get-ExpectedVersion -Target $Target -Src $Src -Explicit $Version

# ---------------- 体检 ----------------
if ($Verify) {
    Say ""
    Say "== 体检（只读）=="
    Say "   程序目录：$Target"
    Say "   期望内核：$(if ($expected) { $expected } else { '（未声明，跳过版本比对）' })"
    $fail = 0
    $exe = Join-Path $Target $ExeName
    if (Test-Path -LiteralPath $exe) {
        Say "   [OK] 程序文件：$ExeName（FileVersion $(Get-FileVersionOf $exe)）"
    } else {
        Say "   [--] 程序文件缺失：$exe" -ForegroundColor Yellow
    }
    $bi = Read-Json (Join-Path $Target "build-info.json")
    if ($bi -and $bi.product) { Say "        build-info：$($bi.product.name) $($bi.product.version)（$($bi.built_at)）" }
    $kernelDir = Join-Path $Target $KernelRel
    $k = Test-KernelDir -KernelDir $kernelDir -Expected $expected
    if ($k.Ok) {
        Say "   [OK] fixed 内核：$kernelDir"
        Say "        版本 $($k.Version)（取自 $($k.VersionFrom)）$(if ($k.VersionOk -eq $true) { '，与期望一致' })"
        Say "        语言包：$($k.Locales -join '/')"
        Say "        KERNEL_MARKER：$(Get-MarkerVersion $kernelDir)"
        Say "   [OK] client_config.webview2_runtime_dir() 将返回该目录（exe 存在）→ 壳启用 fixed 内核"
    } else {
        Say "   [!!] fixed 内核不可用：exe=$($k.Exe) marker=$($k.Marker) 版本=$($k.Version) Locales=$($k.Locales -join '/')" -ForegroundColor Yellow
        if ($k.Exists -and $k.Exe -and -not $k.Marker) { Say "        缺 KERNEL_MARKER（来源不明；建议用 -Kernel 重装）" }
        if ($k.VersionOk -eq $false) { Say "        版本与期望不符（期望 $expected，实际 $($k.Version)）" }
        Say "        → 客户端将回落系统常青内核（client.log 会留痕「webview2 runtime: 系统常青」）"
        $fail = 2
    }
    $wk = Join-Path $Target $WorkerPy
    if (Test-Path -LiteralPath $wk) { Say "   [OK] 插件 worker 运行时：$WorkerPy" }
    else { Say "   [!!] 缺插件 worker 运行时：$WorkerPy（先跑 tools/client/build-client-runtime.py）" -ForegroundColor Yellow }
    $ks = Resolve-KernelSource -PkgDir $Src
    if ($ks) { Say "   [i]  可用内核分件：$ks" } else { Say "   [i]  未发现内核分件（-Kernel 可显式指定）" }
    Say ""
    if ($fail -eq 0) { Say "  结论：fixed 内核链就绪。" } else { Say "  结论：内核链未就绪（见上）。" -ForegroundColor Yellow }
    exit $fail
}

# ---------------- 内核装载（-Kernel 优先） ----------------
if (-not [string]::IsNullOrWhiteSpace($Kernel)) {
    if (-not (Test-Path -LiteralPath $Target)) {
        Fail "程序目录不存在：$Target`n         先跑首次安装（-InstallDir <目录>），或把 -InstallDir 指向现有程序目录。"
    }
    $kernelDir = Join-Path $Target $KernelRel
    $cur = Test-KernelDir -KernelDir $kernelDir -Expected $expected
    if ($cur.Ok -and -not $Force) {
        Say ""
        Say "==> 内核已装且有效（版本 $($cur.Version)，$($cur.Locales -join '/')）——跳过装载"
        Say "    需要强制重装：加 -Force。"
        exit 0
    }
    $rc = Install-KernelFrom -Target $Target -KernelPath $Kernel -Expected $expected -KeepLangs $KeepLangs -DryRun:$DryRun
    Say ""
    Say "==> 内核装载完成。体检：powershell -ExecutionPolicy Bypass -File $($MyInvocation.MyCommand.Name) -InstallDir `"$Target`" -Verify"
    exit $rc
}

# ---------------- 安装 / 更新 ----------------
if ($Target -eq $Src -and -not (Test-Path -LiteralPath (Join-Path $Target $ExeName))) {
    Fail ("就地模式（脚本所在目录=程序目录）下没有 $ExeName：$Target`n" +
          "         ① 首次安装请给 -InstallDir <目标目录>；或把本脚本放进程序包根后运行；`n" +
          "         ② 只装内核：-Kernel <分件> -InstallDir <现有程序目录>。")
}
$rc = Install-Program -Target $Target -Src $Src -DryRun:$DryRun -NoStart:$NoStart
if ($DryRun) { exit 0 }

# ---------------- 内核状态收口 ----------------
$kernelDir = Join-Path $Target $KernelRel
$k = Test-KernelDir -KernelDir $kernelDir -Expected $expected
if ($k.Ok) {
    Say ""
    Say "==> fixed 内核就绪：$kernelDir（版本 $($k.Version)）"
} else {
    $ks = Resolve-KernelSource -PkgDir $Src
    Say ""
    Warn ("fixed 内核不可用（exe=$($k.Exe) marker=$($k.Marker) 版本=$($k.Version)）——客户端将回落系统常青内核。")
    if ($ks) {
        Say "    可用分件：$ks"
        Say "    装载：powershell -ExecutionPolicy Bypass -File $($MyInvocation.MyCommand.Name) -InstallDir `"$Target`" -Kernel `"$ks`""
    } else {
        Say "    内核分件未随包：在打包机跑 tools/client/build-webview2-kernel.py（--list 核验可得版本），"
        Say "    产物 JZClient-内核-webview2-*.zip 放本包同级「运行时\」目录，或 -Kernel 直接指定路径。"
    }
    exit 2
}
Say ""
Say "  体检：powershell -ExecutionPolicy Bypass -File $($MyInvocation.MyCommand.Name) -InstallDir `"$Target`" -Verify"
exit 0
