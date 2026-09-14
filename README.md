# JZToolsHub 工具箱

基于 **Flask + 配置驱动插件机制** 的在线工具集合网站。界面采用 Material Design 风格，首页以卡片展示各工具，工具页以 iframe 方式嵌入外壳页运行。

> 核心设计理念：**一切皆插件** —— 主框架只提供运行底座（首页、外壳页、配置聚合 API、访问日志），业务功能全部收敛在 `plugins/` 下的自包含插件目录里。新增、下线、改名、排序、授权工具，都不需要改动框架代码。

| 项目 | 说明 |
| --- | --- |
| 后端 | Python 3（源码运行 ≥3.8 可跑主框架，**完整功能建议 ≥3.10**）· Flask |
| 前端 | 原生 HTML / CSS / JS，无构建步骤 |
| 插件机制 | 目录扫描 + 配置驱动 + iframe 隔离 + 后端动态加载 |
| 交付形态 | 源码直跑，或 PyInstaller 打包为 Windows 可执行程序（含一键安装/卸载） |
| 移动端 | 配套 Android 离线接收端 `android-app/InfoParse/`（Kotlin） |

---

## 目录

1. [项目简介](#1-项目简介)
2. [环境搭建](#2-环境搭建)
3. [运行与构建](#3-运行与构建)
4. [目录结构](#4-目录结构)
5. [核心模块解析](#5-核心模块解析)
6. [使用示例](#6-使用示例)
7. [HTTP API](#7-http-api)
8. [内置插件一览](#8-内置插件一览)
9. [典型业务链路](#9-典型业务链路)
10. [配置模板同步与数据迁移](#10-配置模板同步与数据迁移)
11. [访问日志](#11-访问日志)
12. [故障排查](#12-故障排查)
13. [开发者约定速查](#13-开发者约定速查)

---

## 1. 项目简介

### 1.1 它解决什么问题

一个单位内部往往零散需要一批小工具（文档协作、公告、知识库、表格过滤、战果台账、轨迹分析、离线传输……）。逐个开发独立网站成本高、账号体系割裂、运维分散。JZToolsHub 提供**统一的外壳**：

- 统一登录与组织架构（单位 → 部门 → 人员 → 角色 → 工具权限点）；
- 统一首页与工具入口（卡片式、可拖拽排序、可按人授权）；
- 统一数据目录（用户数据与程序目录分离，升级不丢数据）；
- 统一访问日志（谁、何时、用了哪个功能、做了什么操作）；
- 插件只管自己的页面、接口和私有数据，互不干扰。

### 1.2 四类使用者

| 角色 | 关注点 | 入口 |
| --- | --- | --- |
| 普通用户 | 用工具 | 首页卡片 |
| 站点管理员 | 建组织、发账号、授权、上下线工具 | `/admin` 管理后台 |
| 插件开发者 | 新增/维护插件 | `plugins/<id>/` + `config/tools.json` |
| 部署运维 | 打包、安装、升级、卸载、换数据目录 | `build-deploy.ps1` / `一键安装.bat` / `一键卸载.bat` |

### 1.3 五条设计原则

1. **框架零改动**：加功能 = 放插件目录 + 改配置，禁止为此改 `app.py` 或 `static/`。
2. **目录即插件**：一个插件 = 一个自包含目录，可整体复制、整体删除、跨项目迁移。
3. **配置即启停**：名称、描述、分类、排序、启停、授权全部由 `config/tools.json` 决定。
4. **隔离即安全**：前端 iframe 物理隔离，后端 `/api/<id>/` 前缀隔离，数据独立目录隔离。
5. **数据可迁移**：用户数据存于数据根目录，与程序目录分离，整体换程序文件夹不丢数据。

---

## 2. 环境搭建

### 2.1 Python 版本

| 场景 | 建议版本 | 说明 |
| --- | --- | --- |
| 源码开发（主框架 + 多数插件） | ≥3.8 | 历史目标版本，主框架与多数插件兼容 |
| 源码开发（**完整功能**） | **≥3.10** | 知识库 Office 预览引擎（vendor xhr/dhr）声明 requires-python ≥3.10 |
| 打包发布 | 3.14（当前实际使用） | 见 `HANDOFF.md` 打包章节；需兼容 Win7 时必须改用 Python 3.8 打包 |

> 结论：**开发/部署请统一用 ≥3.10 的解释器**。低于 3.10 时知识库 Word/Excel 预览会回退到前端降级渲染（不报错，但样式还原度下降）。

### 2.2 安装步骤

```bash
# ① 主框架依赖（Flask + cryptography，管理后台加密存储必需）
pip install "Flask>=3.0,<3.1" "cryptography>=41.0"

# ② 核心插件依赖（管理后台；openpyxl 用于单位/部门/人员批量导入导出的 xlsx，
#    缺失时自动降级为仅 CSV）
pip install -r plugins/admin/backend/requirements.txt

# ③ 按需安装其他插件依赖（缺依赖的插件会优雅降级并在页面提示，不会拖垮主进程）
pip install -r plugins/knowledge-base/backend/requirements.txt
pip install -r plugins/shared-docs/backend/requirements.txt
pip install -r plugins/character-graph/backend/requirements.txt
pip install -r plugins/case-report/backend/requirements.txt
pip install -r plugins/file-filter/backend/requirements.txt
pip install -r plugins/trajectory-sketch/backend/requirements.txt
pip install -r plugins/trajectory-convert/backend/requirements.txt
pip install -r plugins/qr-video-decode/backend/requirements.txt
pip install -r plugins/info-transfer/backend/requirements.txt
```

Windows 下若本机默认是 Python 3.8：

```bash
C:\Users\<你>\AppData\Local\Programs\Python\Python38\python.exe -m pip install Flask cryptography
```

> 仓库根目录**没有**统一的 `requirements.txt`——依赖按插件粒度声明，避免"为了用一个工具装齐全部依赖"。部署机只需装启用的插件的依赖。

### 2.3 外部可选依赖

| 依赖 | 类型 | 缺失影响 |
| --- | --- | --- |
| LibreOffice | 外部程序（非 pip） | 仅影响知识库 `.xls` 高保真归一化与 `.doc` 归一化两条窄路径；缺失时 `.xls` 自动走 xlrd 兜底，其余功能不受影响 |
| 高德地图 Key | 前端配置 | 仅影响「地图标点」插件 |
| 大模型 API（OpenAI 兼容） | 前端配置 | 影响战果录入、人物关系星图、过滤器/轨迹速写的"大模型模式" |

---

## 3. 运行与构建

### 3.1 源码运行

```bash
python app.py
```

浏览器访问 <http://localhost:5000>。

- 默认管理员：`admin` / `admin123`（首次启动自动生成于数据根目录 `config/admin.json`，**登录后请尽快改密**）。
- 管理后台：`/admin`；数据目录设置：`/admin/settings`（仅超级管理员）。
- 环境变量：`JZTOOLS_HOST`（默认 `0.0.0.0`）、`JZTOOLS_PORT`（默认 `5000`，解析失败回落 5000）。

> 源码模式使用 Flask 内置服务器（`debug=True`，自动重载），仅供开发。生产请用打包产物（内置 waitress 8 线程）。

### 3.2 打包构建

```powershell
# 递增版本号并打包（版本号写入 version.json，是目标机配置模板同步的触发依据）
powershell -ExecutionPolicy Bypass -File build-deploy.ps1 -Version "1.7.0"

# 可选参数
#   -Python "C:\...\Python38\python.exe"   指定打包解释器（产物需兼容 Win7 时用 3.8）
#   -DeployName "JZToolsHub-py38"          输出目录名（多版本部署目录共存）
```

产物：`deploy\JZToolsHub\`（可直接运行的部署目录）+ `deploy\JZToolsHub-v<版本>.zip`（分发包）。

```
deploy\JZToolsHub\
├─ JZToolsHub.exe      # 后端可执行程序（双击或 start.bat 启动，无控制台，托盘常驻）
├─ _internal\          # Python 运行时 + Flask + 全部第三方依赖
├─ static\             # 首页前端源码（可修改，重启生效）
├─ plugins\            # 插件：frontend/ 可改；backend/ 随包源码
├─ config\tools.json   # 工具注册清单模板（首启复制到数据根目录）
├─ docs\  README.md  HANDOFF.md
├─ start.bat           # 一键启动
├─ 一键安装.bat         # 安装 / 更新（自动判断）
├─ 一键卸载.bat         # 卸载（含用户数据，需确认）
├─ install.ps1         # 安装 / 更新 / 卸载核心逻辑
└─ version.json        # 版本号（模板同步触发依据）
```

打包脚本依次完成：PyInstaller 按 `JZToolsHub.spec` 打包后端 → 组装部署目录（复制 `static/`、`plugins/`、`docs/`、`config/tools.json`）→ 清理插件运行时数据（`data/`、`.task_cache/`、`__pycache__/`、`out/`、`*.pyc`、`config.json`）→ 复制安装/卸载脚本并写 `version.json` → 生成 `start.bat` → 压缩为 zip。

> **改了安装/卸载逻辑，必须改仓库根目录的源文件再重新打包**：部署包里的是副本，下次打包会被覆盖。

### 3.3 目标机安装 / 更新 / 卸载

| 动作 | 操作 |
| --- | --- |
| 全新安装或升级 | 解压 zip → 双击 `一键安装.bat` |
| 启动 | 双击 `start.bat`（或 `JZToolsHub.exe`） |
| 卸载 | 双击 `一键卸载.bat` 并输入 Y（删程序 + 用户数据）；保留数据用 `install.ps1 -Uninstall -KeepData` |

一键安装流程：停止旧进程 → 检测既有安装（注册表 Uninstall 键 > 默认目录 `%LOCALAPPDATA%\JZToolsHub` > 含 `config/data_root.json` 的源目录 = 就地更新）→ 复制程序文件 → 同步配置模板到数据根目录 → 写 `version.json` / 注册表 / 快捷方式。

> **发版务必递增 `-Version`**：版本号不变时模板同步不触发，新版提示词等配置不会生效。

---

## 4. 目录结构

### 4.1 仓库目录

```
JZToolsHub/
├── app.py                     # ★ 框架入口：数据根初始化、日志、插件加载、核心路由、压缩/缓存中间件
├── jztools_data.py            # ★ 数据根目录管理：双指针解析、旧数据迁移、配置模板同步
├── config/
│   ├── tools.json             # ★ 工具注册清单模板（站点信息 + 分类 + 工具条目）
│   └── data_root.json         # 数据根目录备份指针（开发机产物，见 §12 注意事项）
├── plugins/                   # ★ 插件目录（一切皆插件）
│   ├── admin/                 #   核心插件：登录鉴权 / 组织人员 / 权限 / 批量导入导出
│   ├── notice-board/          #   公告板（home_card() 动态卡片）
│   ├── knowledge-base/        #   知识库（Office 预览引擎 vendor 在 backend/vendor/）
│   ├── file-filter/           #   过滤器（提供 /apply 供其他插件复用）
│   ├── trajectory-sketch/     #   轨迹速写（分析引擎 backend/engine/ 零依赖可插拔）
│   ├── shared-docs/           #   共享文档（多人协作编辑）
│   ├── case-report/           #   战果录入（大模型要素抽取 + 台账）
│   ├── character-graph/       #   人物关系立体星图
│   ├── trajectory-convert/    #   轨迹转换（Excel → 二维码视频流）
│   ├── qr-video-decode/       #   QR 视频流解码
│   ├── info-transfer/         #   信息传输（封装/解析二维码）
│   ├── map-marker/            #   地图标点（纯前端）
│   └── base64/ json-formatter/ color-picker/ md5-generator/   # 基础示例插件（纯前端）
├── static/                    # 框架前端（首页 / 外壳页 / 主题 / SVG 图标回退）
│   ├── index.html  tool.html
│   ├── css/style.css
│   ├── icons/                 # Twemoji SVG（无彩色 emoji 字体环境的回退）
│   └── js/  main.js  tool.js  jz-icon.js
├── android-app/InfoParse/     # 移动端 APP（Kotlin，信息传输的 Android 离线接收端）
├── docs/                      # 设计文档（按功能/插件归档）
├── 插件设计规范.md             # ★ 插件开发铁律（开发插件前必读）
├── 移动端APP.md                # ★ 信息传输协议权威规范
├── JZToolsHub.spec            # PyInstaller 打包配置（显式收集插件后端动态导入的库）
├── build-deploy.ps1           # 一键打包脚本
├── install.ps1                # 一键安装 / 更新 / 卸载核心逻辑（源文件）
├── 一键安装.bat  一键卸载.bat   # 双击入口
└── README.md  HANDOFF.md
```

### 4.2 数据根目录（运行时生成，默认 `<用户主目录>\.jztoolshub\`）

```
<数据根>/
├── config/
│   ├── tools.json             # 工具注册清单（首启从程序目录模板复制，之后改这里）
│   ├── admin.json             # 单位/部门/人员/角色（敏感字段 Fernet 加密）
│   ├── .admin_key             # 加密密钥（必须与 admin.json 同目录迁移）
│   └── .app_state.json        # 记录 last_app 版本号，控制模板同步幂等
├── logs/access.log            # 访问日志（按天滚动，保留 30 天）
└── plugins/<id>/              # 各插件运行数据
    ├── data/                  #   业务记录（共享文档 / 公告 / 战果台账 / 知识库 files）
    ├── config.json            #   插件运行配置（含密钥的已 gitignore）
    ├── prompt.json            #   大模型提示词（若插件使用）
    └── .task_cache/           #   异步任务临时产物（TTL 30 分钟）
```

程序目录与数据目录的对应关系由 `jztools_data.py` 维护，**插件一律通过 `jztools_data.get_data_root_dir()/get_data_root_file()` 定位自己的数据，禁止拼接绝对路径**。

---

## 5. 核心模块解析

### 5.1 app.py —— 框架入口

| 职责 | 关键实现 |
| --- | --- |
| 路径常量 | `BASE_DIR`（frozen 时为 exe 目录）、`DATA_ROOT`、`CONFIG_PATH`、`LOG_DIR`、`PLUGINS_DIR` |
| 数据根初始化 | `init_data_root()`：迁移旧数据 → 解析数据根 → `sync_templates()` 模板同步 |
| 访问日志 | `setup_access_logging()`：内存队列（容量 2000）+ 后台 `QueueListener` 异步落盘 |
| 插件加载 | `register_plugin_backends()`：先无条件加载 `admin`，再按 `tools.json` 的 `enabled` 逐个加载 |
| 核心路由 | `/`、`/tool/<id>`、`/plugin/<id>/<path>`、`/api/tools*`（reorder / visibility） |
| 中间件 | `_compress_response`（gzip，>256B 且可压缩类型）、`_static_cache`（静态 1 天 / HTML no-cache）、`_log_response`（写访问日志） |
| 工具聚合 | `get_aggregated_tools()`：manifest + tools.json + `home_card()` 钩子三级合并 |
| 权限过滤 | `_filter_visible_tools()`：非超管只看到权限点内的工具 |
| 容错 | `WindowsSafeTimedRotatingFileHandler`：Windows 文件占用导致 rename 失败时退化为「复制 + 截断」，跨天零丢日志 |

### 5.2 jztools_data.py —— 数据根目录

| 函数 | 作用 |
| --- | --- |
| `get_data_root()` | 双指针解析：主指针 `~/.jztoolshub.json` > 备份指针 `<程序目录>/config/data_root.json` > 默认 `~/.jztoolshub` |
| `get_data_root_dir(*parts)` / `get_data_root_file(*parts)` | 插件定位自有数据的唯一入口（自动建目录） |
| `migrate_legacy_app_data()` | 启动时把旧版留在程序目录的用户数据搬进数据根目录（幂等） |
| `migrate_data_root(old, new)` / `set_data_root()` | 管理员换数据目录时整体迁移 config/logs/plugins |
| `sync_templates()` | 版本升级时按 `_TEMPLATE_SYNC` 清单把程序目录模板同步进数据根目录（三种模式：`overwrite` / `merge-tools` / `ensure-keys`） |
| `data_usage_summary()` | 数据占用统计（后台设置页展示） |

### 5.3 config/tools.json + manifest.json —— 两级配置

```
展示优先级：插件 home_card() 钩子（请求时实时求值）> config/tools.json（权威）> manifest.json（回退兜底）
                                                     ↑ name/description          ↑ icon/accent/entry/features
```

`tools.json` 工具条目字段：

| 字段 | 缺省 | 说明 |
| --- | --- | --- |
| `id` | 必填 | 与目录名、`manifest.id` 三者一致 |
| `name` / `description` | 回退 manifest | 权威展示文案，改完刷新即生效 |
| `category` | 未分类 | 引用 `categories[].id` |
| `enabled` | `true` | `false` = 卡片隐藏**且后端不加载** |
| `hidden` | `false` | `true` = 只注册后端、不出卡片（`admin` 用） |
| `grant_all` | `false` | `true` = 对全体登录用户开放，无需逐人授权 |
| `order` | `0` | 同分类内升序 |

`manifest.json` 字段：`id`（必填）、`icon`、`accent`、`entry`、`features`、`version`、`author`、`form`（`source`/`package`）、`name`/`description`（仅回退用）。

### 5.4 admin 插件 —— 安全层与组织管理

`admin` 是核心基础设施插件，`register_plugin_backends()` 对其**无条件加载**，不随 `enabled` 启停。

它在 `register()` 里注册 4 个 `before_request`（按序执行）：

1. `make_session_guard` —— 空闲 30 分钟 / 绝对 12 小时超时登出，活跃请求滑动续期；
2. `_enforce_login` —— 白名单外一律要求登录（页面 302 → `/login`，`/api/*` 返回 401）；
   白名单 = `/login`、`/api/login`、`/api/logout`、`/api/session`、`/favicon.ico` + 前缀 `/static/`、`/plugin/admin/css/`、`/plugin/admin/js/`；
3. `_protect_admin_ops` —— 首页布局写操作（`reorder` / `visibility`）需登录；
4. `_enforce_tool_access` —— 非超管按权限点拦截 `/tool/<id>`、`/plugin/<id>/...`、`/api/<插件id>/...`。

它还负责：会话密钥持久化、Fernet 加解密（密码 / 身份证 / API Key）、组织架构 CRUD、角色与权限点、数据目录设置，以及单位/部门/人员的批量导入导出（`backend/batch_io.py`，依赖注入挂载，不反向 import `routes.py`）。

### 5.5 插件后端加载机制

```python
# app.py::_load_backend_module
module_name = "jztools_" + re.sub(r"\W", "_", plugin_id)     # 如 jztools_knowledge_base
spec = importlib.util.spec_from_file_location(module_name, backend/__init__.py,
                                              submodule_search_locations=[backend_dir])
...exec_module... → importlib.import_module(f"{module_name}.routes") → routes.register(app)
```

- 插件以独立模块名加载，符号互不可见；
- `routes.py` 必须导出 `register(app)`；
- 若 `routes.py` 里存在 `home_card`，框架登记为首页卡片钩子，每次首页请求时实时调用。

> **路由函数必须带插件前缀**（如 `kb_status`、`ts_upload`）：Flask 以 `view_func.__name__` 作为 endpoint，两个插件各写一个 `def status()` 会在启动阶段直接抛 `AssertionError` 导致插件整体加载失败。

### 5.6 首页卡片三级合并

```
① home_card()（插件后端可选钩子，请求时求值，可按登录用户返回动态内容）
      ↓ 未声明
② config/tools.json 的 name / description
      ↓ 缺省
③ manifest.json 的 name / description
（icon / accent / entry / features 始终以 manifest 为基础，可被 ① 覆盖）
```

参考实现：`plugins/notice-board/backend/routes.py` 的 `home_card()`（按当前用户可见范围取最新公告）。

---

## 6. 使用示例

### 6.1 首次启动与登录

```bash
python app.py          # 打开 http://localhost:5000
# 用 admin / admin123 登录 → 右上角菜单「修改密码」→ 进入 /admin
# 管理后台：建单位 → 建部门 → 建人员（勾选该人员可用的工具权限点）
```

不带代码的快速自测：

```python
import sys; sys.path.insert(0, r"D:\JZToolsHub")
import app as m
m.init_data_root(); m.setup_access_logging(m.app); m.register_plugin_backends(m.app)
c = m.app.test_client()
print(c.post("/api/login", json={"username": "admin", "password": "admin123"}).status_code)
print(c.get("/api/tools").get_json()["tools"][0]["name"])
```

### 6.2 新增一个纯前端插件

```
plugins/hello/
├── manifest.json
└── frontend/
    └── index.html
```

`manifest.json`

```json
{
  "id": "hello",
  "version": "1.0.0",
  "icon": "👋",
  "accent": "#4285F4",
  "entry": "index.html",
  "features": ["示例"],
  "form": "source"
}
```

在**数据根目录**的 `config/tools.json` 的 `tools` 数组追加：

```json
{ "id": "hello", "name": "你好工具", "description": "第一个示例插件",
  "category": "dev", "enabled": true, "order": 99 }
```

刷新浏览器首页即可看到卡片，**无需重启**（纯前端插件）。

### 6.3 新增一个带后端的插件

```
plugins/my-tool/
├── manifest.json
├── frontend/index.html
└── backend/
    ├── __init__.py          # 空文件，标识 Python 包
    ├── routes.py            # 必须导出 register(app)
    └── requirements.txt
```

`backend/routes.py`

```python
from flask import jsonify, request
import jztools_data

try:                                  # 取当前登录用户（admin 提供，导入失败兜底 None）
    from jztools_admin.routes import get_session_user
except Exception:
    get_session_user = None

try:                                  # 访问日志操作标签（B-8）
    from jztools_admin.routes import set_operation
except Exception:
    def set_operation(op): pass

API_PREFIX = "/api/my-tool"
DATA_DIR = jztools_data.get_data_root_dir("plugins", "my-tool", "data")


def register(app):
    @app.get(f"{API_PREFIX}/status")
    def mt_status():                  # ★ 函数名带插件前缀，避免 endpoint 冲突
        deps = {}
        try:
            import openpyxl; deps["openpyxl"] = True
        except Exception:
            deps["openpyxl"] = False
        return jsonify({"ok": all(deps.values()), "dependencies": deps})

    @app.post(f"{API_PREFIX}/echo")
    def mt_echo():
        set_operation("回显文本")
        user = get_session_user() if get_session_user else None
        data = request.get_json(silent=True) or {}
        return jsonify({"you_said": str(data.get("msg", ""))[:500],
                        "by": (user or {}).get("username")})
```

登记 `tools.json` 后**重启服务**，访问 `/api/my-tool/status` 验证。

### 6.4 长耗时操作：异步任务模式

预计超过 3 秒的处理（大模型调用、视频转码、大文件解析）必须走「提交即返回 + 轮询」：

```text
POST /api/<id>/<action>            → 立即返回 {"task_id": "..."}（毫秒级）
GET  /api/<id>/result/<task_id>    → {"status": "pending|running|done|error", ...}
```

实现要点：有界线程池（`ThreadPoolExecutor(max_workers=2)`）、任务表加锁、结果 TTL 30 分钟清理、任务归属校验（创建者或超管，其余 404）。

### 6.5 插件之间复用能力

**禁止** import 其他插件的后端模块（规范 B-7）。正确做法是提供并调用程序化 HTTP 接口：

```bash
# 轨迹速写调用「过滤器」的字段过滤能力
curl -X POST http://localhost:5000/api/file-filter/apply \
     -H "Content-Type: application/json" \
     -d '{"rows": [["姓名","时间","地点"], ["张三","2026-01-01 10:00","A"]], "mode": "hard"}'
# → {"rows": [...], "kept": ["时间","地点"], "removed": ["姓名"], "replace_count": 0}
```

### 6.6 运维动作速查

| 动作 | 操作 | 是否重启 |
| --- | --- | --- |
| 改名称 / 描述 / 排序 | 编辑数据根目录 `config/tools.json` | 否 |
| 临时下线 / 恢复 | 该条目 `enabled: false` / `true` | 否 |
| 按人授权 | 管理后台「人员管理 → 权限」勾选插件 ID | 否（重新登录或刷新会话） |
| 新增 / 升级插件 | 覆盖目录 → 前端递增 `?v=` → 有后端改动则重启 | 视改动而定 |
| 卸载插件 | 先删 `tools.json` 条目 → 备份数据 → 再删目录 → 重启 | 是 |
| 换数据目录 | 后台「系统设置」→ 输入新路径 →「更改并迁移」 | 否（自动迁移） |

---

## 7. HTTP API

### 7.1 框架核心接口

| 接口 | 说明 |
| --- | --- |
| `GET /` | 首页 |
| `GET /tool/<id>` | 工具外壳页（iframe 宿主） |
| `GET /plugin/<id>/<path>` | 插件前端静态资源（映射到 `plugins/<id>/frontend/`，目录穿越已拦截） |
| `GET /api/tools` | 聚合工具列表（站点信息 + 分类 + 工具清单，按权限过滤） |
| `GET /api/tools/<id>` | 单个工具信息，不存在返回 404 |
| `POST /api/tools/reorder` | 保存首页布局 `{categories: [分类id…], tools: {分类id: [工具id…]}}` |
| `GET /api/tools/visibility` | 全部分类与工具及其启用状态（含已隐藏项） |
| `POST /api/tools/visibility` | 切换启用 `{type: 'tool'\|'category', id, enabled}` |

### 7.2 登录 / 管理后台（admin 插件）

| 接口 | 说明 |
| --- | --- |
| `GET /login`、`POST /api/login`、`POST /api/logout`、`GET /api/session` | 登录闭环（`logout` 是 **POST**） |
| `POST /api/account/password` | 自助改密 `{old_password, new_password}`（新密码 ≥6 位） |
| `GET /admin`、`GET /admin/<module>` | 后台页面（`unit` / `department` / `user` / `settings`） |
| `GET /api/admin/summary` | 后台总览（各模块记录数 + 当前账号可访问性） |
| `GET /api/admin/org-tree` | 组织架构树（只读，供业务插件选可见范围） |
| `GET\|POST /api/admin/data-settings` | 查看 / 修改数据根目录（仅超管） |
| `GET\|POST /api/admin/units`、`PUT\|DELETE /api/admin/units/<id>` | 单位 CRUD（需 `unit` 权限） |
| `GET\|POST /api/admin/departments`、`PUT\|DELETE /api/admin/departments/<id>` | 部门 CRUD（需 `department` 权限） |
| `GET\|POST /api/admin/users`、`PUT\|DELETE /api/admin/users/<username>` | 人员 CRUD（需 `user` 权限）；密码/身份证/API Key 加密存储 |
| `GET\|POST /api/admin/permissions`、`PUT\|DELETE /api/admin/permissions/<id>` | 角色 CRUD（已并入人员管理） |
| `GET /api/admin/batch/<module>/template?format=xlsx\|csv` | 批量导入模板下载（`<module>` = `unit`/`department`/`user`） |
| `GET /api/admin/batch/<module>/export?format=xlsx\|csv` | 批量导出（`?sensitive=1` 才导出 API Key 明文） |
| `POST /api/admin/batch/<module>/import` | 批量导入（multipart：`file`、`mode`、`dry_run`、`auto_create_parent`、`on_error`） |

> **权限模型**：账号的 `permissions` = `tools.json` 中的插件 ID 列表。非超管只能访问权限点内的工具（页面 403、API 403、`/api/tools` 自动过滤）。拥有全部管理模块的角色视为超级管理员。`grant_all: true` 的工具对全体登录用户开放。
> **批量导入**为两步式：先 `dry_run=1` 预览（逐行给出 新增/更新/跳过/错误），确认后再写入；预览与执行共用同一套推演代码，保证「预览所见 = 执行所得」。

### 7.3 各业务插件接口（摘要）

完整逐条清单见 `plugins/<id>/README.md` 与 `docs/` 下对应设计文档。各插件接口统一挂在 `/api/<插件id>/` 前缀下：

| 插件 | 关键接口 |
| --- | --- |
| knowledge-base | `/status` `/config` `/categories`(CRUD) `/files`(CRUD) `/files/<id>/raw` `/files/<id>/preview` `/files/<id>/download` |
| file-filter | `/status` `/config` `/config/test` `/filter` `/result/<task>` `/download/<task>` `/apply`（程序化接口） |
| trajectory-sketch | `/status` `/config` `/upload` `/analyze` `/result/<task>` `/download/<task>` |
| shared-docs | `/status` `/documents`(CRUD) `/documents/<id>/content` `/presence` `/rename` `/scope` `/export` `/import` |
| case-report | `/config` `/config/test` `/status` `/parse` `/result/<task>` `/records`(CRUD) `/aggregate` `/cases` `/months` `/categories` `/export` |
| character-graph | `/status` `/config` `/prompt` `/analyze` `/result/<task>` |
| notice-board | `/status` `/config` `/announcements`(CRUD) `/latest` |
| trajectory-convert | `/config` `/status` `/convert` `/status/<task>` `/download/<task>` `/image/<task>/<i>` `/download-selected` |
| qr-video-decode | `/status` `/reassemble` `/reassemble/<task>` |
| info-transfer | `/status` `/formats` `/encode` `/task/<task>` `/encode/<task>/cancel` `/download/<task>` `/image/<task>/<i>` `/frame/<task>/<i>` `/decode` `/decode/<task>` `/export` `/ping` |

---

## 8. 内置插件一览

当前 16 个插件：5 个纯前端、11 个前后端一体（其中 `admin` 为核心插件）。

| 插件 | 分类 | 形态 | 后端依赖 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| admin | — | 核心（hidden） | cryptography / Flask / openpyxl | 始终加载 | 登录鉴权、会话超时、单位/部门/人员/角色、工具访问拦截、数据目录设置、批量导入导出 |
| notice-board | office | 前后端 | 无第三方 | 启用（grant_all） | 按单位/部门/人员可见范围发布公告；`home_card()` 动态卡片 |
| knowledge-base | office | 前后端 | openpyxl / xlrd / python-docx / olefile（旧版格式转换，缺失优雅降级）；LibreOffice 可选 | 启用（grant_all） | 上传 PDF/OFD/Word/Excel/MD（≤20MB）并多级分类；Word/Excel 由 xhr/dhr 引擎按需渲染预览；原件下载 |
| file-filter | office | 前后端 | openpyxl / xlrd / requests | 启用 | 表格脱敏过滤：硬过滤 / 大模型语义匹配 / 文本与正则后处理；`/apply` 供其他插件复用 |
| trajectory-sketch | office | 前后端 | openpyxl / xlrd / requests | 启用 | 轨迹表 → 字段过滤 → 轨迹分析 → 速写报告；分析引擎为零依赖可插拔包 |
| shared-docs | office | 前后端 | python-docx / openpyxl / xlrd | 启用 | 多人协作编辑 Word/Excel，乐观锁、在线用户、导入导出 |
| case-report | office | 前后端 | requests / openpyxl | 启用 | 收网报告 → 大模型五要素抽取 → 键值对台账、跨记录汇总、Excel 导出 |
| character-graph | office | 前后端 | python-docx / pypdf / requests | 启用 | 文档 → 大模型抽取人物关系 → 3D 星图 |
| info-transfer | office | 前后端 | qrcode / zfec / opencv / numpy / openpyxl（+ 可选 python-docx / xlrd / olefile） | 启用 | 文字/文档封装为二维码（静态多张 / 视频流），解析还原；协议 v2 `fmt=file` 原文件完整传输 |
| trajectory-convert | dev | 前后端 | openpyxl / xlrd / qrcode / opencv / numpy / zfec | 启用 | Excel 轨迹 → 二维码视频流 / 静态二维码 |
| qr-video-decode | dev | 前后端 | zfec | 启用 | 二维码视频流逐帧识别 + zfec 纠错重组 |
| md5-generator | dev | 纯前端 | — | 启用 | MD5 摘要生成 |
| map-marker | maps | 纯前端 | — | **停用** | 高德地图标点、二维码识别回放、移动轨迹 |
| base64 | dev | 纯前端 | — | **停用** | Base64 编解码 |
| json-formatter | dev | 纯前端 | — | **停用** | JSON 格式化 / 压缩 |
| color-picker | design | 纯前端 | — | **停用** | 取色器 |

> `ai` / `design` / `maps` 三个分类在当前默认配置下没有启用中的工具（其成员均为停用状态），首页会显示空分类；可在「隐藏工具」中把空分类一并下线。

---

## 9. 典型业务链路

### 9.1 轨迹数据 ↔ 二维码闭环

```
Excel 轨迹表 ──▶ [trajectory-convert] ──▶ 二维码视频流 / 静态二维码
                                              │
        恢复的轨迹 JSON ◀── [qr-video-decode] ◀┘
                        │
                        └──▶ [map-marker] 扫码识别 → 地图标点 + 移动轨迹回放
```

数据格式统一为键值对 JSON：`key` = 时间，`value` = `[经度, 纬度]`。

### 9.2 信息传输与移动端 APP

「信息传输」把文字/文档封装为二维码进行**离线传输**，`android-app/InfoParse/` 是配套的 Android 接收端，扫码还原。全程无网络通信。

信封：`{"jzt":1,"fmt":<fmt>,"name":<名>,"data":<载荷>}`，静态多页加 `"pg":{"i":1,"n":3}`。

| fmt | data | name | 状态 |
| --- | --- | --- | --- |
| `text` / `markdown` | 原文 | 无扩展名基名 | 在用 |
| `word` / `excel` | 逐段文本 / 二维数组 | 无扩展名基名 | 兼容保留（旧码可解析） |
| `file` | 文件字节 base64 | **完整文件名（含扩展名）** | v2 起：原文件完整传输 |

判别顺序不可变：`{` 开头 → JSON 信封（有 `pg` → 多页收集器）；否则按 base64 帧头 → 视频流收集器。
**协议权威定义在根目录《移动端APP.md》**，错误文案是逐字契约；改协议必须四端一文档齐改（桌面后端 + Web 前端 + APP + 《移动端APP.md》）。APP 的构建环境、真机限制与回归清单一并维护在 `HANDOFF.md` 第 9 节。

---

## 10. 配置模板同步与数据迁移

### 10.1 为什么需要模板同步

程序目录里的 `config/tools.json`、插件 `prompt.json` / `config.json` 是**随版本迭代的模板**；程序运行时读的是**数据根目录里的运行配置**。早期版本升级只替换程序文件，数据根目录的 prompt 等未更新，导致"新版提示词不生效"。

### 10.2 触发与规则

触发条件：程序目录 `version.json` 的 `app` ≠ 数据根目录 `config/.app_state.json` 的 `last_app`（一致则跳过，幂等）。

| 模板（程序目录 → 数据根目录） | 模式 | 含义 |
| --- | --- | --- |
| `config/tools.json` | merge-tools | 新分类/新工具追加，保留用户启停与排序 |
| `plugins/case-report/backend/config.template.json` | ensure-keys | 只补模板新增键，保留用户 LLM 配置 |
| `plugins/character-graph/backend/config.template.json` | ensure-keys | 同上（含 `ui.api_source`） |
| `plugins/trajectory-sketch/backend/config.template.json` | ensure-keys | 保留管理员自定义的保留字段名单 / 列映射 / 阈值 / 报告文案 |
| `plugins/file-filter/backend/config.template.json` | ensure-keys | 保留管理员配置（保留字段名单 / 后处理规则 / LLM） |

> **模板命名纪律：配置模板一律命名 `config.template.json`，与运行时 `config.json` 分离。**
> 打包脚本会删除插件树内所有 `config.json`（清掉本机含 API Key 的运行时配置），模板若沿用
> `config.json` 命名会被一并删除，导致同步链路静默失效。新增带模板配置的插件时，必须在
> `jztools_data._TEMPLATE_SYNC` 与 `install.ps1` 的 `Sync-ConfigTemplates` **两处同时登记**且保持一致
> （`build-deploy.ps1` 打包后会自检 `*.template.json` 数量，防止误删）。
>
> **提示词（`prompt.json`）不再随版本下发**：模板已删除，提示词由插件内 `llm_client` 的内置默认值
> 提供（代码即唯一来源）。如需在目标机调整，直接手写 `<数据根>/plugins/<id>/prompt.json` 即可，
> 插件的 `load_prompt()` 存在即优先读取，且从此不会被版本升级覆盖。

双保险：`install.ps1` 的 `Sync-ConfigTemplates` 在安装时也做等价合并；即使手动替换程序文件夹，app 启动时也会自动同步。

> **新增带模板配置的插件时，必须在两处同时登记**并保持一致：① `jztools_data.py` 的 `_TEMPLATE_SYNC`；② `install.ps1` 的 `Sync-ConfigTemplates`。
> 用户数据（`admin.json`、`.admin_key`、插件 `data/`、日志）**绝不参与**模板同步。

### 10.3 升级 / 换目录

1. **旧版（数据在程序目录）升级**：新版首启自动把程序目录的 `admin.json` / `.admin_key` / `logs/` / 各插件 `backend/data|config.json|prompt.json|.task_cache` 迁入数据根目录（幂等，目标已存在不覆盖）。
2. **整体替换程序文件夹**：数据根目录不动，用户数据不丢。
3. **管理员换目录**：后台 → 系统设置 → 输入新绝对路径 →「更改并迁移」（按 config/logs/plugins 三个子目录整体搬移，已存在不覆盖，并更新双指针）。

> 迁移是「移动」而非「复制」。`.admin_key` 与 `admin.json` 必须一起迁，否则无法解密。

---

## 11. 访问日志

- 位置：数据根目录 `logs/access.log`，按天滚动，保留 30 天；开关：数据根目录 `config/tools.json` → `site.logging`。
- 格式（TAB 分隔，每行一条）：

```
时间戳  级别  ip=客户端IP  user=登录用户名  method=方法  func=功能标签  op=具体操作  path=路径  status=状态码  cost_ms=耗时  ua=浏览器标识
```

- `ip` 兼容反向代理（优先 `X-Forwarded-For` 首段）；`user` 未登录为 `-`；`func` 自动解析（首页 / 工具壳页 / 插件页 / 后端接口 / 静态资源）；`op` 优先取插件显式标记。
- **禁止记录请求体**（含密码与正文）。插件**禁止**自行落盘访问日志（规范 B-8）；如需具体操作描述，在处理器内调用 `set_operation("发布公告")`。

---

## 12. 故障排查

| 现象 | 排查建议 |
| --- | --- |
| 首页看不到某个工具 | 检查**数据根目录** `config/tools.json` 的 `enabled` 与 `id` 是否一致；分类是否被 `enabled:false` 整组下线 |
| 改了插件 JS/CSS 页面没变化 | 静态资源 1 天强缓存；把入口页引用递增为 `app.js?v=N+1` |
| 后端接口 404 | 确认 `backend/__init__.py` 与 `backend/routes.py`（含 `register(app)`）存在；含后端的插件改动后需重启 |
| 启动时报 `View function mapping is overwriting an existing endpoint` | 两个插件的路由函数同名；给路由函数加插件前缀（如 `mt_status`），或显式传 `endpoint=` |
| 卡片点击显示"无法加载工具" | 检查 `manifest.json` 的 `entry` 指向的文件是否存在于 `frontend/` |
| 登录后接口全部 401 | 会话超时（默认空闲 30 分钟 / 登录满 12 小时）；重新登录 |
| 轨迹速写提示「过滤器插件不可用」 | 确认 `file-filter` 的 `enabled: true` 并重启服务，再看 `/api/trajectory-sketch/status` 的 `filter_plugin.reason` |
| 知识库 Word/Excel 预览样式不对 | 确认 Python ≥3.10（引擎要求）；改了引擎或升级 vendor 后需删除 `<数据根>/plugins/knowledge-base/data/files/*.preview.json` 缓存 |
| 地图标点空白 | 在插件页「⚙️ 配置」中填写有效的高德 Web 服务 Key |
| 端口被占用 | 设 `JZTOOLS_PORT` 换端口，或先停旧进程 |
| 升级后数据不见了 | 确认数据根目录未被误删；旧程序目录里的数据会在新版首启自动迁移，升级前勿删旧目录 |
| 升级后提示密码错误 | 只复制了 `admin.json` 而未迁移 `.admin_key`；两者必须同目录 |
| 升级后大模型解析效果没变 | 版本号未递增，模板同步未触发 |
| 图标显示为方块 | 前端 emoji 依赖系统 emoji 字体；框架内置 SVG 回退（`static/icons/` + `jz-icon.js`），新增插件图标请在该文件登记 |

> **`config/data_root.json` 已移出版本库**（含机器相关的绝对路径，且是 `get_data_root()` 的备份指针）。
> 若在旧克隆里发现数据根目录指向他人路径，删除该文件即可回落到默认 `~/.jztoolshub`；
> 程序首启或管理员改数据目录时会自动重写它。

---

## 13. 开发者约定速查

1. **ID 三处一致**：目录名 = `manifest.id` = `tools.json` 的 `id`，仅允许 `[A-Za-z0-9_-]`。
2. **改了 JS/CSS 必须递增 `?v=N`**：`/plugin/` 下静态资源 1 天强缓存，这是本项目最常踩的坑。
3. **后端路由统一挂 `/api/<id>/`**，且**路由函数名必须带插件前缀**（如 `kb_status`、`ff_filter`）。
   Flask 以 `view_func.__name__` 作为 endpoint，两个插件各写一个 `def status()` 会在启动阶段抛
   `AssertionError` 导致注册失败。框架已对单个插件的加载失败做隔离（只记录到
   `app._plugin_load_errors` 并告警，不再拖垮整站），但仍应遵守命名纪律——隔离不能回滚
   已经注册了一半的路由。
4. **长耗时必须异步化**（>3 秒）：提交即返回 `task_id` + 轮询；有界线程池 + TTL 30 分钟 + 归属校验。
5. **数据只写数据根目录**：用 `jztools_data.get_data_root_dir/file()`，禁止拼绝对路径或写别的插件目录。
6. **归属四字段取自会话**：`created_by` / `created_by_name` / `unit_id` / `department_id`，**禁止**从请求体接收；列表接口必须按可见性过滤（越权读 404、越权写 403）。
7. **禁止 import 其他插件后端模块**（B-7）；需要复用能力就提供程序化 HTTP 接口（参考 `file-filter` 的 `/apply`）。
8. **禁止插件自行落盘访问日志**（B-8）；用 `set_operation("描述")` 标记具体操作。
9. **敏感配置外置**：API Key 写 `backend/config.json`（gitignore）或由用户在页面录入，禁止硬编码入库。
10. **前端资源用相对路径**；`data/` 与含密钥的 `config.json` 必须 gitignore；**随版本下发的配置模板必须命名 `config.template.json`**（不得叫 `config.json`，否则会被打包清理规则删掉）。
11. **自包含验收**：把插件目录 + 一段 `tools.json` 注册片段复制到全新部署即可完整工作。
12. **卸载顺序**：先删 `tools.json` 条目 → 备份数据 → 再删目录（顺序反了会留下悬空引用）。

> 完整条款（B-1~B-21、SEC-1~SEC-11、F-1~F-7、S-1~S-8、M-1~M-3、V-1~V-7）见 **《插件设计规范.md》**。

---

*JZToolsHub · 一切皆插件 · 配置驱动的工具集合*
