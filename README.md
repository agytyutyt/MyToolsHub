# JZToolsHub 工具箱

基于 **Flask + 配置驱动插件机制** 的在线工具集合网站。界面采用 **Google Material Design** 风格，首页以大方块（卡片）展示各工具模块，工具以 iframe 方式嵌入外壳页运行。

> 核心设计理念：**一切皆插件** —— 主框架（首页 + 外壳页 + 配置聚合 API）与工具业务完全解耦，新增、下线、排序工具均不需要改动框架代码。

---

## 目录

1. [快速开始](#快速开始)
2. [技术栈与运行环境](#技术栈与运行环境)
3. [目录结构](#目录结构)
4. [核心机制：一切皆插件](#核心机制一切皆插件)
5. [交互流程说明](#交互流程说明)
6. [HTTP API](#http-api)
7. [访问日志](#访问日志)
8. [并发与性能优化](#并发与性能优化)
9. [健壮性与安全](#健壮性与安全)
10. [开发者指南：开发插件](#开发者指南开发插件)
    - [7.1 纯前端插件](#71-纯前端插件)
    - [7.2 带 Python 后端的插件](#72-带-python-后端的插件)
    - [7.3 插件清单字段说明](#73-插件清单字段说明)
    - [7.4 注册插件到首页](#74-注册插件到首页)
    - [7.5 常见约定与注意事项](#75-常见约定与注意事项)
11. [内置插件一览](#内置插件一览)
12. [轨迹数据与二维码闭环](#轨迹数据与二维码闭环)
13. [打包部署（可上线程序）](#打包部署可上线程序)
14. [故障排查](#故障排查)
15. [后期接入真实后端](#后期接入真实后端)

---

## 快速开始

```bash
pip install -r requirements.txt
python app.py
```

浏览器访问 <http://localhost:5000> 即可看到首页。

> **默认管理员账号：** `admin` / `admin123`，首次启动自动生成（`config/admin.json`）。
> 登录后可访问管理后台 `/admin`，进行部门、人员、权限管理。
> 登录状态在首页顶栏右侧显示；顶部用户菜单内可直接「修改密码」（原密码 + 新密码 ≥6 位）。
> **强制登录（R2）**：未登录访问任意页面一律重定向到 `/login?next=…`，登录后原路返回；
> 未登录调用任意 `/api/*` 一律返回 401 JSON（白名单仅保留 `/login`、`/api/login`、
> `/api/logout`、`/api/session`、`/favicon.ico` 与无业务数据的静态资源）。
> **会话超时（R1）**：连续 `session.idle_minutes` 分钟（默认 30）无任何请求，或登录满
> `session.absolute_hours` 小时（默认 12）自动登出；可在 `config/admin.json` 顶层 `session`
> 节调整（首启自动生成，缺省即上述默认值）。页面每 60 秒心跳探测一次登录状态，过期自动跳登录。
> **敏感数据加密：** 账号密码、身份证号、大模型 API Key 等以 Fernet 对称加密密文
> 存入 `config/admin.json`（加密密钥保存在 `config/.admin_key`，两者均已 gitignore）；
> 该文件不存在时首次启动自动生成，请勿提交仓库。

> 本仓库开发目标环境为 **Python 3.8**（`requirements.txt` 已将 Flask 锁定在 `>=3.0,<3.1`，兼容 3.8）。若使用本机安装的 3.8，启动命令为：
> ```bash
> C:\Users\yfjz\AppData\Local\Programs\Python\Python38\python.exe app.py
> ```

---

## 技术栈与运行环境

| 项目 | 说明 |
| --- | --- |
| 后端 | Python 3.8 · Flask 3.0 |
| 前端 | 原生 HTML / CSS / JS（Material Design 风格） |
| 插件机制 | 目录扫描 + 配置文件驱动，前端 iframe 隔离，后端按需动态加载 |
| 依赖 | 见 `requirements.txt`（Flask / requests / python-docx / pypdf / openpyxl / xlrd / qrcode / zfec / numpy / opencv-python） |

```bash
# 依赖安装（Python 3.8）
C:\Users\yfjz\AppData\Local\Programs\Python\Python38\Scripts\pip.exe install -r requirements.txt
```

---

## 目录结构

```
JZToolsHub/
├── app.py                    # Flask 入口 + 配置聚合 API + 插件静态/后端加载
├── config/
│   └── tools.json            # ★ 后台配置：站点信息 + 分类 + 工具注册清单（含名称/说明/排序）+ 日志开关
├── plugins/                  # ★ 插件目录（一切皆插件：前端 + 后端自包含）
│   ├── base64/               #   示例插件：Base64 编解码（纯前端）
│   │   ├── manifest.json     #   插件清单（图标/主题色/入口/能力标签）
│   │   └── frontend/         #   前端资源（iframe 静态服务）
│   │       └── index.html
│   ├── json-formatter/       #   示例插件：JSON 格式化（纯前端）
│   ├── color-picker/         #   示例插件：取色器（纯前端）
│   ├── md5-generator/        #   示例插件：MD5 生成器（纯前端）
│   ├── map-marker/           #   插件：地图标点（高德地图 + 二维码识别回放）
│   ├── character-graph/      #   插件：人物关系立体星图（前后端一体）
│   │   ├── manifest.json
│   │   ├── frontend/         #   前端入口 + Three.js 资源
│   │   └── backend/          #   Python 后端（routes.py 等）
│   ├── trajectory-convert/   #   插件：轨迹转换（Excel → 二维码视频流 / 静态二维码）
│   ├── qr-video-decode/      #   插件：QR 视频流解码（jsQR 逐帧扫码 + zfec 纠错重组）
│   ├── shared-docs/          #   插件：共享文档（多人共同编辑 Word / Excel，实时同步 + 在线协作）
│   ├── case-report/          #   插件：战果录入（收网报告 → 五要素键值对 JSON 本地台账）
│   └── admin/                #   核心插件：管理后台（登录鉴权 + 部门/人员/权限管理 + 工具访问控制）
│       ├── manifest.json
│       ├── frontend/         #   后台页面 / 登录页 / 样式与脚本（/plugin/admin/... 提供）
│       └── backend/          #   routes.py：鉴权、后台接口、加密存储、工具访问拦截
├── static/
│   ├── index.html            # 首页（大方块展示）
│   ├── tool.html             # 工具外壳页（加载插件 iframe）
│   ├── css/style.css         # Material 风格主题
│   └── js/
│       ├── main.js           # 首页渲染逻辑（读取 /api/tools）
│       └── tool.js           # 外壳页加载插件逻辑
├── logs/                     # 访问日志（按天滚动，自动生成）
└── requirements.txt
```

---

## 核心机制：一切皆插件

JZToolsHub 的核心是可插拔工具架构，一切工作围绕 **`plugins/` 下的自包含插件目录**展开：

- **插件即目录**：一个插件 = `plugins/<插件id>/` 目录，内部包含 `manifest.json`（图标/入口/能力标签等元信息）、`frontend/`（前端资源）、以及可选的 `backend/`（Python 后端）。
- **配置即启停，也即展示**：`config/tools.json` 决定哪些插件展示、展示顺序与所属分类，同时是工具**名称 / 描述**的权威来源；`enabled: false` 即可后台下线，无需删代码；`hidden: true` 的插件（如 `admin`）只注册后端、不展示为首页卡片。
- **前端隔离**：工具页面通过 iframe 嵌入外壳页（`static/tool.html`），插件之间、插件与框架之间互不污染。
- **后端自发现**：只要插件目录下存在 `backend/__init__.py` 与含 `register(app)` 的 `routes.py`，框架启动时自动导入注册，无需在配置中声明。
- **核心插件始终加载**：`admin`（管理后台）为基础设施插件，`register_plugin_backends()` 对其无条件加载（不随 `enabled` 启停），保证登录鉴权、后台接口与工具访问控制一直可用。

可视化关系：

```
config/tools.json ──注册 + 展示名/描述──▶ plugins/<id>/manifest.json（图标/入口/能力）
        │                                    │
        │  enabled + name/description      frontend/（iframe 加载）
        ▼                                    ▼
   首页卡片 ◀── /api/tools ────────────── 外壳页 /tool/<id>
        │                                    │
        └─── 名称解析（日志）◀────────── backend/（启动时自动注册 Flask 路由）
```

> **首页卡片内容两级读取（名称 / 描述 / 图标 / 主题色 / 能力标签）**：
> 1. **插件主动声明（方式一）**：插件后端若提供可选钩子 `home_card()`，主应用在启动时登记该钩子，首页请求时于请求上下文内实时调用（可按当前登录用户权限返回动态内容），返回值作为卡片展示字段的**最高优先级来源**（见 7.2 / 7.4）；
> 2. **tools.json 兜底（方式二）**：插件未提供钩子时，`name` / `description` 以 `config/tools.json` 为权威取值，插件目录 `manifest.json` 中若也写了这两个字段，仅作为**缺省回退**（便于把插件目录整体复制到其他项目、尚未在配置中补充名称时依然可用）；`icon` / `accent` / `features` 默认取 `manifest.json`。

---

## 交互流程说明

1. 用户访问首页 `/`：`static/js/main.js` 请求 `/api/tools`，按分类渲染工具卡片（**每行固定最多 4 个**）；同时在页面**右侧生成分类快速定位标签**（每个分类一个标点，悬浮显示分类名，单击平滑滚动定位，随滚动自动高亮当前分类）。
2. 点击卡片进入外壳页 `/tool/<id>`：`static/js/tool.js` 请求 `/api/tools/<id>` 获取工具元信息，并将 iframe 指向 `/plugin/<id>/<entry>`。
3. `/plugin/<id>/<path>` 路由将 `plugins/<id>/frontend/` 下的文件作为静态资源返回。
4. 若插件含后端，后端路由已在启动时注册到同一 Flask 应用（如 `/api/character-graph/...`）。
5. **右下角「更多」悬浮球（⋮）**：悬浮展开菜单，含三个工具——
   - **编辑位置**：进入「编辑位置」模式。**插件卡片**采用 Android 桌面式拖拽：按住图标拖起后图标放大悬停跟随光标，拖动到目标槽位时该位置实时生成该图标的**半透明虚影**，沿途卡片顺序让位，松手落位即自动调用 `POST /api/tools/reorder` 保存（Esc 取消）。**分类区块**可直接拖动标题调整上下顺序。编辑模式下点击卡片不会跳转，再次点「完成编辑」退出。
   - **隐藏工具**：浮窗内以开关切换**分类 / 插件**的显示与隐藏，实时调用 `POST /api/tools/visibility` 写回 `config/tools.json`，隐藏的分类与插件即刻从首页消失。
   - 各插件的大模型 API 配置请在对应插件页面内完成（如「人物关系立体星图」「战果录入」）。

---

## HTTP API

| 接口 | 说明 |
| --- | --- |
| `GET /` | 首页 |
| `GET /tool/<id>` | 工具外壳页 |
| `GET /plugin/<id>/<path>` | 插件前端静态资源（映射到 `plugins/<id>/frontend/`），目录穿越已拦截 |
| `GET /api/tools` | 聚合后的工具列表（站点信息 + 分类 + 工具清单） |
| `GET /api/tools/<id>` | 单个工具信息，不存在返回 404 |
| `POST /api/tools/reorder` | 保存首页布局：请求体 `{categories: [分类id按显示顺序], tools: {分类id: [工具id…]}}`，按分类顺序重排 `categories`、按位置重写各工具的 `category` 与 `order` 并写回 `config/tools.json` |
| `GET /api/tools/visibility` | 返回全部分类与工具及启用状态（`enabled`），供「隐藏工具」浮窗使用（含已隐藏项） |
| `POST /api/tools/visibility` | 切换启用：`{type: 'tool'\|'category', id, enabled}`，写回 `config/tools.json`（分类/工具各支持 `enabled` 字段） |

**登录 / 管理后台接口**（由 `admin` 插件提供，`config/admin.json` 存储单位 / 部门 / 人员 / 角色，层级：**单位(units) → 部门(departments) → 用户(users)**）：

| 接口 | 说明 |
| --- | --- |
| `GET /login` | 登录页 |
| `POST /api/login` | 登录 `{username, password}`，成功写入 session（含 `login_at` / `last_active` 时间戳） |
| `POST /api/logout` | 退出登录，清空 session |
| `GET /api/session` | 当前登录状态：`{user: {username, name, role, unit, unit_id, department, department_id, modules, super_admin, permissions}\|null}`（匿名返回 `user: null`，用于前端心跳探测） |
| `POST /api/account/password` | 自助修改密码 `{old_password, new_password}`：新密码 ≥6 位且不得与旧密码相同；成功后当前会话保持有效 |
| `GET /admin` | 管理后台首页（需登录） |
| `GET /admin/unit\|/admin/department\|/admin/user` | 三个管理模块页（需登录）；`/admin/permission` 暂时屏蔽（权限管理已并入「人员管理」，直接访问返回 404） |
| `GET /api/admin/summary` | 后台总览：各模块名称 / 记录数 / 当前账号是否可访问（需登录）；不含「权限管理」卡片 |
| `GET\|POST /api/admin/units`、`PUT\|DELETE /api/admin/units/<id>` | 单位管理 CRUD（需 `unit` 权限）；删除前需清空其下部门 |
| `GET\|POST /api/admin/departments`、`PUT\|DELETE /api/admin/departments/<id>` | 部门管理 CRUD（需 `department` 权限）；创建/编辑需指定 `unit_id`；删除前需清空其下人员 |
| `GET\|POST /api/admin/users`、`PUT\|DELETE /api/admin/users/<username>` | 人员管理 CRUD（需 `user` 权限）；支持字段：`username`、`name`、`password`、`unit_id`、`department_id`、`role`、`idcard`（身份证号）、`permissions`（权限点 = tools.json 插件 ID 数组）、`llm`（`{base_url, api_key, model}`）；密码/身份证/API Key 以 Fernet 加密存储；密码为空时保持不变，删除当前登录账号会被拒绝。返回的用户条目含 `super_admin` 标记（前端据此区分角色）。权限点不在 Web 界面展示，仅后端读取并据此拦截工具访问 |
| `GET\|POST /api/admin/permissions`、`PUT\|DELETE /api/admin/permissions/<id>` | 权限（角色）管理 CRUD（需 `user` 权限，**已并入「人员管理」模块**），可勾选模块为 `unit` / `department` / `user` / `permission`；`modules` 可为空（纯工具角色，如办案员） |

> **权限点与工具访问控制**：账号的 `permissions` 字段为 `config/tools.json` 中插件 ID 的列表，
> 不随 Web 界面展示。已登录的非超级管理员账号只能访问权限点内的工具：
> `GET /tool/<id>`、`GET /plugin/<id>/...` 返回 403，`/api/tools` 列表自动过滤，`/api/tools/<id>` 返回 404；
> 拥有全部管理模块（单位/部门/人员/权限）的角色视为超级管理员，默认可访问全部工具。
> 未登录（匿名）访问任何页面/接口一律被拦截（302 到 `/login` / 401 JSON），不再放行。
> 权限点需通过后端 API（`POST/PUT /api/admin/users`）或直接编辑 `config/admin.json` 配置，非法 ID 会被拒绝。

> 以上接口均由 `plugins/admin/backend/routes.py`（admin 插件）提供，贯彻「一切皆插件」理念。
> 未登录访问后台接口返回 401，页面重定向到 `/login`；无模块权限返回 403 / 重定向回 `/admin`。
> 首页「编辑位置」「隐藏工具」等写操作同样需要登录（由 admin 插件的 `before_request` 保护）。
> 权限数据模型：**角色（permissions）→ 可访问的管理模块列表**；人员归属单位下的部门并绑定一个角色。
> 内置角色：**管理员**（`role-admin`，拥有全部 4 个模块，即超级管理员）与 **办案员**
> （`role-case-handler`，无管理模块，仅可被分配工具权限点，不含「管理后台」）。
> 内置管理员角色（`role-admin`）默认拥有全部 4 个模块，升版时自动补齐新模块，保证超级管理员始终可访问全部工具。
> **按人分配工具权限**：人员管理列表「操作」列的「权限」按钮以浮窗展示系统中全部功能模块
> （来源于 `/api/tools/visibility`）。管理员角色显示全部模块（含管理后台，只读）；办案员等其他角色
> 不展示「管理后台」，可勾选该人员可访问的工具后保存（写回该用户的 `permissions` 字段）。
> 新建办案员账号默认授予全部工具（不含管理后台），管理员可在「权限」浮窗中再调整。

**character-graph 插件后端接口**（演示含后端插件的 API 模式）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/character-graph/config` | 读取大模型配置（base_url / api_key / model / api_source） |
| `POST /api/character-graph/config` | 保存大模型配置 |
| `GET /api/character-graph/prompt` | 读取抽取 Prompt 模板 |
| `POST /api/character-graph/analyze` | 提交文档分析任务（multipart），**立即返回 `task_id`**，后台异步执行 |
| `GET /api/character-graph/result/<task_id>` | 轮询分析任务状态与结果 |

> 各插件后端路由统一挂在 `/api/<插件id>/` 前缀下，动态注册，互不冲突。

**trajectory-convert 插件后端接口**（Excel 轨迹表 → 二维码视频流 / 静态二维码）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/trajectory-convert/config` | 读取抽样/解析配置 |
| `GET /api/trajectory-convert/status` | 依赖可用性检查（openpyxl / xlrd / qrcode / zfec / cv2 / numpy） |
| `POST /api/trajectory-convert/convert` | 上传 Excel（`.xlsx`/`.xls`）与参数（时间间隔 / 二维码版本 / 模式），**异步执行返回 `task_id`** |
| `GET /api/trajectory-convert/status/<task_id>` | 轮询转换进度与结果 |
| `GET /api/trajectory-convert/download/<task_id>/<filename>` | 下载产物：二维码视频 `.mp4` / 单张静态图 `.png` / 多张静态图 ZIP |
| `GET /api/trajectory-convert/image/<task_id>/<index>` | 预览某一张静态二维码图片（`index` 从 1 开始） |
| `POST /api/trajectory-convert/download-selected` | 把勾选的多张静态二维码打包为 ZIP（`{task_id, indices}`） |

**qr-video-decode 插件后端接口**（二维码视频流解码）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/qr-video-decode/status` | 依赖可用性检查（zfec / jsQR） |
| `POST /api/qr-video-decode/reassemble` | 提交前端逐帧识别出的分块数据，**异步 zfec 前向纠错重组，返回 `task_id`** |
| `GET /api/qr-video-decode/reassemble/<task_id>` | 轮询重组进度与恢复结果 |

**shared-docs 插件后端接口**（共享文档协作编辑，**数据按「单位 / 部门 / 私人」三级隔离**）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/shared-docs/status` | 依赖可用性检查（python-docx / openpyxl / xlrd） |
| `GET /api/shared-docs/documents` | 文档列表 —— 仅返回当前用户可见的文档（超管可见全部；单位级同单位可见、部门级同部门可见、私人仅本人可见）；条目含 `created_by` / `created_by_name` / `scope{level,owner,owner_name,unit_id,department_id}` |
| `POST /api/shared-docs/documents` | 新建文档 `{name, type, level?}`，type 为 `word` / `excel`；`level` 为 `unit` / `department` / `private`（默认 `private`）。创建人、单位/部门 ID 一律取自 session，不接受前端传入 |
| `GET /api/shared-docs/documents/<id>` | 文档详情（含内容、修订历史、在线用户）；对「存在但无权访问」的文档返回 **404** |
| `POST /api/shared-docs/documents/<id>/content` | 保存内容 `{base_version, content}`（**可见即可编辑**）；**乐观锁**：版本不匹配返回 409 并携带服务端最新文档 |
| `POST /api/shared-docs/documents/<id>/presence` | 在线心跳 `{client_id}`（身份只认 session，忽略前端自报昵称），返回当前在线用户真实姓名 |
| `POST /api/shared-docs/documents/<id>/rename` | 重命名 `{name}` —— 仅创建者或超管（其余 **403**） |
| `POST /api/shared-docs/documents/<id>/scope` | 调整挂靠层级 `{level}` —— 仅创建者或超管（其余 403）；只改 level，保留原 owner/unit/dept |
| `DELETE /api/shared-docs/documents/<id>` | 删除文档 —— 仅创建者或超管（其余 403） |
| `GET /api/shared-docs/documents/<id>/export` | 导出真实 Office 文件（Word→`.docx`、Excel→`.xlsx`） |
| `POST /api/shared-docs/documents/<id>/import` | 上传 `.docx` / `.xlsx` / `.xls` 导入覆盖当前文档（可见即可编辑） |

> 文档数据以 JSON 文件保存在 `plugins/shared-docs/backend/data/`（已 gitignore），
> 内容格式：Word 为块级结构（`blocks`：段落 / 标题 / 列表 + 富文本 runs），
> Excel 为二维数组（`rows`）加可选列宽（`colWidths`，像素，0=自动）。
> **历史文档迁移**：启动时自动给没有 `scope` 的存量文档补「私人 + 创建者 admin」，
> 默认仅超管可见；需要老文档共享时由管理员在前端改成「单位」层级即可（幂等，重复启动不重复迁移）。

**case-report 插件后端接口**（战果录入——收网情况报告要素抽取 + 本地台账）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/case-report/config` | 读取大模型配置（base_url / api_key / model）及是否已配置 |
| `POST /api/case-report/config` | 保存大模型配置 |
| `POST /api/case-report/parse` | 提交解析任务 `{text, base_url?, api_key?, model?}`，**立即返回 `task_id`**，后台异步执行 |
| `GET /api/case-report/result/<task_id>` | 轮询解析结果：`fields`（五要素键值对）、`items`（缴获物品逐项拆分）、`method`（`llm` / `llm+rules` / `rules`）、`llm_error` |
| `POST /api/case-report/items` | 把缴获物品文本拆分为单列物品 `{text}`（供前端实时预览归类/数量） |
| `GET /api/case-report/categories` | 既有类别集合：`learned`（用户已学习的「物品名→类别」）+ `known`（可选类别列表） |
| `POST /api/case-report/categories` | 学习一条类别 `{name, category}`，持久化后后续解析优先采用 |
| `DELETE /api/case-report/categories/<name>` | 删除某物品名的学习类别，恢复默认判定 |
| `GET /api/case-report/aggregate` | 跨记录「战果汇总」：类似物品归为统一类别、数量叠加（重量→克、货币→元自动归并）；「涉及 N 起」按规整后案件名去重（同一案件多条记录只计 1 起）。支持 `?case=<案件名>` 仅统计该案件（涉及 N 起 = 1）；支持 `?scope=mine\|dept\|all`（默认 mine）按范围过滤 |
| `GET /api/case-report/cases` | 既有案件列表（按规整后案件名去重、**拼音排序**，含各案件记录条数与 `normalized` 规整名），供筛选侧边栏 / 入库合并 / 改案件名选择；**跟随 `?scope=` 过滤**（切到「本部门」时下拉为部门内所有人案件） |
| `GET /api/case-report/records` | 本地台账列表（按入库时间倒序），支持 `?case=<案件名>` 仅返回该案件的记录；支持 `?scope=mine\|dept\|all`（默认 mine）：mine=本人、dept=本人+同部门（部门+单位双重比对）、all=全站（仅超管，普通账号传 `scope=all` 返回 403） |
| `POST /api/case-report/records` | 保存一条战果 `{fields, source_text, items?, merge_mode?, merge_case?}`；`items` 为用户编辑后的单列明细（可省略，省略则自动拆分），保存时同步学习 `物品名→类别`。案件名保存前自动规整（去引号/空白）。**服务端自动打归属标签**（`created_by` / `created_by_name` / `unit_id` / `department_id`，取自 session）；`merge_mode=auto`（默认）时**只在自己可管理的记录里**检测同名案件（不落盘、返回 `duplicate:true + matches`）；`merge_mode=merge` 时并入既有案件（`merge_case` 指定归入的目标案件名）；`merge_mode=new` 时跳过同案提示直接新增 |
| `GET /api/case-report/records/<rid>` | 单条记录详情 —— 本人或同部门可读，其余 **404** |
| `PUT /api/case-report/records/<rid>/case` | 修改记录的案件名 `{case_name}`（可新建案件名，或改为既有案件名即并入该案）—— 仅录入人本人或超管（其余 **403**） |
| `DELETE /api/case-report/records/<rid>` | 删除记录 —— 仅录入人本人或超管（其余 403） |
| `GET /api/case-report/records/<rid>/download` | 下载单条记录的键值对 JSON 文件（`case-<rid>.json`）—— 本人或同部门可读，其余 404 |

> 解析策略：配置了大模型 API Key 时优先「大模型解析」，缺项由本地规则补全，
> 未配置时自动回落「本地规则解析」（正则），开箱即用。
> 台账以键值对 JSON 一记录一文件保存在 `plugins/case-report/backend/data/`（已 gitignore）。
> **案件名实体对齐**：同一案件可能因写法差异出现不同名称（如 `"2.11"开设赌场案` 与
> `2.11开设赌场案`）。保存/比较前会规整案件名（去掉引号、书名号、空白）；
> 入库时自动检测同名案件并提示「并入 / 作为新案件保存」；
> 台账里可随时修改某条记录的案件名（新建案件或并入既有案件），
> 「战果汇总」的「涉及 N 起」按案件去重统计。
> **案件筛选侧边栏**：战果汇总与台账左侧提供「案件」侧边栏（**拼音排序**汇总所有案件），
> 选中后右侧战果汇总与下方台账联动只展示该案件数据；保存 / 删除 / 改名后侧边栏自动重建
> 并保持选中（选中案件消失时自动回落「全部案件」）。

**时间规则**：仅写「月/日」（如 8月20日）时自动补当前年份；出现「昨天 / 前天 / 今天」等
以当前年月日为基准回推；大模型与规则产出的时间统一归一化为「YYYY年M月D日」。

**缴获物品规则**：除在 `fields.缴获物品` 保留原文摘要外，逐项拆分为
`items: [{category, name, quantity, unit}]` 单列存储——如「冰毒500克、涉案手机6部」拆为
毒品/手机两项；类似物品（如 电脑 / 笔记本电脑 / 台式电脑 / 平板）映射为同一「电脑」类别，
可在 `GET /api/case-report/aggregate` 中跨记录按数量叠加（重量→克、货币→元，批次「一批/若干」不参与数字累加）。

**类别学习（持久化）**：解析结果先在界面展示为可编辑明细（类别 / 名称 / 数量 / 单位）。
修改类别后系统自动把「物品名→类别」写入本地类别库
（`plugins/case-report/backend/data/item_categories.json`，已 gitignore）。
再次解析时的类别判定优先级：**用户已学习类别 > 大模型判定 > 本地规则判定**；
不适用既有类别的新物品由大模型单独归类（未配置大模型时用本地规则兜底）。保存记录时也会把最终明细中的类别一并学习。

**notice-board 插件后端接口**（公告板——按可见范围发布 / 修改 / 删除与查看公告）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/notice-board/status` | 依赖自检 |
| `GET /api/notice-board/config` | 当前用户是否可发布/修改公告（`can_publish`） |
| `GET /api/notice-board/announcements` | 公告列表 —— 仅返回当前用户可见的公告（单位=同单位 / 部门=同部门且同单位 / 用户=本人；超管可见全部）；条目含 `created_by` / `created_by_name` / `created_at` / `updated_at` / `targets` / `manageable`（可删除）/ `editable`（可修改） |
| `GET /api/notice-board/latest` | 最新一条可见公告（首页卡片曾用；现卡片内容由插件 `home_card()` 声明） |
| `POST /api/notice-board/announcements` | 发布公告 `{title, content, targets}` —— 仅管理员角色/超管；归属四字段取自 session；`targets` 为 `[{type: unit\|department\|user, id, uid?}]`（部门需带 `uid`=所属单位） |
| `PUT /api/notice-board/announcements/<aid>` | 修改公告 `{title, content, targets}` —— 仅管理员角色/超管（须可读该公告）；保留创建归属与 `created_at`，更新 `updated_at`；不可读一律 404 |
| `DELETE /api/notice-board/announcements/<aid>` | 删除公告 —— 仅创建者或超管；不可读一律 404 |

> 公告以 JSON 一公告一文件保存在 `plugins/notice-board/backend/data/`（已 gitignore）。
> **首页卡片动态声明**：首页插件卡片内容支持两级读取 —— 插件后端提供可选钩子 `home_card()`
> 时（启动登记、请求时实时求值，可按当前登录用户权限返回动态内容）以声明为准；未提供钩子时
> 回退读取 `config/tools.json`（name/description）+ `manifest.json`（icon/accent/features）。
> 公告板 `home_card()` 按当前用户可见范围取最新公告，以「时间 + 标题」首行、换行展示公告内容。

`GET /api/tools` 返回结构示例：

```json
{
  "site": { "title": "JZ 工具箱", "subtitle": "...", "footer": "..." },
  "categories": [{ "id": "dev", "name": "开发者工具" }],
  "tools": [
    {
      "id": "base64",
      "name": "Base64 编解码",          // ← 来自 config/tools.json（配置为权威来源）
      "description": "...",             // ← 来自 config/tools.json
      "icon": "🔐",                     // ← 来自 manifest.json
      "accent": "#4285F4",              // ← 来自 manifest.json
      "entry": "index.html",            // ← 来自 manifest.json
      "features": ["编码", "解码", "UTF-8"],   // ← 来自 manifest.json
      "category": "开发者工具",
      "category_id": "dev",
      "order": 2
    }
  ]
}
```

---

## 访问日志

系统自动记录所有 HTTP 请求，重点记录**登录用户名、客户端 IP、访问的功能与具体操作**，用于使用统计与安全审计。

- **日志位置**：`logs/access.log`（首次请求时自动创建 `logs/` 目录）。
- **滚动策略**：按天滚动（`TimedRotatingFileHandler`），保留最近 30 天，过期自动清理。
- **启用开关**：`config/tools.json` → `site.logging`，默认 `true`；设为 `false` 即停止写日志。

### 日志格式（TAB 分隔，每行一条）

```
时间戳		级别		ip=客户端IP	user=登录用户名	method=方法	func=功能标签	op=具体操作	path=路径	status=状态码	cost_ms=耗时	ua=浏览器标识
```

示例（真实输出）：

```
2026-08-15 14:04:22,882	INFO	ip=127.0.0.1	user=admin	method=POST	func=后端接口: /api/admin/units	op=新增单位	path=/api/admin/units	status=200	cost_ms=8	ua=Mozilla/5.0 ...
2026-08-15 14:04:22,882	INFO	ip=127.0.0.1	user=admin	method=GET	func=API: 工具列表	op=获取工具列表	path=/api/tools	status=200	cost_ms=5	...
2026-08-15 14:04:22,882	INFO	ip=127.0.0.1	user=admin	method=POST	func=后端接口: /api/notice-board/announcements	op=发布公告	path=/api/notice-board/announcements	status=200	...
2026-08-15 14:04:22,882	INFO	ip=127.0.0.1	user=-	method=GET	func=插件(人物关系立体星图)/frontend/index.html	op=打开插件功能页	path=/plugin/character-graph/index.html	status=200	...
```

### 字段说明

| 字段 | 说明 |
| --- | --- |
| `ip` | 客户端真实 IP；兼容反向代理，优先取 `X-Forwarded-For` 首段 |
| `user` | 当前登录用户名；未登录为 `-`（登录接口本身记登录者，登录前的匿名请求记 `-`） |
| `method` | HTTP 方法（GET / POST / …） |
| `func` | 功能标签，自动解析：`首页` / `API: 工具列表` / `API: 工具详情(工具名)` / `工具壳页(工具名)` / `插件(工具名)/frontend/...` / `后端接口: /api/...` / `静态资源: ...` |
| `op` | 具体操作标签：优先取插件/路由在请求处理中显式标记（如 `新增单位` / `修改密码` / `发布公告` / `删除共享文档` / `保存战果记录`），未标记时回退为核心路由映射或 `接口调用: 方法 路径` / `打开插件功能页` / `打开工具` 等兜底描述 |
| `path` | 原始请求路径 |
| `status` | HTTP 状态码 |
| `cost_ms` | 请求处理耗时（毫秒） |
| `ua` | 客户端 User-Agent（截断 120 字符） |

### 插件如何标记具体操作

插件后端可在路由处理器内调用 `set_operation("操作描述")`（从 `jztools_admin.routes` 导入，导入失败时兜底为空操作），访问日志即记录该描述；不标记时框架自动回退到路径/方法兜底，插件**禁止**自行落盘访问日志（规范 B-8，框架统一异步记录）。

---

## 并发与性能优化

针对并发评估发现的瓶颈，已完成四项优化（本分支合入 main 的内容）：

### 1. LLM 调用异步化（最大瓶颈）

人物关系星图的长耗时大模型调用**不再占用 HTTP worker 线程**：

- `POST /api/character-graph/analyze` 毫秒级返回 `task_id`，文档解析 + 大模型调用放入后台线程池（`ANALYZE_WORKERS = 2`，见 `plugins/character-graph/backend/routes.py`）。
- 新增 `GET /api/character-graph/result/<task_id>` 轮询状态（`pending / running / done / error`），任务结果保留 30 分钟自动清理防内存泄漏。
- API Key 前置校验，避免无效提交白白排入队列。

### 2. gzip 压缩

- HTML / JS / CSS / JSON 等文本响应在客户端声明 `Accept-Encoding: gzip` 时压缩（`app.py` 的 `_compress_response` 中间件），响应附带压缩前大小阈值过滤（<256B 不压缩）。
- 流式响应（`send_file` 大文件，`direct_passthrough` 模式）跳过压缩，避免序列化错误。

### 3. 静态资源缓存

- `.js/.css/.json/.png/...` → `Cache-Control: public, max-age=86400`（1 天）。
- `.html` → `no-cache`，配合 ETag 做协商缓存（`_static_cache` 中间件，覆盖 Flask debug 默认的 no-cache）。

### 4. 异步日志

- 日志经内存队列（容量 2000）由后台线程批量落盘（`QueueHandler` + `QueueListener`），HTTP 线程零写盘阻塞；队列满时丢弃最旧日志，防止日志拖垮站点。

### 实测性能参考

| 场景 | 并发 | 总请求 | 成功率 | 平均响应 |
| --- | --- | --- | --- | --- |
| 常规读接口 | 200 | 14000 | 100% | 28-49ms |
| gzip 开启 | 200 | 14000 | 100% | 40-67ms |
| 压力边界 | 300 | 16800 | 88%（连接排队/拒绝，非应用异常） | — |

> 300 并发以上的上限源于 Flask 开发服务器单进程线程模型。生产部署建议使用 **Gunicorn 多进程 + Nginx 反向代理**（静态资源分发、gzip、负载均衡），可彻底消除该瓶颈。

---

## 健壮性与安全

### 安全防护

- **目录穿越拦截**：插件 ID 仅允许 `[A-Za-z0-9_-]`，`send_from_directory` 安全控制路径，`..`、`%2f`、`%5c` 等 6 种穿越变体均已验证拦截。
- **敏感信息不入库**：`.gitignore` 排除插件运行配置（`plugins/character-graph/backend/config.json`，含大模型 API Key）与运行时文件（`logs/`、`.server.*`、`__pycache__`）。
- **HTML 转义**：map-marker 等插件对使用者输入进行 XSS 转义。

### 健壮性测试覆盖

已通过 28 项健壮性测试：目录穿越、非法 ID、错误方法（405）、非法 JSON 宽容处理、超长 URL、编码注入、空/坏/超大文件（10MB）、畸形 task_id、20 并发 analyze 无崩溃等，全部通过。

### 已知修复：Windows 跨天日志轮转

- **症状**：服务长期运行跨过午夜后，`TimedRotatingFileHandler` 的 `os.rename` 因文件被占用抛 `WinError 32`（PermissionError），随后日志静默丢失。
- **修复**：`WindowsSafeTimedRotatingFileHandler`（`app.py`）在改名失败时退回「复制 + 截断」（copytruncate）策略，归档完整、写入不断，跨天零日志丢失。

---

## 开发者指南：开发插件

### 7.1 纯前端插件

**最短步骤**：只需两个文件 + 一行配置。

```
plugins/my-tool/
├── manifest.json
└── frontend/
    └── index.html        # 随便写什么前端页面
```

示例 `plugins/my-tool/manifest.json`：

```json
{
  "id": "my-tool",
  "icon": "🧰",
  "accent": "#4285F4",
  "entry": "index.html",
  "features": ["标签1", "标签2"]
}
```

> **工具的「名称 / 说明」不写在这里**，而是写在下文 7.4 的 `config/tools.json` 中 —— 它们是配置驱动的展示项（也可以不回退，直接由后台配置统一管理）。

`frontend/index.html` 就是一个普通网页，可在其中引用**相对路径**的脚本、样式与图片（因为 iframe 的 src 是 `/plugin/my-tool/xxx`，同目录资源会被正确解析）：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>我的工具</title>
  <script src="./app.js"></script>
  <link rel="stylesheet" href="./style.css">
</head>
<body>
  <h1>你好，世界</h1>
</body>
</html>
```

### 7.2 带 Python 后端的插件

若插件需要文件处理、调用第三方服务等后端能力，在插件目录下新增 `backend/`：

```
plugins/my-tool/
├── manifest.json
├── frontend/
│   └── index.html
└── backend/
    ├── __init__.py      # 可以为空，标识该目录是 Python 包
    ├── routes.py        # 必须导出 register(app)
    └── helper.py        # 该插件私有模块
```

`backend/routes.py` 约定（参考 `plugins/character-graph/backend/routes.py`）：

```python
from flask import jsonify, request

API_PREFIX = "/api/my-tool"

def register(app):
    """框架启动时自动调用，app 为当前 Flask 实例。"""
    @app.get(f"{API_PREFIX}/ping")
    def ping():
        return jsonify({"ok": True})

    @app.post(f"{API_PREFIX}/echo")
    def echo():
        data = request.get_json(silent=True) or {}
        return jsonify({"you_said": data.get("msg", "")})
```

注意事项：

- 后端路由应统一挂在自己的前缀（如 `/api/my-tool/...`），避免与其他插件冲突。
- 同一插件目录内的私有模块使用**相对导入**：`from . import helper`，不要依赖全局包名。
- 框架以插件 ID 派生独立模块名加载 `backend/routes.py`，不会与其它插件或者与主应用符号互相污染。
- 插件后端仅在前端页面被调用时才生效；工具禁用时后端也不会被注册（随 `enabled` 配置整体启停）。

### 7.3 插件清单字段说明

`manifest.json` 描述插件**自身**的图标、入口与能力标签（属于「插件自描述」）：

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `id` | ✅ | - | 工具唯一标识，**必须与目录名一致**，仅允许 `A-Z a-z 0-9 _ -` |
| `name` | - | 同 `id` | **可选回退**：`config/tools.json` 未注册名称时使用 |
| `description` | - | 空 | **可选回退**：`config/tools.json` 未注册说明时使用 |
| `icon` | - | `🧩` | 卡片图标（Emoji） |
| `accent` | - | `#4285F4` | 卡片主题色（Hex） |
| `entry` | - | `index.html` | 前端入口页面，位于 `frontend/` 下 |
| `features` | - | `[]` | 卡片上的能力标签（字符串数组） |

> 展示用**名称 / 描述**建议统一写在 `config/tools.json`（7.4），manifest 中的这两个字段仅作为插件目录迁移到其他项目时的兜底；若插件后端提供了 `home_card(app)` 主动声明（见 7.4），则以其声明为准（两种方式并存时声明优先）。

### 7.4 注册插件到首页

插件卡片展示内容有两种读取方式（可叠加）：

- **方式一：插件主动声明（请求时动态求值）** —— 插件后端在 `plugins/<id>/backend/routes.py` 中提供可选钩子
  `home_card()`，主应用启动时登记该钩子，首页 `/api/tools` 请求时于请求上下文内实时调用（可按当前登录用户
  权限返回动态卡片内容），结果作为首页卡片展示字段的**最高优先级来源**（可覆盖 `config/tools.json` 与
  `manifest.json` 中的同名项）。声明字段：`name` / `description` / `icon` / `accent` / `features`
  （缺省字段自动回退到方式二）。示例（`plugins/notice-board/backend/routes.py` 参考实现）：

  ```python
  def home_card(app=None):
      """首页卡片内容声明：每次请求时被主程序调用，可依据当前用户返回动态内容。"""
      card = {"name": "公告板", "icon": "\U0001f4e2", "accent": "#E8710A",
              "features": ["最新公告", "可见范围发布"]}
      user = get_session_user()  # 在请求上下文内，可获取当前登录用户
      if not user:
          card["description"] = "暂无最新公告。"
          return card
      # 动态检索当前用户可见的最新公告，以「时间 + 公告标题」换行展示公告内容
      latest = fetch_latest_readable(user)
      card["description"] = (f"{latest['time']} {latest['title']}\n{latest['content']}"
                             if latest else "暂无最新公告。")
      return card
  ```

  > 参考实现：`plugins/notice-board/backend/routes.py`。钩子异常不会阻断插件加载，未提供钩子自动回退方式二。

- **方式二：读取 tools.json（未声明时）** —— 插件未提供 `home_card(app)` 时，按 7.4 下方配置读取。

编辑 `config/tools.json`，在 `tools` 数组中增加一条引用；如需新分类，再在 `categories` 中声明：

```json
{
  "site": { "...": "..." },
  "categories": [
    { "id": "dev", "name": "开发者工具" },
    { "id": "mycat", "name": "我的分类" }
  ],
  "tools": [
    {
      "id": "my-tool",
      "name": "我的工具",
      "description": "一句话描述这个工具能做什么",
      "category": "mycat",
      "enabled": true,
      "order": 6
    }
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 插件 ID，与 `manifest.json` 及目录名一致 |
| `name` | - | **展示名称**（插件 `home_card()` 声明优先；其次配置为权威来源；再回退 manifest，最后回退 `id`） |
| `description` | - | **卡片说明文字**（插件 `home_card()` 声明优先；其次配置为权威来源；再回退 manifest） |
| `category` | ✅ | 所属分类 ID（对应 `categories` 中的 `id`） |
| `enabled` | - | 是否启用，`false` 时首页隐藏且后端不加载 |
| `order` | - | 展示顺序（同分类内按此排序） |

保存后刷新首页即生效，**无需重启服务、无需改框架代码**。只改 `name` / `description` 即可调整前台展示文案，不动插件目录。

> 注意：若插件已通过 `home_card(app)` 主动声明了 `name` / `description` 等字段（方式一），
> 该插件卡片将**以声明为准**，此时修改 `tools.json` 中对应字段不会生效；需移除插件中的
> `home_card()` 钩子后，`tools.json` 才会重新接管（方式二）。

### 7.5 常见约定与注意事项

1. **ID 规范**：`manifest.json` 的 `id`、目录名、`config/tools.json` 中的 `id` 三者必须一致。
2. **展示名集中管理**：工具在前台展示的**名称 / 说明**统一写在 `config/tools.json`（配置为准）；manifest 中的同名段仅作回退，不建议两处维护不同文案。若插件需要通过代码主动声明卡片内容（动态生成或自描述），请在 `routes.py` 提供 `home_card(app)` 钩子（方式一），此时以声明为准。
3. **资源隔离**：所有插件文件必须放在 `plugins/<id>/` 下，前端放 `frontend/`，后端放 `backend/`。主框架 `static/` 目录属于框架自身，不要混入插件资源。
4. **目录穿越防护**：插件 ID 只允许 `[A-Za-z0-9_-]`，`/plugin/<id>/<path>` 由 `send_from_directory` 安全控制，不要用 `..`、`/` 等字符作 ID 或路径。
5. **相对路径引用**：前端内部引用其他资源一律用相对路径（`./app.js`、`css/style.css`），不要硬编码 `/static/...` 之外的绝对路径。
6. **后端前缀唯一**：多个插件可能同时注册，后端路由建议统一使用 `/api/<插件id>/` 前缀。
7. **敏感配置**：插件内的高德 Key、大模型 Key 等前端依赖配置建议由插件页面自行管理（如保存到 localStorage），不要写死在仓库文件中。
8. **自包含交付**：一个插件目录可以从项目复制到另一个 JZToolsHub 项目，配合一段 `tools.json` 配置（含 `name` / `description`）即可完整迁移。

### 7.6 插件数据归属规范

凡是插件持久化"用户产生的数据"，必须遵守以下两条铁规矩：

> **规范一（归属标签）**：落盘 JSON 必须含 `created_by` / `created_by_name` / `unit_id` / `department_id` 四个字段（创建时从 `get_session_user()` 取，**不从请求体取**）。

> **规范二（读取必过滤）**：任何返回这些数据的接口，第一步取用户上下文（`get_session_user()`，None 即 401），第二步按以下可见性规则过滤后才允许序列化返回；单条读写按「读=可见性规则、写=本人或超管」控制。

**可见性规则**（记当前用户为 U，一条数据为 X）：

| 数据 | 可见条件（满足其一） |
| --- | --- |
| 共享文档 · 单位级 | U 是超管，或 `X.unit_id == u(U)` |
| 共享文档 · 部门级 | U 是超管，或 `X.department_id == d(U)` |
| 共享文档 · 私人 | U 是超管，或 `X.created_by == n(U)` |
| 战果记录（本人视图） | U 是超管，或 `X.created_by == n(U)` |
| 战果记录（本部门视图） | 本人视图全部 + `X.department_id == d(U)` 且 `X.unit_id == u(U)` |
| 公告板 | U 是超管，或命中公告 `targets` 中任一目标（单位=同单位 / 部门=同部门且同单位 / 用户=本人） |

**写操作约束**：重命名 / 删除 / 改归属等管理操作仅创建者或超级管理员；协作编辑内容（共享文档内容保存）"可见即可编辑"。
**公告板例外**：发布与**修改**为管理员角色或超管（修改须可读该公告），删除仍限创建者或超管。

**标准取用样板**：

```python
from flask import jsonify

try:
    from jztools_admin.routes import get_session_user
except Exception:
    get_session_user = None


def current_user_or_none():
    if get_session_user is None:
        return None
    return get_session_user()
```

---

## 内置插件一览

| 插件 | 分类 | 前端 | 后端 | 说明 |
| --- | --- | --- | --- | --- |
| json-formatter | dev | ✅ | - | JSON 格式化 / 压缩 |
| base64 | dev | ✅ | - | Base64 编解码（UTF-8 中文） |
| color-picker | design | ✅ | - | 取色器 |
| md5-generator | dev | ✅ | - | MD5 摘要生成 |
| map-marker | maps | ✅ | - | 高德地图经纬度标点（需自备高德 Key），支持二维码识别还原轨迹、批量导入、按时间移动轨迹回放 |
| character-graph | ai | ✅ | ✅ | 上传文档 → 大模型提取人物关系 → 3D 星图展示 |
| trajectory-convert | dev | ✅ | ✅ | Excel 轨迹表 → 时间间隔抽样 → JSON 封装 → 生成二维码视频流，或静态二维码图片（单张/多张） |
| qr-video-decode | dev | ✅ | ✅ | 解码二维码视频流，前端 jsQR 逐帧识别 + 后端 zfec 前向纠错重组，恢复原始数据 |
| shared-docs | office | ✅ | ✅ | 多人共同编辑 Word / Excel 文档：实时同步、在线用户、版本冲突提示、`.docx` / `.xlsx` 导入导出 |
| case-report | police | ✅ | ✅ | 输入收网情况报告 → 抽取 时间/主办大队/案件名/抓获人数/缴获物品 → 键值对 JSON 本地台账 |
| notice-board | office | ✅ | ✅ | 单位/部门/用户可见范围公告：管理员发布与修改、创建者/超管删除，按可见性过滤展示；首页卡片经 `home_card()` 动态声明最新可读公告（时间+标题+换行+内容） |

---

## 轨迹数据与二维码闭环

「轨迹转换」「QR 视频流解码」「地图标点」三个插件组成一条完整的**轨迹数据 ↔ 二维码**闭环，数据格式统一为键值对 JSON（`key` 为时间，`value` 为经纬度 `[经度, 纬度]`）。

```
Excel 轨迹表 ──▶ [trajectory-convert] ──▶ 二维码视频流 / 静态二维码
                                              │
        恢复的轨迹 JSON ◀── [qr-video-decode] ◀┘
                        │
                        └──▶ [map-marker] 扫码识别 → 地图标点 + 移动轨迹回放
```

### trajectory-convert（轨迹转换）

- 上传 `.xlsx` / `.xls` 轨迹表，按后端配置的字段名自动解析（时间 / 经度 / 纬度），按时间间隔抽样。
- **视频模式（默认）**：将 JSON 切块 + zfec 前向纠错，编码为 QR-transfer 二维码视频流（H.264，浏览器可直接预览），供 qr-video-decode 解码。
- **静态模式**：输出可直接扫码的静态二维码图片。
  - 数据能装下时输出**单张 PNG**，内容为完整可读 JSON；
  - 装不下时**自动按点位拆分多张 PNG** 并打包为 ZIP，每张都是独立可读的 JSON 子集（`{时间: [经度, 纬度]}`），**可直接扫码导入「地图标点」**查看对应片段；页面支持逐张放大预览、勾选部分下载。
- 后端任务异步执行，前端轮询进度；二维码版本 1-40 可调（版本越大单码容量越大）。

### qr-video-decode（QR 视频流解码）

- 前端 Web Worker 中用 jsQR **逐帧识别**二维码视频帧，收集分块与帧头（序号 / 总帧数 / 纠错参数）。
- 后端按帧头信息执行 zfec 前向纠错**重组**原始数据，支持进度轮询与结果下载预览。
- 可与 trajectory-convert 视频模式对接，恢复原始轨迹 JSON；也支持地图标点生成的二维码轨迹回放数据。

### map-marker（地图标点）

- 高德地图输入经纬度标点（需自备高德 Key），支持批量导入、点地图添加。
- **二维码识别**：上传二维码图片，jsQR 识别并解析轨迹 JSON（兼容 `[{"时间": "经度,纬度"}]`、`{时间: [经度,纬度]}` 等形态），确认后按时间在地图上**标点并生成移动轨迹**。
- **轨迹回放**：生成轨迹折线与时间标点，进度条可非线性步进，时间压缩让长跨度的轨迹在约 20 秒内播完，支持播放/暂停/进度跳转，播放时同步高亮当前点。

---

## 打包部署（可上线程序）

后端以 **PyInstaller 单目录可执行程序** 打包（免安装 Python / 依赖），前端保留源码便于快速修改。

**产物结构**（`build-deploy.ps1` 生成，`deploy\JZToolsHub\`，另附 `JZToolsHub-v1.0.zip`）：

```
deploy\JZToolsHub\
├─ JZToolsHub.exe      # 后端可执行程序（双击或 start.bat 启动）
├─ _internal\          # 编译后的 Python 运行时 + Flask + 全部第三方依赖
├─ static\             # 首页前端源码（可修改，重启生效）
├─ plugins\            # 插件：frontend/ 前端源码可改；backend/ 后端源码 + 运行期 data/
├─ config\tools.json   # 工具注册清单（可改；admin.json 等首启自动生成）
├─ logs\               # 访问日志（运行期写入）
└─ start.bat           # 一键启动脚本
```

**使用**：
1. 解压后双击 `start.bat`（或直接运行 `JZToolsHub.exe`）；
2. 浏览器访问 `http://<服务器IP>:5000`，默认管理员 `admin` / `admin123`（首启自动生成，请尽快登录后台改密）；
3. 修改前端：编辑 `static\` 或 `plugins\<id>\frontend\` 下的文件后重启服务即生效；
4. 端口 / 监听地址可用环境变量覆盖：`JZTOOLS_PORT`、`JZTOOLS_HOST`（默认 `0.0.0.0:5000`）。

**重新打包**（需本机装有 Python 与依赖，PyInstaller 自动收集插件后端动态导入的
`docx / openpyxl / xlrd / qrcode / zfec / cv2 / numpy / pypdf / requests / cryptography / waitress`）：

```powershell
pip install -r requirements.txt pyinstaller
powershell -ExecutionPolicy Bypass -File .\build-deploy.ps1
```

> 运行期数据（`config/admin.json`、`logs/`、`plugins/*/backend/data/`）写在 exe 同层目录，
> 与 `_internal\` 编译产物分离：升级程序时仅替换 exe + `_internal\` 即可保留数据。
> 打包运行使用 **waitress** 多线程生产级 WSGI 服务器（禁用 Flask debug/reloader）。

---

## 故障排查

| 现象 | 排查建议 |
| --- | --- |
| 首页看不到某个工具 | 检查 `config/tools.json` 中 `enabled` 是否为 `true`、`id` 是否一致 |
| 工具卡片点击后显示"无法加载工具" | 检查 `manifest.json` 的 `entry` 对应文件是否存在于 `frontend/` |
| `/api/tools` 返回但页面空白 | 刷新浏览器缓存；确认 `static/js/main.js` 正常加载 |
| 后端接口 404 | 确认插件目录下有 `backend/__init__.py` 与 `backend/routes.py`，且 `register(app)` 已导出 |
| 轨迹转换报缺依赖 | 确认已安装 `openpyxl / xlrd / qrcode / zfec / numpy / opencv-python`；`GET /api/trajectory-convert/status` 可查看各依赖可用性 |
| 静态二维码扫描报"缺少时间/经纬度" | 若为多张静态二维码中的一张，其内容是该段的 JSON 子集，可导入地图标点查看对应片段；完整轨迹请用「二维码视频流」模式 |
| 地图标点空白/地图不显示 | 在「⚙️ 配置」中填写有效的高德 Web 服务 Key 并保存 |
| 共享文档导入/导出不可用 | 确认已安装 `python-docx / openpyxl`（Word/Excel）与 `xlrd`（.xls）；页面顶部依赖提示会列出缺失项 |
| 端口被占用 | 修改 `app.py` 末尾 `port=5000`，或先停止旧进程再启动 |

---

## 后期接入真实后端

现阶段 `config/tools.json` 为本地 Mock。接入真实后台时，将 `app.py` 中 `load_registry()` 替换为数据库或远程配置接口即可 —— 前端渲染、插件加载机制、外壳页与 API 结构均无需任何改动。这保证了"配置驱动"的机制可以平滑过渡到真实的运营后台。

---

*JZToolsHub · 一切皆插件 · 配置驱动的工具集合*