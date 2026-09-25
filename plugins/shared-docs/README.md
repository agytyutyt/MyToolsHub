# 共享文档（shared-docs）

多用户实时协同编辑 Word / Excel 文档。

## 功能

- **Word 协作**：富文本编辑（加粗、斜体、下划线、标题、列表），防抖自动保存；
- **Excel 协作**：表格编辑，支持行/列增删、列宽拖拽调整；
- **实时同步**：3s 轮询文档版本，检测到他人更新后自动拉取；版本冲突时弹冲突条（丢弃本地修改 / 覆盖保存）；
- **在线用户**：10s 心跳上报，显示当前正在编辑的用户列表；
- **导入/导出**：.docx / .xlsx / .xls 导入，导出为标准 Office 文件。

## 数据隔离

| 层级 | 可见条件 |
| --- | --- |
| 单位（unit） | 同单位用户可见 |
| 部门（department） | 同部门且同单位用户可见 |
| 私人（private） | 仅创建者本人 |

创建者或超级管理员可重命名、删除、调整挂靠层级。

## 接口

前缀 `/api/shared-docs`（`backend/routes.py`）：

| 接口 | 方法 | 说明 | 权限 |
| --- | --- | --- | --- |
| `GET /api/shared-docs/status` | GET | 依赖自检：返回 `python_docx` / `openpyxl` / `xlrd` 三项可用性 | 无需登录 |
| `GET /api/shared-docs/documents` | GET | 文档列表（按可见性过滤后返回） | 登录用户（未登录 401） |
| `POST /api/shared-docs/documents` | POST | 创建文档：`name`（≤100 字）、`type`（word / excel）、`level`（unit / department / private） | 登录用户（未登录 401；参数非法 400） |
| `GET /api/shared-docs/documents/<id>` | GET | 文档详情（含内容与当前在线用户） | 可见者（未登录 401 / 不可见 404） |
| `POST /api/shared-docs/documents/<id>/content` | POST | 保存内容（乐观锁：`base_version` 与当前版本不一致返回 409，响应附服务端最新文档） | 可见者（401 / 404 / 409 / 内容校验失败 400） |
| `POST /api/shared-docs/documents/<id>/presence` | POST | 在线心跳（前端 10s 定时上报 `client_id`，20s 无心跳视为离线），返回当前文档在线用户；身份只认 session，忽略前端自报昵称 | 可见者（401 / 404） |
| `POST /api/shared-docs/documents/<id>/rename` | POST | 重命名文档 | 创建者或超级管理员（401 / 404 / 403 / 名称非法 400） |
| `DELETE /api/shared-docs/documents/<id>` | DELETE | 删除文档 | 创建者或超级管理员（401 / 404 / 403 / 删除失败 500） |
| `POST /api/shared-docs/documents/<id>/scope` | POST | 调整挂靠层级（`level` = unit / department / private）；只改共享范围，保留原始 owner / 单位 / 部门组织信息 | 创建者或超级管理员（401 / 404 / 403 / 层级非法 400） |
| `GET /api/shared-docs/documents/<id>/export` | GET | 导出为真实 Office 文件（.docx / .xlsx，以附件下载） | 可见者（401 / 404；依赖组件包缺失时 400） |
| `POST /api/shared-docs/documents/<id>/import` | POST | 从 .docx / .xlsx / .xls 导入内容并覆盖当前文档（Word 仅 .docx，Excel 仅 .xlsx / .xls），版本号 +1 | 可见者（401 / 404 / 文件或格式非法 400） |

- 可见性口径：unit 级同单位、department 级同部门且同单位、private 仅创建者，超级管理员可见全部；
  可见即可保存内容与导入；重命名 / 删除 / 调整挂靠限创建者或超级管理员。
- 内容保存与导入均记录版本历史（保留最近 100 条）。

## 依赖与升级

| 项 | 内容 |
| --- | --- |
| 框架已打包依赖 | `flask` >=3.0（必需，随主包） |
| 依赖组件包（不随主包） | `docx` >=1.0、`openpyxl` >=3.1、`xlrd` >=2.0（均为可选：缺失时对应 Office 格式的导入导出不可用） |
| 插件自带依赖（`backend/vendor/`） | 无 |
| 外部程序组件 | 无 |
| 能否单独升级 | ✅ 可以——业务依赖走「依赖组件包」，插件不 import 其他插件（构建期 C-4/C-10/C-11 校验） |
| 升级是否需重启 | 含 `backend/**` 改动**需要**重启（后台/安装器会自动重启，约 5~10 秒）；纯前端改动免重启，Ctrl+F5 即可 |

> 声明真源是 `manifest.json` 的 `requires`（三类依赖：框架包 / 自带 vendor / 外部程序组件）；
> 出包工具构建期校验「声明 ↔ 实测 import」一致（C-10）。后台「插件管理」按它显示
> 逐依赖徽标与可运行性：缺**必需**依赖 → 标记不可运行并暂不加载，补齐后重启自动恢复；
> 缺**可选**依赖 → 照常加载但标注功能降级。规范依据：《插件设计规范》§15 U-7a / U-8。

