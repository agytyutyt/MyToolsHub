# 管理后台（admin）

JZToolsHub 的核心基础设施插件，提供登录鉴权、组织架构与权限管理能力。

## 功能

- **登录鉴权**：基于 Flask session 的登录/登出，支持空闲超时与绝对有效期
- **单位管理**：单位（units）→ 部门（departments）→ 用户（users）三层架构 CRUD
- **人员管理**：用户创建、编辑、删除，支持角色分配与逐人工具权限点授权
  （权限设置弹窗经 `GET /api/admin/permission-points` 取全量工具清单，
  与首页「隐藏工具」浮窗的按权限过滤接口解耦）
- **权限管理**：角色（role）定义，控制各角色对管理模块的访问——**已并入人员管理，无独立模块权限**
  （`GET /admin/permission` 返回 404「权限管理已并入「人员管理」模块，暂不单独开放」，
  后台首页不再展示独立模块卡片；角色 CRUD 接口一律按「人员管理」权限鉴权）
- **工具访问控制**：对非超级管理员按权限点拦截无权限的工具页面与 API
- **数据加密**：密码、身份证、大模型 API Key 以 Fernet 对称加密存储
- **数据目录管理**：支持数据根目录迁移，升级替换程序时数据不丢失
- **批量导入导出**：单位 / 部门 / 人员三处均支持 xlsx / csv 模板下载、导出与导入；
  导入为两步式（先逐行预览校验，再确认写入），支持「存在则更新/仅新增/仅更新」三种模式、
  上级组织自动创建、逐行错误定位与整批回滚

## 批量导入导出（`backend/batch_io.py`）

| 接口 | 说明 |
| --- | --- |
| `GET /api/admin/batch/<module>/template?format=xlsx\|csv` | 下载导入模板（xlsx 附「填写说明」工作表） |
| `GET /api/admin/batch/<module>/export?format=xlsx\|csv[&sensitive=1]` | 导出当前数据（列结构与模板一致，可直接回导）；`sensitive=1` 会把大模型 API Key **以明文导出**（默认不导出），谨慎使用 |
| `POST /api/admin/batch/<module>/import` | 导入（`dry_run=1` 预览 / `dry_run=0` 写入） |

`<module>` = `unit` / `department` / `user`，分别要求对应管理模块权限。

**单元格语义**：留空 = 不修改（更新场景）；填**清空哨兵** = 清空该字段——半角 `-`（亦接受
`--` / `—` / `无` / `空` / `清空` / `不分配` / `none` / `null` / `clear`，比较时忽略大小写与首尾空白）。
**唯一键**：单位名称 / 所属单位+部门名称 / 登录名。
**安全护栏**：超级管理员账号的角色与权限点不可通过批量导入修改；导入默认「有错误行则整批回滚」。
本模块以依赖注入方式挂载（`register(app, deps)`），不反向 import `routes.py`，避免循环依赖。

**单元格格式与文件结构兼容**（设计文档 §10）：

- 结构层：表头上方可留标题行/说明行（自动探前 8 行定位，回传 `header_row`）；数据可放任意工作表
  （回传 `sheet`）；纵向合并单元格自动补全；重复表头行忽略；CSV 支持逗号/分号/制表符/竖线分隔；
  超行数/超体积前置拦截。
- 值层：百分比/货币/日期等显示格式不影响取值（取底层值）；日期转可读文本、布尔转「是/否」；
  标识字段（登录名/身份证）全角转半角 + 去空白；**权限点只做全角转半角、保留空白**（空白是分隔符，去掉会把多个权限点粘成一个）；表头清 BOM/零宽字符。
- **会丢数据的格式直接阻断该行**并给出修改办法：① ≥16 位的长数字按「数字」存储（Excel 仅 15 位
  有效数字，身份证末尾已被改写）；② 必填列的公式从未保存过计算结果。
- 已知限制：表头须为单行；前导零（`00123`）被 Excel 存为数字后无法还原；`.xls` 不支持（提示另存）。

## 插件管理（`backend/routes.py`）

