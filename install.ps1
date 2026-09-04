# ============================================================================
# JZToolsHub 一键安装 / 更新 / 卸载脚本（Windows）
#
# 用法：
#   安装 / 更新：   powershell -ExecutionPolicy Bypass -File install.ps1
#   卸载：          powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#   卸载（保留用户数据）：powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall -KeepData
#   自定义安装目录：powershell -ExecutionPolicy Bypass -File install.ps1 -InstallDir D:\JZToolsHub
#
# 行为：
#   - 已安装  → 停止旧服务 → 更新项目文件 → 同步配置模板到用户数据根目录 → 重建快捷方式
#   - 未安装  → 完整安装到 %LOCALAPPDATA%\JZToolsHub（可 -InstallDir 覆盖）
#   - 卸载    → 停止服务 → 删除项目文件 → 删除用户数据根目录（默认；-KeepData 保留）
#
# 用户数据（数据根目录，默认 %USERPROFILE%\.jztoolshub）独立于程序目录存放，
# 升级时只同步「配置模板」（prompt.json / tools.json 等），账号、台账、文档、
# 公告等用户数据不丢失。卸载时默认一并删除用户数据根目录与指针文件。
# ============================================================================

param(
    [switch]$Uninstall,
    [switch]$KeepData,          # 卸载时保留用户数据根目录（默认删除）
    [string]$InstallDir = ""    # 安装目录；留空时用 %LOCALAPPDATA%\JZToolsHub
)
$ErrorActionPreference = "Stop"

$AppName  = "JZToolsHub"
$ExeName  = "$AppName.exe"
$Source   = $PSScriptRoot                 # 一键安装包所在目录（解压后的程序目录）
$RegKey   = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
$DataRoot = ""                            # 数据根目录，启动时解析
# 默认安装目录：%LOCALAPPDATA%\JZToolsHub（不能在 param 里用 $env:，这里兜底解析）
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = Join-Path $env:LOCALAPPDATA $AppName
}

# ---------------- 版本号 ----------------
$Version = "0.0.0"
$verFile = Join-Path $Source "version.json"
if (Test-Path $verFile) {
    try { $Version = (Get-Content $verFile -Raw | ConvertFrom-Json).app } catch {}
}

# ---------------- 数据根目录解析 ----------------
function Get-DataRootDir {
    param([string]$Target)
    # 主指针：用户目录 .jztoolshub.json（整体替换程序文件夹后仍可找到）
    $ptr = Join-Path $env:USERPROFILE ".jztoolshub.json"
    if (Test-Path $ptr) {
        try {
            $o = Get-Content $ptr -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($o.data_root) { return $o.data_root }
        } catch {}
    }
    # 备份指针：程序目录 config\data_root.json
    $bkp = Join-Path $Target "config\data_root.json"
    if (Test-Path $bkp) {
        try {
            $o = Get-Content $bkp -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($o.data_root) { return $o.data_root }
        } catch {}
    }
    return (Join-Path $env:USERPROFILE ".jztoolshub")
}

# ---------------- 进程控制 ----------------
function Stop-JZService {
    $p = Get-Process -Name $AppName -ErrorAction SilentlyContinue
    if ($p) { $p | Stop-Process -Force; Start-Sleep -Milliseconds 600 }
}

# ---------------- 已安装信息 ----------------
function Get-InstalledInfo {
    # 返回 @{ Dir; Version } 或 $null
    $reg = Get-ItemProperty -Path $RegKey -ErrorAction SilentlyContinue
    if ($reg -and (Test-Path $reg.InstallLocation)) {
        $v = $null
        $vf = Join-Path $reg.InstallLocation "version.json"
        if (Test-Path $vf) { try { $v = (Get-Content $vf -Raw | ConvertFrom-Json).app } catch {} }
        return @{ Dir = $reg.InstallLocation; Version = $v }
    }
    if (Test-Path (Join-Path $InstallDir $ExeName)) {
        $v = $null
        $vf = Join-Path $InstallDir "version.json"
        if (Test-Path $vf) { try { $v = (Get-Content $vf -Raw | ConvertFrom-Json).app } catch {} }
        return @{ Dir = $InstallDir; Version = $v }
    }
    return $null
}

