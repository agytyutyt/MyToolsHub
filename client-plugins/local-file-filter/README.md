# 过滤器（本地）—— local-file-filter

file-filter 的**本地化形态**（C 类客户端工件，`kind=hybrid`）：过滤引擎跑在客户端进程里，
**表格数据不出机**；保留字段名单/后处理规则/自学习映射库的**权威仍在服务器**，大模型推理
**在服务器工件端点完成**（Key 不落本机）。

> 功能语义真源：`plugins/file-filter/README.md`（页面流程、过滤判定口径、映射治理规则）。
> 本文只登记**本地工件与服务器版的差异面**：形态、挂载、边界、依赖、出包。

## 形态与挂载

| 项 | 值 |
| --- | --- |
| 插件 id / 版本 | `local-file-filter`（版本号跟随服务器版 `file-filter`，当前 1.6.1） |
| runtime / kind | `local` / `hybrid`（`server_data: true`：需服务器中继） |
| 包根 | 即插件目录（`manifest.json` + `frontend/` + `backend/` + `README.md`） |
| 挂载点 | `manifest.entry.mount` = `/api/local/local-file-filter` |
| 内置后端路由前缀 | 仍是 `/api/file-filter`（源码里的 `API_PREFIX` 不变） |
| 浏览器可见全路径 | `/api/local/local-file-filter/api/file-filter/<端点>` |

前端复用 `plugins/file-filter/frontend`（出包时 `-FrontendFrom` 指定），构建期把代码字面量
`api/file-filter` 改写为 `api/local/local-file-filter/api/file-filter`（规范 §16.10 的 L-13 执行点；
C 类工件**必须**带 `--api-rewrite`，否则漏改写会继续打中继而静默失去本地化）。

## 边界（什么在本机、什么在服务器）

- **本机**：读表、背景图片摘除、硬过滤、后处理、输出生成、任务表与上传暂存
  （`<客户端数据根>/plugins/file-filter/.task_cache`，TTL 30 分钟，非创建者且非超管一律 404）。
- **服务器（经 `jz_relay.own("local-file-filter")` 垫片）**：
  - 配置：`load_config` 取 `/api/file-filter/config`，`save_config` 透传 POST（管理员权限由服务器按会话判定）；
    **服务器不可达 → 空配置**（精确匹配/正则后处理按本地默认照常，名单类判定不可用）。
  - 映射库：三档查表（confirmed → exact → suggested）走 `POST /api/file-filter/mappings/lookup`，
    键归一化与歧义消解在服务器；**写**（建议落库/确认/否决/投票/复核）全留服务器，单一写方纪律不变。
    配对契约：本地 `record_suggestions`/`bump_stats` 为 no-op（服务器 `/llm/match-columns` 已代为计数），
    `is_rejected` 恒 False（服务器返回前已按否决表预过滤）。
  - 大模型：推理在服务器工件端点 `POST /api/file-filter/llm/match-columns`（jz_llm + Key 不出服务器，红线 5 / L-10）；
    本机 `llm_gateway` 只回答「能不能用」（60 秒缓存，离线按不可用降级）。
  - `mappings/*` 管理端点由 `_passthrough()` 透明转发到服务器同名端点（挂载已剥，路径尾一致）。

## 接口

端点集合与服务器版**一一对应**（22 条，无本地独有、无删减），请求/响应形态同 `plugins/file-filter/README.md`
「接口」一节，只是浏览器侧前缀换成本地挂载点：

| 组 | 端点 |
| --- | --- |
| 自检与配置 | `GET /status`、`GET/POST /config`、`POST /config/test`、`POST /config/import-columns` |
| 过滤主流程 | `POST /preview`、`POST /filter`、`GET /result/<task_id>`、`GET /download/<task_id>`、`POST /apply` |
| 映射治理 | `GET /mappings`、`POST /mappings/{confirm,reject,delete,clear,remap,vote,recheck,lookup}`、`GET /mappings/export`、`GET /mappings/recheck/<task_id>` |
| 大模型辅助 | `POST /llm/match-columns` |

## 依赖

真源 = `manifest.json` 的 `requires`：

| 依赖 | 必需 | 缺失后果 |
| --- | --- | --- |
| `openpyxl` >=3.1 | 否 | xlsx 表格过滤不可用（商店「依赖」页签装组件包） |
| `xlrd` >=2.0 | 否 | xls 表格过滤不可用 |

`flask` 未在 `requires` 声明——客户端运行时自带；`requests` 不再由本插件直接 import（凭据与 HTTP 调用归框架）。

## 出包与上架

```bash
python tools/client/build-local-plugin-package.py client-plugins/local-file-filter \
    -FrontendFrom plugins/file-filter/frontend --api-rewrite \
    -OutDir deploy/插件包/本地插件
```

产物 `JZClient-本地插件-local-file-filter-v<版本>.zip`（+ 旁挂 `.sha256`），可重现构建（zip 内不带 mtime）。
上架：管理后台「商店管理」上传，`runtime=local`，审核通过后客户端商店可见。

## 已知限制

- 需登录且服务器可达才能拿到保留字段名单/映射库；离线只余「本地默认配置 + 手工列开关」能力。
- 客户端固定内核/WebView2 与中继会话失效（401）等桌面端通用限制见 `docs/guide/桌面端与商店真机验收清单.md`。
