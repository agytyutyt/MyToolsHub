# ============================================================================
# 插件独立升级 · 沙箱端到端测试（开发侧回归测试，可重复运行）
#
# 目的：在一份"假的程序目录 + 假的数据根"上验证 install-plugin.ps1 / install.ps1 的关键路径，
#       不触碰真实安装、不触碰真实数据根（显式传 -InstallDir / -DataRoot）。
#       构建侧用沙箱登记文件（-RegistryFile），可重复运行、不污染 tools\plugin-packages.json。
#       沙箱与夹具都在 gitignore 的 build\e2e-plugin\ 下运行时生成。
#
# 用法：powershell -ExecutionPolicy Bypass -File tools\e2e\plugin-upgrade-sandbox-tests.ps1
# ============================================================================
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # tools\e2e -> 仓库根
$T    = Join-Path $Repo "build\e2e-plugin"
$App  = Join-Path $T "app"
$Data = Join-Path $T "data"
$Fix  = Join-Path $T "fixtures"
$Installer = Join-Path $Repo "tools\plugin-upgrade\install-plugin.ps1"
$Builder   = Join-Path $Repo "tools\build-plugin-package.ps1"
$OutDir    = Join-Path $T "packages"
$OutVar    = Join-Path $T "packages-variant"
$RegFile   = Join-Path $T "sandbox-registry.json"
$script:Failures = @()

function Say  { param([string]$m = "") Write-Host $m }
function Head { param([string]$m) Write-Host ""; Write-Host ("=== " + $m) -ForegroundColor Cyan }
function Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = "")
    if ($Ok) { Write-Host ("  [PASS] " + $Name) -ForegroundColor Green }
    else { Write-Host ("  [FAIL] " + $Name + "   " + $Detail) -ForegroundColor Red; $script:Failures += $Name }
}
function Get-Hash16 {
    param([string]$Path)
    $md5 = [System.Security.Cryptography.MD5]::Create()
    try {
        $fs = [System.IO.File]::OpenRead($Path)
        try { return ([BitConverter]::ToString($md5.ComputeHash($fs)) -replace '-', '').ToLower().Substring(0, 16) }
        finally { $fs.Dispose() }
    } finally { $md5.Dispose() }
}
function Get-TreeHash {
    param([string]$Dir)
    if (-not (Test-Path -LiteralPath $Dir)) { return "(absent)" }
    $lines = Get-ChildItem -LiteralPath $Dir -Recurse -File -Force | Sort-Object FullName | ForEach-Object {
        $rel = $_.FullName.Substring($Dir.Length).TrimStart('\')
        ($rel + "  " + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)
    }
    return ($lines -join "`n")
}
function Run-Installer {
    param([string[]]$InstallerArgs)
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $Installer @InstallerArgs 2>&1
    $code = $LASTEXITCODE
    return @{ Code = $code; Text = ($out | Out-String) }
}
function Run-Builder {
    param([string[]]$BuilderArgs)
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $Builder @("-RegistryFile", $RegFile) @BuilderArgs 2>&1
    $code = $LASTEXITCODE
    return @{ Code = $code; Text = ($out | Out-String) }
}
function Run-MainInstaller {
    param([string]$MainPkgDir, [string[]]$InstallerArgs)
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $MainPkgDir "install.ps1") @InstallerArgs 2>&1
    $code = $LASTEXITCODE
    return @{ Code = $code; Text = ($out | Out-String) }
}
function Get-ManifestVersion {
    param([string]$PluginDir)
    $m = Get-Content -LiteralPath (Join-Path $PluginDir "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    return [string]$m.version
}
function Write-JsonFile {
    param([string]$Path, $Obj)
    [System.IO.File]::WriteAllText($Path, ($Obj | ConvertTo-Json -Depth 20), (New-Object System.Text.UTF8Encoding($false)))
}
function Read-ZipJson {
    param([string]$ZipPath, [string]$EntryName)
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $e = @($zip.Entries | Where-Object { $_.FullName -eq $EntryName })[0]
        $sr = New-Object System.IO.StreamReader($e.Open())
        try { return ($sr.ReadToEnd() | ConvertFrom-Json) } finally { $sr.Close() }
    } finally { $zip.Dispose() }
}

