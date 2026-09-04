# HANDOFF.md — JZToolsHub 交接文档

> 写给一个没有上下文的会话：请先完整读完本文，再动手。
> 最后更新：2026-09-04（文档对齐代码实际：API 表补全、架构速记、插件后端速查表、git 状态刷新）

---

## 1. 这个项目是什么

`D:\JZToolsHub` 是一个 **Flask + 配置驱动的插件化工具箱**（"一切皆插件"）：

- 主应用 `app.py` 启动时扫描 `plugins/<插件id>/`，注册插件后端路由到同一 Flask 应用。
- 插件前端放在 `plugins/<id>/frontend/`，通过 `/plugin/<id>/<path>` 静态访问。
- 工具清单由 `config/tools.json` 声明（name/description/category/order/enabled/hidden/grant_all）。
- 管理后台（`admin`）是**核心基础设施插件**：登录鉴权、会话超时、单位/部门/人员/权限管理、
   工具访问控制、Fernet 加密存储，始终加载、不随 enabled 启停。
- `README.md` 有完整架构说明与 HTTP API 表，改完功能记得同步它；`插件设计规范.md` 是插件开发铁律。

### 1.1 架构速记（改代码前先看这里）

**启动时序**（app.py `__main__`）：
`init_data_root()`（迁移旧数据 → 解析数据根目录 → `sync_templates()` 模板同步）→
`setup_access_logging()`（异步日志队列）→ `register_plugin_backends()`（先无条件加载 admin，再按
tools.json 的 `enabled` 逐个加载其余插件后端）→ 启动 HTTP 服务。

**请求拦截管线**（admin 插件 `register()` 注册的 4 个 before_request，按序执行）：
1. `make_session_guard`：空闲 30 分钟 / 绝对 12 小时超时登出，活跃滑动续期；
2. `_enforce_login`：白名单（/login、/api/login、/api/logout、/api/session、/favicon.ico、
   /static/、/plugin/admin/css|js）之外一律要求登录（页面 302，API 401）；
3. `_protect_admin_ops`：首页布局写操作需登录；
4. `_enforce_tool_access`：非超管按权限点拦 `/tool/<id>`、`/plugin/<id>/...`、`/api/<插件id>/...`。

**关键单例/全局**：
- `app.py`：`DATA_ROOT/CONFIG_PATH/LOG_DIR`（init_data_root 后指向数据根目录）、
  `_plugin_home_card_hooks`（home_card 钩子表）、`_tool_meta_cache`（日志解析用 2s TTL 缓存）。
- `jztools_data.py`：`get_data_root()` 双指针解析（主指针 `~/.jztoolshub.json` 优先，
  备份指针 `<程序目录>/config/data_root.json`）；`_TEMPLATE_SYNC` 模板同步清单；
  `_LEGACY_MAP` 旧版数据迁移映射。
- admin：`ADMIN_CONFIG_PATH`（数据根目录 config/admin.json）、`_fernet`（懒加载）、
  `_registered_tool_ids()` / `_grant_all_tool_ids()`（权限点全集 / grant_all 全集）。

**tools.json 特殊字段**：`hidden: true`（不出卡片但注册后端，admin 用）、
`grant_all: true`（全体登录用户默认可用，notice-board 用）、`enabled: false`（下线且后端不加载）。

**环境变量**：`JZTOOLS_HOST`（默认 0.0.0.0）、`JZTOOLS_PORT`（默认 5000，解析失败回落 5000）。

## 2. 当前 git 状态

- 分支：`main`。最近提交：
  - `1d25e21` feat: 一键安装/更新/卸载机制+配置模板同步（install.ps1 / jztools_data.sync_templates / build-deploy -Version）
  - `906585c` feat: 优化战果录入大模型提示词以改善低性能模型解析
  - `3fc45c9` feat: 战果录入汇总/筛选排序与主办人匹配优化
  - 更早：战果录入导出 Excel / 主办人字段、JZIcon emoji 兼容层、数据目录可配置化、共享文档/公告板/战果录入等历史功能
- 依赖改为**按插件声明**：根 `requirements.txt` 已删除，各插件在
  `plugins/<id>/backend/requirements.txt` 声明自身依赖（缺依赖插件优雅降级并在页面提示）。
  `app.py` 保留 PyInstaller `frozen` 分支 + waitress 生产 WSGI；**打包脚本 `build-deploy.ps1`
  与打包配置 `JZToolsHub.spec` 已在仓库**（见第 5 节发布流程）。
