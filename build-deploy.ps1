# JZToolsHub 打包部署脚本
# 产物：deploy\JZToolsHub\ —— 后端单目录可执行程序（JZToolsHub.exe + _internal/）
#       + 前端源码（static/、plugins/）+ 配置（config/）+ 一键启动脚本（start.bat）
#       + 一键安装/更新（一键安装.bat + install.ps1）+ 一键卸载（一键卸载.bat）+ 版本号（version.json）
# 用法：powershell -ExecutionPolicy Bypass -File build-deploy.ps1 [-Python "python"] [-DeployName "JZToolsHub"] [-Version "1.3.6"] [-Force]
#       默认用 PATH 上的 python 打包、输出到 deploy\JZToolsHub\；
#       可用 -Python 指定其他解释器（如 Python 3.8：C:\...\Python38\python.exe）、
#       用 -DeployName 指定不同的部署目录名（如 py38 版输出到 deploy\JZToolsHub-py38）、
#       用 -Version 指定版本号（写入 version.json，一键安装脚本据此判断更新）。
#       未传 -Version 时自动在上一版基础上递增 patch；若最终版本号与上一版相同会直接报错中止
#       （版本号不变 → 目标机 sync_templates() 判定未升级 → 全部配置模板同步被跳过），
#       确需同号重打时加 -Force。
#       -Python 默认取 PATH 上的 python；脚本会记录其版本到 version.json，并在与基线
#       3.14 不一致时告警（用旧解释器打包会打出版本残缺却无人察觉的包）。
#       默认会把仓库 runtime\ 下的离线运行组件（Chrome / LibreOffice 安装包）一并打进包，
#       供无外网目标机部署；只想做瘦包时加 -SkipOfflineRuntime。
#       LibreOffice 若已生成裁剪核心包（runtime\libreoffice\libreoffice-core.zip，
#       用 tools\build-libreoffice-core.py 生成），打包时默认「用它替代原始 MSI」，
#       包内体积由 357.5 MB 降到约 166 MB；仍要随包带完整 MSI 时加 -KeepFullLibreOffice。
param(
    [string]$Python = "python",
    [string]$DeployName = "JZToolsHub",
    [string]$Version = "",
    [switch]$Force,
    [switch]$SkipOfflineRuntime,   # 不打包 runtime\ 离线运行组件（产出瘦包）
    [switch]$KeepFullLibreOffice,  # 保留原始 LibreOffice MSI（不用裁剪核心包替代）
    [switch]$ZipOnly               # 跳过 PyInstaller 与目录组装，仅用既有部署目录重生成 zip
)
$ErrorActionPreference = "Stop"

# 打包解释器基线（与 Windows 10+ 目标机配套；见 docs/Python版本选型评估.md）
$PyBaseline = "3.14"

$Root    = $PSScriptRoot
$Dist    = Join-Path $Root "dist"
$Deploy  = Join-Path $Root "deploy"
$AppDir  = Join-Path $Deploy $DeployName
$WorkDir = Join-Path $Root "build"

# 打包解释器及其版本（尽早取，便于出错时定位）
$PyExe = (Get-Command $Python -ErrorAction SilentlyContinue).Source
if (-not $PyExe) { $PyExe = $Python }
$PyVer = ""
try { $PyVer = (& $Python -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null | Select-Object -First 1) } catch {}
if (-not $PyVer) { $PyVer = "unknown" }
$PyShort = if ($PyVer -match '^(\d+\.\d+)') { $matches[1] } else { "unknown" }
Write-Host "  [解释器] $Python → $PyVer（$PyExe）"
if ($PyShort -ne $PyBaseline) {
    Write-Warning "打包解释器版本 $PyShort 与基线 $PyBaseline 不一致！产物可能功能残缺（如知识库 Office 预览引擎）。建议用 Python $PyBaseline 打包。"
}

# 上一版版本号：读取既有部署目录的 version.json（必须在下方清理旧产物之前读取）
$PrevVer = ""
$oldVerFile = Join-Path $AppDir "version.json"
if (Test-Path $oldVerFile) {
    try { $PrevVer = (Get-Content $oldVerFile -Raw | ConvertFrom-Json).app } catch {}
}

