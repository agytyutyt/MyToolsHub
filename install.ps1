# ============================================================================
# JZToolsHub 一键安装 / 更新 / 卸载脚本（Windows）
#
# 用法：
#   安装 / 更新：   powershell -ExecutionPolicy Bypass -File install.ps1
#   卸载：          powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#   卸载（保留用户数据）：powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall -KeepData
#   自定义安装目录：powershell -ExecutionPolicy Bypass -File install.ps1 -InstallDir D:\JZToolsHub
#   强制以主包为准：powershell -ExecutionPolicy Bypass -File install.ps1 -ForcePluginOverwrite
#   绿色模式（不写注册表/不建快捷方式）：powershell -ExecutionPolicy Bypass -File install.ps1 -NoRegistry
#
# 行为：
#   - 已安装  → 停止旧服务 → 更新项目文件 → 同步配置模板到用户数据根目录 → 重建快捷方式
#   - 未安装  → 完整安装到 %LOCALAPPDATA%\JZToolsHub（可 -InstallDir 覆盖）
#   - 卸载    → 停止服务 → 删除项目文件 → 删除用户数据根目录（默认；-KeepData 保留）
#
# 插件版本防回退：plugins\ 下已用「插件包」单独升级到更高版本的插件会被跳过（保持现状），
# 避免整包更新把它们静默降级；确需以主包为准时加 -ForcePluginOverwrite。
#
# 用户数据（数据根目录，默认 %USERPROFILE%\.jztoolshub）独立于程序目录存放，
# 升级时只同步「配置模板」（prompt.json / tools.json 等），账号、台账、文档、
# 公告等用户数据不丢失。卸载时默认一并删除用户数据根目录与指针文件。
# ============================================================================

param(
    [switch]$Uninstall,
    [switch]$KeepData,          # 卸载时保留用户数据根目录（默认删除）
    [switch]$ForcePluginOverwrite,  # 更新时不跳过"已单独升级到更高版本"的插件（默认跳过，防回退）
    [switch]$NoRegistry,        # 绿色模式：不写注册表、不建快捷方式（受管环境/移动介质部署）
    [string]$InstallDir = "",   # 安装目录；留空时用 %LOCALAPPDATA%\JZToolsHub
    [string]$DataRoot = ""      # 数据根目录；留空时按 .jztoolshub.json 指针自动解析
)
$ErrorActionPreference = "Stop"

$AppName  = "JZToolsHub"
$ExeName  = "$AppName.exe"
$Source   = $PSScriptRoot                 # 一键安装包所在目录（解压后的程序目录）
$RegKey   = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"
# $DataRoot 由参数给出时以参数为准；为空则启动时按指针解析（见 Get-DataRootDir）
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
    # 显式 -DataRoot 优先（数据根不在默认位置 / 受管环境）
    if (-not [string]::IsNullOrWhiteSpace($script:DataRoot)) {
        return [System.IO.Path]::GetFullPath($script:DataRoot)
    }
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

# ---------------- 语义化版本比较（插件版本防回退用） ----------------
# 返回 -1 / 0 / 1；任一侧不是 主.次.修订 时返回 $null（调用方按"不参与比较"处理）
function Compare-SemVer {
    param([string]$A, [string]$B)
    if ($A -notmatch '^(\d+)\.(\d+)\.(\d+)') { return $null }
    $pa = @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
    if ($B -notmatch '^(\d+)\.(\d+)\.(\d+)') { return $null }
    $pb = @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
    for ($i = 0; $i -lt 3; $i++) {
        if ($pa[$i] -gt $pb[$i]) { return 1 }
        if ($pa[$i] -lt $pb[$i]) { return -1 }
    }
    return 0
}

