# JZToolsHub 打包部署脚本
# 产物：deploy\服务端\JZToolsHub\ —— 后端单目录可执行程序（JZToolsHub.exe + _internal/）
#       + 前端源码（static/、plugins/）+ 配置（config/）+ 一键启动脚本（start.bat）
#       + 一键安装/更新（一键安装.bat + install.ps1）+ 一键卸载（一键卸载.bat）+ 版本号（version.json）
# 用法：powershell -ExecutionPolicy Bypass -File build-deploy.ps1 [-Python "python"] [-DeployName "JZToolsHub"] [-Version "1.3.6"] [-Force]
#       默认用 PATH 上的 python 打包、输出到 deploy\服务端\JZToolsHub\；
#       可用 -Python 指定其他解释器（如 Python 3.8：C:\...\Python38\python.exe）、
#       用 -DeployName 指定不同的部署目录名（如 py38 版输出到 deploy\JZToolsHub-py38）、
#       用 -Version 指定版本号（写入 version.json，一键安装脚本据此判断更新）。
#       未传 -Version 时自动在上一版基础上递增 patch；若最终版本号与上一版相同会直接报错中止
#       （版本号不变 → 目标机 sync_templates() 判定未升级 → 全部配置模板同步被跳过），
#       确需同号重打时加 -Force。
#       -Python 默认取 PATH 上的 python；脚本会记录其版本到 version.json（组装阶段写入），
#       并在与基线 3.14 不一致时告警（用旧解释器打包会打出版本残缺却无人察觉的包）。
#       默认**不**把离线运行组件打进包（主包瘦身，约 27 MB 级（以出包产物为准））：Chrome 与 LibreOffice
#       改由独立的「离线组件包」分发，目标机按需一键安装（见 tools\build-offline-component.ps1
#       与 docs\guide\LibreOffice核心组件一键安装评估.md）。要出旧式「组件随包」的胖包时加
#       -WithOfflineRuntime；-SkipOfflineRuntime 为旧开关，现已等价于默认行为（保留仅为兼容）。
#       LibreOffice 若已生成裁剪核心包（runtime\libreoffice\libreoffice-core.zip，
#       用 tools\build-libreoffice-core.py 生成），随包时默认「用它替代原始 MSI」，
#       包内体积由 357.5 MB 降到约 166 MB；加 -KeepFullLibreOffice 保留包内完整 LibreOffice MSI
#       （与裁剪核心包并存，包体约 682 MB；默认仅留裁剪核心包）。
#
# ★ P2-2「构建脚本单一真源」（2026-10-10）：本脚本只保留 清理 / 前置检查 / PyInstaller /
#   离线组件 / 压缩 五段；**组装段调用 tools\build-deploy-local.py**（与「两段式」流程同一份
#   实现，不再有第二份 PS 组装代码）。随包清单真源 = tools\package-content.json（本脚本 /
#   组装脚本 / tools\verify-package.py 三方共读）；打包解释器依赖清单真源 = JZToolsHub.spec
#   的 PACKAGES；共用工具函数见 tools\_shared.psm1。
param(
    [string]$Python = "python",
    [string]$DeployName = "服务端\JZToolsHub",
    [string]$Version = "",
    [switch]$Force,
    [switch]$WithOfflineRuntime,   # 随包携带离线运行组件（Chrome / LibreOffice）；默认不带
    [switch]$SkipOfflineRuntime,   # 旧开关：等价于默认行为，保留以免旧命令行报错
    [switch]$KeepFullLibreOffice,  # 保留原始 LibreOffice MSI（不用裁剪核心包替代）
    [switch]$ZipOnly               # 跳过 PyInstaller 与目录组装，仅用既有部署目录重生成 zip
)
$ErrorActionPreference = "Stop"

# 出包链共用函数（Say/Warn/Die、Compare-SemVer、压缩、spec 清单解析、解释器信息…）
Import-Module (Join-Path $PSScriptRoot "tools\_shared.psm1") -Force

$Root    = $PSScriptRoot
$Dist    = Join-Path $Root "dist"
$Deploy  = Join-Path $Root "deploy"
$AppDir  = Join-Path $Deploy $DeployName
$WorkDir = Join-Path $Root "build"