# -ZipOnly：只改了文档 / 前端时不必重跑 PyInstaller（省十几分钟），
#           直接用既有 deploy\<DeployName>\ 重新压缩。版本号沿用部署目录里的值。
if ($ZipOnly) {
    $exePath = Join-Path $AppDir "JZToolsHub.exe"
    if (-not (Test-Path $exePath)) {
        throw "-ZipOnly 需要既有的完整部署目录，但 $exePath 不存在。请先正常打包一次。"
    }
    if (-not $PrevVer) { throw "-ZipOnly 无法从 $oldVerFile 读到版本号。" }
    $Version = $PrevVer
    # 注意：变量名不能叫 $zipOnly —— PowerShell 变量名不区分大小写，
    # 会与开关参数 -ZipOnly 撞成同一个变量（赋值字符串给 SwitchParameter 直接报错）。
    $zipPath = Join-Path $Deploy "JZToolsHub-v$Version.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Write-Host "==> [-ZipOnly] 压缩既有部署目录（版本 $Version，跳过打包与组装）..."
    Write-Host "    提示：部署目录里若还是旧文档，请先从仓库复制最新文件进去。"
    Compress-Archive -Path (Join-Path $AppDir "*") -DestinationPath $zipPath -CompressionLevel Fastest
    Write-Host "==> 已生成安装包：$zipPath（$([math]::Round((Get-Item $zipPath).Length/1MB,1)) MB）"
    exit 0
}

# 版本号：-Version 未指定时自动递增 patch（1.6 → 1.7）
if (-not $Version) {
    if     ($PrevVer -match '^(\d+)\.(\d+)$')        { $Version = "{0}.{1}"     -f $matches[1], ([int]$matches[2] + 1) }
    elseif ($PrevVer -match '^(\d+)\.(\d+)\.(\d+)$') { $Version = "{0}.{1}.{2}" -f $matches[1], $matches[2], ([int]$matches[3] + 1) }
    else                                             { $Version = "1.0.0" }
    Write-Host "  [版本] 未指定 -Version，自动递增为 $Version（上一版：$($PrevVer)）"
}
if ($PrevVer -and $Version -eq $PrevVer -and -not $Force) {
    throw "版本号与上一版相同（$Version）→ 目标机模板同步不会触发。请指定更大的 -Version，或加 -Force 强制同号重打。"
}

# 1. 清理旧产物（只清理本次目标，保留其他 DeployName 的旧版本共存）
if (Test-Path $Dist)  { Remove-Item -Recurse -Force $Dist }
if (Test-Path $AppDir){ Remove-Item -Recurse -Force $AppDir }
if (Test-Path $WorkDir){ Remove-Item -Recurse -Force $WorkDir }

# 1.5 前置检查：打包解释器必须已装齐 spec 里 collect_all 的第三方库
#     （插件后端由 importlib 动态加载，PyInstaller 静态扫描看不到；缺库不会报错，
#       只会打出功能残缺的包，所以在这里显式拦一下）
Write-Host "==> 检查打包解释器的依赖完整性..."
$depCheck = @"
import importlib
pkgs = ['waitress','cryptography','requests','docx','openpyxl','xlrd','olefile',
        'qrcode','zfec','cv2','numpy','pypdf','pystray','PIL']
missing = []
for p in pkgs:
    try:
        importlib.import_module(p)
    except Exception as e:
        missing.append(f'{p} ({type(e).__name__})')
print('|'.join(missing))
"@
$missingDeps = (& $Python -c $depCheck 2>$null | Select-Object -First 1)
if ($missingDeps) {
    Write-Warning ("打包解释器缺少以下依赖，产物将缺少对应功能：`n  " + ($missingDeps -replace '\|', "`n  "))
    Write-Warning "提示：zfec 无 Python 3.14 官方 wheel，需先 pip install --find-links wheels zfec（见 wheels/README.md）"
} else {
    Write-Host "    依赖完整（14/14）"
}

# 2. PyInstaller 打包后端（单目录：exe + _internal/）
Write-Host "==> PyInstaller 打包后端（$Python）..."
& $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $WorkDir (Join-Path $Root "JZToolsHub.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（exit $LASTEXITCODE）" }

# 3. 组装部署目录
Write-Host "==> 组装部署目录：$AppDir"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null

# 3.1 exe + _internal
Copy-Item -Recurse -Force (Join-Path $Dist "JZToolsHub\*") $AppDir

