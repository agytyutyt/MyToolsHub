# ============================================================================
# JZToolsHub supervisor 分件组装（T-O2-11d；Q9 拍板：独立安装包，主应用升级不动 supervisor）
#
# 用途：把「管理进程」需要的文件组装成独立分件目录（不含主包——supervisor 依赖的
#       框架模块由 worker 进程从主包目录 import，本分件只装管理进程自身）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\build-supervisor-package.ps1
#   ... -OutDir D:\deploy\supervisor分件
#
# 产物（deploy\supervisor分件\）：
#   supervisor\*.py                  管理进程本体（core/apply_chain/control_api/__main__/bridge/venv_mgr）
#   jz_worker.py                     worker 引导件（worker 进程也从主包目录取这份，随主包分发）
#   install\install-supervisor.ps1   服务化安装（NSSM 包装 python -m supervisor，含服务恢复策略）
#   install\README.txt               目标机安装步骤 + 注册表 worker 节模板
#
# 目标机安装（摘要，详见 install\README.txt）：
#   1. 把 supervisor\ 与 jz_worker.py 复制进主包目录（jz_* 内核模块可 import 的那层）
#   2. 写 <数据根>\config\supervisor-units.json（worker 节 + 单元表）
#   3. install-supervisor.ps1 -Python <主包解释器> -DataRoot <数据根> -MainPkgDir <主包目录>
# ============================================================================
param(
    [string]$OutDir = ""            # 缺省 <仓库>\deploy\supervisor分件
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Join-Path (Join-Path $Root "deploy") "supervisor分件" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Say { param([string]$m = "") Write-Host $m }

Say "==> 组装 supervisor 独立分件 → $OutDir"

# ① 管理进程本体 + worker 引导件
Copy-Item -LiteralPath (Join-Path $Root "supervisor") -Destination (Join-Path $OutDir "supervisor") -Recurse -Force
Remove-Item -LiteralPath (Join-Path $OutDir "supervisor\__pycache__") -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item -LiteralPath (Join-Path $Root "jz_worker.py") -Destination (Join-Path $OutDir "jz_worker.py") -Force

# ② 安装脚本
New-Item -ItemType Directory -Force -Path (Join-Path $OutDir "install") | Out-Null
Copy-Item -LiteralPath (Join-Path $Root "supervisor\install\install-supervisor.ps1") `
    -Destination (Join-Path $OutDir "install\install-supervisor.ps1") -Force

# ③ 目标机说明 + 注册表 worker 节模板
$readme = @"
JZToolsHub 管理进程（supervisor）独立分件
==========================================

定位（Q9 拍板）：主应用升级不动 supervisor；插件后端包 worker 由 supervisor
统一托管（启动编排 / 健康探针 / 熔断拉起 / 升级停换起 / 回滚 / 孤儿收割）。

安装步骤（目标机，需管理员权限）：
  1. 把 supervisor\ 目录与 jz_worker.py 复制进主包目录（与 app.py 同层，
     保证 worker 进程可 import jz_api / jz_deps / jz_plugin_host）；
  2. 写数据根配置 <数据根>\config\supervisor-units.json（模板见下）；
  3. 执行：
       powershell -ExecutionPolicy Bypass -File install\install-supervisor.ps1 `
         -Python "<主包解释器>" -DataRoot "<数据根>" -MainPkgDir "<主包目录>"
     （NSSM 包装 python -m supervisor；服务崩溃自动重启=Job Object kill-on-close
      的必配配套，见运行架构设计文档 §2）
  4. 验证：Get-Service JZToolsHubSupervisor；<数据根>\logs\supervisor.log
     出现「启动编排完成」；控制接口 127.0.0.1:5100 用 .supervisor_token 调 /status。

注册表模板（<数据根>\config\supervisor-units.json）：
{
  "schema": 1,
  "supervisor": { "port": 5100 },
  "port_range": [5101, 5199],
  "health_interval": 10, "health_grace": 8, "stable_window": 60, "circuit_threshold": 3,
  "worker": {
    "python": "<主包解释器路径>",
    "worker_script": "<主包目录>\\jz_worker.py",
    "main_url": "http://127.0.0.1:5000",
    "plugins_dir": "<主包目录>\\plugins",
    "main_pkg_dir": "<主包目录>"
  },
  "units": [
    { "name": "notice-board", "kind": "plugin", "plugin_id": "notice-board",
      "interpreter": "main", "enabled": true }
  ]
}
说明：插件单元的 port 留空——supervisor 首次启动分配并回写（分配一次长期占用）。
"@
[System.IO.File]::WriteAllText((Join-Path $OutDir "install\README.txt"), $readme,
    (New-Object System.Text.UTF8Encoding($true)))

Say "==> 完成。分件内容："
Get-ChildItem -LiteralPath $OutDir -Recurse -File | ForEach-Object {
    Say ("    " + $_.FullName.Substring($OutDir.Length + 1))
}
Say "    （NSSM 的 nssm.exe 需另行放入 install\——Apache-2.0 分发，不入仓库）"
exit 0
