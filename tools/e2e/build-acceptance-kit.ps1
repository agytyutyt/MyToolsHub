# ============================================================================
# build-acceptance-kit.ps1 —— 打「自动验收工具包」（目标机无需 Python）
#
# 为什么：目标机通常没有 Python 环境，而自动验收脚本是 .py。这里用 PyInstaller 把
# tools\e2e\acceptance-runner.py 冻成单文件 exe（**保持一份实现**，不另写 PowerShell 版），
# 连同测试数据与双击入口打成一个 zip。
#
# 产物（OutDir 缺省 deploy\）：
#   JZToolsHub-验收测试-v<日期>.zip
#     ├── acceptance-runner.exe     # 单文件 exe（内含 Python 运行时）
#     ├── 一键验收测试.bat           # 双击入口（默认连本机 5000，跑完暂停）
#     ├── README.md                 # 怎么用 / 覆盖什么 / SKIP 口径
#     ├── 使用说明.md                # 面向执行人的一页纸（含常见问题）
#     └── testdata\...              # 各插件测试文件 + 预生成二维码/视频
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\e2e\build-acceptance-kit.ps1
#   ... -Python C:\Python314\python.exe -SkipBuild     # 复用已有 exe，只重打包
#   ... -OutDir D:\out
# ============================================================================
param(
    [string]$Python = "python",
    [string]$OutDir = "",
    [switch]$SkipBuild,
    [switch]$NoZip
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $OutDir) { $OutDir = Join-Path $Root "deploy" }
$Entry  = Join-Path $Root "tools\e2e\acceptance-runner.py"
$Testdata = Join-Path $Root "testdata"
$Work   = Join-Path $Root "build\acceptance-kit"
$Staging = Join-Path $Work "kit"

function Say  { param([string]$m = "") Write-Host $m }
function Fail { param([string]$m) Write-Host ""; Write-Host ("  [失败] " + $m) -ForegroundColor Red; exit 1 }
function Write-Utf8NoBom { param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false))) }

if (-not (Test-Path -LiteralPath $Entry)) { Fail "缺少脚本：$Entry" }
if (-not (Test-Path -LiteralPath (Join-Path $Testdata "README.md"))) {
    Fail "缺少测试数据：$Testdata（先跑 python tools\e2e\make-acceptance-testdata.py）"
}

# ---- 1. 冻结 exe ----
$exeName = "acceptance-runner"
$exeSrc = Join-Path $Work "$exeName.exe"
if (-not $SkipBuild) {
    Say "==> PyInstaller 冻结 $exeName（单文件）..."
    New-Item -ItemType Directory -Force -Path $Work | Out-Null
    $dist = Join-Path $Work "dist"
    $work2 = Join-Path $Work "pyi"
    & $Python -m PyInstaller --noconfirm --clean --onefile --console `
        --name $exeName --distpath $dist --workpath $work2 `
        --specpath $Work $Entry
    if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 打包失败（exit $LASTEXITCODE）" }
    Copy-Item -LiteralPath (Join-Path $dist "$exeName.exe") -Destination $exeSrc -Force
} elseif (-not (Test-Path -LiteralPath $exeSrc)) {
    Fail "-SkipBuild 需要已有 exe：$exeSrc"
}
Say ("    体积：{0:N1} MB" -f ((Get-Item -LiteralPath $exeSrc).Length / 1MB))

# ---- 2. 组装工具包 ----
Say "==> 组装工具包：$Staging"
if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Staging | Out-Null
Copy-Item -LiteralPath $exeSrc -Destination $Staging -Force
Copy-Item -LiteralPath $Testdata -Destination (Join-Path $Staging "testdata") -Recurse -Force

$bat = @'
@echo off
chcp 65001 >nul
title JZToolsHub 自动验收
cd /d "%~dp0"
echo ================================================
echo   JZToolsHub 自动验收（目标机无需 Python）
echo ================================================
echo.
echo 将连接 http://127.0.0.1:5000 并跑一遍可自动化的验收用例。
echo 请先启动 JZToolsHub 服务（双击 start.bat 或托盘图标）。
echo.
echo 如需指定其它地址/参数，可直接命令行运行：
echo   acceptance-runner.exe --base http://127.0.0.1:5000 --report 验收报告.md
echo.
pause
acceptance-runner.exe --report "验收报告.md" %*
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (
  echo [完成] 全部可自动化的用例通过（跳过项见上方列表，需人工验证）。
) else (
  echo [注意] 存在失败用例，请查看上方输出与「验收报告.md」。
)
echo.
pause
exit /b %RC%
'@
Write-Utf8NoBom (Join-Path $Staging "一键验收测试.bat") $bat

$readme = @'
# 自动验收工具包（目标机无需 Python）

> 配套文档：《JZToolsHub 验收手册》§2.5。本包把其中**可自动化**的用例跑一遍，人工只补差集。

## 怎么用

1. 目标机上启动服务（双击主包的 `start.bat`，或托盘图标 → 启动服务）。
2. 解压本包，**双击「一键验收测试.bat」**。
3. 看输出末尾的汇总：`通过 N / 失败 M / 跳过 K`；失败项会打印接口响应体，便于定位。
   报告同时写入本目录的 `验收报告.md`。

