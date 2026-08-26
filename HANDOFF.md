# HANDOFF.md — JZToolsHub 交接文档

> 写给一个没有上下文的会话：请先完整读完本文，再动手。
> 最后更新：2026-08-26（`main` 已与 `origin/main` 同步，最新提交 `ea320f3`）

---

## 1. 这个项目是什么

`D:\JZToolsHub` 是一个 **Flask + 配置驱动的插件化工具箱**（"一切皆插件"）：

- 主应用 `app.py` 启动时扫描 `plugins/<插件id>/`，注册插件后端路由到同一 Flask 应用。
- 插件前端放在 `plugins/<id>/frontend/`，通过 `/plugin/<id>/<path>` 静态访问。
- 工具清单由 `config/tools.json` 声明（name/description/category/order/enabled/hidden）。
- 管理后台（`admin`）是**核心基础设施插件**：登录鉴权、会话超时、单位/部门/人员/权限管理、
  工具访问控制、Fernet 加密存储，始终加载、不随 enabled 启停。
- `README.md` 有完整架构说明与 HTTP API 表，改完功能记得同步它；`插件设计规范.md` 是插件开发铁律。

## 2. 当前 git 状态

- 分支：`main`，**已与 `origin/main` 同步**（用户已授权推送）。
- 最近提交：
  - `ea320f3` feat: 访问日志记录用户名与具体操作（`ip/user/method/func/op/path/status/cost_ms/ua`）
  - `669bcf5` chore: 移除打包部署相关文件（`JZToolsHub.spec` / `build-deploy.ps1` / 根 `requirements.txt`）
  - `3c1818a` ~ `c891d0e`：公告板动态声明/编辑、首页卡片两级读取、插件设计规范等历史功能
- 依赖改为**按插件声明**：根 `requirements.txt` 已删除，各插件在
  `plugins/<id>/backend/requirements.txt` 声明自身依赖（缺依赖插件优雅降级并在页面提示）。
  `app.py` 仍保留 PyInstaller `frozen` 分支 + waitress 生产 WSGI，但构建脚本/打包配置已不在仓库。

## 3. 内置插件一览

| 插件 | 目录 | 类型 | 依赖 | 说明 |
| --- | --- | --- | --- | --- |
| 管理后台 | `plugins/admin/` | 前后端一体（核心） | cryptography / Flask | 登录鉴权、单位/部门/人员/角色权限、工具访问拦截、Fernet 加密、会话超时 |
| 公告板 | `plugins/notice-board/` | 前后端一体 | 无第三方 | 管理员发布/修改/删除公告，树状可见范围，首页卡片动态声明（`home_card()` 钩子） |
| 共享文档 | `plugins/shared-docs/` | 前后端一体 | python-docx/openpyxl/xlrd | 多人协作编辑 Word/Excel，乐观锁版本冲突，在线用户，导入/导出 Office |
| 战果录入 | `plugins/case-report/` | 前后端一体 | requests | 收网报告→五要素键值对台账，缴获物品拆分归类、跨记录汇总、LLM/规则双解析 |
| 人物关系立体星图 | `plugins/character-graph/` | 前后端一体 | python-docx/pypdf/requests | 上传文档→LLM 提取人物关系→3D 星点图（后台线程池 + task_id 轮询） |
| 轨迹转换 | `plugins/trajectory-convert/` | 前后端一体 | openpyxl/xlrd/qrcode/opencv/numpy/zfec | Excel 轨迹→二维码视频流 / 静态二维码 ZIP |
| QR 视频流解码 | `plugins/qr-video-decode/` | 前后端一体 | zfec | jsQR 逐帧扫码 + zfec 纠错重组 |
| 地图标点 | `plugins/map-marker/` | 纯前端 | — | 高德地图标点、二维码识别回放、移动轨迹 |
| Base64 / JSON 格式化 / 取色器 / MD5 | `plugins/{base64,json-formatter,color-picker,md5-generator}/` | 纯前端 | — | 示例/基础工具（tools.json 中 `enabled` 控制显隐） |

## 4. 最近一次功能：访问日志增强（`ea320f3`）

在既有异步访问日志（`logs/access.log`，按天滚动保留 30 天，`config/tools.json` → `site.logging` 开关）
基础上新增两个字段，全部请求统一记录：

```
时间戳  级别  ip=客户端IP  user=登录用户名  method=方法  func=功能标签  op=具体操作  path=路径  status=状态码  cost_ms=耗时  ua=浏览器标识
```

- `user`：`app.py::get_current_user()` 经 admin 插件的 `get_session_user()` 解析当前登录用户名，
  未登录为 `-`；`/static/` 静态资源请求跳过解析省 I/O。
- `op`：`app.py::parse_operation()` 解析——优先取 `g._current_operation`（插件在请求内显式标记），
  其次核心路由映射表 `_OPERATION_MAP`，最后路径/方法兜底。
- 插件标记方式：`from jztools_admin.routes import set_operation`（导入失败兜底空操作），处理器内调用
  `set_operation("发布公告")`。已打标：admin 全部 CRUD（登录/改密/单位/部门/人员/角色）、
  公告板发布/修改/删除、共享文档增删改挂靠/导入导出、战果录入保存/删除/改案件名/学习类别。

## 5. 当前状态 / 卡点

- **无硬性阻塞**。`main` 已推送，工作区干净。
- 以下为未充分验证/待办项（别当成已解决）：
  1. 访问日志新增字段仅经 Flask test client 验证（`user=admin`、`op=新增单位/发布公告/删除共享文档`
     等标签正确），未在真实浏览器/多用户环境走查；`get_session_user()` 每请求读 `config/admin.json`，
     静态资源已跳过，但高并发 API 场景若吃紧需加缓存。
  2. 战果录入 LLM 路径的「物品分类」真实模型输出格式未在真实网络环境严格验证。
  3. 公告板/共享文档/战果录入等前端新功能多靠 `node --check` + test client 回归，浏览器走查较少。
  4. 若后续重新做打包部署，需自建 `build-deploy.ps1` / `JZToolsHub.spec`（已从仓库移除）。

## 6. 踩过的坑 —— 绝对不要踩

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

## 7. 关键信息速查

- 启动：`python app.py`（默认 `0.0.0.0:5000`，`JZTOOLS_PORT`/`JZTOOLS_HOST` 可覆盖）。
- 默认管理员：`admin` / `admin123`（首启自动生成于 `config/admin.json`，登录后请尽快改密）。
- 管理后台：`/admin`；会话超时 `session.idle_minutes`（默认 30 分钟）/ `session.absolute_hours`（默认 12 小时）。
- 访问日志：`logs/access.log`（TAB 分隔，异步落盘，`site.logging` 开关）。
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
- 关键文档：`README.md`（架构/API 表/日志）、`插件设计规范.md`（B-1~B-8、SEC-1~6）、
  `docs/`（登录改造与数据隔离 / 容器化与插件热插拔 两份设计文档）。
