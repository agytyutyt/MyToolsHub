# HANDOFF.md — JZToolsHub 交接文档

> 面向没有上下文的接手者：**请先完整读完本文，再动手改代码。**
> 最后更新：2026-09-14（文档全面重写：同步 git 实际状态、合并移动端章节、重排踩坑清单）。
> 配套必读：`README.md`（架构 / 环境 / API / 目录 / 使用示例）、`插件设计规范.md`（插件开发铁律）、
> `20260914评估报告.md`（插件规范符合性与项目问题清单）。

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

### 1.1 启动时序

```
python app.py
  → init_data_root()              迁移旧数据 → 解析数据根目录 → sync_templates() 模板同步
  → setup_access_logging(app)     异步访问日志（队列 2000 + 后台线程落盘）
  → register_plugin_backends(app) 先无条件加载 admin，再按 tools.json 的 enabled 逐个加载
  → 监听 0.0.0.0:5000             源码=Flask debug 服务器；打包=waitress 8 线程
```

### 1.2 请求拦截管线（admin 注册的 4 个 before_request，按序）

1. `make_session_guard`：空闲 30 分钟 / 绝对 12 小时超时登出，活跃滑动续期；
2. `_enforce_login`：白名单之外一律要求登录（页面 302 → `/login`，`/api/*` 401）；
   白名单 = `/login`、`/api/login`、`/api/logout`、`/api/session`、`/favicon.ico` + 前缀 `/static/`、`/plugin/admin/css/`、`/plugin/admin/js/`；
3. `_protect_admin_ops`：首页布局写操作需登录；
4. `_enforce_tool_access`：非超管按权限点拦 `/tool/<id>`、`/plugin/<id>/...`、`/api/<插件id>/...`。

### 1.3 关键单例与全局

| 位置 | 内容 |
| --- | --- |
| `app.py` | `DATA_ROOT` / `CONFIG_PATH` / `LOG_DIR` / `LOG_FILE`（`init_data_root()` 后指向数据根目录）、`_plugin_home_card_hooks`（首页卡片钩子表）、`_tool_meta_cache`（日志用，2s TTL） |
| `jztools_data.py` | `get_data_root()` 双指针解析、`_TEMPLATE_SYNC` 模板同步清单、`_LEGACY_MAP` 旧数据迁移映射 |
| `plugins/admin/backend/routes.py` | `ADMIN_CONFIG_PATH`（数据根 `config/admin.json`）、`_fernet`（懒加载）、`_registered_tool_ids()` / `_grant_all_tool_ids()` |

### 1.4 环境变量

`JZTOOLS_HOST`（默认 `0.0.0.0`）、`JZTOOLS_PORT`（默认 5000，解析失败回落 5000）。

---

## 2. 当前开发状态

### 2.1 git 实际状态（2026-09-14 核实）

| 项 | 值 |
| --- | --- |
| 当前分支 | `main` |
| HEAD | `ca69141`（2026-09-14）知识库阅读页背景卡片贴合预览内容宽度 + 长 token 撑破版心的横向滚动条修复 |
| 工作区 | **干净**（无未提交改动，无未跟踪文件） |
| 远程 | `origin = https://github.com/agytyutyt/MyToolsHub.git`，`origin/main` 与本地一致 |
| 其他分支 | `G2改造`、`PluginDesign`、`共享文档`、`功能优化`、`压力测试`、`战果录入`、`插件位置编辑`、`界面滑块`、`登录改造`（均为历史功能分支，未合并） |

> 早前版本文档记录的"大量未提交改动"与"最新提交 7269cb8"已过时：相关改动已随后续提交入库。
> `G2改造` 分支的连续曲率圆角引擎（jz-radius v1.2 + 基准页 + 设计文档）**未并入 main**，打包不含。

### 2.2 产物状态（需人工确认后决定是否重打）

- 最近一次明确记录的打包是 **v1.6**（2026-09-11，`deploy/JZToolsHub-v1.6.zip` 约 102MB）。
- 但 `ca69141` / `98dabe7` / `77e0f22`（2026-09-13 ~ 09-14）的知识库 Office 预览引擎改造**在此之后**。
- **结论：若要发布含最新知识库预览的版本，必须重新打包并递增 `-Version`**（否则目标机模板同步不触发）。

