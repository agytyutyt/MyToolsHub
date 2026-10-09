# 轨迹速写（本地）—— local-trajectory-sketch

trajectory-sketch 的**本地化形态**（C 类客户端工件，`kind=hybrid`）：上传的轨迹表在**本机**完成
背景图片摘除、字段自检、轨迹分析引擎计算与报告工作簿生成，**敏感轨迹数据不出机**；配置权威在服务器
（本地留离线缓存），大模型语义匹配在服务器工件端点完成（Key 不落本机）。

> 功能语义真源：`plugins/trajectory-sketch/README.md`（流程 ①~④、算法 v2/v3 说明、分析参数逐项含义）。
> 本文只登记**本地工件与服务器版的差异面**：形态、挂载、边界、依赖、出包。

## 形态与挂载

| 项 | 值 |
| --- | --- |
| 插件 id / 版本 | `local-trajectory-sketch`（版本号跟随服务器版 `trajectory-sketch`，当前 1.3.1） |
| runtime / kind | `local` / `hybrid`（`server_data: true`：需服务器中继） |
| 包根 | 即插件目录（`manifest.json` + `frontend/` + `backend/` + `README.md`） |
| 挂载点 | `manifest.entry.mount` = `/api/local/local-trajectory-sketch` |
| 内置后端路由前缀 | 仍是 `/api/trajectory-sketch`（源码里的 `API_PREFIX` 不变） |
| 浏览器可见全路径 | `/api/local/local-trajectory-sketch/api/trajectory-sketch/<端点>` |

前端复用 `plugins/trajectory-sketch/frontend`（出包时 `-FrontendFrom` 指定），构建期把代码字面量
`api/trajectory-sketch` 改写为 `api/local/local-trajectory-sketch/api/trajectory-sketch`（规范 §16.10 的
L-13 执行点；C 类工件**必须**带 `--api-rewrite`）。

## 边界（什么在本机、什么在服务器）

- **本机**：读表（`excel_io`）、背景图片摘除（`bg_image`）、字段硬过滤（`filter_bridge` → 本插件自带
  `filter_local`，与 file-filter `core.py` 同源副本、**独立演进**）、轨迹分析引擎（`engine/`：地点簇 →
  停留点 → 出行段 → 报告）、报告工作簿生成、暂存与任务表（`<客户端数据根>/plugins/trajectory-sketch/.task_cache`，
  TTL 30 分钟，含归属校验）。
- **服务器（经 `jz_relay.own("local-trajectory-sketch")`）**：
  - 配置（保留字段名单/列映射/分析阈值/报告文案）：**权威在服务器**，管理员统一维护；服务器可达时刷新
    本地 `config.json` 缓存，不可达或未登录时用最近缓存，首装无缓存回落到代码默认值
    （`config_store.DEFAULT_CONFIG`，规范「代码默认值 = 配置同源」）。写操作透传服务器
    （管理员权限由服务器按会话判定），本机留一份缓存。
  - 大模型：可用性探测走服务器 `/status` 的 `llm_configured`；语义匹配全部发生在服务器工件端点
    `POST /api/trajectory-sketch/llm/match-columns`（Key 不出服务器，红线 5 / L-10）。本机 60 秒缓存，
    离线按「不可用」降级——**分析仍走硬过滤档**，界面明示。本插件**无映射库、不落库**（无状态匹配）。
- **同源副本纪律**：`backend/` 与 `backend/engine/` 下带「同源副本登记（C 类客户端工件，2026-10-09）」
  首行的文件，与服务器插件同名文件同源、各自独立演进（规范 §16.15）；改动只在各自副本内进行。

## 接口

端点集合与服务器版**一一对应**（10 条，无本地独有、无删减），请求/响应形态同
`plugins/trajectory-sketch/README.md` §4，只是浏览器侧前缀换成本地挂载点：

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/ping` | GET | 探活 |
| `/status` | GET | 依赖自检 + 过滤能力状态（含大模型是否可用，经中继探测）+ 算法版本 + 配置提醒 |
| `/config` | GET | 读配置（权威在服务器，离线用本地缓存；含别名展开明细与 `can_manage`） |
| `/config` | POST | 保存配置（透传服务器，仅管理员/超管；不含算法版本） |
| `/algorithm` | POST | `{algo}` 切换分析算法（办案员即可，下一次生成报告生效） |
| `/upload` | POST | multipart `file` → 删除背景图片 → 本机暂存 + 硬过滤预演 + 字段自检（同步） |
| `/analyze` | POST | `{staged_id, mode}` → 本机异步分析，返回 `task_id` |
| `/result/<task_id>` | GET | 轮询进度与结果（质量/停留点/出行段/报告/警告/`sanitize`） |
| `/download/<task_id>` | GET | 下载速写报告 `.xlsx`（5 个 sheet） |
| `/llm/match-columns` | POST | 无状态大模型字段匹配（经 `own()` 交服务器执行；不落库） |

## 依赖

真源 = `manifest.json` 的 `requires`：

| 依赖 | 必需 | 缺失后果 |
| --- | --- | --- |
| `flask` >=3.0 | **是** | 插件全部功能不可用（该依赖随客户端分发，缺则重装客户端或联系维护者） |
| `openpyxl` >=3.1 | 否 | 报告导出与 xlsx 读取不可用（商店「依赖」页签装组件包） |
| `xlrd` >=2.0 | 否 | xls 读取不可用 |

## 出包与上架

```bash
python tools/client/build-local-plugin-package.py client-plugins/local-trajectory-sketch \
    -FrontendFrom plugins/trajectory-sketch/frontend --api-rewrite \
    -OutDir deploy/插件包/本地插件
```

产物 `JZClient-本地插件-local-trajectory-sketch-v<版本>.zip`（+ 旁挂 `.sha256`），可重现构建（zip 内不带 mtime）。
上架：管理后台「商店管理」上传，`runtime=local`，审核通过后客户端商店可见。

## 已知限制

- 大模型辅助档需服务器可达且服务器已配置统一大模型；离线只有硬过滤档。
- 配置首启需联网取回（无缓存时回落代码默认值）；服务器端改了阈值需本机重新联网刷新才生效。
- 客户端固定内核/WebView2 与中继会话失效（401）等桌面端通用限制见 `docs/guide/桌面端与商店真机验收清单.md`。