# 3.2 前端与插件源码（可修改）、配置模板、文档
Copy-Item -Recurse -Force (Join-Path $Root "static")  $AppDir
Copy-Item -Recurse -Force (Join-Path $Root "plugins") $AppDir
# wheels/tools 随包分发：体积很小（约百 KB），便于后续在部署目录里重装/重建 zfec
# （目标是冻结 exe 运行时不依赖它们，但 README §2.3 与 wheels/README.md 的说明要可用）
foreach ($extra in @("wheels", "tools")) {
    $p = Join-Path $Root $extra
    if (Test-Path $p) { Copy-Item -Recurse -Force $p $AppDir }
}
Copy-Item -Recurse -Force (Join-Path $Root "docs")    $AppDir
Copy-Item -Force (Join-Path $Root "README.md") $AppDir
Copy-Item -Force (Join-Path $Root "HANDOFF.md") $AppDir
# 顶层契约文档（README/HANDOFF 会引用它们，此前漏拷导致部署包内引用悬空）
foreach ($doc in @("插件设计规范.md", "移动端APP.md")) {
    $p = Join-Path $Root $doc
    if (Test-Path $p) { Copy-Item -Force $p $AppDir }
}

# config 仅复制 tools.json 模板（admin.json / .admin_key 属密钥，首启自动生成）
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "config") | Out-Null
Copy-Item -Force (Join-Path $Root "config\tools.json") (Join-Path $AppDir "config")

# 清理插件目录中的运行时数据 / 密钥 / 缓存（全新部署由程序自动重建）
# out 为插件本地测试产物目录（如 knowledge-base 渲染引擎的目检样例，已 gitignore）
# ★ 只删精确名 config.json（本机运行时配置，含 API Key）——配置模板一律命名
#   *.template.json，与运行时配置分离，不会被这条规则命中（历史教训见
#   docs/P0问题修复方案.md FIX-1：模板曾叫 config.json，被此处删掉导致同步链路静默失效）。
$pluginDir = Join-Path $AppDir "plugins"
if (Test-Path $pluginDir) {
  Get-ChildItem -Recurse -Directory $pluginDir |
    Where-Object { $_.Name -in @("data", ".task_cache", "__pycache__", "out") } |
    Remove-Item -Recurse -Force
  Get-ChildItem -Recurse -File $pluginDir |
    Where-Object { $_.Name -like "*.pyc" -or $_.Name -eq "config.json" } |
    Remove-Item -Force
}

# 3.2.1 模板自检：清理后必须仍保留同步模板（防止未来有人把模板又命名回 config.json）
$tpl = @(Get-ChildItem -Recurse -File $pluginDir -Filter *.template.json -ErrorAction SilentlyContinue)
$tplExpect = 4   # case-report / character-graph / file-filter / trajectory-sketch
if ($tpl.Count -lt $tplExpect) {
    throw "打包清理异常：包内配置模板仅剩 $($tpl.Count) 个（期望 >= $tplExpect）。" +
          "请检查上方清理规则是否误删了 *.template.json（模板不得命名为 config.json）。"
}
Write-Host "  模板自检通过：$($tpl.Count) 个 *.template.json 已保留"