# ---------------- 已安装信息 ----------------
function Get-InstalledInfo {
    # 返回 @{ Dir; Version } 或 $null
    # 显式 -InstallDir 且该目录已是安装目录 → 以它为准（否则本机注册表登记过其它位置时会改错地方）
    if (-not [string]::IsNullOrWhiteSpace($script:InstallDir) -and (Test-Path (Join-Path $script:InstallDir $ExeName))) {
        $v = $null
        $vf = Join-Path $script:InstallDir "version.json"
        if (Test-Path $vf) { try { $v = (Get-Content $vf -Raw | ConvertFrom-Json).app } catch {} }
        return @{ Dir = $script:InstallDir; Version = $v }
    }
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

    # 1) overwrite：已取消。
    #    历史上此处同步 plugins\<id>\backend\prompt.json → 数据根 plugins\<id>\prompt.json，
    #    但提示词模板已删除、改为插件内 llm_client 的内置默认值（代码即唯一来源），
    #    再引入文件模板会造成「双来源」。用户若需在目标机调提示词，直接手写
    #    <数据根>\plugins\<id>\prompt.json 即可——插件的 load_prompt() 存在即优先读取，
    #    且从此不再被版本升级覆盖。

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

    # 3) ensure-keys：插件 config.json 仅补缺失键（保留用户 LLM 配置与自定义阈值）
    # ★ 模板一律命名 config.template.json：打包与安装过程中插件树内的 config.json
    #   会被当作「本机运行时配置」清理，模板若同名会被连带删除（历史缺陷，见
    #   docs/archive/P0问题修复方案.md FIX-1）。新增带模板的插件时，本清单须与
    #   jztools_data._TEMPLATE_SYNC 同时登记且保持一致。
    $cfgPairs = @(
        @("plugins\case-report\backend\config.template.json",      "plugins\case-report\config.json"),
        @("plugins\character-graph\backend\config.template.json",  "plugins\character-graph\config.json"),
        @("plugins\trajectory-sketch\backend\config.template.json","plugins\trajectory-sketch\config.json"),
        @("plugins\file-filter\backend\config.template.json",      "plugins\file-filter\config.json")
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
                # 递归补键（与 jztools_data._ensure_deep_keys 语义一致：只补缺失、不覆盖已有）
                Merge-DeepKeys -User $u -Template $t
                $json = $u | ConvertTo-Json -Depth 20
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

# 递归补键：把模板中「用户配置缺失的键」补进用户配置，已有值一律不覆盖。
# 与 Python 侧 jztools_data._ensure_deep_keys() 语义必须保持一致（此前只并顶层键，
# 模板在嵌套层新增键时一键安装路径补不上，与 app 启动同步路径行为分叉）。
function Merge-DeepKeys {
    param($User, $Template)
    foreach ($p in $Template.PSObject.Properties) {
        if ($User.PSObject.Properties.Name -contains $p.Name) {
            if ($p.Value -is [PSCustomObject] -and $User.($p.Name) -is [PSCustomObject]) {
                Merge-DeepKeys -User $User.($p.Name) -Template $p.Value
            }
            # 用户已有标量/数组值：保留
        } else {
            $User | Add-Member -NotePropertyName $p.Name -NotePropertyValue $p.Value -Force
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

    # 清理快捷方式（桌面路径可能为空：重定向 profile / 服务账户等环境）
    $links = @( (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk") )
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not [string]::IsNullOrWhiteSpace($desktop)) {
        $links += (Join-Path $desktop "$AppName.lnk")
    }
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

# ---- 插件版本防回退（单独升级过的插件不被整包覆盖回去）----
# 背景：本脚本把 plugins\ 整体覆盖到目标目录。若某个插件已用「插件包」单独升级到更高版本，
# 直接覆盖会把它静默降级回主包内嵌版本（见 docs\design\插件独立升级方案-设计文档.md §7）。
# 规则：数据根状态登记（config\.app_state.json → plugins.<id>.version）高于主包内嵌版本 → 跳过该插件；
#       确需"以主包为准"覆盖时用 -ForcePluginOverwrite。
$statePath = Join-Path (Get-DataRootDir -Target $Target) "config\.app_state.json"
$skipPlugins = @{}
if ((Test-Path -LiteralPath $statePath) -and (-not $ForcePluginOverwrite)) {
    try {
        $stateObj = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($stateObj.plugins) {
            foreach ($p in $stateObj.plugins.PSObject.Properties) {
                $pluginId = $p.Name
                $regVer = [string]$p.Value.version
                $srcMan = Join-Path (Join-Path $Source "plugins\$pluginId") "manifest.json"
                if (-not $regVer -or -not (Test-Path -LiteralPath $srcMan)) { continue }
                $pkgVer = ""
                try { $pkgVer = [string]((Get-Content -LiteralPath $srcMan -Raw -Encoding UTF8 | ConvertFrom-Json).version) } catch { }
                if (-not $pkgVer) { continue }
                if ((Compare-SemVer $regVer $pkgVer) -eq 1) { $skipPlugins[$pluginId] = @{ reg = $regVer; pkg = $pkgVer } }
            }
        }
    } catch {
        Write-Host "  [提示] 插件版本防回退检查已跳过（状态文件不可读）：$($_.Exception.Message)"
    }
}

# ---- 复制项目文件（源为解压目录时复制；源=目标则就地更新） ----
$SourceFull = [System.IO.Path]::GetFullPath($Source)
$TargetFull = [System.IO.Path]::GetFullPath($Target)
if ($SourceFull -ne $TargetFull) {
    Write-Host "  正在复制项目文件 → $Target"
    if ($skipPlugins.Count -gt 0) {
        Write-Host "  ---- 插件版本防回退 ----"
        foreach ($k in $skipPlugins.Keys) {
            Write-Host "    跳过 $k：已单独升级到 $($skipPlugins[$k].reg)（主包内嵌 $($skipPlugins[$k].pkg)）→ 保持现状"
        }
        Write-Host "    如需以主包为准覆盖这些插件，请加 -ForcePluginOverwrite"
    }
    $exclude = @(".zcode", "deploy", "dist", "build", "__pycache__")
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        if ($_.Name -in $exclude) { return }
        if ($_.Name -eq "plugins" -and $skipPlugins.Count -gt 0) {
            # 逐插件复制，跳过受保护插件（其余插件照常更新）
            $dstPlugins = Join-Path $Target "plugins"
            New-Item -ItemType Directory -Force -Path $dstPlugins | Out-Null
            Get-ChildItem -LiteralPath $_.FullName -Force | ForEach-Object {
                if ($skipPlugins.ContainsKey($_.Name)) { return }
                Copy-Item -LiteralPath $_.FullName -Destination $dstPlugins -Recurse -Force
            }
            return
        }
        Copy-Item -LiteralPath $_.FullName -Destination $Target -Recurse -Force
    }

    # 状态登记校正：被主包覆盖的插件（含 -ForcePluginOverwrite 场景），登记版本改为主包内嵌版本，
    # 否则登记与实际不符，下次插件包升级的版本判定会失真。
    if (Test-Path -LiteralPath $statePath) {
        try {
            $stateObj = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $fixed = 0
            if ($stateObj.plugins) {
                foreach ($p in @($stateObj.plugins.PSObject.Properties)) {
                    $pluginId = $p.Name
                    if ($skipPlugins.ContainsKey($pluginId)) { continue }
                    $srcMan = Join-Path (Join-Path $Source "plugins\$pluginId") "manifest.json"
                    if (-not (Test-Path -LiteralPath $srcMan)) { continue }
                    $pkgVer = ""
                    try { $pkgVer = [string]((Get-Content -LiteralPath $srcMan -Raw -Encoding UTF8 | ConvertFrom-Json).version) } catch { }
                    if (-not $pkgVer) { continue }
                    if ([string]$p.Value.version -ne $pkgVer) {
                        $p.Value | Add-Member -NotePropertyName version -NotePropertyValue $pkgVer -Force
                        $p.Value | Add-Member -NotePropertyName installed_by -NotePropertyValue "install.ps1/$Version" -Force
                        $p.Value | Add-Member -NotePropertyName installed_at -NotePropertyValue (Get-Date -Format "o") -Force
                        $fixed++
                    }
                }
            }
            if ($fixed -gt 0) {
                [System.IO.File]::WriteAllText($statePath, ($stateObj | ConvertTo-Json -Depth 20), (New-Object System.Text.UTF8Encoding($false)))
                Write-Host "  已校正 $fixed 个插件的状态登记（以主包内嵌版本为准）"
            }
        } catch {
            Write-Host "  [提示] 状态登记校正失败（不影响安装）：$($_.Exception.Message)"
        }
    }
} else {
    Write-Host "  就地更新（源目录即为安装目录），跳过文件复制"
}

# 确保关键文件在
$exe = Join-Path $Target $ExeName
if (-not (Test-Path $exe)) { throw "安装目录缺少 $ExeName，请确认在一键安装包（含 exe 的目录）内执行" }

# ---- 写版本号（保留打包期元信息：commit / built_at / python / offline）----
# 此前这里直接覆盖为 {app, schema}，会把 build-deploy.ps1 写入的溯源字段抹掉，
# 导致装完之后再也无法从 version.json 判断包内代码出自哪个提交（可维护性隐患）。
$verFile = Join-Path $Target "version.json"
$verObj = @{ app = $Version; schema = 1 }
if (Test-Path $verFile) {
    try {
        $cur = Get-Content $verFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($p in $cur.PSObject.Properties) {
            if (-not $verObj.ContainsKey($p.Name)) { $verObj[$p.Name] = $p.Value }
        }
    } catch {}
}
$verObj["app"] = $Version
ConvertTo-Utf8NoBom -Path $verFile -Json ($verObj | ConvertTo-Json)

# ---- 解析数据根目录并同步配置模板 ----
$DataRoot = Get-DataRootDir -Target $Target
Write-Host "  用户数据根目录：$DataRoot"
Sync-ConfigTemplates -SourceDir $Source -DataDir $DataRoot -Version $Version

# ---- 离线运行组件（Chrome / LibreOffice）----
# 组件默认**不**随主包分发（主包瘦身）。判断依据用**源包**是否带 runtime\manifest.json，
# 而不是目标目录下有没有 setup 脚本：目标目录可能残留上一版胖包的 runtime\，
# 若按目标目录判断，就会在每次更新时被旧脚本误触发一遍。
$rtDir     = Join-Path $Target "runtime"
$rtSetup   = Join-Path $rtDir "setup-offline-runtime.ps1"
$rtCarried = Test-Path (Join-Path $Source "runtime\manifest.json")
if ($rtCarried -and (Test-Path $rtSetup)) {
    Write-Host ""
    Write-Host "  ---- 离线运行组件（Chrome / LibreOffice）----"
    # 用子进程执行：setup 脚本以 exit 结束，dot-source 会连带终止本次安装流程
    & powershell -NoProfile -ExecutionPolicy Bypass -File $rtSetup -RuntimeDir $rtDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [注意] 离线组件处理存在失败项（退出码 $LASTEXITCODE），不影响工具箱本体使用。"
        Write-Host "         可稍后重跑：powershell -ExecutionPolicy Bypass -File `"$rtSetup`""
    }
} else {
    Write-Host ""
    Write-Host "  ---- 离线运行组件（本包未携带，按需另装）----"
    Write-Host "    浏览器    ：用目标机已有的 Chrome / Edge 即可，无需额外安装"
    Write-Host "    高保真预览：解压「离线组件包」后双击「安装LibreOffice核心组件.bat」"
    Write-Host "                （免管理员、无快捷方式、不改文件关联与默认应用，对使用者不可见）"
}

# ---- 注册表与快捷方式（-NoRegistry 绿色模式：两者都跳过） ----
if (-not $NoRegistry) {
    Write-Registry -Dir $Target
    $lnkDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    New-Item -ItemType Directory -Force -Path $lnkDir | Out-Null
    New-Shortcut -Link (Join-Path $lnkDir "$AppName.lnk") -Target $exe
    # 桌面路径可能为空（重定向 profile / 服务账户）：为空时跳过桌面快捷方式
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not [string]::IsNullOrWhiteSpace($desktop)) {
        New-Shortcut -Link (Join-Path $desktop "$AppName.lnk") -Target $exe
        Write-Host "  已创建开始菜单与桌面快捷方式"
    } else {
        Write-Host "  已创建开始菜单快捷方式（桌面路径不可用，跳过桌面快捷方式）"
    }
} else {
    Write-Host "  [-NoRegistry] 绿色模式：未写注册表、未建快捷方式"
}

Write-Host ""
Write-Host "==> 安装 / 更新完成。"
Write-Host "    双击桌面「$AppName」或运行 start.bat 启动（浏览器访问 http://localhost:5000）"
Write-Host "    默认管理员：admin / admin123（首启自动生成，请登录后尽快改密）"
Write-Host "    用户数据保存在：$DataRoot"
Write-Host "    如需卸载：双击「一键卸载.bat」（将同时删除用户数据，可用 -KeepData 保留）"