命令行（可选参数）：

```
acceptance-runner.exe --base http://127.0.0.1:5000 --report 验收报告.md
acceptance-runner.exe --groups F,R            # 只跑功能与依赖组
acceptance-runner.exe --media D:\介质\插件包   # 额外跑 S-01（改一个字节的包必须被拒绝）
acceptance-runner.exe --app-dir "%LOCALAPPDATA%\JZToolsHub"   # 额外跑 R-06（真实 import 自检）
```

- 退出码：`0` = 无失败（跳过项不算失败）；`1` = 有失败。
- `--app-dir` 不填时会**自动探测**（注册表 InstallLocation → `%LOCALAPPDATA%\JZToolsHub`）。
- 测试数据默认读 exe 旁的 `testdata\`。

## 覆盖范围（32 条，含 5 条按条件跳过）

| 组 | 内容 |
| --- | --- |
| F 功能 | admin 登录/列表/批量导入（含错误行逐行报错）/导出；file-filter xlsx/xls/csv 与正则脱敏；info-transfer 静态码**原件传输往返 sha256 比对**、精简传输、视频码、文字封装；trajectory-convert 视频码；trajectory-sketch 出图；knowledge-base docx/xlsx/pdf 预览；shared-docs 新建/列表/详情/导出；notice-board 发布；5 个工具插件页面 |
| R 依赖 | 逐依赖判定与「已安装依赖」面板一致；插件 `/status` 自报依赖可读；依赖组件登记与目录一致 |
| S 安全 | 篡改包被拒绝（需 `--media`）；空文件与错误扩展名被拒 |
| X 边界 | 不存在的任务 404；3 个并发任务全部完成；本地资源不依赖外网 |

## 跳过项（不假装通过）

| 用例 | 为什么跳过 |
| --- | --- |
| F-20 character-graph、F-22 case-report | 解析依赖大模型 API Key（未配置时接口如实返回 400）——配置后人工验 |
| qr-video-decode 视频解码 | 解码在**浏览器侧**完成（服务端只接收已解码分块）——人工在页面上传视频验 |
| color-picker / map-marker | 纯交互界面 / 需高德 Key |
| R-06 / S-01 / S-02 | 需要 `--app-dir` / `--media` / 普通账号，未提供时自动跳过 |

## 说明

- exe 内含独立 Python 运行时，**目标机无需安装 Python**，也不需要联网。
- 用例跑的是**真实接口**（上传、解码、导出、校验、并发），不是"页面能不能打开"。
- 报告里的失败项请连同响应体一起反馈（多为环境问题：依赖组件未装、服务未启动等）。
'@
Write-Utf8NoBom (Join-Path $Staging "README.md") $readme

$usage = @'
# 使用说明（一页纸）

## 三步

1. **启动服务**：双击主包的 `start.bat`，等托盘图标出现（约 5~10 秒）。
2. **跑验收**：解压本工具包 → 双击「一键验收测试.bat」。
3. **看结论**：输出末尾 `通过 / 失败 / 跳过`；失败项在「验收报告.md」里逐条列出。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 提示"无法连接/登录" | 服务没起或端口不是 5000：先双击 `start.bat`；端口改过就加 `--base http://127.0.0.1:<端口>` |
| 大量 F 组失败且提示缺依赖 | 依赖组件包没装：按《干净机器部署验收手册》步骤 2 装齐（cv2 需同时装 numpy） |
| 提示"测试数据缺失" | 本包的 `testdata\` 目录被删了：重新解压本包，或从仓库复制 `testdata\` |
| 想只跑一部分 | 加 `--groups F`（功能）/ `R`（依赖）/ `S`（安全）/ `X`（边界），逗号分隔 |
| 想连别人的机器验收 | `--base http://<目标机IP>:5000`（对方需允许局域网访问） |
| R-06 跳过 | 未指定/未探测到程序目录：加 `--app-dir "<程序目录>"`（含 JZToolsHub.exe 的那层） |

## 退出码

`0` = 无失败（跳过项不算失败，需人工补）；`1` = 有失败。批处理里可直接判 `%ERRORLEVEL%`。
'@
Write-Utf8NoBom (Join-Path $Staging "使用说明.md") $usage

$count = (Get-ChildItem -LiteralPath $Staging -Recurse -File).Count
Say ("    工具包内容：{0} 个文件" -f $count)

if ($NoZip) { Say "    （-NoZip：只组装到 $Staging）"; exit 0 }

# ---- 3. 打 zip（不传 entryNameEncoding：默认 UTF-8 且置标志位，中文名不乱码） ----
$date = Get-Date -Format "yyyyMMdd"
$zip = Join-Path $OutDir "JZToolsHub-验收测试-v$date.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $Staging, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
$sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLower()
Write-Utf8NoBom "$zip.sha256" ("{0}  {1}`n" -f $sha, (Split-Path -Leaf $zip))
Say ""
Say ("==> 已产出：{0}（{1:N1} MB）" -f $zip, ((Get-Item -LiteralPath $zip).Length / 1MB))
Say ("    sha256：{0}" -f $sha)
Say "    目标机操作：解压 → 双击「一键验收测试.bat」"