### 2.3 无硬性阻塞，但以下事项未充分验证

1. 访问日志字段仅经 Flask test client 验证，未在真实浏览器/多用户高并发环境走查；`get_session_user()` 每请求读 `config/admin.json`（静态资源已跳过），高并发如吃紧要加缓存。
2. 战果录入为**仅大模型解析**，缴获物品明细依赖大模型结构化输出；换低性能模型需复测。
3. 公告板 / 共享文档 / 战果录入等前端功能多靠 `node --check` + test client 回归，浏览器走查较少。
4. 一键安装/卸载脚本已在开发机通过语法检查与 exe 冒烟，**尚未在目标机做完整「全新安装 → 更新 → 卸载」三段式实测**。
5. `info-transfer` 的 `fmt=file` 端到端（桌面封装 → APP 扫码 → 导出 → 与原文件逐字节比对）尚未真机验证，是移动端首要待办。
6. 打包用 Python 3.14 时产物**不支持 Win7**；需兼容 Win7 必须用 Python 3.8 打包。

---

## 3. 已完成事项

| 时间 | 里程碑 | 关键内容 |
| --- | --- | --- |
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

| # | 事项 | 说明 |
| --- | --- | --- |
| 1 | 重新打包并发版 | 2026-09-13 之后的知识库引擎改造尚未进入任何 zip；发版需递增 `-Version` |
| 2 | 补齐 `prompt.json` 模板或清理同步清单 | `_TEMPLATE_SYNC` 与 `install.ps1` 仍登记两个 `prompt.json`，但仓库中文件已不存在，每次版本升级都会打印「缺少模板」告警；要么补回模板，要么从清单移除（详见评估报告 §5.2） |
| 3 | 修复 `trajectory-sketch` 模板被打包脚本删除 | `build-deploy.ps1` 会删除插件树内所有 `config.json`，包括作为同步模板的 `trajectory-sketch/backend/config.json`（详见评估报告 §5.3） |
| 4 | `config/data_root.json` 移出版本库 | 该文件被跟踪且含开发机绝对路径，克隆到新机器会指向错误数据目录；应加入 `.gitignore` |
| 5 | 验证 `fmt=file` 端到端 | 桌面封装 → APP 扫码 → 导出 → 逐字节哈希比对 |

### P1（功能与质量）

| # | 事项 |
| --- | --- |
| 6 | `file-filter/backend/routes.py` 的 `def status()` 改为带前缀（如 `ff_status`），消除 endpoint 冲突隐患（当前唯一不带前缀的路由函数） |
| 7 | 统一前端资源版本号起点与步长（现 case-report 用序号 v34、character-graph 用日期戳、admin 各页同一份 CSS 引用了 v1/v2/v5 三个版本） |
| 8 | 清理随包分发的开发期文件：`knowledge-base/backend/{_build_phase8.py,test_routes_preview.py}`、`trajectory-sketch/frontend/icons/_generate.py`、`map-marker/frontend/*.py` |
| 9 | 为 `file-filter`（🧹）补 SVG 回退图标，或统一 SVG 回退为按 codepoint 自动推导（不再维护硬编码映射表） |
| 10 | 首页空分类处理：`ai` / `design` / `maps` 无启用工具时不应显示 |
| 11 | 删除 `app.py` 中未被消费的 `_EMOJI_ICON_FILES` / `icon_file` 字段（前端统一由 `jz-icon.js` 兜底），或将前端改为消费它 |

### P2（体验与规范）

| # | 事项 |
| --- | --- |
| 12 | 更新《插件设计规范》：补充数据根目录、`home_card()`、`grant_all`、endpoint 命名前缀、程序化接口、模板同步清单登记等（详见评估报告 §4） |
| 13 | 在目标机做一次「全新安装 → 更新 → 卸载」三段式实测 |
| 14 | 清理工作区构建产物：`deploy/`、`dist/`、`build/` 合计约 1.2GB（含 3 个历史版本目录与 11 个旧 zip） |
| 15 | 生产部署评估：当前上限约 300 并发（Flask/waitress 单进程线程模型），高负载建议 Gunicorn 多进程 + Nginx 反代 |
| 16 | 移动端：恢复或彻底移除「重置」功能；`CameraScanner.averageLuminance` 亮度采样保留但无人调用 |

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