- 仓库根目录**没有** `version.json`（由 `build-deploy.ps1 -Version` 在部署目录生成）；
  根目录也没有 `start.bat`（由打包脚本生成进部署目录）。

## 3. 内置插件一览

| 插件 | 目录 | 类型 | 依赖 | 说明 |
| --- | --- | --- | --- | --- |
| 管理后台 | `plugins/admin/` | 前后端一体（核心） | cryptography / Flask | 登录鉴权、单位/部门/人员/角色权限、工具访问拦截、Fernet 加密、会话超时 |
| 公告板 | `plugins/notice-board/` | 前后端一体 | 无第三方 | 管理员发布/修改/删除公告，树状可见范围，首页卡片动态声明（`home_card()` 钩子） |
| 共享文档 | `plugins/shared-docs/` | 前后端一体 | python-docx/openpyxl/xlrd | 多人协作编辑 Word/Excel，乐观锁版本冲突，在线用户，导入/导出 Office |
| 战果录入 | `plugins/case-report/` | 前后端一体 | requests | 收网报告→大模型五要素键值对台账（仅大模型解析），缴获物品明细结构化输出、跨记录汇总、主办大队限定一大队/二大队/三大队 |
| 人物关系立体星图 | `plugins/character-graph/` | 前后端一体 | python-docx/pypdf/requests | 上传文档→LLM 提取人物关系→3D 星点图（后台线程池 + task_id 轮询） |
| 轨迹转换 | `plugins/trajectory-convert/` | 前后端一体 | openpyxl/xlrd/qrcode/opencv/numpy/zfec | Excel 轨迹→二维码视频流 / 静态二维码 ZIP |
| QR 视频流解码 | `plugins/qr-video-decode/` | 前后端一体 | zfec | jsQR 逐帧扫码 + zfec 纠错重组 |
| 地图标点 | `plugins/map-marker/` | 纯前端 | — | 高德地图标点、二维码识别回放、移动轨迹 |
| Base64 / JSON 格式化 / 取色器 / MD5 | `plugins/{base64,json-formatter,color-picker,md5-generator}/` | 纯前端 | — | 示例/基础工具（tools.json 中 `enabled` 控制显隐） |

## 4. 最近一次功能：数据目录可配置化（`1c4ddb3`）

**目标**：系统数据（管理配置 / 日志 / 各插件运行数据）默认保存到计算机用户目录，管理员可改，
更换数据目录后自动迁移旧数据 —— 整体替换程序文件夹升级时用户数据不丢失。

### 数据根目录（`jztools_data.py`）

