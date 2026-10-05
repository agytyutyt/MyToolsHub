# ============================================================================
# JZToolsHub supervisor 独立安装（T-O2-11d，Q9 拍板：独立安装分件）
#
# 用途：把「管理进程 supervisor」注册为 Windows 服务（NSSM 包装 python -m supervisor），
#       主应用升级不动 supervisor。目标机操作：
#
#   1. 解压 supervisor 分件（supervisor/ 包 + 本脚本 + nssm.exe）到任意目录
#   2. powershell -ExecutionPolicy Bypass -File install-supervisor.ps1 `
#        -Python "C:\JZToolsHub\_internal\python.exe" -DataRoot "C:\JZToolsHub\data" `
#        -MainPkgDir "C:\JZToolsHub" -MainUrl "http://127.0.0.1:5000"
#   3. 验证：服务 JZToolsHubSupervisor 运行中 + <数据根>\logs\supervisor.log 有"启动编排完成"
#
# 卸载：install-supervisor.ps1 -Remove（停服务 + 删注册）
#
# 注意（拍板）：服务账户运行时 JZTOOLS_DATA_ROOT 显式注入（NSSM AppEnvironmentExtra），
# 共享盘映射等环境差异按 §2 服务化注意项人工确认。
# ============================================================================
param(
    [string]$Python = "",          # 主包解释器（frozen=JZToolsHub.exe 所在 _internal 的 python 或系统 python）
    [string]$DataRoot = "",        # 数据根（与主应用同根）
    [string]$MainPkgDir = "",      # 主包目录（jz_worker.py / plugins 所在）
    [string]$MainUrl = "http://127.0.0.1:5000",
    [string]$ServiceName = "JZToolsHubSupervisor",
    [string]$Nssm = "",            # nssm.exe 路径（缺省取本目录）
    [int]$Port = 5100,             # 控制接口端口
    [switch]$Remove
)
$ErrorActionPreference = "Stop"

if ($Remove) {
    & $Nssm stop $ServiceName 2>$null
    & $Nssm remove $ServiceName confirm 2>$null
    Write-Host "==> 服务 $ServiceName 已移除"
    exit 0
}

if (-not $Python -or -not $DataRoot -or -not $MainPkgDir) {
    Write-Host "缺少必填参数：-Python / -DataRoot / -MainPkgDir" -ForegroundColor Red
    exit 1
}
if (-not $Nssm) { $Nssm = Join-Path $PSScriptRoot "nssm.exe" }
if (-not (Test-Path -LiteralPath $Nssm)) {
    Write-Host "缺 nssm.exe（放本脚本同目录，或 -Nssm 显式指定）" -ForegroundColor Red
    exit 1
}

$supDir = Split-Path -Parent $PSScriptRoot          # supervisor 包上级（含 supervisor/ 与 jz_worker.py）
Write-Host "==> 注册服务 $ServiceName"
& $Nssm install $ServiceName $Python "-m" "supervisor" "--data-root" $DataRoot "--port" $Port
& $Nssm set $ServiceName AppDirectory $MainPkgDir
& $Nssm set $ServiceName AppStdout (Join-Path $DataRoot "logs\supervisor-nssm.log")
& $Nssm set $ServiceName AppStderr (Join-Path $DataRoot "logs\supervisor-nssm.log")
& $Nssm set $ServiceName AppRotateFiles 1
& $Nssm set $ServiceName AppRotateOnline 1
# ★ 服务恢复策略（拍板：Job Object kill-on-close 的必配配套——supervisor 崩溃必须自动拉起）
& $Nssm set $ServiceName AppExit Default Restart
& $Nssm set $ServiceName AppRestartDelay 3000
# 数据根环境变量（服务账户无用户态指针，显式注入）
& $Nssm set $ServiceName AppEnvironmentExtra "JZTOOLS_DATA_ROOT=$DataRoot" "JZ_MAIN_PKG_DIR=$MainPkgDir" "JZ_WORKER_MAIN_URL=$MainUrl"
& $Nssm start $ServiceName
Write-Host "==> 完成。验证：Get-Service $ServiceName；日志：$DataRoot\logs\supervisor.log"
Write-Host "    部署注册表：<数据根>\config\supervisor-units.json（worker 节指向 jz_worker.py=$supDir\jz_worker.py）"