# ---------------- 快捷方式 / 注册表 ----------------
function New-Shortcut {
    param([string]$Link, [string]$Target)
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($Link)
    $sc.TargetPath = $Target
    $sc.WorkingDirectory = (Split-Path $Target)
    $sc.Save()
}

function Write-Registry {
    param([string]$Dir)
    New-Item -Path $RegKey -Force | Out-Null
    New-ItemProperty -Path $RegKey -Name "DisplayName"      -Value "JZToolsHub 工具箱" -Force | Out-Null
    New-ItemProperty -Path $RegKey -Name "DisplayVersion"   -Value $Version -Force | Out-Null
    New-ItemProperty -Path $RegKey -Name "InstallLocation"  -Value $Dir -Force | Out-Null
    New-ItemProperty -Path $RegKey -Name "DisplayIcon"      -Value (Join-Path $Dir $ExeName) -Force | Out-Null
    New-ItemProperty -Path $RegKey -Name "UninstallString"  -Value "powershell -NoProfile -ExecutionPolicy Bypass -File `"$Dir\install.ps1`" -Uninstall" -Force | Out-Null
    New-ItemProperty -Path $RegKey -Name "Publisher"        -Value "JZToolsHub" -Force | Out-Null
    New-ItemProperty -Path $RegKey -Name "NoModify"         -Value 1 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $RegKey -Name "NoRepair"         -Value 1 -PropertyType DWord -Force | Out-Null
}

# ---------------- 配置模板同步（与 jztools_data.sync_templates 保持一致） ----------------
function ConvertTo-Utf8NoBom {
    param([string]$Path, [string]$Json)
    [System.IO.File]::WriteAllText($Path, $Json, (New-Object System.Text.UTF8Encoding($false)))
}

function Sync-ConfigTemplates {
    param([string]$SourceDir, [string]$DataDir, [string]$Version)

    # 1) overwrite：prompt.json 直接覆盖（旧文件备份 .bak-old）
    $promptPairs = @(
        @("plugins\case-report\backend\prompt.json",    "plugins\case-report\prompt.json"),
        @("plugins\character-graph\backend\prompt.json", "plugins\character-graph\prompt.json")
    )
    foreach ($pp in $promptPairs) {
        $src = Join-Path $SourceDir $pp[0]
        $dst = Join-Path $DataDir    $pp[1]
        if (-not (Test-Path $src)) { continue }
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
        if (Test-Path $dst) {
            $bak = "$dst.bak-old"
            if (-not (Test-Path $bak)) { Copy-Item $dst $bak -Force }
            Copy-Item $src $dst -Force
            Write-Host "  [同步] 已更新 $($pp[1])（旧版备份 .bak-old）"
        } else {
            Copy-Item $src $dst -Force
            Write-Host "  [同步] 已初始化 $($pp[1])"
        }
    }

    # 2) merge-tools：tools.json 合并（保留用户启停/排序/自定义）
    $srcTools = Join-Path $SourceDir "config\tools.json"
    $dstTools = Join-Path $DataDir    "config\tools.json"
    if (Test-Path $srcTools) {
        New-Item -ItemType Directory -Force -Path (Split-Path $dstTools) | Out-Null
        if (Test-Path $dstTools) {
            try {
                $t = Get-Content $srcTools -Raw -Encoding UTF8 | ConvertFrom-Json
                $u = Get-Content $dstTools -Raw -Encoding UTF8 | ConvertFrom-Json
                # site：模板为底，用户已有字段覆盖
                $site = @{}
                foreach ($p in $t.site.PSObject.Properties) { $site[$p.Name] = $p.Value }
                if ($u.site) { foreach ($p in $u.site.PSObject.Properties) { $site[$p.Name] = $p.Value } }
                # categories：按 id 合并，已有保留用户版本，新分类追加
                $cats = New-Object System.Collections.ArrayList
                $catIds = @{}
                if ($u.categories) { foreach ($c in $u.categories) { [void]$cats.Add($c); $catIds[$c.id] = $true } }
                if ($t.categories) { foreach ($c in $t.categories) { if (-not $catIds.ContainsKey($c.id)) { [void]$cats.Add($c); $catIds[$c.id] = $true } } }
                # tools：按 id 合并，已有保留用户版本，新工具追加
                $tools = New-Object System.Collections.ArrayList
                $toolIds = @{}
                if ($u.tools) { foreach ($tl in $u.tools) { [void]$tools.Add($tl); $toolIds[$tl.id] = $true } }
                if ($t.tools) { foreach ($tl in $t.tools) { if (-not $toolIds.ContainsKey($tl.id)) { [void]$tools.Add($tl); $toolIds[$tl.id] = $true } } }
                $merged = @{ site = $site; categories = $cats; tools = $tools }
                $json = $merged | ConvertTo-Json -Depth 20
                ConvertTo-Utf8NoBom -Path $dstTools -Json $json
                Write-Host "  [同步] 已合并 config\tools.json（保留用户启停/排序）"
            } catch {
                Write-Host "  [同步] tools.json 合并失败，保留原配置：$($_.Exception.Message)"
            }
        } else {
            Copy-Item $srcTools $dstTools -Force
            Write-Host "  [同步] 已初始化 config\tools.json"
        }
    }

    # 3) ensure-keys：插件 config.json 仅补缺失键（保留用户 LLM 配置）
    $cfgPairs = @(
        @("plugins\case-report\backend\config.json",    "plugins\case-report\config.json"),
        @("plugins\character-graph\backend\config.json", "plugins\character-graph\config.json")
    )
    foreach ($cp in $cfgPairs) {
        $src = Join-Path $SourceDir $cp[0]
        $dst = Join-Path $DataDir    $cp[1]
        if (-not (Test-Path $src)) { continue }
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
        if (Test-Path $dst) {
            try {
                $t = Get-Content $src -Raw -Encoding UTF8 | ConvertFrom-Json
                $u = Get-Content $dst -Raw -Encoding UTF8 | ConvertFrom-Json
                # 用 Newtonsoft 不存在，手动深层补键：简单地把模板键补充到用户配置（不覆盖已有）
                $merged = @{}
                foreach ($p in $t.PSObject.Properties) { $merged[$p.Name] = $p.Value }
                foreach ($p in $u.PSObject.Properties) { $merged[$p.Name] = $p.Value }
                $json = $merged | ConvertTo-Json -Depth 20
                ConvertTo-Utf8NoBom -Path $dst -Json $json
                Write-Host "  [同步] 已补全 $($cp[1])（保留用户 LLM 配置）"
            } catch {
                Write-Host "  [同步] $($cp[1]) 处理失败，保留原配置：$($_.Exception.Message)"
            }
        } else {
            Copy-Item $src $dst -Force
            Write-Host "  [同步] 已初始化 $($cp[1])"
        }
    }
}

# ============================================================================
#  卸载
# ============================================================================
if ($Uninstall) {
    Write-Host "==> 正在卸载 $AppName ..."
    Stop-JZService

    $info = Get-InstalledInfo
    $target = if ($info) { $info.Dir } else { $InstallDir }
    $DataRoot = Get-DataRootDir -Target $target

    # 清理快捷方式
    $links = @(
        (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"),
        (Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk")
    )
    foreach ($l in $links) { if (Test-Path $l) { Remove-Item $l -Force } }

    # 删除程序目录
    if (Test-Path $target) {
        Remove-Item $target -Recurse -Force
        Write-Host "  已删除程序目录：$target"
    }

    # 删除用户数据根目录（默认删除，-KeepData 保留）
    if (-not $KeepData) {
        if (Test-Path $DataRoot) {
            Remove-Item $DataRoot -Recurse -Force
            Write-Host "  已删除用户数据根目录：$DataRoot"
        }
        $ptr = Join-Path $env:USERPROFILE ".jztoolshub.json"
        if (Test-Path $ptr) { Remove-Item $ptr -Force; Write-Host "  已删除数据目录指针：$ptr" }
    } else {
        Write-Host "  已保留用户数据根目录：$DataRoot"
    }

    # 清理注册表
    Remove-Item -Path $RegKey -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "==> 卸载完成。"
    exit 0
}

# ============================================================================
#  安装 / 更新
# ============================================================================
Write-Host "==> JZToolsHub 安装 / 更新（版本 $Version）..."
Stop-JZService

# 源目录必须是完整安装包（含 JZToolsHub.exe）。在源码仓库 / 开发目录里误跑时
# 尽早报错退出：仓库根目录常带有开发运行产生的 config\data_root.json（备份指针），
# 会被误判为「既有安装目录 → 就地更新」，最后因缺 exe 才报错，提示语令人困惑。
if (-not (Test-Path (Join-Path $Source $ExeName))) {
    Write-Host ""
    Write-Host "  [失败] 当前目录不是一键安装包：$Source"
    Write-Host "  该目录下没有 $ExeName。请解压 JZToolsHub-v*.zip 后，"
    Write-Host "  在解压出的目录内双击「一键安装.bat」重新执行。"
    exit 1
}

$info = Get-InstalledInfo
if ($info) {
    Write-Host "  检测到已安装：$($info.Dir)（版本 $($info.Version)）→ 更新到 $Version"
    $Target = $info.Dir
} elseif (Test-Path (Join-Path $Source "config\data_root.json")) {
    # 源目录本身是既有部署（含数据目录备份指针，说明曾被运行/安装于此）→ 就地更新
    # 注意：走到这里源目录已确认含 JZToolsHub.exe（上方守卫），
    # 源码仓库/开发目录不会落入本分支。
    Write-Host "  检测到当前目录为既有安装目录 → 就地更新到：$Source"
    $Target = $Source
} else {
    Write-Host "  未检测到安装 → 全新安装到：$InstallDir"
    $Target = $InstallDir
}

New-Item -ItemType Directory -Force -Path $Target | Out-Null

# ---- 复制项目文件（源为解压目录时复制；源=目标则就地更新） ----
$SourceFull = [System.IO.Path]::GetFullPath($Source)
$TargetFull = [System.IO.Path]::GetFullPath($Target)
if ($SourceFull -ne $TargetFull) {
    Write-Host "  正在复制项目文件 → $Target"
    $exclude = @(".zcode", "deploy", "dist", "build", "__pycache__")
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        if ($_.Name -in $exclude) { return }
        Copy-Item -LiteralPath $_.FullName -Destination $Target -Recurse -Force
    }
} else {
    Write-Host "  就地更新（源目录即为安装目录），跳过文件复制"
}

# 确保关键文件在
$exe = Join-Path $Target $ExeName
if (-not (Test-Path $exe)) { throw "安装目录缺少 $ExeName，请确认在一键安装包（含 exe 的目录）内执行" }

# ---- 写版本号 ----
$verObj = @{ app = $Version; schema = 1 }
ConvertTo-Utf8NoBom -Path (Join-Path $Target "version.json") -Json ($verObj | ConvertTo-Json)

# ---- 解析数据根目录并同步配置模板 ----
$DataRoot = Get-DataRootDir -Target $Target
Write-Host "  用户数据根目录：$DataRoot"
Sync-ConfigTemplates -SourceDir $Source -DataDir $DataRoot -Version $Version

# ---- 注册表与快捷方式 ----
Write-Registry -Dir $Target
$lnkDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
New-Item -ItemType Directory -Force -Path $lnkDir | Out-Null
New-Shortcut -Link (Join-Path $lnkDir "$AppName.lnk") -Target $exe
New-Shortcut -Link (Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk") -Target $exe
Write-Host "  已创建开始菜单与桌面快捷方式"

Write-Host ""
Write-Host "==> 安装 / 更新完成。"
Write-Host "    双击桌面「$AppName」或运行 start.bat 启动（浏览器访问 http://localhost:5000）"
Write-Host "    默认管理员：admin / admin123（首启自动生成，请登录后尽快改密）"
Write-Host "    用户数据保存在：$DataRoot"
Write-Host "    如需卸载：双击「一键卸载.bat」（将同时删除用户数据，可用 -KeepData 保留）"