# ============================================================================
#  0. 准备沙箱
# ============================================================================
Head "0. 准备沙箱（$T）"
if (Test-Path -LiteralPath $T) { Remove-Item -LiteralPath $T -Recurse -Force }
New-Item -ItemType Directory -Force -Path $App, $Data, $OutDir, $OutVar | Out-Null

Copy-Item -LiteralPath (Join-Path $Repo "deploy\JZToolsHub\version.json") -Destination $App -Force
Copy-Item -LiteralPath (Join-Path $Repo "deploy\JZToolsHub\config") -Destination $App -Recurse -Force
Copy-Item -LiteralPath (Join-Path $Repo "deploy\JZToolsHub\plugins") -Destination $App -Recurse -Force
[System.IO.File]::WriteAllText((Join-Path $App "JZToolsHub.exe"), "", (New-Object System.Text.UTF8Encoding($false)))
# 沙箱里的 knowledge-base 当作"目标机上的旧版本 0.9.0"
$kbApp = Join-Path $App "plugins\knowledge-base"
$kbMan = Get-Content -LiteralPath (Join-Path $kbApp "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$kbMan.version = "0.9.0"
Write-JsonFile (Join-Path $kbApp "manifest.json") $kbMan
# 数据根：config + 模拟用户数据（升级全程必须逐字节不变）
New-Item -ItemType Directory -Force -Path (Join-Path $Data "config") | Out-Null
Copy-Item -LiteralPath (Join-Path $Repo "config\tools.json") -Destination (Join-Path $Data "config\tools.json") -Force
$kbDataFiles = Join-Path $Data "plugins\knowledge-base\data\files"
New-Item -ItemType Directory -Force -Path $kbDataFiles | Out-Null
[System.IO.File]::WriteAllText((Join-Path $kbDataFiles "sample-001.txt"), "用户上传的文档（升级时绝不能被动）", (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText((Join-Path $Data "plugins\knowledge-base\config.json"), '{"api_key":"SENSITIVE-KEY-ABC"}', (New-Object System.Text.UTF8Encoding($false)))
Write-JsonFile (Join-Path $Data "config\.app_state.json") @{ last_app = "1.9.0"; plugins = @{ "knowledge-base" = @{ version = "0.9.0"; installed_at = "2026-01-01T00:00:00"; installed_by = "seed" } } }
$dataHash0 = Get-TreeHash (Join-Path $Data "plugins")
$toolsHash0 = Get-Hash16 (Join-Path $Data "config\tools.json")
# 留一份 knowledge-base 用户数据的基线快照：第 9 节用 install.ps1 会按设计补配置模板
# （数据根 plugins\<id>\config.json），因此只能断言"用户数据目录"未变。
Copy-Item -LiteralPath (Join-Path $Data "plugins\knowledge-base") -Destination (Join-Path $T "data-kb-baseline") -Recurse -Force
Say "  app =$App"
Say "  data=$Data"
Check "沙箱就绪（knowledge-base=0.9.0，含模拟用户数据）" (Test-Path -LiteralPath (Join-Path $kbApp "backend\routes.py"))

# ============================================================================
#  1. 构建侧校验（C-2 版本递增 / C-3 缓存戳）
# ============================================================================
Head "1. 构建侧：基线 → 改 JS → 递增 ?v=N → 同版本被拒"
$fixKb = Join-Path $Fix "knowledge-base"
if (Test-Path -LiteralPath $fixKb) { Remove-Item -LiteralPath $fixKb -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $fixKb) | Out-Null
Copy-Item -LiteralPath (Join-Path $Repo "plugins\knowledge-base") -Destination $fixKb -Recurse -Force
Get-ChildItem -LiteralPath $fixKb -Recurse -Directory -Force | Where-Object { $_.Name -in @("__pycache__", "out") } | Remove-Item -Recurse -Force
# 【必须】把夹具版本钉死在 1.0.0：本测试后续步骤用 1.0.1 / 同号 做"版本递增"判定，
# 若夹具沿用真实插件当前版本（会随发版一路上涨），基线就会高于 1.0.1，
# 于是步骤②③会被正确地判为"版本未递增"而失败——**这是测试夹具的缺陷，不是产品缺陷**。
# 同号重建：沙箱登记（-RegistryFile）在本轮起始可能残留上一次的 1.3.0 记录，故先清空。
if (Test-Path -LiteralPath $RegFile) { Remove-Item -LiteralPath $RegFile -Force }
$fixMan0 = Get-Content -LiteralPath (Join-Path $fixKb "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$fixMan0.version = "1.0.0"
Write-JsonFile (Join-Path $fixKb "manifest.json") $fixMan0
# ① 基线构建（钉死 1.0.0）→ 记录 ?v=N 基线到沙箱登记
$r = Run-Builder @("-Id", "knowledge-base", "-From", $fixKb, "-OutDir", $OutDir)
Check "① 基线构建成功（1.0.0，记录缓存戳基线）" ($r.Code -eq 0) $r.Text
# ② 改后端 + 改前端 JS，版本 → 1.0.1，但不递增 ?v=N → C-3 必须拒绝
Add-Content -LiteralPath (Join-Path $fixKb "backend\routes.py") -Value "`n# e2e 夹具：模拟后端改动（测试重启判定）" -Encoding UTF8
Add-Content -LiteralPath (Join-Path $fixKb "frontend\app.js") -Value "`n// e2e 夹具：前端改动" -Encoding UTF8
$fixMan = Get-Content -LiteralPath (Join-Path $fixKb "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$fixMan.version = "1.0.1"
Write-JsonFile (Join-Path $fixKb "manifest.json") $fixMan
$r = Run-Builder @("-Id", "knowledge-base", "-From", $fixKb, "-OutDir", $OutDir)
Check "② C-3：JS 已变但 ?v=N 未递增 → 构建被拒" ($r.Code -ne 0 -and $r.Text -match "缓存") $r.Text
# ③ 递增 app.js 的 ?v=N（**按当前值 +1，勿写死数字**）→ 再构建 → 成功
#    写死 ?v=16→17 会在插件真实 ?v 早已大于 16 时静默不生效，导致步骤③被 C-3 正确拒绝。
$html = Get-Content -LiteralPath (Join-Path $fixKb "frontend\index.html") -Raw -Encoding UTF8
$m = [regex]::Match($html, 'app\.js\?v=(\d+)')
if (-not $m.Success) { throw "夹具 index.html 未找到 app.js?v=N，无法执行 C-3 步骤③" }
$curV = [int]$m.Groups[1].Value
$html = $html -replace ('app\.js\?v=' + $curV), ('app.js?v=' + ($curV + 1))
[System.IO.File]::WriteAllText((Join-Path $fixKb "frontend\index.html"), $html, (New-Object System.Text.UTF8Encoding($false)))
$r = Run-Builder @("-Id", "knowledge-base", "-From", $fixKb, "-OutDir", $OutDir, "-MinApp", "1.9.0", "-FromMin", "0.9.0")
Check "③ C-3：递增 ?v=$curV→$($curV + 1) 后构建成功（v1.0.1）" ($r.Code -eq 0) $r.Text
$pkg = Join-Path $OutDir "JZToolsHub-插件-knowledge-base-v1.0.1.zip"
Check "   包已产出" (Test-Path -LiteralPath $pkg)
# ④ 同版本再次发布（不加 -Force）→ C-2 必须拒绝
$r = Run-Builder @("-Id", "knowledge-base", "-From", $fixKb, "-OutDir", $OutDir)
Check "④ C-2：同版本重复发布 → 构建被拒（版本未递增）" ($r.Code -ne 0 -and $r.Text -match "版本未递增") $r.Text
$meta = Read-ZipJson $pkg "plugin-package.json"
Check "   包声明 requires_restart=true（后端有改动）" ($meta.requires_restart -eq $true)
Check "   包声明 min_app_version=1.9.0 / upgrade_from_min=0.9.0" ($meta.min_app_version -eq "1.9.0" -and $meta.upgrade_from_min -eq "0.9.0")
Check "   依赖声明：vendor 含 dhr/xhr；框架库含 flask" (($meta.deps.vendored -contains "dhr") -and ($meta.deps.vendored -contains "xhr") -and ($meta.deps.framework_packages -contains "flask"))
$entries = @()
$zip = [IO.Compression.ZipFile]::OpenRead($pkg)
try { $entries = @($zip.Entries | ForEach-Object { $_.FullName }) } finally { $zip.Dispose() }
Check "   包含 SHA256SUMS / install-plugin.ps1 / 安装插件.bat / 升级说明.md" (($entries -contains "SHA256SUMS") -and ($entries -contains "install-plugin.ps1") -and ($entries -contains "安装插件.bat") -and ($entries -contains "升级说明.md"))
# 篡改包（改一个 payload 字节）→ 用于哈希拒绝测试
$pkgBad = Join-Path $T "tampered.zip"
Copy-Item -LiteralPath $pkg -Destination $pkgBad -Force
$py = @'
import sys, zipfile, os
p = sys.argv[1]
tmp = p + ".tmp"
zin = zipfile.ZipFile(p); zout = zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED)
for item in zin.infolist():
    data = zin.read(item.filename)
    if item.filename.endswith("backend/routes.py"):
        data = data + b"\n# tampered\n"
    zout.writestr(item, data)
zout.close(); zin.close(); os.replace(tmp, p)
'@
$py | & python - $pkgBad
Check "   篡改包已生成" (Test-Path -LiteralPath $pkgBad)

# ============================================================================
#  2. DryRun（不落盘）
# ============================================================================
Head "2. -DryRun 只演算"
$r = Run-Installer @("-Package", $pkg, "-InstallDir", $App, "-DataRoot", $Data, "-DryRun")
Check "DryRun 退出码 0" ($r.Code -eq 0) ("exit=" + $r.Code + "`n" + $r.Text)
Check "DryRun 输出含增删改演算" ($r.Text -match "仅演算")
Check "DryRun 判定需要重启（后端有变化）" ($r.Text -match "重启判定：需要")
Check "DryRun 未改程序目录版本" ((Get-ManifestVersion $kbApp) -eq "0.9.0")

# ============================================================================
#  3. 真实升级（无服务在运行）
# ============================================================================
Head "3. 升级 0.9.0 → 1.0.1"
$r = Run-Installer @("-Package", $pkg, "-InstallDir", $App, "-DataRoot", $Data)
Check "升级退出码 0" ($r.Code -eq 0) ("exit=" + $r.Code + "`n" + $r.Text)
Check "程序目录版本 → 1.0.1" ((Get-ManifestVersion $kbApp) -eq "1.0.1")
Check "后端改动已落地（夹具注释存在）" ((Get-Content -LiteralPath (Join-Path $kbApp "backend\routes.py") -Raw -Encoding UTF8) -match "e2e 夹具")
Check "★ 用户数据逐字节未变（底线一）" ((Get-TreeHash (Join-Path $Data "plugins")) -eq $dataHash0)
Check "tools.json 未被升级流程改写" ((Get-Hash16 (Join-Path $Data "config\tools.json")) -eq $toolsHash0)
$bk = Join-Path $Data "backups\plugins\knowledge-base"
Check "已生成旧版备份（1 份）" (@(Get-ChildItem -LiteralPath $bk -File -Filter "*.zip" -ErrorAction SilentlyContinue).Count -eq 1)
$st = Get-Content -LiteralPath (Join-Path $Data "config\.app_state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
Check "状态登记：version=1.0.1" ($st.plugins.'knowledge-base'.version -eq "1.0.1")
Check "状态登记：含 code_sha256 / package_hash / backup" ($st.plugins.'knowledge-base'.code_sha256 -and $st.plugins.'knowledge-base'.package_hash -and $st.plugins.'knowledge-base'.backup)
Check "状态登记：保留 last_app" ($st.last_app -eq "1.9.0")

# ============================================================================
#  4. 拒绝路径（必须零写入）
# ============================================================================
Head "4. 拒绝路径（三次拒绝均零写入）"
$beforeHash = Get-TreeHash $kbApp
$r = Run-Installer @("-Package", $pkg, "-InstallDir", $App, "-DataRoot", $Data)
Check "同版本重装被拒（未加 -Force）" ($r.Code -ne 0 -and $r.Text -match "同版本重装") $r.Text
Check "拒绝后程序目录未变" ((Get-TreeHash $kbApp) -eq $beforeHash)
$r = Run-Installer @("-Package", $pkgBad, "-InstallDir", $App, "-DataRoot", $Data, "-Force")
Check "篡改包被哈希校验拒绝（-Force 也拦）" ($r.Code -ne 0 -and $r.Text -match "哈希校验未通过") $r.Text
Check "拒绝后程序目录仍未变" ((Get-TreeHash $kbApp) -eq $beforeHash)
$r = Run-Builder @("-Id", "knowledge-base", "-From", $fixKb, "-OutDir", $OutVar, "-Force", "-MinApp", "99.0.0")
$pkgMin = Join-Path $OutVar "JZToolsHub-插件-knowledge-base-v1.0.1.zip"
Check "变体包已构建（min_app=99.0.0）" (Test-Path -LiteralPath $pkgMin) $r.Text
$r = Run-Installer @("-Package", $pkgMin, "-InstallDir", $App, "-DataRoot", $Data, "-Force")
Check "主程序版本过低被拒" ($r.Code -ne 0 -and $r.Text -match "主程序版本过低") $r.Text
# 变体升到 1.0.2（比已装 1.0.1 新），但声明最高只允许从 0.5.0 起升 → 不加 -Force 时必须被拒
$fixMan2 = Get-Content -LiteralPath (Join-Path $fixKb "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$fixMan2.version = "1.0.2"
Write-JsonFile (Join-Path $fixKb "manifest.json") $fixMan2
$r = Run-Builder @("-Id", "knowledge-base", "-From", $fixKb, "-OutDir", $OutVar, "-FromMax", "0.5.0")
$pkgMax = Join-Path $OutVar "JZToolsHub-插件-knowledge-base-v1.0.2.zip"
Check "变体包已构建（v1.0.2，upgrade_from_max=0.5.0）" (Test-Path -LiteralPath $pkgMax) $r.Text
$r = Run-Installer @("-Package", $pkgMax, "-InstallDir", $App, "-DataRoot", $Data)
Check "超出 upgrade_from_max 被拒（未加 -Force）" ($r.Code -ne 0 -and $r.Text -match "最高起升版本") $r.Text
Check "三次拒绝后程序目录仍未变" ((Get-TreeHash $kbApp) -eq $beforeHash)

# ============================================================================
#  5. 未知文件保留 + 回滚
# ============================================================================
Head "5. 未知文件与回滚"
$unknownFile = Join-Path $kbApp "user-notes.txt"
[System.IO.File]::WriteAllText($unknownFile, "人工放入的文件，不该被升级删掉", (New-Object System.Text.UTF8Encoding($false)))
$bkBefore = @(Get-ChildItem -LiteralPath $bk -File -Filter "*.zip").Count
$r = Run-Installer @("-Package", $pkg, "-InstallDir", $App, "-DataRoot", $Data, "-Force")
Check "同号重装（-Force）成功" ($r.Code -eq 0) $r.Text
Check "未知文件被保留（默认不删）" (Test-Path -LiteralPath $unknownFile)
Check "输出列出了未知文件" ($r.Text -match "不在新旧任何版本清单内")
Check "备份 +1 份" (@(Get-ChildItem -LiteralPath $bk -File -Filter "*.zip").Count -eq ($bkBefore + 1))
$bkBefore2 = @(Get-ChildItem -LiteralPath $bk -File -Filter "*.zip").Count
$r = Run-Installer @("-Rollback", "knowledge-base", "-InstallDir", $App, "-DataRoot", $Data)
Check "回滚退出码 0" ($r.Code -eq 0) $r.Text
Check "回滚后版本 = 最近备份那份（1.0.1）" ((Get-ManifestVersion $kbApp) -eq "1.0.1")
Check "回滚动作本身也留了备份（+1 份）" (@(Get-ChildItem -LiteralPath $bk -File -Filter "*.zip").Count -eq ($bkBefore2 + 1))
$oldest = Get-ChildItem -LiteralPath $bk -File -Filter "*.zip" | Sort-Object LastWriteTime | Select-Object -First 1
$r = Run-Installer @("-Rollback", "knowledge-base", "-InstallDir", $App, "-DataRoot", $Data, "-BackupFile", $oldest.FullName)
Check "跨版本回滚（指定 0.9.0 备份）退出码 0" ($r.Code -eq 0) $r.Text
Check "跨版本回滚后版本 → 0.9.0" ((Get-ManifestVersion $kbApp) -eq "0.9.0")
$st = Get-Content -LiteralPath (Join-Path $Data "config\.app_state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
Check "回滚后状态登记 → 0.9.0" ($st.plugins.'knowledge-base'.version -eq "0.9.0")
Check "全程用户数据未变" ((Get-TreeHash (Join-Path $Data "plugins")) -eq $dataHash0)

# ============================================================================
#  6. 全新安装（base64）+ 注册条目合并 + 卸载
# ============================================================================
Head "6. 全新安装与卸载（base64）"
Remove-Item -LiteralPath (Join-Path $App "plugins\base64") -Recurse -Force
$r = Run-Builder @("-Id", "base64", "-OutDir", $OutDir)
$pkgB64 = Join-Path $OutDir "JZToolsHub-插件-base64-v1.0.0.zip"
Check "base64 包构建成功（纯前端）" ($r.Code -eq 0 -and (Test-Path -LiteralPath $pkgB64)) $r.Text
# 从沙箱 tools.json 里删掉 base64 条目 → 测"新增注册条目"路径
$tj = Join-Path $Data "config\tools.json"
$cfg = Get-Content -LiteralPath $tj -Raw -Encoding UTF8 | ConvertFrom-Json
$cfg.tools = @($cfg.tools | Where-Object { $_.id -ne "base64" })
Write-JsonFile $tj $cfg
$r = Run-Installer @("-Package", $pkgB64, "-InstallDir", $App, "-DataRoot", $Data)
Check "全新安装退出码 0" ($r.Code -eq 0) $r.Text
Check "代码已落盘" (Test-Path -LiteralPath (Join-Path $App "plugins\base64\frontend\index.html"))
$st = Get-Content -LiteralPath (Join-Path $Data "config\.app_state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
Check "已写入状态登记" ($null -ne $st.plugins.base64)
$cfg = Get-Content -LiteralPath $tj -Raw -Encoding UTF8 | ConvertFrom-Json
$b64 = @($cfg.tools | Where-Object { $_.id -eq "base64" })
Check "注册条目已追加（1 条）" ($b64.Count -eq 1)
Check "注册条目 enabled 取包内声明（false）" ($b64[0].enabled -eq $false)
$b64[0].enabled = $true
Write-JsonFile $tj $cfg
$r = Run-Installer @("-Uninstall", "base64", "-InstallDir", $App, "-DataRoot", $Data)
Check "卸载退出码 0" ($r.Code -eq 0) $r.Text
Check "卸载后代码目录已删" (-not (Test-Path -LiteralPath (Join-Path $App "plugins\base64")))
$cfg = Get-Content -LiteralPath $tj -Raw -Encoding UTF8 | ConvertFrom-Json
Check "注册条目被置为 enabled=false（未删除）" (@($cfg.tools | Where-Object { $_.id -eq "base64" })[0].enabled -eq $false)
$st = Get-Content -LiteralPath (Join-Path $Data "config\.app_state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
Check "状态登记已清除" (-not ($st.plugins.PSObject.Properties.Name -contains "base64"))

# ============================================================================
#  7. 无注册条目的全新安装（应拒绝）
# ============================================================================
Head "7. 无 tools_entry 的全新安装应被拒"
$fixNoreg = Join-Path $Fix "e2e-noreg"
if (Test-Path -LiteralPath $fixNoreg) { Remove-Item -LiteralPath $fixNoreg -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $Repo "plugins\base64") -Destination $fixNoreg -Recurse -Force
$nm = Get-Content -LiteralPath (Join-Path $fixNoreg "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$nm.id = "e2e-noreg"
Write-JsonFile (Join-Path $fixNoreg "manifest.json") $nm
$r = Run-Builder @("-Id", "e2e-noreg", "-From", $fixNoreg, "-OutDir", $OutVar)
Check "构建无注册条目的包（仅警告不阻断）" ($r.Code -eq 0) $r.Text
$pkgNoreg = Join-Path $OutVar "JZToolsHub-插件-e2e-noreg-v1.0.0.zip"
$r = Run-Installer @("-Package", $pkgNoreg, "-InstallDir", $App, "-DataRoot", $Data)
Check "全新安装缺 tools_entry → 拒绝" ($r.Code -ne 0 -and $r.Text -match "tools_entry") $r.Text
Check "拒绝后未落盘" (-not (Test-Path -LiteralPath (Join-Path $App "plugins\e2e-noreg")))

# ============================================================================
#  8. 服务在运行时的完整路径（假 JZToolsHub 进程 + 本地 HTTP 桩）
# ============================================================================
Head "8. 服务运行时：停服 → 替换 → 启动 → 冒烟"
$fakeExe = Join-Path $App "JZToolsHub.exe"     # 放程序目录：安装器的"启动服务"步骤会拉起它
# 用 powershell.exe 改名做假进程（python.exe 改名后会因缺同目录 python3XX.dll 起不来）
Copy-Item -LiteralPath (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe") -Destination $fakeExe -Force
$webroot = Join-Path $T "webroot"
New-Item -ItemType Directory -Force -Path (Join-Path $webroot "api\knowledge-base") | Out-Null
[System.IO.File]::WriteAllText((Join-Path $webroot "api\tools"), '{"tools":[{"id":"knowledge-base","name":"知识库"}]}', (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText((Join-Path $webroot "api\knowledge-base\status"), '{"ok":true}', (New-Object System.Text.UTF8Encoding($false)))
$env:JZTOOLS_PORT = "5099"
$fake = Start-Process -FilePath $fakeExe -ArgumentList @("-NoProfile", "-Command", "Start-Sleep -Seconds 600") -PassThru -WindowStyle Hidden
$web  = Start-Process -FilePath (Get-Command python).Source -ArgumentList @("-m", "http.server", "5099", "--directory", $webroot) -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2
Check "假服务进程已就位（进程名 JZToolsHub）" ($null -ne (Get-Process -Name JZToolsHub -ErrorAction SilentlyContinue))
Check "HTTP 桩可访问" ((Invoke-WebRequest -Uri "http://127.0.0.1:5099/api/tools" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200)
$r = Run-Installer @("-Package", $pkg, "-InstallDir", $App, "-DataRoot", $Data)
Check "升级（服务运行中）退出码 0" ($r.Code -eq 0) $r.Text
Check "输出显示已停止服务" ($r.Text -match "已停止服务进程")
Check "输出显示已启动服务" ($r.Text -match "已启动服务")
Check "输出包含冒烟结果" ($r.Text -match "冒烟")
$st = Get-Content -LiteralPath (Join-Path $Data "config\.app_state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
Check "升级后状态 → 1.0.1" ($st.plugins.'knowledge-base'.version -eq "1.0.1")
Remove-Item Env:JZTOOLS_PORT -ErrorAction SilentlyContinue
try { Stop-Process -Id $fake.Id -Force -ErrorAction SilentlyContinue } catch { }
try { Stop-Process -Id $web.Id -Force -ErrorAction SilentlyContinue } catch { }
Get-Process -Name JZToolsHub -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# ============================================================================
#  9. 整包升级不得回退已单独升级的插件（install.ps1 防回退）
# ============================================================================
Head "9. 整包升级防回退（install.ps1）"
# 造一个"主包安装包"：install.ps1 + version.json + plugins（其中 knowledge-base 内嵌 0.9.0）
$mainPkg = Join-Path $T "main-package"
if (Test-Path -LiteralPath $mainPkg) { Remove-Item -LiteralPath $mainPkg -Recurse -Force }
New-Item -ItemType Directory -Force -Path $mainPkg | Out-Null
Copy-Item -LiteralPath (Join-Path $Repo "install.ps1") -Destination $mainPkg -Force
Write-JsonFile (Join-Path $mainPkg "version.json") @{ app = "1.9.1"; schema = 1 }
Copy-Item -LiteralPath (Join-Path $Repo "deploy\JZToolsHub\plugins") -Destination $mainPkg -Recurse -Force
[System.IO.File]::WriteAllText((Join-Path $mainPkg "JZToolsHub.exe"), "", (New-Object System.Text.UTF8Encoding($false)))
$mainKbMan = Get-Content -LiteralPath (Join-Path $mainPkg "plugins\knowledge-base\manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$mainKbMan.version = "0.9.0"
Write-JsonFile (Join-Path $mainPkg "plugins\knowledge-base\manifest.json") $mainKbMan
# 沙箱现状：knowledge-base 已单独升级到 1.0.1（第 8 节末尾的状态）
Check "前置：沙箱 knowledge-base = 1.0.1" ((Get-ManifestVersion $kbApp) -eq "1.0.1")
# 记录一个"未单独升级"的插件，验证它仍会被主包更新（base64 在第 6 节已被卸载 → 应被重新装上）
Check "前置：base64 当前不存在（第 6 节已卸载）" (-not (Test-Path -LiteralPath (Join-Path $App "plugins\base64")))
$r = Run-MainInstaller $mainPkg @("-InstallDir", $App, "-DataRoot", $Data, "-NoRegistry")
Check "主包升级退出码 0（-NoRegistry 绿色模式，不动注册表/快捷方式）" ($r.Code -eq 0) $r.Text
Check "输出声明跳过受保护插件" ($r.Text -match "插件版本防回退" -and $r.Text -match "跳过 knowledge-base")
$st = Get-Content -LiteralPath (Join-Path $Data "config\.app_state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
Check "★ 已单独升级的插件未被回退（仍是 1.0.1）" ((Get-ManifestVersion $kbApp) -eq "1.0.1")
Check "状态登记也仍为 1.0.1（未被主包改写）" ($st.plugins.'knowledge-base'.version -eq "1.0.1")
Check "其它插件照常随主包更新（base64 被重新装上）" (Test-Path -LiteralPath (Join-Path $App "plugins\base64\frontend\index.html"))
# 反向：-ForcePluginOverwrite 时应以主包为准（并校正状态登记）
$r = Run-MainInstaller $mainPkg @("-InstallDir", $App, "-DataRoot", $Data, "-NoRegistry", "-ForcePluginOverwrite")
Check "-ForcePluginOverwrite 退出码 0" ($r.Code -eq 0) $r.Text
Check "以主包为准后版本 → 0.9.0" ((Get-ManifestVersion $kbApp) -eq "0.9.0")
$st = Get-Content -LiteralPath (Join-Path $Data "config\.app_state.json") -Raw -Encoding UTF8 | ConvertFrom-Json
Check "状态登记已同步为主包内嵌版本 0.9.0" ($st.plugins.'knowledge-base'.version -eq "0.9.0")
Check "数据根用户数据依然未变" ((Get-TreeHash (Join-Path $Data "plugins\knowledge-base")) -eq (Get-TreeHash (Join-Path $T "data-kb-baseline")))

# ============================================================================
#  10. 体检（-List）
# ============================================================================
Head "10. -List 体检"
$r = Run-Installer @("-List", "-InstallDir", $App, "-DataRoot", $Data)
Check "-List 退出码 0" ($r.Code -eq 0) $r.Text
Check "-List 输出版本与备份信息" ($r.Text -match "knowledge-base" -and $r.Text -match "代码版本")

Say ""
if ($script:Failures.Count -eq 0) {
    Write-Host "全部通过" -ForegroundColor Green
    exit 0
} else {
    Write-Host ("失败 " + $script:Failures.Count + " 项：" + ($script:Failures -join ", ")) -ForegroundColor Red
    exit 1
}
