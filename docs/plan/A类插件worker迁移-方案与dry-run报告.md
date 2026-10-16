# A 类插件 worker 迁移 · 方案与 dry-run 报告（真机清单 R2）

| 文档属性 | 内容 |
| --- | --- |
| 状态 | 现行（**方案 + dry-run 留痕**；本文**不执行切换**——真实数据根登记 `supervisor-units.json` = 运行时行为切换，属人工动作） |
| 最后核对 | 2026-10-09（对照 `app.py`、`supervisor/mainapp_bridge.py`、`tools/e2e/worker-migration-demo.py` 实况与实跑输出） |
| 作用域 | 交付 / 运行架构（E 架构下 A 类插件从进程内迁到 supervisor 托管 worker） |
| 随包 | 否（`docs/plan/` 层） |
| 覆盖范围 | 迁移动机与机制、目标插件逐个就绪取证、切换步骤与回滚点、dry-run 实测结果、尚未验证项 |
| 不覆盖 | E 架构契约本身 → `docs/design/服务端插件运行架构-设计文档.md`；supervisor 服务化（R1，NSSM）→ 真机清单 |
| 关联文档 | `docs/guide/桌面端与商店真机验收清单.md` R2、`docs/plan/桌面端适配-TODO清单.md` 批次 9–11、`docs/design/存量插件三类适配-设计文档.md` |

## 1. 现状与机制（先纠正一处取证行号）

- **真实数据根目前没有任何单元登记**：`~/.jztoolshub/config/` 实测（**2026-10-17 复核**）为 `admin.json` / `.admin_key` / `.app_state.json` / `shell-token.json` / `tools.json`（另有 C1b 遗留的 `llm_migrated.flag`，已无代码引用），**无 `supervisor-units.json`** → 四个 A 类业务插件（knowledge-base、shared-docs、case-report、notice-board）**全部仍是进程内加载**。
- 判定入口（真机清单 R2 原文写 `app.py:865-874`，**已漂移**；**2026-10-17 再次复核并更正**）：现行实现是
  `jz_host/plugin_loader.py:464`（判定函数 `_worker_units_snapshot()` 定义在同文件 `:367`）—— 注册期调
  `supervisor.mainapp_bridge.worker_units_snapshot(get_data_root())`，
  命中单元的插件**跳过进程内加载**并返回 `{"reason": "supervisor 托管（E 架构）"}`（日志见 `jz_host/plugin_loader.py:398`），
  其 `/api/<id>/*` 由反代桥（`install_worker_proxy`，`app.py:538`）转发到 worker。
  单元文件路径与 schema 校验：`supervisor/supervisor_core.py:56,75`（`schema` 不支持即 `ValueError`）。
- **一句话**：切移动作 = 往真实数据根写一份 `supervisor-units.json` 并重启主应用；机制、台架、回滚都在，缺的是**有人在真机上按流程做并留证**。

## 2. 目标插件就绪取证（2026-10-09 实测）

| 插件 | 版本 | 声明依赖 | worker 化注意点 | 就绪判定 |
| --- | --- | --- | --- | --- |
| notice-board | 1.0.5 | flask | 试点，已在临时数据根全链演练过 | ✅ 可迁（流程已彩排） |
| knowledge-base | 1.3.6 | docx、flask、olefile、openpyxl、xlrd | **依赖外部程序 LibreOffice/soffice**（`backend/office_render.py`）；worker 进程必须能看到该程序路径与 `vendor/`；渲染重、首启慢 → `health_grace` 要给足 | ◐ 需真机确认外部程序可达 |
| shared-docs | 1.2.5 | docx、flask、openpyxl、xlrd | 体量小（8 文件），无外部程序 | ✅ 可迁 |
| case-report | 1.3.1 | flask、openpyxl、requests | 有出网 HTTP（requests）；`config.json`/`item_categories.json` 等在数据根，worker 与主应用共用同一数据根即可 | ✅ 可迁 |

> **版本轴注记（2026-10-17）**：§2 表的插件版本为 2026-10-09 快照；2026-10-16 起全产品版本轴已统一归 **1.0.0**，
> 引用时以各插件 `manifest.json` 为准。

排序沿用 `存量插件三类适配-设计文档.md` Q7 拍板：**knowledge-base → shared-docs → case-report**（notice-board 已演练）。
注意该排序把最重的 knowledge-base 放首位是**有意**的：它最能暴露外部程序与冷启动问题；若目标机没有 LibreOffice，建议改从 shared-docs 起。

## 3. 切换步骤（每次只迁一个插件）

1. **备份**：`Copy-Item <数据根>\config\supervisor-units.json`（不存在则跳过）与 `<数据根>\config\tools.json`。
2. **登记单元**：在 `supervisor-units.json` 的 `units[]` 追加
   `{"name": "<id>", "kind": "plugin", "plugin_id": "<id>", "interpreter": "main", "enabled": true}`；
   首次建文件需带全 `schema/supervisor/port_range/health_interval/health_grace/stable_window/circuit_threshold/worker` 各节
   （形状照抄 §4 的 dry-run 实例；`worker.plugins_dir` 指向**主包的 plugins 目录**，不是数据根）。