# 3.2.2 离线运行组件（Chrome / LibreOffice 安装包 + 随包安装脚本）
#   源 A：<仓库>\runtime\           —— 由 tools/fetch-offline-bundle.py 从官方源下载（已 gitignore）
#        其中 libreoffice\libreoffice-core.zip 由 tools\build-libreoffice-core.py 生成（裁剪核心包）
#   源 B：tools\offline-runtime\    —— 随包脚本与说明（入库，随每版一起更新）
#   组装后：<AppDir>\runtime\{ manifest.json, README.md, 安装离线组件.bat, setup-offline-runtime.ps1,
#                              chrome\*.msi, libreoffice\{libreoffice-core.zip,.json} 或 libreoffice\*.msi }
if ($SkipOfflineRuntime) {
    Write-Host "==> 跳过离线运行组件（-SkipOfflineRuntime）"
} else {
    Write-Host "==> 组装离线运行组件（Chrome / LibreOffice）..."
    $rtSrc   = Join-Path $Root "runtime"
    $rtDst   = Join-Path $AppDir "runtime"
    $rtTools = Join-Path $Root "tools\offline-runtime"
    New-Item -ItemType Directory -Force -Path $rtDst | Out-Null

    if (Test-Path $rtSrc) {
        Copy-Item -Recurse -Force (Join-Path $rtSrc "*") $rtDst
    } else {
        Write-Warning "  未找到 $rtSrc —— 本次产出的包不含离线组件，目标机需自备浏览器与 LibreOffice。"
        Write-Warning "  先在有网机器执行：python tools\fetch-offline-bundle.py"
    }
    if (Test-Path $rtTools) { Copy-Item -Recurse -Force (Join-Path $rtTools "*") $rtDst }

    $rtMf   = Join-Path $rtDst "manifest.json"
    $coreZm = $null

    # --- LibreOffice：默认用「裁剪核心包」替代原始 MSI（357.5 MB → 约 166 MB）---
    #     核心包由 tools\build-libreoffice-core.py 生成：官方 MSI 管理安装解包后裁掉
    #     词典/语言包/图标主题/字体等死重，只留 .doc→.docx 与 .xls→.xlsx 需要的那套。
    $loDstDir = Join-Path $rtDst "libreoffice"
    $coreZip  = Join-Path $loDstDir "libreoffice-core.zip"
    $coreMeta = Join-Path $loDstDir "libreoffice-core.json"
    if ((Test-Path $coreZip) -and (-not $KeepFullLibreOffice)) {
        if (-not (Test-Path $coreMeta)) {
            throw "包内有 libreoffice-core.zip 但缺 libreoffice-core.json，无法校验溯源。请重跑：python tools\build-libreoffice-core.py --force"
        }
        $cm = Get-Content $coreMeta -Raw -Encoding UTF8 | ConvertFrom-Json
        if ((Get-Item $coreZip).Length -ne [int64]$cm.artifact.size) {
            throw ("libreoffice-core.zip 体积与元数据不符（{0} vs {1}）；请重跑：python tools\build-libreoffice-core.py --force" -f (Get-Item $coreZip).Length, [int64]$cm.artifact.size)
        }
        if ((Get-FileHash -LiteralPath $coreZip -Algorithm SHA256).Hash.ToLower() -ne ([string]$cm.artifact.sha256).ToLower()) {
            throw "libreoffice-core.zip 的 sha256 与元数据不符；请重跑：python tools\build-libreoffice-core.py --force"
        }
        $srcMsiPath = Join-Path $loDstDir ([string]$cm.source.file)
        if ((Test-Path $srcMsiPath) -and
            ((Get-FileHash -LiteralPath $srcMsiPath -Algorithm SHA256).Hash.ToLower() -ne ([string]$cm.source.sha256).ToLower())) {
            throw "runtime\libreoffice\$($cm.source.file) 与核心包的溯源 sha256 不符（换了 MSI 却没重建核心包）；请重跑：python tools\build-libreoffice-core.py --force"
        }
        Get-ChildItem -LiteralPath $loDstDir -Filter *.msi -File | Remove-Item -Force
        $coreZm = $cm
        $coreMb = [math]::Round($cm.artifact.size / 1MB, 1)
        $unpMb  = [math]::Round($cm.artifact.unpacked_bytes / 1MB, 1)
        Write-Host "  LibreOffice 改用裁剪核心包：libreoffice-core.zip（$coreMb MB，解包 $unpMb MB / $($cm.artifact.unpacked_files) 文件）"
        Write-Host "    原始 MSI 已移出包外（要随包带完整版：-KeepFullLibreOffice）"
    }

    # 清单自检：manifest.json 声明了什么，包里就必须有什么、且体积一致。
    # （缺文件不打自招是好事；怕的是「清单说有两个组件、实际只有一个」而无人察觉）
    if (Test-Path $rtMf) {
        $mf = Get-Content $rtMf -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($coreZm) {
            # 包内清单必须如实反映「MSI 已被核心包替代」，否则校验必然误报
            foreach ($c in $mf.components) {
                if ($c.id -eq "libreoffice") {
                    # 名称也要跟着改：清单里仍写「完整版 MSI」会与包内实际形态（裁剪核心包）自相矛盾
                    $c.name    = "LibreOffice $($c.version) 裁剪核心包（.doc→.docx / .xls→.xlsx 所需子集，源 MSI 派生）"
                    $c | Add-Member -NotePropertyName "core_pruned"    -NotePropertyValue $true -Force
                    $c | Add-Member -NotePropertyName "source_msi"     -NotePropertyValue ([string]$coreZm.source.file) -Force
                    $c | Add-Member -NotePropertyName "unpacked_bytes" -NotePropertyValue ([int64]$coreZm.artifact.unpacked_bytes) -Force
                    $c.file    = "libreoffice-core.zip"
                    $c.size    = [int64]$coreZm.artifact.size
                    $c.sha256  = [string]$coreZm.artifact.sha256
                    $c.install = "zip-unpack"
                }
            }
            $mf | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $rtMf -Encoding UTF8
        }
        $miss = @()
        $bad  = @()
        foreach ($c in $mf.components) {
            $fp = Join-Path (Join-Path $rtDst $c.subdir) $c.file
            if (-not (Test-Path $fp)) {
                $miss += "$($c.subdir)\$($c.file)"
            } elseif ((Get-Item $fp).Length -ne [int64]$c.size) {
                $bad += "$($c.subdir)\$($c.file)"
            }
        }
        if ($miss.Count -or $bad.Count) {
            throw ("离线组件不完整：缺失 [" + ($miss -join ", ") + "]，体积不符 [" + ($bad -join ", ") +
                   "]。请重跑 tools\fetch-offline-bundle.py --verify-only 后重新打包。")
        }
        $rtTotal = [math]::Round((Get-ChildItem $rtDst -Recurse -File |
                                  Measure-Object Length -Sum).Sum / 1MB, 1)
        Write-Host "  离线组件自检通过：$($mf.components.Count) 个组件，runtime\ 合计 $rtTotal MB"
        $offlineSummary = @()
        foreach ($c in $mf.components) {
            if ($coreZm -and $c.id -eq "libreoffice") {
                $offlineSummary += "libreoffice $($c.version)-core"
            } else {
                $offlineSummary += "$($c.id) $($c.version)"
            }
        }
    } else {
        Write-Warning "  包内没有 runtime\manifest.json —— 无法校验组件完整性"
    }
}

