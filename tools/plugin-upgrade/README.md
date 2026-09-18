# tools/plugin-upgrade —— 插件包目标机侧（安装 / 升级 / 回滚 / 卸载）

本目录是**随插件包分发**的目标机侧脚本（单一真源）：

| 文件 | 作用 |
| --- | --- |
| `install-plugin.ps1` | 插件安装/升级/回滚/卸载/体检的全部逻辑（免管理员） |
| `安装插件.bat` | 双击入口（内部以 `-ExecutionPolicy Bypass` 调用上面的脚本） |

> 构建插件包时（`tools\build-plugin-package.ps1`）会把这两个文件**原样拷进包内**，
> 因此本目录是唯一需要维护的地方——不要把逻辑复制到别处，否则各包版本会漂移。

## 用法

```powershell
# 安装 / 升级（包即本脚本所在目录；也可 -Package <zip|目录>）
powershell -ExecutionPolicy Bypass -File install-plugin.ps1
# 体检：各插件 代码版本 / 状态登记 / 备份 / 数据占用
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -List
# 只演算不落盘：打印增/删/改与重启判定
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -DryRun
# 回滚到最近一次升级前（或 -BackupFile 指定某份备份）
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Rollback <插件id>
# 卸载（注册条目置 enabled=false，数据默认保留）
powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Uninstall <插件id>
```

常用开关：`-InstallDir <程序目录>`、`-DataRoot <数据根目录>`、`-ExpectedSha256 <哈希>`、
`-Force`（越过同版本/降级/跳版限制，仍强制校验文件哈希）、`-NoStart`、`-Hot`（纯前端包）、
`-PurgeUnknown`、`-UpdateEntry`、`-PurgeEntry`、`-BackupData`、`-PruneBackups <n>`。

## 三条底线（改脚本前先读）

1. **数据零触碰**：只写程序目录 `plugins\<插件id>\`、数据根 `backups\` 与状态文件
   `config\.app_state.json`；从不读写数据根 `plugins\<插件id>\` 里的用户数据。
2. **先校验后写入**：包结构 / schema / id / 逐文件 SHA256 / 版本规则 / 主程序最低版本
   全部通过后才停服务与备份；任何拒绝都发生在第一次写操作之前。
3. **可回退**：替换前必留一份可独立恢复的旧版快照；备份失败即中止，绝不在无备份时替换。

## 关键行为说明

- **三分法替换**：只删除"上次由本安装器装进去、本版已没有"的文件；
  磁盘上多出来的文件（人工/第三方放入）默认保留并列出，`-PurgeUnknown` 才清理。
  基线取自状态登记的 `installed_files`（首次由整包安装的插件退回"备份快照"口径）。
- **重启判定用事实**：包里的 `requires_restart` 仅供参考；安装器会比较包内 `backend/**`
  与 `manifest.json` 和目标机磁盘现状，只要不同（新增/变更/将删除）就必须重启（规范 R-4），
  此时 `-Hot` 会被拒绝。
- **只恢复自己停掉的服务**：升级前服务没在运行，升级后也不会替你启动（`was_running` 判定）。
- **服务恢复兜底**：失败退出（`Fail`）前会把本次自己停掉的服务拉起来，不把机器留在"服务停着"的状态。
- **配置模板不在这里同步**：插件新增的配置键由应用启动时的 `jztools_data.sync_plugin_templates()`
  自动补进数据根（只补缺失键、保留用户已有值）；安装器只登记状态。

## 端到端回归测试（开发侧）

```powershell
powershell -ExecutionPolicy Bypass -File tools\e2e\plugin-upgrade-sandbox-tests.ps1
```

在一份"假的程序目录 + 假的数据根"上跑完整链路（构建校验 / 升级 / 四类拒绝 / 回滚 /
全新安装 / 卸载 / 服务运行时的停服-替换-启动-冒烟 / 整包防回退），**不触碰真实安装与真实数据根**。

## 相关文档

- `docs/design/插件独立升级方案-设计文档.md` —— 方案、包规范、验收标准
- `docs/guide/离线部署包说明.md` §11 —— 目标机操作口径（与整包部署并列）
- `插件设计规范.md` §15 —— 对插件开发者的新增约束（版本纪律 / 依赖自包含 / 可独立升级性）