| 接口 | 方法 | 说明 | 权限 |
| --- | --- | --- | --- |
| `GET /api/admin/deps` | GET | 已安装依赖清单（框架包 / 外部组件 / 插件自带，含版本） | 仅超管 |
| `POST /api/admin/plugins/uninstall` | POST | 卸载插件（**核心插件 `admin` 不可卸载**，`manifest.core=true` 时拒绝） | 仅超管 |
| `POST /api/admin/plugins/align` | POST | 对齐插件登记版本到程序目录实际版本（修复登记漂移；支持单插件或一键 `*`） | 仅超管 |
| `POST /api/admin/plugins/upload` | POST | multipart `file`（.zip）+ `force`：上传插件包并**只读校验**，返回应用计划（`from_version` / `requires_restart` / `plan.unknown` / `plan.deleted_files` 等），**尚未写程序目录** | 仅超管 |
| `POST /api/admin/plugins/apply` | POST | JSON `{file, force?, purge_unknown?, update_entry?, auto_restart?}`：应用已上传的包——服务端重新校验 → 备份 → 替换 → 登记（可回滚）；成功后清理上传包。`auto_restart` 默认 `true`（含后端改动则自动停服重启） | 仅超管 |
| `GET /api/admin/plugins/backups?id=<pid>` | GET | 某插件的备份清单（`name` / `size` / `mtime`，供回滚选择） | 仅超管 |
| `POST /api/admin/plugins/rollback` | POST | JSON `{id, backup?, auto_restart?}`：回滚到某份备份（缺省=最近一份，只接受该插件备份目录内的文件）；整目录替换**一律需要重启** | 仅超管 |
| `POST /api/admin/plugins/enable` | POST | JSON `{id, enabled}`：启用 / 停用插件（写数据根 `tools.json`，不动代码与数据）；无该插件条目时 404 | 仅超管 |
| `GET /api/admin/plugins/index?path=` | GET | 读取共享盘索引（index.json）并比对已装版本（`path` 缺省用上次记住的路径） | 仅超管 |
| `POST /api/admin/plugins/batch-apply` | POST | JSON `{index_path?, ids[]}`：按索引批量升级勾选的插件（顺序应用；全部成功后统一重启一次） | 仅超管 |
| `POST /api/admin/plugins/restart` | POST | 重启服务让新插件代码生效（仅打包运行；源码模式返回 `ok=false` 并提示手工重启） | 仅超管 |

## 依赖组件包扫描安装（`backend/routes.py`）

- **扫描**：`POST /api/admin/packages/scan`，入参 `dir`（扫描目录，缺省用 `default_scan_dirs()`：程序目录 / 上级 / 桌面 / 下载）；只读扫描介质目录里的插件包与依赖组件包，返回可安装清单（含 `status=install` 的建议项）。
- **安装**：`POST /api/admin/packages/install`，入参 `paths`（选定要装的包路径）+ `dir`；**安全语义：只接受服务端重新扫描得到的包路径**——前端把扫描结果里的 `path` 回传，服务端用 `scan_packages` 再扫一次做白名单二次比对，不在扫描结果里的路径一律拒绝（前端无法传入任意路径）。
- **重启策略**：依赖组件包装完**免重启**（主体下次请求自动补注入路径，`jz_deps.install_refresher` + `rescan_components`）；例外：组件文件被运行中的服务占用时会先暂存（.pending），重启服务后自动完成安装；**含后端改动的插件包需重启**（后端自动停服重启）。

## 特性

- 始终加载，不受 `enabled/disabled` 控制
- 默认账号 `admin` / `admin123`，首次启动自动生成

## 依赖与升级

| 项 | 内容 |
| --- | --- |
| 框架已打包依赖 | `cryptography` >=41.0、`flask` >=3.0、`werkzeug` >=3.0（必需，随主包） |
| 依赖组件包（不随主包） | `openpyxl` >=3.1（**可选**：缺失时批量导入导出的 Excel 读写不可用，其余功能不受影响） |
| 插件自带依赖（`backend/vendor/`） | 无 |
| 外部程序组件 | 无 |
| 能否单独升级 | ✅ 可以——业务依赖走「依赖组件包」，插件不 import 其他插件（构建期 C-4/C-10/C-11 校验） |
| 升级是否需重启 | 含 `backend/**` 改动**需要**重启（后台/安装器会自动重启，约 5~10 秒）；纯前端改动免重启，Ctrl+F5 即可 |

> 声明真源是 `manifest.json` 的 `requires`（三类依赖：框架包 / 自带 vendor / 外部程序组件）；
> 出包工具构建期校验「声明 ↔ 实测 import」一致（C-10）。后台「插件管理」按它显示
> 逐依赖徽标与可运行性：缺**必需**依赖 → 标记不可运行并暂不加载，补齐后重启自动恢复；
> 缺**可选**依赖 → 照常加载但标注功能降级。规范依据：《插件设计规范》§15 U-7a / U-8。