# 3.3 运行期目录
New-Item -ItemType Directory -Force -Path (Join-Path $AppDir "logs") | Out-Null

# 3.4 一键安装 / 卸载脚本 + 版本号（含 commit / built_at / python，便于核对包内代码与运行时来源）
Write-Host "==> 写入一键安装/卸载脚本与 version.json（版本 $Version）..."
foreach ($f in @("install.ps1", "一键安装.bat", "一键卸载.bat")) {
    $srcF = Join-Path $Root $f
    if (Test-Path $srcF) { Copy-Item -Force $srcF $AppDir }
}
$gitCommit = ""
try { $gitCommit = (& git -C $Root rev-parse --short HEAD 2>$null | Select-Object -First 1) } catch {}
if (-not $gitCommit) { $gitCommit = "unknown" }
# 工作区有未提交改动时标 -dirty：否则 version.json 的 commit 会指向一个
# **并不包含包内代码**的提交，事后无法据此定位"这个包到底是哪份源码打出来的"。
$gitDirty = ""
try {
    $porcelain = (& git -C $Root status --porcelain 2>$null)
    if ($porcelain) { $gitDirty = "-dirty" }
} catch {}
$gitCommit = "$gitCommit$gitDirty"
$verObj = @{
    app      = $Version
    schema   = 1
    commit   = $gitCommit
    built_at = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    python   = $PyVer          # 打包解释器版本（基线 $PyBaseline，不符时已在开头告警）
    offline  = ($offlineSummary -join "; ")   # 随包离线组件（空串表示瘦包）
}
# 注意：必须 UTF-8 无 BOM（Python json.load 遇 BOM 会报错）
[System.IO.File]::WriteAllText(
    (Join-Path $AppDir "version.json"),
    ($verObj | ConvertTo-Json),
    (New-Object System.Text.UTF8Encoding($false))
)
Write-Host "    版本 $Version（commit $gitCommit，Python $PyVer）"

# 4. 一键启动脚本
Write-Host "==> 生成 start.bat..."
$startBat = @'
@echo off
title JZToolsHub
echo 正在启动 JZToolsHub...（无窗口运行，托盘图标常驻右下角）
echo 浏览器访问 http://localhost:5000 ；退出服务请右键托盘图标选择「退出服务」
start "" /min JZToolsHub.exe
exit
'@
Set-Content -Path (Join-Path $AppDir "start.bat") -Value $startBat -Encoding Default

# 5.（可选）生成版本 zip：deploy\JZToolsHub-v<版本>.zip
if (Test-Path (Join-Path $AppDir "JZToolsHub.exe")) {
    $zip = Join-Path $Deploy "JZToolsHub-v$Version.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    # 离线组件约 500MB（MSI 本身已是压缩格式），用 Fastest 换时间：体积影响极小、
    # 打包时长从数分钟降到可接受范围。解压兼容性不受影响。
    Write-Host "==> 压缩安装包（含离线组件，体积较大请耐心等待）..."
    Compress-Archive -Path (Join-Path $AppDir "*") -DestinationPath $zip -CompressionLevel Fastest
    $zipMB = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "==> 已生成安装包：$zip（$zipMB MB）"
}

Write-Host ""
Write-Host "==> 部署完成：$AppDir"
Write-Host "    全新安装 / 更新：双击「一键安装.bat」（自动停止旧服务、复制程序、同步配置模板到用户数据根目录）"
Write-Host "    完全卸载（含用户数据）：双击「一键卸载.bat」"
Write-Host "    双击 start.bat 一键启动；前端源码在 static\ 与 plugins\<id>\frontend\，改完重启即生效。"
