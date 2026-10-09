# 插件商店（store）

JZToolsHub 的**第二核心插件**：对客户端提供商店目录（读面）、包文件分发与下载统计。
随主包分发（manifest `core: true`，管理后台拒绝卸载），**可单独升级**（走既有插件包链，
与 admin 同款）；不依赖 admin 即可浏览——admin 只负责写面（上架/审核/发布/下架）。

本文件是该插件接口的**唯一真源**（规范 S-5）。

## 功能

- **目录浏览**：读 `catalog.json` 做 flat 投影（每个 id 只出「最新已上架版本」），
  字段与共享盘 `index.json` 同名同义 + 扩展（`icon`/`deps_mode`/`provides`/`frontend_*`，
  客户端契约同构）；`?all=1` 附版本历史（详情页用）。
- **包分发**：按 `id` + `version` 流式回包（不全量缓冲），仅**已上架（published）**版本可下载；
  server 条目为双包（需求⑧），可用 `?artifact=frontend` 取前端包工件。
- **下载统计**：每次成功下载追加一条事件到 `stats.jsonl`（拍板口径：**仅记下载事件**，
  「下载 ≠ 安装」；不做 `/track` 回执）。
- **桌面端生命周期入口**：商店页在桌面端（`html.jz-shell`）提供「安装/升级/停用/启用/卸载」
  按钮，操作全部走客户端**本地** `/local-api/*`（同源，永不中继）；浏览器路径只有浏览与下载。
  这些本地端点由客户端能力层提供，**不在本插件**。

## 数据契约（本插件只读，唯一写点 = stats.jsonl）

| 路径 | 谁写 | 本插件 |
| --- | --- | --- |
| `数据根\store\catalog.json`（schema 2，含 `versions[]`/`status`） | admin 商店管理面 | 只读（防御性校验：结构不符/坏条目一律跳过） |
| `数据根\store\packages\<规范文件名>` | admin 商店管理面（上架即原包，不改内容） | 只读 + 分发 |
| `数据根\store\stats.jsonl` | **本插件**（追加式下载事件） | 读 + 写 |

