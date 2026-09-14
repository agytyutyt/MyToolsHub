# HANDOFF.md — JZToolsHub 交接文档

> 面向没有上下文的接手者：**请先完整读完本文，再动手改代码。**
> 最后更新：2026-09-14（第四轮：**打通离线部署** —— 部署包内置 Chrome 与 LibreOffice 安装包
> 并自动安装/解包，目标机无外网即可完整部署；新增 `tools/fetch-offline-bundle.py` 组件获取器、
> `docs/离线部署包说明.md`。第三轮为取消 Win7 + vendor `Optional` 补丁 + zfec wheel 落库；
> 第二轮为 P0 六项修复；第一轮为文档全面重写）。
> 配套必读：`README.md`（架构 / 环境 / API / 目录 / 使用示例）、`插件设计规范.md`（插件开发铁律）、
> `docs/离线部署包说明.md`（离线包的组成、安装与适用场景）、
> `20260914评估报告.md`（插件规范符合性与项目问题清单）、`docs/P0问题修复方案.md`（P0 修复方案与实测）、
> `docs/Python版本选型评估.md`（版本基线的完整实测依据）、`wheels/README.md`（zfec wheel 的来源与重建）。

---

## 目录

1. [项目定位与架构速记](#1-项目定位与架构速记)
2. [当前开发状态](#2-当前开发状态)
3. [已完成事项](#3-已完成事项)
4. [待办清单](#4-待办清单)
5. [关键架构决策](#5-关键架构决策)
6. [已知问题](#6-已知问题)
7. [踩坑清单（按主题分类）](#7-踩坑清单按主题分类)
8. [插件后端速查表](#8-插件后端速查表)
9. [移动端 APP（android-app/InfoParse）](#9-移动端-appandroid-appinfoparse)
10. [文档同步约定与快速命令](#10-文档同步约定与快速命令)

---

## 1. 项目定位与架构速记

`D:\JZToolsHub` 是一个 **Flask + 配置驱动的插件化工具箱**（"一切皆插件"）：

- 主应用 `app.py` 启动时扫描 `plugins/<插件id>/`，把插件后端路由注册进同一个 Flask 应用；
- 插件前端在 `plugins/<id>/frontend/`，经 `/plugin/<id>/<path>` 静态访问，以 iframe 嵌入外壳页 `/tool/<id>`；
- 工具清单由 `config/tools.json` 声明（name / description / category / order / enabled / hidden / grant_all）；
- 管理后台（`admin`）是**核心基础设施插件**：登录鉴权、会话超时、组织架构、权限、Fernet 加密、批量导入导出，始终加载、不随 `enabled` 启停；
- 移动端 `android-app/InfoParse/`（Kotlin）是 `info-transfer` 插件的 Android 离线接收端，协议权威定义在仓库根目录《移动端APP.md》。

### 1.1 目标环境基线（部署前必须与甲方对齐）

| 维度 | 基线 | 备注 |
| --- | --- | --- |
| 操作系统 | **Windows 10 及以上（x64）** | **不支持 Windows 7**（2026-09-14 起取消，3.8 打包支线已删除） |
| Python | **3.14**（打包与生产唯一基线） | 开发机最低 3.12；3.15 发布后不急于跟进（见 `docs/Python版本选型评估.md` §6） |
| 浏览器 | **Chrome ≥ 72**（Edge ≥ 79、同内核国产浏览器） | 与 OS 基线**互相独立**：§7.3 的旧浏览器兼容措施不能删 |
| 网络 | 目标机**可完全无外网** | 部署包自带 Chrome / LibreOffice 离线安装包（`runtime/`），依赖全部随包；装完不联网即可跑。见 `docs/离线部署包说明.md` |
| 许可证 | Chrome（专有）/ LibreOffice（MPL-2.0） | 再分发口径与替代方案见 `runtime/README.md` §6 |

> ⚠️ **最容易犯的错：把"取消 Win7"理解成"可以删掉旧浏览器兼容代码"**。两者触发条件不同——
> 前者是操作系统，后者是浏览器版本 / 缺少彩色 emoji 字体。详见 §7.3 第 17 条。

### 1.2 启动时序

```
python app.py
  → init_data_root()              迁移旧数据 → 解析数据根目录 → sync_templates() 模板同步
  → setup_access_logging(app)     异步访问日志（队列 2000 + 后台线程落盘）
  → register_plugin_backends(app) 先无条件加载 admin，再按 tools.json 的 enabled 逐个加载
  → 监听 0.0.0.0:5000             源码=Flask debug 服务器；打包=waitress 8 线程
```

### 1.3 请求拦截管线（admin 注册的 4 个 before_request，按序）

1. `make_session_guard`：空闲 30 分钟 / 绝对 12 小时超时登出，活跃滑动续期；
2. `_enforce_login`：白名单之外一律要求登录（页面 302 → `/login`，`/api/*` 401）；
   白名单 = `/login`、`/api/login`、`/api/logout`、`/api/session`、`/favicon.ico` + 前缀 `/static/`、`/plugin/admin/css/`、`/plugin/admin/js/`；
3. `_protect_admin_ops`：首页布局写操作需登录；
4. `_enforce_tool_access`：非超管按权限点拦 `/tool/<id>`、`/plugin/<id>/...`、`/api/<插件id>/...`。

### 1.4 关键单例与全局

| 位置 | 内容 |
| --- | --- |
| `app.py` | `DATA_ROOT` / `CONFIG_PATH` / `LOG_DIR` / `LOG_FILE`（`init_data_root()` 后指向数据根目录）、`_plugin_home_card_hooks`（首页卡片钩子表）、`_tool_meta_cache`（日志用，2s TTL） |
| `jztools_data.py` | `get_data_root()` 双指针解析、`_TEMPLATE_SYNC` 模板同步清单、`_LEGACY_MAP` 旧数据迁移映射 |
| `plugins/admin/backend/routes.py` | `ADMIN_CONFIG_PATH`（数据根 `config/admin.json`）、`_fernet`（懒加载）、`_registered_tool_ids()` / `_grant_all_tool_ids()` |

### 1.5 环境变量

`JZTOOLS_HOST`（默认 `0.0.0.0`）、`JZTOOLS_PORT`（默认 5000，解析失败回落 5000）。

---

## 2. 当前开发状态

### 2.1 git 实际状态（2026-09-14 核实）

| 项 | 值 |
| --- | --- |
| 当前分支 | `main` |
| HEAD | 本提交（第三轮：取消 Win7 + vendor 补丁 + zfec wheel + 版本基线固化） |
| 第二轮（P0 修复三连） | `e907b5e` 日志补记 / `05cc6ea` 仓库卫生与文档 / `03137d7` 路由前缀与加载隔离 / `8d0301d` 模板命名分离与打包防呆 |
| 第一轮（文档重写） | `7d2d3dc` README/HANDOFF 全面重写 + 新增 20260914 评估报告 |
| 此前基线 | `ca69141`（2026-09-14）知识库阅读页背景卡片贴合预览内容宽度 + 长 token 撑破版心的横向滚动条修复 |
| 工作区 | **干净**（无未提交改动，无未跟踪文件） |
| 远程 | `origin = https://github.com/agytyutyt/MyToolsHub.git`；**上述提交均未 push**，`origin/main` 仍在 `ca69141` |
| 其他分支 | `G2改造`、`PluginDesign`、`共享文档`、`功能优化`、`压力测试`、`战果录入`、`插件位置编辑`、`界面滑块`、`登录改造`（均为历史功能分支，未合并） |

> 早前版本文档记录的"大量未提交改动"与"最新提交 7269cb8"已过时：相关改动已随后续提交入库。
> `G2改造` 分支的连续曲率圆角引擎（jz-radius v1.2 + 基准页 + 设计文档）**未并入 main**，打包不含。

### 2.2 产物状态

**最新产物：v1.7 离线部署包（2026-09-14）**

| 项 | 值 |
| --- | --- |
| 部署目录 | `deploy/JZToolsHub/` — **777.5 MB / 2886 个文件** |
| 分发包 | `deploy/JZToolsHub-v1.7.zip` — **618.4 MB** |
| 版本 | `1.7`（上一版 1.6，脚本自动递增） |
| 打包解释器 | Python **3.14.7**（基线 3.14，符合） |
| 构建提交 | `fd06e32`（写入 `version.json.commit`；构建时工作区有未提交改动，已按提交后内容校正） |
| 离线组件 | Chrome 企业版 MSI 159.6 MB + LibreOffice 26.8.0 MSI 357.5 MB（`runtime/` 合计 517.2 MB） |
| 包内新改动 | 知识库 Office 预览引擎改造（09-13）+ P0 六项修复 + 离线部署能力 |

体积构成：`_internal/` 224.4 MB（Python 运行时与依赖）、`runtime/` 517.2 MB（离线组件）、
`JZToolsHub.exe` 19.0 MB、`plugins/` 16.1 MB，其余不足 1 MB。

> 相对 v1.6（约 102 MB）体积增长 6 倍，全部来自随包分发的两个第三方安装包。
> 不想带它们：`build-deploy.ps1 -SkipOfflineRuntime` 出瘦包。
> 只改了文档要重出 zip：`build-deploy.ps1 -ZipOnly`（跳过 PyInstaller 与组装，实测约 2 分钟，
> 但**要先把新文件复制进 `deploy/JZToolsHub/`**）。全量打包约 16 分钟。

### 2.3 无硬性阻塞，但以下事项未充分验证

1. 访问日志字段仅经 Flask test client 验证，未在真实浏览器/多用户高并发环境走查；`get_session_user()` 每请求读 `config/admin.json`（静态资源已跳过），高并发如吃紧要加缓存。
2. 战果录入为**仅大模型解析**，缴获物品明细依赖大模型结构化输出；换低性能模型需复测。
3. 公告板 / 共享文档 / 战果录入等前端功能多靠 `node --check` + test client 回归，浏览器走查较少。
4. 一键安装/卸载脚本已在开发机通过语法检查与 exe 冒烟，**尚未在目标机做完整「全新安装 → 更新 → 卸载」三段式实测**。
5. `info-transfer` 的 `fmt=file` 端到端（桌面封装 → APP 扫码 → 导出 → 与原文件逐字节比对）尚未真机验证，是移动端首要待办。
6. ~~打包用 Python 3.14 时产物不支持 Win7~~ → **已作废**：2026-09-14 起取消 Win7 兼容，目标机基线为 Windows 10+，不再维护 3.8 打包支线（见 §1.1 与 `docs/Python版本选型评估.md` §4）。

---

## 3. 已完成事项

| 时间 | 里程碑 | 关键内容 |
| --- | --- | --- |
| 2026-09-14 | **离线部署包打通 + v1.7 发版** | `runtime/` 随包分发 Chrome 企业版 MSI（159.6MB）与 LibreOffice 26.8.0 MSI（357.5MB）；新增 `tools/fetch-offline-bundle.py`（断点续传 + sha256 冻结校验，清单入 `tools/offline-components.json`）与 `tools/offline-runtime/setup-offline-runtime.ps1`（Chrome 静默安装 / **LibreOffice `msiexec /a` 免管理员解包**）；`office_render` 新增便携 soffice 三级探测（**零配置**）；`install.ps1` 安装末尾自动处理离线组件且**失败不阻断**，并修掉覆盖 `version.json` 溯源字段的缺陷；打包脚本新增**离线组件完整性自检**与 `-SkipOfflineRuntime` / `-ZipOnly`；产出 `JZToolsHub-v1.7.zip`（618.4MB）。文档见 `docs/离线部署包说明.md`。实测：`.xls→.xlsx` 归一化、应用自动探测、`render_sheet` 真实渲染全绿 |
| 2026-09-14 | 知识库阅读体验收尾 | `.reader-frame` 改 `width: fit-content` 贴合预览内容宽度；Word 超版心固定宽表格在卡片内横向滚动；长 token 断词修复横向滚动条（style.css v24） |
| 2026-09-13 | 知识库 Office 预览引擎替换 | 引入 `backend/vendor/{xhr,dhr}` 双引擎（纯标准库，零新增 pip 依赖），**舍弃** LibreOffice 转 PDF 与 openpyxl 手绘 HTML 两套旧方案；新增适配层 `office_render.py`；`/preview` 改为按需同步渲染 + 磁盘缓存 `<id>.preview.json`，失败 404 前端回退 mammoth/SheetJS |
| 2026-09-11 | 知识库旧版格式自动转换 | `doc_convert.py`（olefile + python-docx / xlrd + openpyxl，纯 Python，不依赖 Office COM）；上传白名单加 `.doc`，元数据记 `original_ext`，页面三处提示 |
| 2026-09-11 | 管理后台批量导入导出 | `admin/backend/batch_io.py`（依赖注入挂载）；单位/部门/人员三页「下载模板 / 导出 / 批量导入」；两步式 dry_run 预览；单元格格式系统加固（合并单元格、表头定位、多工作表、多分隔符、长数字精度、公式无缓存值、全角与不可见字符） |
| 2026-09-11 | 轨迹速写插件 | `plugins/trajectory-sketch/` 新增；调「过滤器」`/apply` 做字段过滤 → 轨迹分析 → 速写报告；`backend/engine/` 零依赖可插拔分析引擎（与 `D:\SQLRewrite` v2 逐项对拍一致） |
| 2026-09-11 | v1.6 打包 | `deploy/JZToolsHub-v1.6.zip`（约 102MB）；源码 + waitress 全链路验收通过 |
| 2026-09-10 | 过滤器插件 | `plugins/file-filter/` 新增：硬过滤 / 大模型语义过滤 / 文本与正则后处理；`/apply` 程序化接口 |
| 2026-09-09 | v1.5 战果录入优化 | 入库时间不展示、时间筛选改按战果时间、主办人默认空、防浏览器自动填充、手动录入卡片、返回落点修正、单位换算修复 |
| 2026-09-08 | v1.4 信息传输重构 + APP 1.4 | 双模式（精简/原件）、zlib 压缩信封、多文件封装、停止按钮、协议 v2 `fmt=file`；APP 深色模式、XlsxWriter/Zlib 还原 |
| 更早 | 基础设施 | 数据根目录可配置化 + 双指针 + 旧数据迁移；一键安装/更新/卸载；配置模板同步（版本门控）；登录改造与强制鉴权；首页拖拽排序与隐藏工具 |

---

## 4. 待办清单

### P0（建议下次发版前处理）

> **2026-09-14 更新：1~6 项已全部修复并实测通过**（方案见 `docs/P0问题修复方案.md`，实测结果见评估报告 §5.1）。
> 第 1 项（发版）已于同日执行完毕，产出 **v1.7 离线部署包**（见 §2.2 与 `docs/离线部署包说明.md`）。

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | 重新打包并发版 | ✅ **已完成**：`deploy/JZToolsHub-v1.7.zip`；版本 1.6 → 1.7 自动递增，打包解释器 Python 3.14.7；已包含知识库引擎改造、P0 六项修复，并新增随包离线组件 |
| 2 | 清理/补齐模板同步清单 | ✅ 已修：模板统一改名 `config.template.json`，移除 2 条失效的 `prompt.json` 登记，补齐 case-report / character-graph / file-filter 三个模板。实测同步 **5 / 5 全绿**，4 个运行时 config.json 正确初始化 |
| 3 | 模板被打包脚本删除 | ✅ 已修：模板改名后不再命中"删 `config.json`"规则（规则为精确名匹配）；`build-deploy.ps1` 新增打包后自检（`*.template.json` 少于 4 个即中止） |
| 4 | `config/data_root.json` 移出版本库 | ✅ 已修：`.gitignore` + `git rm --cached`；`git ls-files config` 现只剩 `tools.json` |
| 5 | 验证 `fmt=file` 端到端 | ⏳ **待真机**：桌面封装 → APP 扫码 → 导出 → 逐字节哈希比对 |
| 6 | 单插件加载失败隔离 | ✅ 已修：`register_plugin_backends` 内 per-plugin try/except，失败记入 `app._plugin_load_errors` 并告警。实测"两个插件各定义 `def status()`"不再抛异常，仅 plug-b 被隔离，整站正常启动 |

### P1（功能与质量）

| # | 事项 |
| --- | --- |
| 7 | ~~`file-filter` 的 `def status()` 改为带前缀~~ ✅ **2026-09-14 已修**：8 个路由函数统一 `ff_*` 前缀，`admin` 的 `account_change_password` → `admin_account_password`；实测 11 个插件 136 个 endpoint 全部注册成功 |
| 8 | 统一前端资源版本号起点与步长（现 case-report 用序号 v34、character-graph 用日期戳、admin 各页同一份 CSS 引用了 v1/v2/v5 三个版本） |
| 9 | 清理随包分发的开发期文件：`knowledge-base/backend/{_build_phase8.py,test_routes_preview.py}`、`trajectory-sketch/frontend/icons/_generate.py`、`map-marker/frontend/*.py` |
| 10 | 为 `file-filter`（🧹）补 SVG 回退图标，或统一 SVG 回退为按 codepoint 自动推导（不再维护硬编码映射表） |
| 11 | 首页空分类处理：`ai` / `design` / `maps` 无启用工具时不应显示 |
| 12 | 删除 `app.py` 中未被消费的 `_EMOJI_ICON_FILES` / `icon_file` 字段（前端统一由 `jz-icon.js` 兜底），或将前端改为消费它 |
| 13 | 在 `/api/tools` 暴露 `plugin_errors`（默认空 dict），首页对加载失败的插件显示"暂不可用"（本轮只做了日志与 `_plugin_load_errors` 记录） |
| 14 | **离线包真机验证**：在一台**真的断网**机器上走完整链路（解压 → 一键安装 → Chrome 安装 → LibreOffice 解包 → 启动 → 知识库打开 `.doc`/`.xls`）。开发机已验证 `msiexec /a` 解包（免管理员）与渲染链路，但 **Chrome 静默安装需提权**，开发会话无管理员权限未能实测 |
| 15 | 每次发版前校准 `runtime/` 组件版本：Chrome 组件用固定 URL `stable`（内容随 Google 更新而变），冻结的 `sha256` 会随之失配 → 需重跑 `fetch-offline-bundle.py --pin`，或改用带版本号的下载地址 |

### P2（体验与规范）

| # | 事项 |
| --- | --- |
| 16 | 更新《插件设计规范》：补充数据根目录、`home_card()`、`grant_all`、endpoint 命名前缀、程序化接口、模板同步清单登记等（详见评估报告 §4） |
| 17 | 在目标机做一次「全新安装 → 更新 → 卸载」三段式实测 |
| 18 | 清理工作区构建产物：`deploy/`、`dist/`、`build/` 与 `runtime/` 合计约 2.5GB（含 4 个历史版本目录与 12 个旧 zip） |
| 19 | 生产部署评估：当前上限约 300 并发（Flask/waitress 单进程线程模型），高负载建议 Gunicorn 多进程 + Nginx 反代 |
| 20 | 移动端：恢复或彻底移除「重置」功能；`CameraScanner.averageLuminance` 亮度采样保留但无人调用 |
| 21 | 知识库 `.xls`/`.doc` 每次转换都新建临时 profile（vendor 行为，实测约 30~60s/次）——若该路径调用频繁，考虑在适配层做 profile 复用（历史上曾用 `<数据根>/.lo-profile/` 把冷启动从 37~40s 降到 15~18s） |

---

## 5. 关键架构决策

| # | 决策 | 理由 | 代价 / 注意 |
| --- | --- | --- | --- |
| A1 | **一切皆插件，连鉴权都是插件** | 框架一经打包不再修改；新功能零代码接入 | `admin` 必须始终加载，否则全站鉴权失效；`register_plugin_backends()` 对它无条件加载 |
| A2 | **数据与程序目录分离**（数据根目录 + 双指针） | 整体替换程序文件夹升级不丢用户数据 | 插件必须经 `jztools_data` 定位数据，禁止拼绝对路径；`.admin_key` 与 `admin.json` 必须同目录迁移 |
| A3 | **配置模板同步按版本门控** | 根治"换文件后 prompt 不更新" | 发版必须递增 `-Version`；新增模板要两处登记（`_TEMPLATE_SYNC` + `install.ps1`） |
| A4 | **前端 iframe 隔离** | 插件之间、插件与框架互不污染 | 插件前端必须用相对路径；插件不能操作 parent 文档 |
| A5 | **后端路由统一 `/api/<id>/` 前缀 + 函数名带插件前缀** | 避免多插件路由与 endpoint 冲突 | 新增插件务必重启并确认日志出现「已注册后端插件：\<id\>」 |
| A6 | **长耗时一律异步任务**（提交即返回 + 轮询） | 不占用 HTTP worker | 有界线程池 + TTL 30 分钟 + 归属校验；前端轮询上限要与后端超时对齐 |
| A7 | **插件间禁止 import，只走程序化 HTTP 接口** | 解耦，避免卸载连锁崩溃 | 能力复用需提供 `/apply` 类接口并自己处理会话传递 |
| A8 | **知识库预览改为按需渲染 + 磁盘缓存** | 引擎亚秒级（首渲染 19~70ms），无需后台状态机 | 文件不可变所以缓存无失效逻辑；**改引擎/升级 vendor 后必须删旧 `<id>.preview.json`** |
| A9 | **轨迹分析引擎做成零依赖可插拔包** | 算法独立演进、可独立自测，不动路由与前端 | 新增算法版本只需在 `engine/algorithms/` 下加目录并注册 |
| A10 | **移动端完全离线**（仅 CAMERA 权限，禁止 INTERNET） | 业务数据通过二维码光学传输，不落公网 | 任何联网能力都不应被加入 |
| A11 | **错误文案作为逐字契约** | 桌面端按文案做 e2e 断言 | 改文案 = 改《移动端APP.md》+ 桌面端 + APP 三处 |
| A12 | **OS 基线与浏览器基线分开定义**（OS = Windows 10+；浏览器 = Chrome ≥72） | 两者触发不同的兼容措施，混在一起会导致"取消 Win7 顺手删掉旧浏览器兼容代码"的误伤 | 提浏览器基线必须先确认内网浏览器已升级；§7.3 第 15/17 条列明了不可删的 4 处兼容措施 |
| A13 | **取消 Windows 7 支持，Python 基线冻结 3.14** | 实测项目自身代码 3.8 可用，唯一"必须 3.14"的原因是 vendor 引擎的一行 bug——**修 bug 后版本选择就不再被绑架**；而 3.8 已 EOL、3.10 也将 EOL，维持旧支线只有成本 | 目标机基线收窄到 Windows 10+；不再维护 3.8 打包支线；依据与实测见 `docs/Python版本选型评估.md` |
| A14 | **无官方 wheel 的 C 扩展由仓库随包提供预编译 wheel**（`wheels/`） | `zfec` 无 3.14 官方 wheel 会导致"换台机器打包就失败"；固化制品后打包可复现 | 需维护制品（升级 Python 小版本时重做）；注意 zfec 为 GPL-2+，再分发需确认合规口径（见 `wheels/README.md`） |

---

## 6. 已知问题

### 6.1 功能/数据类

| 级别 | 问题 |
| --- | --- |
> **2026-09-14 状态更新**：本节原列的 P0-1/2/3 与 Python 版本表述问题**均已修复**（详见下方标注与
> `docs/P0问题修复方案.md` §7 实测）。保留行文便于回溯。

| 级别 | 问题 |
| --- | --- |
| ✅ 已修 | ~~`_TEMPLATE_SYNC` 与 `install.ps1` 登记的 `prompt.json` 模板在仓库中不存在~~ → 已从两处清单移除该 2 条登记；提示词改由 `llm_client` 内置默认值提供（用户手写的 `<数据根>/plugins/<id>/prompt.json` 仍优先且不再被版本覆盖）。复测：清单 5 条、源缺失 0、同步成功 5/5 |
| ✅ 已修 | ~~`build-deploy.ps1` 删除插件树内所有 `config.json`，会连带删除同步模板~~ → 模板统一改名 `config.template.json`（清理规则为精确名匹配，不再命中），打包后自检 `*.template.json` 数量 < 4 即中止 |
| ✅ 已修 | ~~`config/data_root.json`（含开发机绝对路径）已纳入版本库~~ → 已 `.gitignore` + `git rm --cached`；`git ls-files config` 现只剩 `tools.json` |
| ✅ 已修 | ~~README 宣称 Python 3.8 为目标环境，而知识库引擎要求 ≥3.10~~ → **两处说法都是错的**。实测：项目自身代码 3.8 可用；引擎在 3.8/3.13 都因 `vendor/xhr/__init__.py` 缺 `from typing import Optional` 而导入即 `NameError`，3.14 靠 PEP 649 侥幸可用。已打补丁 + 统一口径为"最低 3.12 / 打包 3.14" + 取消 Win7（见 `docs/Python版本选型评估.md`） |
| 低 | `app.py::_EMOJI_ICON_FILES` 提供的 `icon_file` 字段前端无人消费，属冗余维护点；`file-filter` 的 🧹 无任何 SVG 回退 |
| 低 | `ai` / `design` / `maps` 分类在当前默认配置下无启用工具，首页显示空分类 |
| 低 | `knowledge-base/backend/` 与 `trajectory-sketch/frontend/icons/`、`map-marker/frontend/` 混入了开发/测试脚本，随包分发 |
| 低 | `plugins/info-transfer/backend/requirements.txt` 仍列 python-docx / xlrd / olefile（代码中确为可选依赖，用于"精简传输"模式），与部分文档"已移除该依赖"的表述不一致 |

### 6.2 兼容性 / 部署类

| 级别 | 问题 |
| --- | --- |
| 中 | **zfec 无 Python 3.14 官方 wheel**（PyPI 上 1.6.0.0 只到 cp313、最新 1.6.0.1.post0 也只到 cp313），`pip install` 会退化为源码编译，要求构建机装 MSVC。**已缓解**：仓库 `wheels/` 随包提供预编译 wheel，安装时加 `--find-links wheels`；打包脚本已加依赖完整性前置检查。**未彻底解决**：升级 Python 小版本时仍需重做该 wheel（见 `wheels/README.md`） |
| 中 | `config/data_root.json` 已移出版本库，但**旧克隆**里仍带着开发机路径；若发现数据根指向他人目录，删除该文件即可回落默认 |
| 低 | 300 并发以上成功率下降（连接排队/拒绝，非应用异常），源于单进程线程模型 |
| 低 | 工作区构建产物约 1.2GB（`deploy/` 含 3 个历史版本目录与 11 个旧 zip、`dist/` 220MB、`build/` 42MB），均已被 gitignore 但占用磁盘与备份 |
| 低 | zip 覆盖安装时旧版独有文件不会被删除（若未来删文件需注意残留） |

### 6.3 流程类

| 级别 | 问题 |
| --- | --- |
| 中 | 本次评估前的 HANDOFF 记录"大量未提交改动"已与 git 实际状态不符；文档滞后于仓库是本项目反复出现的风险 |
| 低 | 各插件前端 `?v=` 版本号起点/步长不统一；`admin` 各页面对同一份 CSS 引用了不同版本号 |

---

## 7. 踩坑清单（按主题分类）

### 7.1 数据安全（最高优先级）

1. **绝不要清理 `plugins/*/backend/data/` 或数据根目录下的 `plugins/<id>/data/`**。历史上两次在测试脚本里误删用户真实台账（git 不含插件 data）。测试清理只删自己刚建的文件，或改用独立临时目录；动手前先列目录。
2. **`config/admin.json` 含密码哈希、Fernet 加密字段与真实 LLM API Key 密文**；`config/.admin_key` 是密钥。两者均已 gitignore，任何 `git add` 前确认不会带上。各插件的 `backend/config.json`（如战果录入/星图/过滤器含明文 API Key）同理。
3. **测试任何插件后端都要隔离数据根目录**：先 `import jztools_data; jztools_data.get_data_root = lambda: <临时目录>`，**再** `import app`；用 `app.test_client()` 打接口，**不要调用 `init_data_root()`**（会触发旧数据迁移）。需要真实 HTTP 时用 `app.run(port=5099)` 避开用户的 5000。
4. **浏览器走查不要污染真实数据**：把数据根整目录 `copytree` 到临时目录（约 10MB），在副本里注入预置记录再起隔离服务。

### 7.2 插件后端

5. **Flask 视图函数名全局唯一 —— 路由函数必须带插件前缀**。两个插件各写一个 `def status()` 会在启动时抛 `AssertionError: View function mapping is overwriting an existing endpoint function`，导致注册中断（轨迹速写与过滤器曾因此撞车）。新增插件后务必重启并确认日志出现「已注册后端插件：\<id\>」。
   *（2026-09-14 起框架已加 per-plugin 隔离：失败插件只记入 `app._plugin_load_errors` 并告警，不再拖垮整站。但隔离**无法回滚**已注册了一半的路由，命名纪律仍需遵守。）*
6. **插件 backend 内跨模块 import 必须两步式兜底**：
   ```python
   try:
       from . import X as _X
   except ImportError:
       try:
           import X as _X
       except ImportError:
           _X = None
   ```
   只写 `except Exception` 或缺裸 import 兜底，会让脚本方式加载的实例永远判定"模块不存在"。
7. **Werkzeug ≥2.3 的 test_client 会忽略手写 Cookie 头**。插件间"进程内派发"复用彼此接口时，`headers={"Cookie": ...}` 与 `environ_overrides={"HTTP_COOKIE": ...}` 都会被忽略导致 401；唯一可靠做法是 `client.set_cookie(name, value, domain="localhost")` 逐条塞进 jar（见 `trajectory-sketch/backend/filter_bridge.py::_client_with_session`）。
8. **元数据迁移的幂等判定用「键是否存在」而非取值**：`if rec.get("pdf_size") is None` 会让每次启动都判定需迁移；正确写法 `if "pdf_size" not in rec:`。
9. **同目录扫描 `.json` 要过滤非记录文件**（如战果录入的 `item_categories.json`），否则把辅助库当"记录"读入导致 undefined。
10. **`/api/logout` 是 POST 不是 GET**。
11. **`/files?category=root` 只返回已归类文件**（未分类的 `category_id=None` 不在过滤集内）；隔离测试/兜底轮询用 `category=all`。

### 7.3 前端

12. **改了 JS/CSS 必须递增 `?v=N`**：`/plugin/` 下静态资源 1 天强缓存，`.html` 才是 no-cache。这是最常踩的坑。
13. **DOMPurify 会整块丢弃 `<style>` 元素**。知识库 xhr/dhr 引擎是 class 型 CSS，`sanitize()` 之后 Excel 全裸、Word 表格无边框。解法：先正则摘取全部 `<style>`（**全局正则**，dhr 的表格内还有第二个 style 块）→ 正文过 DOMPurify → CSS 原样挂回。**改 `reader.js` 时别把这段摘取逻辑当冗余删掉。**
14. **下载文件名从响应头解析**：Flask `send_file` 的 `download_name` 已处理 RFC 5987（中文名老浏览器可用），前端不要自己拼 `Content-Disposition`；前端走 fetch→blob 时从 `filename*=UTF-8''` 解析，回退 `filename=`。
15. **旧浏览器兼容代码不是"Win7 遗产"，禁止当垃圾清理**（Chrome 72/78 是内网既有环境，OS 基线提到 Win10+ 也照样存在）：
    - `static/js/main.js` 不用可选链 `?.`（Chrome < 80 会语法报错，整页白屏）；
    - `static/js/jz-icon.js` + `static/icons/` 共 42 个 Twemoji SVG：emoji 渲染成方框时回退图片（触发条件是"缺彩色 emoji 字体"，Windows Server / 精简版系统同样命中）；
    - `knowledge-base` 的 pdf.js 用 legacy 构建，避免 `??` 与 CSS `gap`（Chrome < 84）；
    - 各插件页内图标优先自带 SVG（`plugins/trajectory-sketch/frontend/icons/`）。
    新样式优先用纯文本或自绘 SVG；新增插件 emoji 图标只需在 `static/js/jz-icon.js` 的 MAP 补一条。
16. **知识库「已转换」提示用 `sessionStorage` 记忆关闭状态**，不能用 `localStorage`（一个文件被多人先后打开，互不影响）。
17. **目标机 OS 基线与浏览器基线是两件独立的事**：OS 基线是 Windows 10+，浏览器基线是 Chrome ≥72。**取消 Win7 支持不等于可以放宽浏览器基线**——若要提浏览器基线，必须先确认内网老浏览器确实已升级，否则会出现"在 Win10 机器上照样白屏"的误判（详见 §1.1 与 README §2.1）。

### 7.4 打包 / 安装脚本与本地环境

18. **改了安装/卸载逻辑必须改仓库根目录源文件并重新打包**：部署包里的是副本，直接改包内脚本不会回写仓库。
19. **编码约定**：`install.ps1` 必须 UTF-8 with BOM；`一键安装.bat`/`一键卸载.bat` 必须 GBK/ANSI **且不要加 `chcp 65001`**；两个 .bat **必须 CRLF 换行**（曾因存成 LF 导致 cmd 拼接解析报碎片错误）。脚本写出的 JSON 一律 UTF-8 无 BOM。
20. **`install.ps1` 只能在「含 JZToolsHub.exe 的解压目录」里跑**（`install.ps1:264` 有守卫，会直接报「当前目录不是一键安装包」并退出）。历史上曾在仓库根目录误跑——那里的 `config/data_root.json` 备份指针会让"既有安装判定"分支（`:276`）误判为就地更新；该误导已在守卫 + 注释中说明修复，**别再拆掉这个守卫**。
21. **发版必须递增 `-Version`**，否则 `sync_templates()` 判定未升级而跳过模板同步。
    *（2026-09-14 起 `build-deploy.ps1` 已防呆：不传 `-Version` 会自动递增 patch；最终版本号与上一版相同时**直接报错中止**，确需同号重打要显式加 `-Force`。）*
22. **配置模板必须命名 `config.template.json`，绝不能叫 `config.json`**。打包脚本 `build-deploy.ps1` 的清理规则是"删插件树内所有 `config.json`"（意图是清掉本机含 API Key 的运行时配置），精确名匹配；模板若沿用 `config.json` 会被顺带删掉，导致部署形态下模板同步**静默失效**（实测曾使 6 条登记项只剩 `tools.json` 有效）。打包后脚本会自检 `*.template.json` 数量（< 4 即中止）。
23. **`JZToolsHub.spec` 的 PACKAGES 不要为纯标准库引擎加条目**（知识库 xhr/dhr 随插件目录分发，PyInstaller 不感知也不需要）；只有 pip 包才需要 `collect_all`。
24. **打包解释器必须与基线一致，且依赖必须装齐**（2026-09-14 起脚本已加两道前置检查）：
    - `build-deploy.ps1` 会打印解释器版本，**与基线 3.14 不符时告警**（用 3.8/3.10 打出来的包会缺功能且无人察觉）；
    - 会逐个 import `JZToolsHub.spec` 里 collect_all 的 14 个库并列出缺失项——**缺库不会让打包失败**，只会产出功能残缺的包，所以必须显式拦截；
    - `version.json` 现在记录 `{app, schema, commit, built_at, python}`，目标机可据此核对"包是哪个提交、哪个 Python 打的"；
    - 其中 **`zfec` 需要 `--find-links wheels`**（无 3.14 官方 wheel），详见 `wheels/README.md`。
25. **`install.ps1` 与 `jztools_data.py` 的模板同步是两套实现，必须语义等价**：`ensure-keys` 在 Python 侧是**递归**补键（`_ensure_deep_keys`），PowerShell 侧此前只并顶层键——模板在嵌套层新增键时，一键安装路径补不上。现 `install.ps1` 已改用 `Merge-DeepKeys` 递归实现（已在 PS 5.1 实测：已有值保留、嵌套新键补入）；改任一侧都要同步另一侧。
26. **本机 PowerShell 环境会把子脚本（`& script.ps1`）的输出整个吞掉**，且 `Invoke-Expression` 被安全策略拦截；验证脚本逻辑时要么把函数体直接写在命令里，要么让脚本自己 `Out-File` 落盘再 Read。
27. **工作区脏文件提示**：`git status` 常报 `.workbuddy/memory/*.md` 与 `*.ps1` 的 LF→CRLF 警告，属换行符归一化提示（`.gitattributes` 只对 `.bat/.ps1/.cmd` 强制 CRLF），非错误。
28. **LibreOffice 便携部署用 `msiexec /a`，不要 `msiexec /i`**：`/a` 是「管理安装」，**免管理员、不写注册表**，解出的目录可直接运行（2026-09-14 实测 26.8.0：退出码 0、耗时 95s、解出 1.5GB，`program\soffice.exe` 就在 `TARGETDIR` 根下）。`/i` 需要管理员且装进 `Program Files`。**解包后的便携目录不要放进仓库 `runtime/`**——它会与 MSI 一起被打进包，体积翻三倍（1.5GB vs 0.36GB）。
29. **调 `msiexec` 必须等它真正结束**：PowerShell 里 `& msiexec.exe ...` 会提前返回（msiexec 是启动安装服务后就退出的壳），必须 `Start-Process -Wait -PassThru` 取 `ExitCode`；`TARGETDIR=<含空格路径>` 要整体加引号。
30. **便携 LibreOffice 的探测优先级是三层**：插件配置 `office.soffice_path` > 进程环境变量 `XHR_SOFFICE` > `<程序目录>/runtime/libreoffice/program/soffice.exe`（多套一层时在 `runtime/libreoffice/**` 内有限深度搜索）。改这条链要同时看 `office_render._apply_soffice_env()` 与 vendor 的 `find_soffice()`——vendor **每次调用都读环境变量**，所以配置改动无需重启即生效。
31. **`install.ps1` 曾把 `version.json` 覆盖成 `{app, schema}`**，抹掉 `build-deploy.ps1` 写进去的 `commit` / `built_at` / `python` / `offline`，导致装完之后再也无法从包内判断代码出自哪个提交。现改为「读旧文件 → 保留未知字段 → 只更新 `app`」。
32. **离线组件失败绝不能阻断安装**：Chrome 是全机 MSI，没提权必然失败；脚本据此打印手动指引后继续（与「缺依赖插件优雅降级」同一原则）。另外 `install.ps1` 调 `setup-offline-runtime.ps1` **必须走子进程**——那个脚本以 `exit` 结尾，dot-source 会连带终止整个安装流程。
33. **zip 压缩用 `-CompressionLevel Fastest`**：离线包约 700MB，其中 MSI 本身已是压缩格式，`Optimal` 几乎减不了体积却要多花数分钟。
34. **打包前先提交，否则 `version.json` 的 `commit` 指向错误的提交**：`build-deploy.ps1` 记的是**构建那一刻的 HEAD**，工作区若有未提交改动，包内代码其实来自"HEAD + 改动"，事后无法据此定位源码。2026-09-14 已加防护：`git status --porcelain` 非空时把 `commit` 记成 `<sha>-dirty`。**正确姿势是先 commit 再打包**；若已用脏工作区打了包，要么重打，要么手工把 `version.json.commit` 校正为实际对应的提交（v1.7 首版就是后者）。
35. **只重出 zip 用 `-ZipOnly`**：跳过 PyInstaller 与目录组装，实测约 2 分钟（全量打包约 16 分钟）。**前提是先把新文件复制进 `deploy\JZToolsHub\`**——它不重新组装，只压缩既有目录。另注意变量名不能叫 `$zipOnly`（PowerShell 变量名不分大小写，会与开关参数撞成同一变量）。

### 7.5 第三方库行为

36. **openpyxl 四个坑**：① `read_only=True` 读不到合并单元格（`ReadOnlyWorksheet` 无 `merged_cells`），要处理合并必须用普通模式；② `data_only=True` 对"从未被 Excel 计算过"的公式返回 `None`，判断"是不是公式"要用 `data_only=False` 再加载一遍比对；③ Excel 数字只保留 15 位有效数字，18 位身份证按数字存会被静默改写（必须靠"原始单元格是 float 且 ≥1e15"识别）；④ 拒绝写入 XML 非法控制字符（造测试夹具时别塞，但 CSV 可以携带，解析时要清理）。
37. **Word 二进制 `.doc` 解析五个易错点**：① FIB 在 `WordDocument` 流 0x1A2 处的 `fcClx/lcbClx` 指向 `0Table`/`1Table` 的 CLX 分片；② PlcPcd 用可变长度 CPs；③ `\x07\x07` 是行结束（单 `\x07` 是单元格结束，`\r` 是段落结束）；④ 闭包捕获 `buf=[]` 后函数内 `buf=[]` 会重绑定，要用 `del buf[:]`；⑤ `close_row()` 不得给空缓冲区补单元格，否则凭空多出空列。
38. **跨项目移植算法必须对齐计量口径**：地球半径取 6378137 且结果 `round()` 取整；地点簇建在"清洗后未去重的行"上；"采样间隔中位"含 0 间隔而阈值推导用有效间隔（Δt>0），两者不可混用。差一个采样点对拍就不一致。
39. **xhr 渲染器的 `style_table[0]` 不是"默认样式"**而是"最先出现的样式"；缺格取 `[0]` 会把空白区染色（已在 vendor 副本打补丁，见 `backend/vendor/README.md`）。
40. **LibreOffice 解包版 `soffice --version` 会挂起**；超时压到 10s 并吞异常（版本仅展示用）。固定 profile 复用（`<数据根>/.lo-profile`）可把冷启动从 37~40s 降到 15~18s；失败时重置 profile 再试一次。
41. **给外部转换程序做测试夹具用 `.bat` 转调 Python**：`subprocess.run` 能直接跑 .bat 但不能跑 .py。
42. **vendor 引擎的 `Optional` 缺失（已修，但历史极隐蔽）**：`vendor/xhr/__init__.py:84`
    `def convert(data: bytes, options: Optional[ConvertOptions] = None)` 用了 `Optional` 却**既未导入、
    也未写 `from __future__ import annotations`**，而该注解是**运行时立即求值**的 →
    **Python ≤3.13 导入引擎即 `NameError`**，Word/Excel 预览整体失效。Python 3.14 因
    PEP 649 惰性注解**恰好躲过**，所以这个缺陷在 3.14 开发机上完全看不见。
    影响面隐蔽还有一层：`office_render.py` 用 `except Exception` 兜住导入失败，所以**不会报错**，
    只是静默回退到 mammoth/SheetJS 降级渲染，只有 `GET /api/knowledge-base/status` 的
    `office_render.available=false` 能看出来。已补 `from typing import Optional` 并记入
    `vendor/README.md` 的**升级后必须重打清单**（与 `renderer/table.py` 补丁同级）。
    **推论：不要用 Python 3.14 的"能跑"去判断第三方代码的正确性**——PEP 649 会掩盖注解类缺陷。
43. **新增依赖前先分清新旧 wheel 类型**：`cpXX-cpXX`（版本锁定，每个 Python 小版本都要等新 wheel）
    如 `zfec` / `numpy` / `pillow`；`cp3X-abi3`（稳定 ABI，跨版本可用）如 `opencv-python`（cp37-abi3）
    与 `cryptography`（cp311-abi3）。
    验证命令：`python -m pip download <包> --no-deps --only-binary :all: -d <临时目录>`
    —— 返回 `No matching distribution found` 就说明当前版本只能源码编译。
    **`zfec` 就是这样卡住的**（PyPI 上最高只到 cp313），处理方式见 `wheels/README.md`。

### 7.6 工具链

44. **Bash 工具 PATH 损坏**（`ls`/`wc`/`dirname` 可能 command not found），**PowerShell 的 stdout 可能被吞**——探测类命令写临时文件再用 Read 读取；`Read`/`Glob`/`Grep`/`Write`/`Edit` 工具不受影响，优先用它们。
45. **Bash heredoc 会吃反斜杠**（`"\t"`/`"\n"` 落盘成真实制表符/换行导致 JS 语法错误）。含转义序列的补丁一律用 Write 工具写脚本文件再执行。
46. **PowerShell 里跑含引号/花括号的 `python -c "..."` 极易翻车**；复杂断言先写临时 .py 再执行。
47. **中文输出乱码大多是 PowerShell 管道显示问题**，数据本身是 UTF-8；断言写在 Python 代码里（`assert ... , dict`），别靠肉眼读控制台。
48. **浏览器自动化（agent-browser）**：插件前端跑在 iframe 里，`click <css>` 默认作用于父文档会 "Element not found"，登录后直接开 `/plugin/<id>/index.html` 最省事；**每次 CLI 调用都是新会话**，登录与后续操作必须放进同一次 `batch`；默认视口约 1080×480，模态框高于视口时真实点击会落到遮罩上关掉浮窗，实测前先 `set viewport 1440 1000`；本机有 `http_proxy` 时要设 `no_proxy=127.0.0.1,localhost`。

---

## 8. 插件后端速查表

所有后端插件共用同一套模式：`register(app)` 注册路由；会话取 `from jztools_admin.routes import get_session_user`；日志标记 `set_operation("…")`；数据统一存数据根目录 `plugins/<id>/`。

| 插件 | 路由前缀 | 长任务机制 | 数据落盘（数据根目录下） |
| --- | --- | --- | --- |
| admin | `/api/admin`、`/api/admin/batch`、`/login` | openpyxl（批量导入导出的 xlsx，缺库降级 CSV） | `config/admin.json` + `.admin_key` |
| notice-board | `/api/notice-board` | 无 | `plugins/notice-board/data/*.json`（一公告一文件，RLock 串行化） |
| knowledge-base | `/api/knowledge-base` | 无后台状态机；预览为**按需同步渲染**（`office_render._RENDER_LOCK` 串行 + `<id>.preview.json` 磁盘缓存，失败 404 前端回退） | `data/{categories.json,files.json}` + `data/files/<id>.<original_ext>` 原件 + `<id>.docx/.xlsx` 渲染件 + `<id>.preview.json` 预览缓存 |
| shared-docs | `/api/shared-docs` | 无（全局 RLock） | `plugins/shared-docs/data/*.json`（一文档一文件，历史上限 100） |
| case-report | `/api/case-report` | ThreadPoolExecutor(2)，TTL 30min | `data/*.json`（一记录一文件）+ `item_categories.json` + `config.json` |
| character-graph | `/api/character-graph` | ThreadPoolExecutor(2)，TTL 30min | `config.json`（LLM） |
| trajectory-convert | `/api/trajectory-convert` | ThreadPoolExecutor(2)，产物按 mtime TTL 30min 清理 | `.task_cache/`（mp4/png/zip）+ `config.json`（列名） |
| qr-video-decode | `/api/qr-video-decode` | ThreadPoolExecutor(2)，结果仅存内存 | 无落盘 |
| file-filter | `/api/file-filter` | ThreadPoolExecutor(2)，TTL 30min，产物与任务表双清理 | `.task_cache/` + `config.json`；`POST /apply` 为程序化接口 |
| trajectory-sketch | `/api/trajectory-sketch` | ThreadPoolExecutor(2)，TTL 30min，上传暂存与产物双清理 | `.task_cache/`（`_upload.<ext>` + `_report.xlsx`）+ `config.json`；引擎在 `backend/engine/`（`selftest.py` 可独立跑） |
| info-transfer | `/api/info-transfer` | ThreadPoolExecutor(2)，TTL 30min，支持取消 | `.task_cache/`（mp4/png/zip/帧 PNG） |

**异步任务三件套**（新插件抄这里）：`POST /api/<id>/<action>` 立即返回 `task_id` → 后台线程池执行 → `GET /api/<id>/result/<task_id>` 轮询 `{status: pending|running|done|error}`。任务表加锁、结果 TTL 30 分钟、任务归属校验（创建者或超管可见）。

---

## 9. 移动端 APP（android-app/InfoParse）

> 功能/协议/构建运行的"说明书"版本在 README「信息传输与移动端 APP（Android）」章节；**协议权威定义在仓库根目录《移动端APP.md》**（改协议必须同步它）。
> 注：本目录**已纳入版本管理**（63 个文件），早前文档"未纳入版本管理"的表述已过时。

### 9.1 定位与技术栈

- JZToolsHub「信息传输」插件的 **Android 离线接收端**：扫桌面端生成的二维码（静态多张 / QR-transfer 视频流）还原内容，全程无网络。
- Kotlin + CameraX 1.3.4 + ML Kit Barcode 17.3.0（bundled 离线模型，不依赖 GMS）+ Material3 1.12.0 + Gson；minSdk 29 / targetSdk 34。
- **完全离线**：唯一运行时权限 CAMERA，**禁止声明 INTERNET**。
- 三个 Activity（无 Fragment）：`HomeActivity`（历史卡片 + FAB）→ `ScanActivity`（识别页）→ `ResultActivity`（结果页）。

### 9.2 关键源码索引

| 文件 | 职责 |
| --- | --- |
| `HomeActivity.kt` | 主页：历史卡片 + FAB + 清空/删除确认（onResume 必刷新列表） |
| `ScanActivity.kt` | 识别页：相机扫码分流、图片/视频导入、暂存恢复（逻辑最重） |
| `ResultActivity.kt` | 结果渲染 + 导出/复制/分享（`file` 格式不预览内容） |
| `history/HistoryStore.kt` | 历史持久化（Gson，一条一 JSON，存 `filesDir/history/`） |
| `protocol/Envelope.kt` | 信封模型 + 解析器 + `Fmt` 常量（协议核心契约） |
| `protocol/PageCollector.kt` | 多页收集状态机（serialize/restore 暂存） |
| `protocol/QrFrame.kt` + `ZfecCompat.kt` | 视频流帧解析 + zfec 前向纠错重组 |
| `scan/CameraScanner.kt` | CameraX + ML Kit 封装；同文本去重；亮度采样（暂无调用方） |
| `export/Exporter.kt` | 导出 mime/文件名/MediaStore Downloads/分享 intent |
| `util/DocxWriter.kt` | 逐段文本 → 最小 .docx（兼容旧 word 码导出） |

### 9.3 构建命令（离线，可复制）

项目**没有可用的 gradle wrapper**（只有 `gradle/wrapper/gradle-wrapper.properties`），用本机 Gradle 发行版直接构建：

```bash
export JAVA_HOME="C:/Users/yfjz/.jdks/jbr-21.0.11"    # JBR 21；不要用 JBR 25，gradle 8.7 不支持
export GRADLE_USER_HOME="D:/GradleHome"
"D:/GradleHome/wrapper/dists/gradle-8.7-bin/bhs2wmbdwecv87pi65oeuq5iu/gradle-8.7/bin/gradle" \
  -p "D:/JZToolsHub/android-app/InfoParse" :app:assembleDebug :app:testDebugUnitTest --console=plain --offline
```

- SDK：`D:/Android/Sdk`（`local.properties` 已指向）；产物 `app/build/outputs/apk/debug/app-debug.apk`（约 37MB）。
- 正式签名：`android-app/InfoParse/release.keystore`（alias `infoparse`，密码 `infoparse2024`）。**升级包必须用同一 keystore**，否则用户无法覆盖安装。
- release 构建有 `lintVitalRelease` 卡点：**资源只在 `values-night` 声明、base values 没有同名项时 release 直接失败**（debug 不报）。
- adb：`D:/Android/Sdk/platform-tools/adb.exe`（不在 PATH）；崩溃排查第一步 `adb logcat -b crash -d`。

### 9.4 这台测试机的限制（别浪费时间尝试）

- `adb shell input keyevent/tap` 全部被拒（ROM 的「USB 调试（安全设置）」未开），**无法远程替用户点 UI**；
- `ScanActivity`/`ResultActivity` 未导出，`am start` 直拉会 Permission Denial，只有 `HomeActivity` 可直拉；
- 设备锁屏时 `uiautomator dump` 抓到的是 SystemUI；Git Bash 需 `export MSYS_NO_PATHCONV=1` 才能用 `/sdcard`。

因此 UI 层改动只能「编译 → 安装 → 请机主人工点验」；日志验证上限是"主页能启动 + crash 缓冲为空"。

### 9.5 移动端踩坑与铁律

1. **【最严重】insets 监听器里禁止引用 lateinit 成员**。`setupImmersive()` 曾因在视图绑定前执行且立即解引用 `btnReset`，导致识别页启动必崩（`UninitializedPropertyAccessException`）。三个 Activity 的 `setupImmersive()` 现在都只用局部 `findViewById`，改 UI 时别破坏；引用顺序必须是「setContentView → findViewById → 才能用」。
2. **Material 属性名不要想当然**：`?attr/materialButtonTonalStyle` 在 1.12.0 不存在（用 `@style/Widget.Material3.Button.TonalButton`）；BottomNavigationView 是 `itemIconTint` 而非 `itemIconTintList`。查真名：`unzip material-1.12.0.aar` 后 grep `res/values/values.xml`。
3. **Kotlin 表达式体函数里不能写 `return`**（带 return 一律写块体 `{}`）。
4. **单元测试里的 JSON 字符串别嵌裸引号**（`${'"'}` 生成的不是合法 JSON，会走到错误分支）。
5. **"word 还原不了原文件"是链路设计而非 APP 缺陷**：v1 封装端只提取文本且去掉扩展名，APP 端不可能还原；真正的修复是 v2 的 `fmt=file` 完整传输。排查"还原不对"先看封装端塞了什么数据。
6. **协议 v2 的数据量代价**：`fmt=file` base64 体积 +33%，静态码每张约 20KB、上限 200 页、20MB 上传上限；大文件引导用户用二维码视频流。
7. **历史回读必须复用协议解析**（`EnvelopeParser.jsonToRows`），自己随手 `asString` 会改变数字语义（1 → "1.0"）。
8. **桌面端改 `routes.py` 必须重启 JZToolsHub 服务**才生效。
9. **长会话中凭记忆 Edit 出过重复 import / 重复函数**：对同一文件多次编辑后、编译报符号冲突时先 Read 全文核对。
10. **进程被杀后从最近任务恢复 `ResultActivity` 会直接 finish**（`ResultStore.current == null`）——已知轻微问题，从历史卡片重新点开即可。

### 9.6 回归验证清单

- [ ] `assembleDebug` + `testDebugUnitTest` 全绿
- [ ] 主页：空状态 / 历史卡片（徽标配色）/ 长按删除 / 菜单清空（有确认）/ FAB 进识别页
- [ ] 识别页：权限被拒仍可「导入图片」；同码可重扫；多页码收齐出结果；「继续收集/放弃」可用
- [ ] 导入图片多选、导入视频（.mp4）
- [ ] 结果页：五种 fmt 徽标与统计；文本 4000 字符 / excel 100 行截断；file 不显示内容
- [ ] 导出 / 复制（excel 得 TSV）/ 分享（file 显示原文件名）/ 长按出名称气泡
- [ ] 沉浸式：三页状态栏延伸、底部不压手势条（手势 + 三键两种导航）
- [ ] **file 端到端：桌面封装 → APP 还原 → 与原文件哈希一致**（当前首要待办）

---

## 10. 文档同步约定与快速命令

### 10.1 改完功能必须同步的文档

```
README.md（架构 / 环境 / API / 目录 / 使用示例 / 插件一览 / 故障排查）
  → plugins/<id>/README.md（插件自身说明）
  → docs/<功能>-设计文档.md（如有）
  → HANDOFF.md（状态头「最后更新」、§2 git 状态、§3 已完成、§4 待办、§6 已知问题）
  → .workbuddy/memory/YYYY-MM-DD.md（当日工作日志）
```

改动**打包 / 安装 / 离线组件**链路时，另外三处必须一起改：
`build-deploy.ps1` ↔ `install.ps1` ↔ `tools/offline-runtime/`（脚本副本会随包分发到 `runtime/`）
+ `docs/离线部署包说明.md`（包体组成与体积数字）+ `tools/offline-components.json`（组件清单）。

改信息传输协议时**四端一文档齐改**：桌面插件 `routes.py` + Web 前端 + APP 端 + 《移动端APP.md》（改完记得重启服务）。
改 vendor 引擎时**必须同步 `vendor/README.md` 的补丁清单**（升级 vendor 整目录替换后要重打两个补丁）。

### 10.2 快速命令

```bash
# 准备依赖（3.14 基线；--find-links wheels 用于 zfec，缺了会退化成源码编译）
python -m pip install --find-links wheels -r plugins/admin/backend/requirements.txt
python -m pip install --find-links wheels -r plugins/<id>/backend/requirements.txt

# 启动（源码）
python app.py

# 下载离线运行组件（Chrome / LibreOffice，约 517MB，支持断点续传；--verify-only 只校验）
python tools/fetch-offline-bundle.py
python tools/fetch-offline-bundle.py --verify-only

# 打包（-Version 省略会自动递增；写出的 version.json 含 commit/built_at/python/offline）
powershell -ExecutionPolicy Bypass -File build-deploy.ps1 -Version "1.7.0"
powershell -ExecutionPolicy Bypass -File build-deploy.ps1 -SkipOfflineRuntime   # 瘦包

# 重建 zfec 预编译 wheel（换 Python 小版本后）
python tools/build-zfec-wheel.py

# 目标机：解压 zip → 双击 一键安装.bat → start.bat 启动；卸载双击 一键卸载.bat
#         离线组件可单独重跑：右键 runtime\安装离线组件.bat → 以管理员身份运行
```

```python
# 不进浏览器的快速自测
import sys; sys.path.insert(0, r"D:\JZToolsHub")
import app as m
m.init_data_root(); m.setup_access_logging(m.app); m.register_plugin_backends(m.app)
c = m.app.test_client()
c.post("/api/login", json={"username": "admin", "password": "admin123"})
```

### 10.3 关键文档索引

| 文档 | 内容 |
| --- | --- |
| `README.md` | 架构、环境搭建、运行构建、目录结构、核心模块、使用示例、HTTP API、插件一览、故障排查 |
| `HANDOFF.md`（本文件） | 开发状态、已完成/待办、架构决策、已知问题、踩坑清单、移动端 |
| `插件设计规范.md` | 插件开发铁律（B-1~B-21、SEC-1~SEC-11、F-1~F-7、S-1~S-8、M-1~M-3、V-1~V-7） |
| `移动端APP.md` | 信息传输协议权威规范（含错误文案逐字契约、判别顺序） |
| `20260914评估报告.md` | 插件规范符合性分析、开发/更新/移除便利性评估、规范优化建议、项目问题清单（P0 已修，附实测） |
| `docs/P0问题修复方案.md` | P0 六项的补丁级方案 + 实施清单 + 实测结果（含两处与原方案不同的判断） |
| `docs/Python版本选型评估.md` | Python 版本基线的完整实测依据：vendor 引擎一行 bug、三版本对照、zfec 无 wheel、升级节奏建议 |
| `docs/离线部署包说明.md` | **离线部署包**的组成、体积/时间成本、部署流程、适用场景、校验与已知限制 |
| `wheels/README.md` | zfec 预编译 wheel 的成因、来源、许可证（GPL-2+）、使用方式与重建步骤 |
| `runtime/README.md`（源在 `tools/offline-runtime/`） | 离线运行组件（Chrome / LibreOffice）的清单、安装方式、校验与再分发口径 |
| `docs/` | 登录改造与数据隔离、容器化与插件热插拔、知识库、插件库优化方案、过滤器、轨迹速写、批量导入导出等设计文档 |

---

*接手者请优先处理 §4 的 P0 清单，并在完成任何功能改动后按 §10.1 同步文档。*