3. **重启主应用**：观察日志出现 `插件 <id> 已 worker 化（supervisor 托管），跳过进程内加载`（`jz_host/plugin_loader.py:398`）。
4. **核对 worker 起来**：supervisor 状态/控制接口能看到该单元 `pid`/`port`；`/api/<id>/…` 任一 GET 返回 200。
5. **功能与鉴权走查**：登录态下该插件的核心用例逐个点一遍；确认身份是**真实登录用户**（回环 `/api/session` 取到的 dict，不是夹具）。
6. **隔离性验证**：停掉该 worker → 该插件路由 503、主应用与其余插件照常、登录不丢、首页卡片不消失。
7. **回滚**：把该 unit 的 `enabled` 置 false（或整段删除）→ 重启主应用 → 插件回到进程内加载。**每迁一个都要真回滚一次并留证**，否则回滚路径没被验证过。
8. **留痕**：迁移前后控制接口输出、`supervisor-units.json` 快照、进程列表、逐插件走查结论（真机清单 R2 的留痕要求）。

**失败处置**：worker 起不来 → 先看 `health_grace` 是否过短（冷启动重的插件如 knowledge-base 建议 ≥15s），再看 `worker.plugins_dir`/`interpreter` 指向；熔断阈值 `circuit_threshold` 触发说明反复崩溃，**先回滚再排查**，不要挂着反复重启观察。

## 4. dry-run 实测结果（临时数据根，真实主应用 + 真实插件）

命令：`python tools\e2e\worker-migration-demo.py`　｜　**2026-10-17 复跑（D8 修复后）：9 项 / FAIL 0 / 退出码 0**

```
[OK] S1 supervisor 拉起 notice-board worker  — {'ok': True, 'pid': 6260, 'port': 15101}
[OK] S1 worker 健康（register(app) 原样契约）  — {'ok': True, 'plugin': 'notice-board', 'version': '1.0.0'}
[OK] S2 壳密钥 shell-token.json 惰性生成（首个非公开路径请求触发）
[OK] S2 真实登录成功（默认 admin 账号）
[OK] S2 经反代访问公告板 API → 200（鉴权管线→反代→worker→回环取真实身份）— {'count': 0, 'items': [], 'ok': True}
[OK] S2 会话回环取到真实身份（非夹具 dict）
[OK] S3 worker 停止 → 该插件 503 兜底（证明服务路径经 worker）
[OK] S3 主应用照常 + 登录态不丢 — 200
[OK] S3 工具清单照常（公告板卡片不消失）
结果：9 项，FAIL 0
```
> **【2026-10-17 更正（D8 销项）】** 2026-10-09 之后本台架曾长期跑红：`FileNotFoundError: …\config\shell-token.json`。
> **根因**＝壳门接入密钥的**惰性生成时序**（2026-10-11 壳门批次引入）：密钥只在**首个非公开路径请求**时生成
> （`jz_host/shell_gate.py` 的 `_load_state()`——文件缺失即写入），而本台架的启动就绪探测只打 `GET /login`
> （它在 `shell_gate.PUBLIC_EXACT` 白名单内，`is_public()` 提前 return）→ **永不触发**生成；旧夹具只轮询等文件出现。
> **修法（修夹具，未动产品纪律）**：新增 `_shell_token()` —— 读密钥前先打一个非公开路径（`/api/tools`，403 属预期）
> 触发惰性生成，并把它固化为一条显式断言（S2 首项）。
> **未采用**「`shell_gate` 在 `install()` 期预生成」的替代方案：那与 `shell_gate.py:19` 明写的
> 「`install()` 自身不触碰数据根」**直接冲突**。
> 另：本台架**不需要 waitress**（用 `werkzeug.serving.make_server`；`app.py` 的 waitress 是函数内惰性 import，
> `:492`）——早期「waitress 缺失致失败」的归因属**误判**，一并更正。



本次跑用的单元登记实例（**写在 `tempfile.mkdtemp` 的临时数据根**，跑完随进程临时目录丢弃）：

```json
{"schema": 1, "supervisor": {"port": <free>}, "port_range": [15101, 15199],
 "health_interval": 2.0, "health_grace": 8.0, "stable_window": 60.0, "circuit_threshold": 3,
 "worker": {"python": "<解释器>", "worker_script": "jz_worker.py",
            "main_url": "http://127.0.0.1:<主应用端口>",
            "plugins_dir": "<临时包目录>/plugins", "main_pkg_dir": "<仓库根>"},
 "units": [{"name": "notice-board", "kind": "plugin", "plugin_id": "notice-board",
            "interpreter": "main", "enabled": true}]}
```

**真实数据根未被触碰**：dry-run 前后都核对过 `~/.jztoolshub/config/` 仍无 `supervisor-units.json`（列目录实测（2026-10-17 复核）：`admin.json`、`.admin_key`、`.app_state.json`、`shell-token.json`、`tools.json`）。

## 5. 这份 dry-run **没有**证明的事

1. **目标机差异**：演练在开发机源码态跑（解释器 = 仓库 python，`main_pkg_dir` = 仓库根）。冻结 exe 形态下 worker 用哪个解释器、`plugins_dir` 落在安装目录何处，需在目标机实测（R1 supervisor 服务化同批）。
2. **knowledge-base 的外部程序可达性**：LibreOffice/soffice 路径在 worker 进程里能否解析（环境变量继承差异），未验。
3. **并发与长时稳定**：`stable_window`/`circuit_threshold` 只在真实流量下才有意义；演练是单请求级别。
4. **升级链**：worker 化插件的包升级走 `supervisor.apply_chain`（`try_supervisor_apply`），与进程内形态的 `inspect/apply` 是两条路，**迁移后的插件升级未在目标机验过**。
5. **数据根多实例/权限**：worker 与主应用同数据根的写冲突、Windows 服务账号权限，未验。

> 结论：**机制齐备、流程可复制、dry-run 全绿**；剩下的全是「在真机上按 §3 做一遍并留证」。因此 R2 保持真机项状态，本文只把它的方案与彩排证据钉下来。