对 `catalog.json` / `packages\` **无任何写代码**（C6 验收口径）；上架/部署动作全部在 admin 侧。

## 对外接口

> 前缀 `API_PREFIX = /plugin/store/api`（核心插件全局面，直接挂 `/plugin/<id>/api/*`；
> 设计文档《桌面端插件商店-设计文档.md》§4.2）。全部端点**仅要求已登录会话**
> （`jz_api.get_session_user`），不区分管理员/办事员；未登录一律 `401 {"error": "未登录或登录已过期"}`。

### GET `/plugin/store/api/catalog`

查询参数：`all=1`（或 `true`）附版本历史。

成功 `200`：

```json
{"ok": true, "schema": 2, "generated_at": "2026-10-05T12:00:00",
 "packages": [{
   "id": "...", "name": "...", "description": "...", "icon": "🧩",
   "runtime": "local|server|dep-slot", "deps_mode": "shared|venv",
   "requires": {}, "version": "1.2.3", "file": "...zip", "sha256": "...",
   "size": 12345, "api_version": 1, "min_app_version": "",
   "requires_restart": false, "changelog": "...", "published_at": "..."
 }]}
```

- 每条目只出**最新 published 版本**；条目无 published 版本则整体隐藏（可见性规则）。
- `server` 条目附 `frontend_file` / `frontend_sha256` / `frontend_size`（双包模型）；
  `dep-slot` 条目附 `provides`（客户端预检把缺依赖映射到可装依赖包）。
- `?all=1` 时每条目附 `versions[]`：published 项含 `changelog`/`published_at`/`status`，
  其余状态只回 `version`/`status`。
- catalog 缺失 / 损坏 / schema 不符 → 返回**空目录**（`generated_at` 为空），不报错
  （防御式，坏数据不炸页面）。

### GET `/plugin/store/api/download/<pid>/<version>`

查询参数：`artifact=backend`（缺省）| `frontend`；`via=client|browser`（其他值归一为 `browser`）。

- `pid` 须 `[A-Za-z0-9_-]+`、`version` 须数字点分，否则 `400 {"error": "id 或版本号非法"}`。
- 仅 **published** 版本可下载，否则 `404 {"error": "条目不存在或当前版本未上架"}`；
  `artifact=frontend` 但该版本无前端包工件 → `404`；包文件缺失 → `404`（提示重新上架）。
- 成功：`Content-Disposition: attachment` 流式回包（`send_file`，文件名为 `basename`）。
- 副作用：追加一条 `stats.jsonl` 事件
  `{ts, user, id, version, via: client|browser}`（统计失败不影响下载）。

### GET `/plugin/store/api/stats`

成功 `200`：`{"ok": true, "rows": [<stats.jsonl 原始事件行…>]}`。
只回原始行，聚合展示由调用方完成（admin 商店管理页统计页签走
`GET /plugin/admin/store/stats` 读同一文件，admin 侧只读）。

## 下载 / 安装 / 卸载生命周期口径

（客户端工件落位与按钮语义见《桌面端插件商店-设计文档.md》§3.5 / §3.5.1；
2026-10-08 权限与双包修订）

| 动作 | 入口 | 口径 |
| --- | --- | --- |
| 下载 | 商店页「下载」 | 浏览器 = 普通文件下载（server 条目取前端包工件）；桌面端 = 落客户端数据目录 `downloads\`（仅留档/转移）。**安装不经过此按钮**（安装内含拉包） |
| 安装 | 桌面端「安装」 | `POST /local-api/store/install`（能力层以用户会话从本插件拉包 → sha256 对 catalog → `pkg_install` → 落位 → rescan）。`local` → `local-plugins\<id>`；`dep-slot` → `runtime\pylibs`；`server` = **仅装前端包**到 `server-frontends\<id>`（后端包发布即部署在服务器，不对用户暴露） |
| 升级 | 桌面端「升级到」 | 同安装；catalog 版本高于已装版本时按钮亮起。worker 即时；hybrid 走秒级重启口径；server 条目前后端包**同版本成对**发布 |
| 停用 / 启用 | 桌面端 | `POST /local-api/plugins/state`（既有接口复用）：只置禁用态，**包与数据原样保留**，随时启用恢复 |
| 卸载 | 桌面端 | `POST /local-api/plugins/uninstall`（既有接口复用）：删本机包目录，数据默认保留。server 条目卸载 = 删本机前端包副本 + 本机主界面移除显示；**服务器端后端包不卸载**（服务器侧撤包只在 admin 商店管理面：管理员停用/卸载） |

未安装 / 停用 / 卸载 server 条目时，页面自动回退**中继直出**（零安装即用，浏览器路径不受影响）。
已装前端副本可能落后于服务器后端（版本漂移）——缓解：同版本号成对发布 + 商店升级提示 +
`api_version` 门控 + 中继直出兜底。

## 依赖

- `flask >= 3.0`（`required: true`）：随主包分发的框架库。运行时另用框架模块
  `jz_api`（会话）、`jztools_data`（数据根定位）；无其他第三方依赖。
- 缺失 flask 时插件商店不可用（见 `manifest.json` 的 `requires[].hint`），需重装主包。

## 已知限制

- **只读目录面**：本插件不做任何上架/审核/发布/下架/服务器侧停用-卸载；这些是 admin
  商店管理面的职责。本插件唯一的写点 `stats.jsonl` 也是「坏写不抛出」（统计失败不阻断下载）。
- 统计口径仅下载事件：下载数不等于安装数；无后端回执，无法从本插件推断安装结果。
- 下架（delisted）版本不可下载（404）；已装客户端的升级提示依赖 catalog 的 published 投影。
- catalog 损坏时表现为「空商店」（防御式读），需管理员检查数据根 `store\catalog.json`；
  `stats.jsonl` 坏行按跳过处理（不修复、不报错）。
- 安装/升级/停用/启用/卸载按钮仅在桌面端壳内出现（`jz-shell` 门控）；浏览器路径可浏览+下载，
  但不可安装（设计红线 2 的既有口径）。
- `stats` 端点返回原始事件行，无聚合/分页（数据量按内网下载频次，当前规模可接受）。
