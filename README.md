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
12. [故障排查](#故障排查)
13. [后期接入真实后端](#后期接入真实后端)

---

## 快速开始

```bash
pip install -r requirements.txt
python app.py
```

浏览器访问 <http://localhost:5000> 即可看到首页。

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
| 依赖 | 见 `requirements.txt`（Flask / requests / python-docx / pypdf） |

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
│   └── tools.json            # ★ 后台配置：站点信息 + 分类 + 工具注册清单 + 日志开关
├── plugins/                  # ★ 插件目录（一切皆插件：前端 + 后端自包含）
│   ├── base64/               #   示例插件：Base64 编解码（纯前端）
│   │   ├── manifest.json     #   插件清单（元信息）
│   │   └── frontend/         #   前端资源（iframe 静态服务）
│   │       └── index.html
│   ├── json-formatter/       #   示例插件：JSON 格式化（纯前端）
│   ├── color-picker/         #   示例插件：取色器（纯前端）
│   ├── md5-generator/        #   示例插件：MD5 生成器（纯前端）
│   ├── map-marker/           #   示例插件：地图标点（高德地图，纯前端）
│   └── character-graph/      #   示例插件：人物关系立体星图（前后端一体）
│       ├── manifest.json
│       ├── frontend/         #   前端入口 + Three.js 资源
│       └── backend/          #   Python 后端（routes.py 等）
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

- **插件即目录**：一个插件 = `plugins/<插件id>/` 目录，内部包含 `manifest.json`（元信息）、`frontend/`（前端资源）、以及可选的 `backend/`（Python 后端）。
- **配置即启停**：`config/tools.json` 决定哪些插件展示、展示顺序与所属分类；`enabled: false` 即可后台下线，无需删代码。
- **前端隔离**：工具页面通过 iframe 嵌入外壳页（`static/tool.html`），插件之间、插件与框架之间互不污染。
- **后端自发现**：只要插件目录下存在 `backend/__init__.py` 与含 `register(app)` 的 `routes.py`，框架启动时自动导入注册，无需在配置中声明。

可视化关系：

```
config/tools.json ──注册──▶ plugins/<id>/manifest.json（改名字/图标/能力）
        │                        │
        │  enabled            frontend/（iframe 加载）
        ▼                        ▼
   首页卡片 ◀── /api/tools ── 外壳页 /tool/<id>
                                │
                     backend/（启动时自动注册 Flask 路由）
```

---

## 交互流程说明

1. 用户访问首页 `/`：`static/js/main.js` 请求 `/api/tools`，按分类渲染工具卡片。
2. 点击卡片进入外壳页 `/tool/<id>`：`static/js/tool.js` 请求 `/api/tools/<id>` 获取工具元信息，并将 iframe 指向 `/plugin/<id>/<entry>`。
3. `/plugin/<id>/<path>` 路由将 `plugins/<id>/frontend/` 下的文件作为静态资源返回。
4. 若插件含后端，后端路由已在启动时注册到同一 Flask 应用（如 `/api/character-graph/...`）。

---

## HTTP API

| 接口 | 说明 |
| --- | --- |
| `GET /` | 首页 |
| `GET /tool/<id>` | 工具外壳页 |
| `GET /plugin/<id>/<path>` | 插件前端静态资源（映射到 `plugins/<id>/frontend/`），目录穿越已拦截 |
| `GET /api/tools` | 聚合后的工具列表（站点信息 + 分类 + 工具清单） |
| `GET /api/tools/<id>` | 单个工具信息，不存在返回 404 |

**character-graph 插件后端接口**（演示含后端插件的 API 模式）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/character-graph/config` | 读取大模型配置（base_url / api_key / model / api_source） |
| `POST /api/character-graph/config` | 保存大模型配置 |
| `GET /api/character-graph/prompt` | 读取抽取 Prompt 模板 |
| `POST /api/character-graph/analyze` | 提交文档分析任务（multipart），**立即返回 `task_id`**，后台异步执行 |
| `GET /api/character-graph/result/<task_id>` | 轮询分析任务状态与结果 |

> 各插件后端路由统一挂在 `/api/<插件id>/` 前缀下，动态注册，互不冲突。

`GET /api/tools` 返回结构示例：

```json
{
  "site": { "title": "JZ 工具箱", "subtitle": "...", "footer": "..." },
  "categories": [{ "id": "dev", "name": "开发者工具" }],
  "tools": [
    {
      "id": "base64",
      "name": "Base64 编解码",
      "description": "...",
      "icon": "🔐",
      "accent": "#4285F4",
      "entry": "index.html",
      "features": ["编码", "解码", "UTF-8"],
      "category": "开发者工具",
      "category_id": "dev",
      "order": 2
    }
  ]
}
```

---

## 访问日志

系统自动记录所有 HTTP 请求，重点记录**使用者 IP 与所访问的功能**，用于使用统计与安全审计。

- **日志位置**：`logs/access.log`（首次请求时自动创建 `logs/` 目录）。
- **滚动策略**：按天滚动（`TimedRotatingFileHandler`），保留最近 30 天，过期自动清理。
- **启用开关**：`config/tools.json` → `site.logging`，默认 `true`；设为 `false` 即停止写日志。

### 日志格式（TAB 分隔，每行一条）

```
时间戳		级别		ip=客户端IP	method=方法	func=功能标签	path=路径	status=状态码	cost_ms=耗时	ua=浏览器标识
```

示例（真实输出）：

```
2026-08-15 14:04:22,882	INFO	ip=127.0.0.1	method=GET	func=API: 工具列表	path=/api/tools	status=200	cost_ms=5	ua=Mozilla/5.0 ...
2026-08-15 14:04:22,882	INFO	ip=127.0.0.1	method=GET	func=工具壳页(地图标点)	path=/tool/map-marker	status=200	cost_ms=1	...
2026-08-15 14:04:22,882	INFO	ip=127.0.0.1	method=GET	func=插件(人物关系立体星图)/frontend/index.html	path=/plugin/character-graph/index.html	status=200	...
2026-08-15 14:04:22,882	INFO	ip=127.0.0.1	method=GET	func=后端接口: /api/character-graph/config	path=/api/character-graph/config	status=200	...
```

### 字段说明

| 字段 | 说明 |
| --- | --- |
| `ip` | 客户端真实 IP；兼容反向代理，优先取 `X-Forwarded-For` 首段 |
| `method` | HTTP 方法（GET / POST / …） |
| `func` | 功能标签，自动解析：`首页` / `API: 工具列表` / `API: 工具详情(工具名)` / `工具壳页(工具名)` / `插件(工具名)/frontend/...` / `后端接口: /api/...` / `静态资源: ...` |
| `path` | 原始请求路径 |
| `status` | HTTP 状态码 |
| `cost_ms` | 请求处理耗时（毫秒） |
| `ua` | 客户端 User-Agent（截断 120 字符） |

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
  "name": "我的工具",
  "description": "一句话描述这个工具能做什么",
  "icon": "🧰",
  "accent": "#4285F4",
  "entry": "index.html",
  "features": ["标签1", "标签2"]
}
```

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

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `id` | ✅ | - | 工具唯一标识，**必须与目录名一致**，仅允许 `A-Z a-z 0-9 _ -` |
| `name` | ✅ | 同 `id` | 展示名称 |
| `description` | - | 空 | 卡片上的描述文字 |
| `icon` | - | `🧩` | 卡片图标（Emoji） |
| `accent` | - | `#4285F4` | 卡片主题色（Hex） |
| `entry` | - | `index.html` | 前端入口页面，位于 `frontend/` 下 |
| `features` | - | `[]` | 卡片上的能力标签（字符串数组） |

### 7.4 注册插件到首页

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
| `category` | ✅ | 所属分类 ID（对应 `categories` 中的 `id`） |
| `enabled` | - | 是否启用，`false` 时首页隐藏且后端不加载 |
| `order` | - | 展示顺序（同分类内按此排序） |

保存后刷新首页即生效，**无需重启服务、无需改框架代码**。

### 7.5 常见约定与注意事项

1. **ID 规范**：`manifest.json` 的 `id`、目录名、`config/tools.json` 中的 `id` 三者必须一致。
2. **资源隔离**：所有插件文件必须放在 `plugins/<id>/` 下，前端放 `frontend/`，后端放 `backend/`。主框架 `static/` 目录属于框架自身，不要混入插件资源。
3. **目录穿越防护**：插件 ID 只允许 `[A-Za-z0-9_-]`，`/plugin/<id>/<path>` 由 `send_from_directory` 安全控制，不要用 `..`、`/` 等字符作 ID 或路径。
4. **相对路径引用**：前端内部引用其他资源一律用相对路径（`./app.js`、`css/style.css`），不要硬编码 `/static/...` 之外的绝对路径。
5. **后端前缀唯一**：多个插件可能同时注册，后端路由建议统一使用 `/api/<插件id>/` 前缀。
6. **敏感配置**：插件内的高德 Key、大模型 Key 等前端依赖配置建议由插件页面自行管理（如保存到 localStorage），不要写死在仓库文件中。
7. **自包含交付**：一个插件目录可以从项目复制到另一个 JZToolsHub 项目，配合一段 `tools.json` 配置即可完整迁移。

---

## 内置插件一览

| 插件 | 分类 | 前端 | 后端 | 说明 |
| --- | --- | --- | --- | --- |
| json-formatter | dev | ✅ | - | JSON 格式化 / 压缩 |
| base64 | dev | ✅ | - | Base64 编解码（UTF-8 中文） |
| color-picker | design | ✅ | - | 取色器 |
| md5-generator | dev | ✅ | - | MD5 摘要生成 |
| map-marker | maps | ✅ | - | 高德地图经纬度标点（需自备高德 Key） |
| character-graph | ai | ✅ | ✅ | 上传文档 → 大模型提取人物关系 → 3D 星图展示 |

---

## 故障排查

| 现象 | 排查建议 |
| --- | --- |
| 首页看不到某个工具 | 检查 `config/tools.json` 中 `enabled` 是否为 `true`、`id` 是否一致 |
| 工具卡片点击后显示"无法加载工具" | 检查 `manifest.json` 的 `entry` 对应文件是否存在于 `frontend/` |
| `/api/tools` 返回但页面空白 | 刷新浏览器缓存；确认 `static/js/main.js` 正常加载 |
| 后端接口 404 | 确认插件目录下有 `backend/__init__.py` 与 `backend/routes.py`，且 `register(app)` 已导出 |
| 端口被占用 | 修改 `app.py` 末尾 `port=5000`，或先停止旧进程再启动 |

---

## 后期接入真实后端

现阶段 `config/tools.json` 为本地 Mock。接入真实后台时，将 `app.py` 中 `load_registry()` 替换为数据库或远程配置接口即可 —— 前端渲染、插件加载机制、外壳页与 API 结构均无需任何改动。这保证了"配置驱动"的机制可以平滑过渡到真实的运营后台。

---

*JZToolsHub · 一切皆插件 · 配置驱动的工具集合*