---

## 6. 已知问题

### 6.1 功能/数据类

| 级别 | 问题 |
| --- | --- |
| 中 | `_TEMPLATE_SYNC` 与 `install.ps1` 登记的 `case-report`/`character-graph` 的 `prompt.json` 模板在仓库中不存在，同步时会被跳过并告警；两插件实际使用 `llm_client` 内置默认提示词，无法通过模板同步更新 |
| 中 | `build-deploy.ps1` 删除插件树内所有 `config.json`，会连带删除作为同步模板的 `trajectory-sketch/backend/config.json`，导致目标机该模板永不生效 |
| 中 | `config/data_root.json`（含开发机绝对路径）已纳入版本库，新机器克隆后数据根目录可能指向错误路径 |
| 低 | `app.py::_EMOJI_ICON_FILES` 提供的 `icon_file` 字段前端无人消费，属冗余维护点；`file-filter` 的 🧹 无任何 SVG 回退 |
| 低 | `ai` / `design` / `maps` 分类在当前默认配置下无启用工具，首页显示空分类 |
| 低 | `knowledge-base/backend/` 与 `trajectory-sketch/frontend/icons/`、`map-marker/frontend/` 混入了开发/测试脚本，随包分发 |
| 低 | README 宣称 Python 3.8 为目标环境，但知识库引擎要求 ≥3.10，**完整功能的最低版本实为 3.10** |
| 低 | `plugins/info-transfer/backend/requirements.txt` 仍列 python-docx / xlrd / olefile（代码中确为可选依赖，用于"精简传输"模式），与部分文档"已移除该依赖"的表述不一致 |

### 6.2 兼容性 / 部署类

| 级别 | 问题 |
| --- | --- |
| 中 | 打包用 Python 3.14 时产物不支持 Win7；需 Win7 兼容必须 3.8 打包，但 3.8 会导致知识库预览引擎不可用（要求 ≥3.10）——二者不可兼得，需按目标机选择 |
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

5. **Flask 视图函数名全局唯一 —— 路由函数必须带插件前缀**。两个插件各写一个 `def status()` 会在启动时抛 `AssertionError: View function mapping is overwriting an existing endpoint function`，导致插件整体加载失败（轨迹速写与过滤器曾因此撞车）。新增插件后务必重启并确认日志出现「已注册后端插件：\<id\>」。
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
15. **旧浏览器 emoji 渲染不稳定**（Chrome 72/78）：新样式优先用纯文本或自绘 SVG，不要依赖 emoji 字体。SVG 回退登记表在 `static/js/jz-icon.js`。
16. **知识库「已转换」提示用 `sessionStorage` 记忆关闭状态**，不能用 `localStorage`（一个文件被多人先后打开，互不影响）。

### 7.4 打包 / 安装脚本

17. **改了安装/卸载逻辑必须改仓库根目录源文件并重新打包**：部署包里的是副本，直接改包内脚本不会回写仓库。
18. **编码约定**：`install.ps1` 必须 UTF-8 with BOM；`一键安装.bat`/`一键卸载.bat` 必须 GBK/ANSI **且不要加 `chcp 65001`**；两个 .bat **必须 CRLF 换行**（曾因存成 LF 导致 cmd 拼接解析报碎片错误）。脚本写出的 JSON 一律 UTF-8 无 BOM。
19. **`install.ps1` 只能在「含 JZToolsHub.exe 的解压目录」里跑**：在仓库根目录跑时，`config/data_root.json` 备份指针会让脚本误判为"既有安装 → 就地更新"，最后报"安装目录缺少 JZToolsHub.exe"。
20. **发版必须递增 `-Version`**，否则 `sync_templates()` 判定未升级而跳过模板同步。
21. **`JZToolsHub.spec` 的 PACKAGES 不要为纯标准库引擎加条目**（知识库 xhr/dhr 随插件目录分发，PyInstaller 不感知也不需要）；只有 pip 包才需要 `collect_all`。

### 7.5 第三方库行为