- 默认 `<用户主目录>\.jztoolshub\`，指针双写：主指针 `~\.jztoolshub.json`（用户目录，替换程序文件夹后仍可找到）+
  备份指针 `<程序目录>/config/data_root.json`。
- 布局：`config/`（tools.json / admin.json / .admin_key）、`logs/`、`plugins/<id>/`（data / config.json / prompt.json / .task_cache）。
- 关键函数：`get_data_root()` / `get_data_root_dir()` / `get_data_root_file()` / `migrate_legacy_app_data()`（启动迁移旧版程序目录数据）/
  `migrate_data_root()`（换目录迁移）/ `set_data_root()` / `data_usage_summary()`。
- **启动顺序（app.py `__main__`）**：`init_data_root()`（先 `migrate_legacy_app_data()` 再重算 CONFIG_PATH/LOG_DIR）→
  `setup_access_logging()` → `register_plugin_backends()`。

### 改动面

- **app.py**：`init_data_root()` 迁移旧数据 + 重算 `CONFIG_PATH/LOG_DIR/LOG_FILE`。
- **admin 插件**：`CONFIG_PATH/ADMIN_CONFIG_PATH/ADMIN_KEY_PATH` 改走 `jztools_data`；新增 `GET/POST /api/admin/data-settings`（仅超管）、
  `/admin/settings` 设置页（`admin-settings.html` + `admin-settings.js`），后台首页加「⚙️ 设置」入口。
- **全部插件后端**（shared-docs / notice-board / case-report / character-graph / trajectory-convert）：
  `data/`、`config.json`、`prompt.json`、`.task_cache` 统一改用 `jztools_data` 定位。
- **配置模板**：程序目录 `config/tools.json` 保留为模板，首启复制到数据根目录；此后读写都改数据根目录副本。

### 升级 / 换目录方法（也见 README「项目更新与数据迁移」）

1. 旧版（数据在程序目录）升级：新版首启自动把程序目录 `admin.json`/`.admin_key`/`logs/`/各插件 `backend/data`、`config.json`、`prompt.json`、`.task_cache` 迁入数据根目录（幂等，目标已存在不覆盖）。
2. 之后整体替换程序文件夹，用户数据仍在数据根目录，不丢失。
3. 管理员换目录：后台 → 系统设置 → 输入新绝对路径 →「更改并迁移」（`migrate_data_root` 按 config/logs/plugins 三个子目录整体搬移，已存在不覆盖，并更新双指针）。

> **注意**：迁移是「移动」而非「复制」；`.admin_key` 与 `admin.json` 密文必须同目录一起迁，否则解密失败。
> 前端 emoji 图标在旧浏览器（Chrome 72/78）可能显示异常，已把首页 `.tool-icon` 显式指定 emoji 字体栈缓解。

## 5. 最新功能：一键安装/更新/卸载 + 配置模板同步（本会话）

**背景**：新版 zip 替换旧文件后，部分配置未能及时更新（如大模型的 prompt）。排查发现：
新版本配置（`plugins/*/backend/prompt.json`、`config/tools.json`）存放在项目主目录，
但程序运行时实际读取的配置位于用户数据根目录（`~/.jztoolshub/plugins/<id>/prompt.json`）。
需在系统升级时把新版本模板同步到用户数据根目录。

### 新增/改动文件

| 文件 | 说明 |
| --- | --- |
| `install.ps1`（根目录，源文件） | 一键安装 / 更新 / 卸载核心 PowerShell 脚本，含 `Sync-ConfigTemplates` 配置模板同步函数 |
| `一键安装.bat` | 双击入口 → 调用 `install.ps1`（无参数 = 安装/更新） |
| `一键卸载.bat` | 双击入口 → 调用 `install.ps1 -Uninstall`（删除程序+用户数据，需确认） |
| `jztools_data.py`（改动） | 新增 `sync_templates()` 版本门控同步函数，`_TEMPLATE_SYNC` 清单定义模板映射与策略 |
| `app.py`（改动） | `init_data_root()` 末尾调用 `jztools_data.sync_templates()` |
| `build-deploy.ps1`（改动） | 新增 `-Version` 参数，打包时自动复制 `install.ps1`/`一键安装.bat`/`一键卸载.bat`/`version.json` 到部署目录，可选生成 zip |
| `README.md`（改动） | 更新「打包部署」「项目更新与数据迁移」章节，新增「配置模板自动同步」说明 |

### 关键机制

**配置模板同步**（`jztools_data.sync_templates()`）：

- 触发条件：`version.json` 中的 `app` 与数据根目录 `config/.app_state.json` 记录的
  `last_app` 不一致时执行（一次），一致时跳过（幂等）。
- 清单（`_TEMPLATE_SYNC`）：

  | 模板路径 | 策略 | 说明 |
  | --- | --- | --- |
  | `plugins/case-report/prompt.json` | overwrite | 备份 `.bak-old` 后覆盖 |
  | `plugins/character-graph/prompt.json` | overwrite | 同上 |
  | `config/tools.json` | merge-tools | 合并：保留用户启停/排序，追加新分类/新工具 |
  | `plugins/case-report/config.json` | ensure-keys | 保留用户 LLM，仅补模板新字段 |
  | `plugins/character-graph/config.json` | ensure-keys | 同上 |

- **双保险**：`install.ps1` 的 `Sync-ConfigTemplates` 函数在更新时也做等价合并；
  即使手动替换文件夹，app 启动时也会自动同步。

**一键安装/更新流程**（`install.ps1`）：

1. 停止旧 JZToolsHub 进程。
2. 检测已安装（注册表 Uninstall 键 > 默认目录 `%LOCALAPPDATA%\JZToolsHub` > 含 `config/data_root.json` 的源目录 → 就地更新）。
3. 复制程序文件（排除运行时产物）；源=目标时跳过复制。
4. 调用 `Sync-ConfigTemplates` 同步模板。
5. 写 `version.json`、注册表卸载信息、开始菜单/桌面快捷方式。

**卸载流程**（`install.ps1 -Uninstall`）：

1. 停止进程。清理快捷方式。删除程序目录。删除数据根目录（默认；`-KeepData` 保留）。
2. 删除数据目录指针（`~/.jztoolshub.json`）。清理注册表。

**版本号**：`version.json` 由 `build-deploy.ps1 -Version` 参数指定（默认读取上一版 patch+1）。

### 版本迭代发布 / 目标机更新流程（后续接手必读）

```
开发者（仓库）                                目标机
─────────────────────────────              ─────────────────────────────
改代码 / 改根目录 install.ps1、
一键安装.bat、一键卸载.bat（如需）
        │
build-deploy.ps1 -Version "x.y.z"          解压 JZToolsHub-v<x.y.z>.zip
  ├─ PyInstaller 打包 exe                          │
  ├─ 组装 deploy\JZToolsHub\                双击「一键安装.bat」
  ├─ 复制根目录安装/卸载脚本                   ├─ 停旧服务 → 定位安装目录 → 复制程序
  └─ 产出 JZToolsHub-v<x.y.z>.zip            ├─ Sync-ConfigTemplates 同步模板
        │                                    └─ 注册表 + 快捷方式 + version.json
分发 zip ──────────────────────────▶         双击「start.bat」启动（或托盘退出旧服后自动）
                                             卸载：双击「一键卸载.bat」（Y 确认，全删；
                                                   -KeepData 仅删程序留数据）
```

- **必须递增版本号**：`sync_templates()` 与 `install.ps1` 均以 `version.json` 的 `app` 与
  数据根目录 `config/.app_state.json` 的 `last_app` 比对作为同步触发条件，版本不变则跳过。
- **脚本源文件在仓库根目录**：部署包里的是副本，直接改包内脚本不会回写仓库，下次打包被覆盖。
- **新增插件带模板配置时**：在 `jztools_data.py` 的 `_TEMPLATE_SYNC` 与 `install.ps1` 的
  `Sync-ConfigTemplates` 两处同时登记（清单须一致），详见 README「配置模板自动同步」。

### 与旧版兼容

- 已有 `deploy/JZToolsHub/` 已有 `install.ps1`（旧版，无模板同步）和 `uninstall.bat`。
  新版 `build-deploy.ps1` 生成的部署目录自动覆盖为新版脚本（`一键安装.bat`、`一键卸载.bat`）。
- 旧版 `install.ps1` 注册的 UninstallString 指向 `install.ps1 -Uninstall`，更新后新版
  `install.ps1` 会替换旧版，卸载入口不变。
- `uninstall.bat` 仍保留在旧部署目录中（新包不再带它），不影响功能，可用 `一键卸载.bat` 替代。


## 6. 当前状态 / 卡点

- **无硬性阻塞**。`main` 已推送，工作区干净（仅 `.zcode/` 未跟踪，是会话工作目录，不要提交）。
- 以下为未充分验证/待办项（别当成已解决）：
  1. 访问日志字段仅经 Flask test client 验证（`user=admin`、`op=新增单位/发布公告/删除共享文档`
     等标签正确），未在真实浏览器/多用户环境走查；`get_session_user()` 每请求读 `config/admin.json`，
     静态资源已跳过（app.py 的 `get_current_user` 对 `/static/` 返回 None），但高并发 API 场景若吃紧需加缓存。
  2. 战果录入为仅大模型解析（LLM-only），缴获物品明细由大模型结构化输出 `缴获物品明细` 数组，
     真实网络环境下该格式输出已多轮调优 prompt（`906585c`），换低性能模型时仍需复测。
  3. 公告板/共享文档/战果录入等前端新功能多靠 `node --check` + test client 回归，浏览器走查较少。
  4. **一键安装/卸载脚本已在开发机通过语法检查、Python 侧 sync_templates 隔离测试与 exe 冒烟测试，
     但尚未在目标机做完整「全新安装 → 更新 → 卸载」三段式实测**；首次正式发版建议在测试机走一遍。
  5. **打包用 Python 3.14（PATH 默认）时产物不支持 Win7**；需兼容 Win7 的部署必须用
     Python 3.8 打包（`-Python "C:\Users\yfjz\AppData\Local\Programs\Python\Python38\python.exe"`，
     3.8 环境需先补装 `pystray` + `pillow`）。
  6. 各插件前端 `?v=` 版本号起点/步长不统一（case-report v28、character-graph 用日期戳、
     trajectory-convert 无版本号因只有内联脚本的单文件）；给无版本号的插件加外部 JS/CSS 时
     必须从一开始就带 `?v=1` 并遵守递增纪律。

## 7. 踩过的坑 —— 绝对不要踩

1. **绝不要清理 `plugins/*/backend/data/` 目录**。曾两次在测试脚本里误删用户真实台账记录
   （git 不含插件 data）。测试清理只删自己刚建的文件，或改用独立临时目录；动手前先 `git status`/列目录。
2. **`config/admin.json` 含明文密码哈希、Fernet 加密字段与真实 LLM API Key 密文**，`.gitignore` 已排除。
   任何 `git add` 前确认不会带上它；密钥 `config/.admin_key` 与密文分离存储，两者均勿提交。
   各插件的 `backend/config.json`（如战果录入/星图含明文 API Key）同样 gitignore。
3. **前端资源改了必须递增版本号**（`index.html` 里 `style.css?v=N`、`app.js?v=N`），
   否则 `/plugin/` 静态资源缓存一整天的策略让用户看不到改动。改完检查资源名带不带新版本号。
4. **访问日志禁止记请求体**（含密码/正文），只记 path/status/字段；插件**禁止**自行落盘访问日志
   （规范 B-8），要更具体的操作描述用 `set_operation()`。
5. **同目录扫描 `.json` 要过滤非记录文件**（如战果录入的 `item_categories.json` 与记录同目录，
   用 `_is_record()` 过滤，否则把类别库当"记录"导致 undefined/删除 404）。
6. **中文输出乱码大多是 PowerShell 管道显示问题**，数据本身是 UTF-8 正常值；断言写在 Python 代码里
   （`assert ... , dict`），别靠肉眼读控制台。
7. **LLM 路径解析在 HTTP 轮询下要数秒**（连接/超时兜底），前端轮询上限 90s；测试轮询别只等 5 秒就断言。
8. **bash/read 工具偶发不稳定**（返回空/ChildProcess.kill），write/edit 更可靠；抽风就重试或改小读取块，
   同一批并发工具调用不要依赖彼此结果。
9. **安装/卸载脚本编码**：`install.ps1` 必须 UTF-8 with BOM（PS 5.1 无 BOM 按 ANSI 解析，中文乱码）；
   `一键安装.bat`/`一键卸载.bat` 必须 GBK/ANSI 且**不要**加 `chcp 65001`（cmd 按系统代码页逐行解析，
   UTF-8 中文会乱码）；脚本写出的 JSON 一律 UTF-8 无 BOM（`json.load` 遇 BOM 报错）。
   **此外两个 .bat 必须 CRLF 换行**：曾因编辑器把 bat 存成 LF（Unix 换行）导致 cmd 把相邻行拼接解析，
   双击报 `'RC"=="0" ('`、`'安装' 不是内部或外部命令` 等碎片错误（2026-09-04 已修复根目录与
   deploy 副本）。用 Git 时建议在 `.gitattributes` 对 `*.bat` 强制 `eol=crlf`，或改完 bat 后
   用 PowerShell 检查字节里有无裸 `\n`。
   **install.ps1 的 `$InstallDir` 不能在 param 里设默认值 `%LOCALAPPDATA%\...` 后留空串跑**：
   曾因 param 默认 `""` 而注册表无记录时 `Join-Path $InstallDir $ExeName` 报
   「无法将参数绑定到参数 Path，因为该参数为空字符串」（2026-09-04 已修复：脚本头部对空值
   兜底 `Join-Path $env:LOCALAPPDATA $AppName`）。改 install.ps1 后必须在无注册表记录的
   机器/账户上跑一遍全新安装路径验证。
   **一键安装脚本只能在「含 JZToolsHub.exe 的解压目录」里跑**：在源码仓库根目录跑时，仓库里
   开发运行产生的 `config\data_root.json` 备份指针会让脚本误判「既有安装目录→就地更新」，
   最后因缺 exe 报「安装目录缺少 JZToolsHub.exe」（2026-09-04 已加源目录守卫：安装入口先
   检查 `$Source` 下有无 exe，没有则明确提示解压 zip 后再执行并 exit 1）。另外仓库根目录
   没有 `version.json`，此时安装横幅会显示版本 0.0.0——这本身就是「不在安装包内」的信号。
10. **测试 sync_templates / install.ps1 时务必隔离数据根目录**：本机 `~/.jztoolshub` 是真实用户数据，
    直接跑会改写 `config/.app_state.json`（曾发生，已恢复）。Python 侧测试用临时目录 +
    monkeypatch `get_base_dir/get_data_root`；不要在未隔离状态下对真实数据根目录做版本变更测试。
11. **PowerShell 里跑含引号/花括号的 `python -c "..."` 内联脚本极易翻车**（引号被 PS 重新解释）；
    复杂断言先写到临时 .py 文件再执行（README/HANDOFF 文档自身的校验脚本也建议这么干）。

## 7.5 插件后端速查表（改哪个插件先看这里）

所有后端插件共用同一套模式：`register(app)` 注册路由；会话取
`from jztools_admin.routes import get_session_user`；日志标记 `set_operation("…")`；
数据统一存数据根目录 `plugins/<id>/`（经 `jztools_data.get_data_root_dir/get_data_root_file`）。

| 插件 | 前缀 | 长任务机制 | 数据落盘（数据根目录下） |
| --- | --- | --- | --- |
| admin | /api/admin、/login 等 | 无 | config/admin.json + .admin_key |
| notice-board | /api/notice-board | 无 | plugins/notice-board/data/*.json（一公告一文件，RLock 串行化） |
| shared-docs | /api/shared-docs | 无（全局 RLock） | plugins/shared-docs/data/*.json（一文档一文件，历史上限 100） |
| case-report | /api/case-report | ThreadPoolExecutor(2)，TASK_TTL 30min | data/*.json（一记录一文件）+ item_categories.json + config.json + prompt.json |
| character-graph | /api/character-graph | ThreadPoolExecutor(2)，TASK_TTL 30min | config.json（LLM）+ prompt.json |
| trajectory-convert | /api/trajectory-convert | ThreadPoolExecutor(2)，产物按 mtime TTL 30min 清理 | .task_cache/（mp4/png/zip）+ backend/config.json（列名） |
| qr-video-decode | /api/qr-video-decode | ThreadPoolExecutor(2)，结果仅存内存 | 无落盘（data_b64 存任务表） |

**异步任务三件套**（新插件抄这里）：`POST /api/<id>/<action>` 立即返回 `task_id` →
后台线程池执行 → `GET /api/<id>/result/<task_id>` 轮询 `{status: pending|running|done|error}`。
任务表加锁、结果 TTL 30 分钟清理；任务归属校验（创建者/超管可见）。

## 8. 关键信息速查

- 启动：`python app.py`（默认 `0.0.0.0:5000`，`JZTOOLS_PORT`/`JZTOOLS_HOST` 可覆盖）。
- **打包发布**：`powershell -ExecutionPolicy Bypass -File build-deploy.ps1 -Version "x.y.z"`
  （产物 `deploy\JZToolsHub\` + `deploy\JZToolsHub-v<x.y.z>.zip`；`-Python` 可换解释器；
  安装/卸载脚本源文件在仓库根目录，打包时自动复制进部署包，**改脚本只改根目录再重新打包**）。
- **目标机安装 / 更新 / 卸载**：解压 zip → 双击 `一键安装.bat`（装/更一体）→ `start.bat` 启动；
  卸载双击 `一键卸载.bat`。发版必须递增 `-Version`，否则目标机模板同步不触发。
- 默认管理员：`admin` / `admin123`（首启自动生成于数据根目录 `config/admin.json`，登录后请尽快改密）。
- 管理后台：`/admin`；会话超时 `session.idle_minutes`（默认 30 分钟）/ `session.absolute_hours`（默认 12 小时）。
- 访问日志：数据根目录 `logs/access.log`（TAB 分隔，异步落盘，`site.logging` 开关）。
- 快速自测（不进浏览器）：
  ```python
  import sys; sys.path.insert(0, r"D:\JZToolsHub")
  import app as m
  m.setup_access_logging(m.app)
  m.register_plugin_backends(m.app)
  c = m.app.test_client()
  c.post("/api/login", json={"username": "admin", "password": "admin123"})
  ```
- 插件后端写法：`plugins/<id>/backend/routes.py` 导出 `register(app)`；会话取
  `from jztools_admin.routes import get_session_user`；日志操作标签取
  `from jztools_admin.routes import set_operation`（均带 try/except 兜底）。
- **改完功能必同步的文档**：`README.md`（HTTP API 表、内置插件一览、目录结构、故障排查——
  接口有增删时至少同步 API 表）、本文件第 2 节（git 最近提交）、必要时 `插件设计规范.md`。
- 关键文档：`README.md`（架构/API 表/日志/**代码阅读地图**/打包发布流程/模板同步机制）、
  `插件设计规范.md`（B-1~B-21、SEC-1~11，含封装型 B 型插件全套规范）、
  `docs/`（登录改造与数据隔离 / 容器化与插件热插拔 两份历史设计文档）。