# 打包解释器及其版本（尽早取，便于出错时定位）
$py = Get-PythonRuntimeInfo $Python
Write-Host "  [解释器] $Python → $($py.Ver)（$($py.Exe)）"
if (-not $py.MatchesBaseline) {
    Write-Warning "打包解释器版本 $($py.Short) 与基线 $($py.Baseline) 不一致！产物可能功能残缺（如知识库 Office 预览引擎）。建议用 Python $($py.Baseline) 打包。"
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
    $zipPath = Join-Path (Join-Path $Deploy "服务端") "JZToolsHub-v$Version.zip"
    Write-Host "==> [-ZipOnly] 压缩既有部署目录（版本 $Version，跳过打包与组装）..."
    Write-Host "    提示：部署目录里若还是旧文档，请先从仓库复制最新文件进去。"
    $zipLen = Compress-DirectoryToZip -SourceDir $AppDir -ZipPath $zipPath -Level Fastest
    Write-Host "==> 已生成安装包：$zipPath（$([math]::Round($zipLen/1MB,1)) MB）"
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
# ★ 清单真源 = JZToolsHub.spec 的 PACKAGES（经 tools\_shared.psm1 解析，不另存抄本；
#   此前这里是手写清单，早已与 spec 对不上——记的仍是"改由依赖组件分发"的库）。
#   缺任何一个直接中止：spec 的 collect_all 失败会被 except 静默吞掉，不拦就是出残包。
Write-Host "==> 检查打包解释器的依赖完整性..."
$depPkgs = @(Get-SpecPackages)
if ($depPkgs.Count -eq 0) { throw "未能从 JZToolsHub.spec 解析出 PACKAGES（打包前置检查无法进行）。" }
$depList = ($depPkgs | ForEach-Object { "'$_'" }) -join ", "
$depCheck = @"
import importlib
pkgs = [$depList]
missing = []
for p in pkgs:
    try:
        importlib.import_module(p)
    except Exception as e:
        missing.append(f'{p} ({type(e).__name__})')
print('|'.join(missing))
"@
$depOut = (& $Python -c $depCheck 2>$null)
$depExit = $LASTEXITCODE
if ($depExit -ne 0) {
    throw "依赖完整性检查自身失败（exit $depExit）——打包解释器 $Python 不可用？请先在命令行跑通 `$Python -c ""import flask"" 排查。"
}
$missingDeps = ($depOut | Select-Object -First 1)
if ($missingDeps) {
    throw ("打包解释器缺少 spec PACKAGES 里的依赖，产物将功能残缺（collect_all 失败被静默跳过）：`n  " +
           ($missingDeps -replace '\|', "`n  ") + "`n  请先在打包解释器上安装后重跑。")
}
Write-Host "    依赖完整（$($depPkgs.Count)/$($depPkgs.Count)）"

# 2. PyInstaller 打包后端（单目录：exe + _internal/）
Write-Host "==> PyInstaller 打包后端（$Python）..."
& $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $WorkDir (Join-Path $Root "JZToolsHub.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（exit $LASTEXITCODE）" }

# 3.（可选）离线运行组件（Chrome / LibreOffice 安装包 + 随包安装脚本）
#   源 A：<仓库>\runtime\           —— 由 tools/fetch-offline-bundle.py 从官方源下载（已 gitignore）
#        其中 libreoffice\libreoffice-core.zip 由 tools\build-libreoffice-core.py 生成（裁剪核心包）
#   源 B：tools\offline-runtime\    —— 随包脚本与说明（入库，随每版一起更新）
#   组装后：<AppDir>\runtime\{ manifest.json, README.md, 安装离线组件.bat, setup-offline-runtime.ps1,
#                              install-libreoffice-core.ps1, 安装LibreOffice核心组件.bat, 卸载LibreOffice核心组件.bat,
#                              chrome\*.msi, libreoffice\{libreoffice-core.zip,.json} 或 libreoffice\*.msi }
#   （安装日志-LibreOffice核心.txt 由安装器在目标机运行时生成，不随包）
#   $offlineSummary 记录随包的离线组件，经 --offline 传给组装脚本写进 version.json.offline；
#   默认瘦包时为空的字符串数组（组装脚本落空串）。
$offlineSummary = @()

# 离线组件清单自检（体积 + R5 sha256 冻结值比对）。
# 独立成函数便于沙箱单测（AST 抽取后可用假 runtime 树直接触发，见报告验证）；
# 函数不依赖脚本其余状态：默认瘦包不调用它，因而无 runtime\ 时不会误报。
# 参数：
#   RuntimeDir      包内 runtime\ 目录
#   ManifestPath    包内 runtime\manifest.json
#   ComponentsFile  仓库 tools\offline-components.json（期望 sha256 真源；空串=跳过哈希比对）
#   CoreMeta        裁剪核心包元数据（$null=未裁剪）
# 返回：随包组件摘要字符串数组（写 version.json.offline）。
function Test-OfflineRuntimeManifest {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeDir,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [string]$ComponentsFile = "",
        [object]$CoreMeta = $null
    )
    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        Write-Warning "  包内没有 runtime\manifest.json —— 无法校验组件完整性"
        return @()
    }
    $mf = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($CoreMeta) {
        # 包内清单必须如实反映「MSI 已被核心包替代」，否则校验必然误报
        foreach ($c in $mf.components) {
            if ($c.id -eq "libreoffice") {
                # 名称也要跟着改：清单里仍写「完整版 MSI」会与包内实际形态（裁剪核心包）自相矛盾
                $c.name    = "LibreOffice $($c.version) 裁剪核心包（.doc→.docx / .xls→.xlsx 所需子集，源 MSI 派生）"
                $c | Add-Member -NotePropertyName "core_pruned"    -NotePropertyValue $true -Force
                $c | Add-Member -NotePropertyName "source_msi"     -NotePropertyValue ([string]$CoreMeta.source.file) -Force
                $c | Add-Member -NotePropertyName "unpacked_bytes" -NotePropertyValue ([int64]$CoreMeta.artifact.unpacked_bytes) -Force
                $c.file    = "libreoffice-core.zip"
                $c.size    = [int64]$CoreMeta.artifact.size
                $c.sha256  = [string]$CoreMeta.artifact.sha256
                $c.install = "zip-unpack"
            }
        }
        $mf | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8
    }

    # 期望 sha256 表（R5）：真源 = 仓库 tools\offline-components.json 的冻结值。
    # 体积一致挡不住「同体积换字节」——故按 id 再比一次哈希，不一致即中止。
    # derived[]（裁剪核心包）没有自身 sha256 字段，期望值取其 meta 文件（libreoffice-core.json）的 artifact.sha256。
    $expSha = @{}
    if ($ComponentsFile -and (Test-Path -LiteralPath $ComponentsFile)) {
        try {
            $exp = Get-Content -LiteralPath $ComponentsFile -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($ec in @($exp.components)) {
                if ($ec.id -and $ec.sha256) { $expSha[[string]$ec.id] = ([string]$ec.sha256).ToLower() }
            }
            foreach ($ed in @($exp.derived)) {
                if ($ed.id -and $ed.meta) {
                    $mp = Join-Path (Join-Path $RuntimeDir $ed.subdir) $ed.meta
                    if (Test-Path -LiteralPath $mp) {
                        $md = Get-Content -LiteralPath $mp -Raw -Encoding UTF8 | ConvertFrom-Json
                        if ($md.artifact.sha256) { $expSha[[string]$ed.id] = ([string]$md.artifact.sha256).ToLower() }
                    }
                }
            }
        } catch {
            Write-Warning "  读取离线组件期望 sha256（$ComponentsFile）失败，跳过哈希比对：$($_.Exception.Message)"
        }
    } elseif ($ComponentsFile) {
        Write-Warning "  缺少 $ComponentsFile —— 跳过离线组件 sha256 冻结值比对"
    }

    $miss   = @()
    $bad    = @()
    $badSha = @()
    foreach ($c in $mf.components) {
        $fp = Join-Path (Join-Path $RuntimeDir $c.subdir) $c.file
        if (-not (Test-Path -LiteralPath $fp)) {
            $miss += "$($c.subdir)\$($c.file)"
        } elseif ((Get-Item -LiteralPath $fp).Length -ne [int64]$c.size) {
            $bad += "$($c.subdir)\$($c.file)"
        } elseif ($expSha.ContainsKey([string]$c.id)) {
            $act = (Get-FileHash -LiteralPath $fp -Algorithm SHA256).Hash.ToLower()
            if ($act -ne $expSha[[string]$c.id]) { $badSha += "$($c.subdir)\$($c.file)" }
        }
    }
    if ($miss.Count -or $bad.Count -or $badSha.Count) {
        throw ("离线组件不完整：缺失 [" + ($miss -join ", ") + "]，体积不符 [" + ($bad -join ", ") +
               "]，sha256 与 tools\offline-components.json 冻结值不符 [" + ($badSha -join ", ") +
               "]。请重跑 tools\fetch-offline-bundle.py --verify-only 后重新打包。")
    }
    $rtTotal = [math]::Round((Get-ChildItem -LiteralPath $RuntimeDir -Recurse -File |
                              Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host "  离线组件自检通过：$($mf.components.Count) 个组件（含 sha256 冻结值比对），runtime\ 合计 $rtTotal MB"
    $offlineSummary = @()
    foreach ($c in $mf.components) {
        if ($CoreMeta -and $c.id -eq "libreoffice") {
            $offlineSummary += "libreoffice $($c.version)-core"
        } else {
            $offlineSummary += "$($c.id) $($c.version)"
        }
    }
    return $offlineSummary
}

#   默认**不**组装（主包瘦身）：Chrome 与 LibreOffice 由独立的「离线组件包」分发，
#   目标机按需一键安装，从而既缩小分发体积，又让组件可独立升级/卸载。
#   要出旧式「组件随包」的胖包：加 -WithOfflineRuntime。
if (-not $WithOfflineRuntime) {
    Write-Host "==> 主包不含离线运行组件（默认瘦身）"
    Write-Host "    组件另发：tools\build-offline-component.ps1 → 目标机一键安装"
    if ($SkipOfflineRuntime) { Write-Host "    注：-SkipOfflineRuntime 已是默认行为，可省略" }
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
    #     体积/sha256/溯源三项校验的单一实现在 tools\_shared.psm1（与
    #     build-offline-component.ps1 共用，此前两处各写一份）。
    $loDstDir = Join-Path $rtDst "libreoffice"
    $coreZip  = Join-Path $loDstDir "libreoffice-core.zip"
    $coreMeta = Join-Path $loDstDir "libreoffice-core.json"
    if ((Test-Path $coreZip) -and (-not $KeepFullLibreOffice)) {
        $cm = Test-LibreOfficeCoreBundle -CoreZip $coreZip -CoreMeta $coreMeta -SourceDir $loDstDir
        Get-ChildItem -LiteralPath $loDstDir -Filter *.msi -File | Remove-Item -Force
        $coreZm = $cm
        $coreMb = [math]::Round($cm.artifact.size / 1MB, 1)
        $unpMb  = [math]::Round($cm.artifact.unpacked_bytes / 1MB, 1)
        Write-Host "  LibreOffice 改用裁剪核心包：libreoffice-core.zip（$coreMb MB，解包 $unpMb MB / $($cm.artifact.unpacked_files) 文件）"
        Write-Host "    原始 MSI 已移出包外（要随包带完整版：-KeepFullLibreOffice）"
    }

    # 清单自检：manifest.json 声明了什么，包里就必须有什么、且体积与 sha256 一致。
    # （缺文件不打自招是好事；怕的是「清单说有两个组件、实际只有一个」而无人察觉；
    #   体积一致但字节被换过同样要拦 —— R5：按 tools\offline-components.json 的冻结 sha256 比对）
    $offlineSummary = @(Test-OfflineRuntimeManifest -RuntimeDir $rtDst -ManifestPath $rtMf `
        -ComponentsFile (Join-Path $Root "tools\offline-components.json") -CoreMeta $coreZm)
}

# 4. 组装部署目录 —— 单一实现：tools\build-deploy-local.py（P2-2）
#    与「两段式」流程（PyInstaller 各跑一段，再跑本脚本 -ZipOnly）共用同一份组装逻辑：
#    组装内容（随包清单、复制/清理、模板自检、installed-deps、version.json、start.bat）
#    全部由该脚本按 tools\package-content.json 完成，本脚本不再复刻。
Write-Host "==> 组装部署目录（tools\build-deploy-local.py）..."
$localArgs = @((Join-Path $Root "tools\build-deploy-local.py"), "-Version", $Version, "-DeployName", $DeployName)
if ($offlineSummary.Count -gt 0) { $localArgs += @("--offline", ($offlineSummary -join "; ")) }
& $Python @localArgs
if ($LASTEXITCODE -ne 0) { throw "组装失败（tools\build-deploy-local.py，exit $LASTEXITCODE）" }

# 5.（可选）生成版本 zip：deploy\JZToolsHub-v<版本>.zip
if (Test-Path (Join-Path $AppDir "JZToolsHub.exe")) {
    $zip = Join-Path $Deploy "JZToolsHub-v$Version.zip"
    # 离线组件（仅 -WithOfflineRuntime 随包）约 330MB 且本身已是压缩格式，用 Fastest 换时间：
    # 体积影响极小、打包时长从数分钟降到可接受范围。解压兼容性不受影响。
    Write-Host "==> 压缩安装包..."
    $zipMB = [math]::Round((Compress-DirectoryToZip -SourceDir $AppDir -ZipPath $zip -Level Fastest) / 1MB, 1)
    Write-Host "==> 已生成安装包：$zip（$zipMB MB）"
}

Write-Host ""
Write-Host "==> 部署完成：$AppDir"
Write-Host "    全新安装 / 更新：双击「一键安装.bat」（自动停止旧服务、复制程序、同步配置模板到用户数据根目录）"
Write-Host "    完全卸载（含用户数据）：双击「一键卸载.bat」"
Write-Host "    双击 start.bat 一键启动；前端源码在 static\ 与 plugins\<id>\frontend\，改完重启即生效。"