22. **openpyxl 四个坑**：① `read_only=True` 读不到合并单元格（`ReadOnlyWorksheet` 无 `merged_cells`），要处理合并必须用普通模式；② `data_only=True` 对"从未被 Excel 计算过"的公式返回 `None`，判断"是不是公式"要用 `data_only=False` 再加载一遍比对；③ Excel 数字只保留 15 位有效数字，18 位身份证按数字存会被静默改写（必须靠"原始单元格是 float 且 ≥1e15"识别）；④ 拒绝写入 XML 非法控制字符（造测试夹具时别塞，但 CSV 可以携带，解析时要清理）。
23. **Word 二进制 `.doc` 解析五个易错点**：① FIB 在 `WordDocument` 流 0x1A2 处的 `fcClx/lcbClx` 指向 `0Table`/`1Table` 的 CLX 分片；② PlcPcd 用可变长度 CPs；③ `\x07\x07` 是行结束（单 `\x07` 是单元格结束，`\r` 是段落结束）；④ 闭包捕获 `buf=[]` 后函数内 `buf=[]` 会重绑定，要用 `del buf[:]`；⑤ `close_row()` 不得给空缓冲区补单元格，否则凭空多出空列。
24. **跨项目移植算法必须对齐计量口径**：地球半径取 6378137 且结果 `round()` 取整；地点簇建在"清洗后未去重的行"上；"采样间隔中位"含 0 间隔而阈值推导用有效间隔（Δt>0），两者不可混用。差一个采样点对拍就不一致。
25. **xhr 渲染器的 `style_table[0]` 不是"默认样式"**而是"最先出现的样式"；缺格取 `[0]` 会把空白区染色（已在 vendor 副本打补丁，见 `backend/vendor/README.md`）。
26. **LibreOffice 解包版 `soffice --version` 会挂起**；超时压到 10s 并吞异常（版本仅展示用）。固定 profile 复用（`<数据根>/.lo-profile`）可把冷启动从 37~40s 降到 15~18s；失败时重置 profile 再试一次。
27. **给外部转换程序做测试夹具用 `.bat` 转调 Python**：`subprocess.run` 能直接跑 .bat 但不能跑 .py。

### 7.6 工具链

28. **Bash 工具 PATH 损坏**（`ls`/`wc`/`dirname` 可能 command not found），**PowerShell 的 stdout 可能被吞**——探测类命令写临时文件再用 Read 读取；`Read`/`Glob`/`Grep`/`Write`/`Edit` 工具不受影响，优先用它们。
29. **Bash heredoc 会吃反斜杠**（`"\t"`/`"\n"` 落盘成真实制表符/换行导致 JS 语法错误）。含转义序列的补丁一律用 Write 工具写脚本文件再执行。
30. **PowerShell 里跑含引号/花括号的 `python -c "..."` 极易翻车**；复杂断言先写临时 .py 再执行。
31. **中文输出乱码大多是 PowerShell 管道显示问题**，数据本身是 UTF-8；断言写在 Python 代码里（`assert ... , dict`），别靠肉眼读控制台。
32. **浏览器自动化（agent-browser）**：插件前端跑在 iframe 里，`click <css>` 默认作用于父文档会 "Element not found"，登录后直接开 `/plugin/<id>/index.html` 最省事；**每次 CLI 调用都是新会话**，登录与后续操作必须放进同一次 `batch`；默认视口约 1080×480，模态框高于视口时真实点击会落到遮罩上关掉浮窗，实测前先 `set viewport 1440 1000`；本机有 `http_proxy` 时要设 `no_proxy=127.0.0.1,localhost`。

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

改信息传输协议时**四端一文档齐改**：桌面插件 `routes.py` + Web 前端 + APP 端 + 《移动端APP.md》（改完记得重启服务）。

### 10.2 快速命令

```bash
# 启动（源码）
python app.py

# 打包（必须递增版本号）
powershell -ExecutionPolicy Bypass -File build-deploy.ps1 -Version "1.7.0"

# 目标机：解压 zip → 双击 一键安装.bat → start.bat 启动；卸载双击 一键卸载.bat
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
| `20260914评估报告.md` | 插件规范符合性分析、开发/更新/移除便利性评估、规范优化建议、项目问题清单 |
| `docs/` | 登录改造与数据隔离、容器化与插件热插拔、知识库、插件库优化方案、过滤器、轨迹速写、批量导入导出等设计文档 |

---

*接手者请优先处理 §4 的 P0 清单，并在完成任何功能改动后按 §10.1 同步文档。*
