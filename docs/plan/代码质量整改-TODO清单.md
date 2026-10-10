# 代码质量整改-TODO清单（`eval/20260929代码质量分析报告` 承接）

| 文档属性 | 内容 |
| --- | --- |
| 状态 | 现行（活清单；2026-10-09 Q6 批次后：P0 C1/C2/C3/C4 已销项（C1b 删除窗口仍待 flag 全覆盖），P1 D3/D4/D7 已修、D1/D2 部分完成（跨处理器读改写锁需人工），D5 需拍板、D6/D8 已完成（8105ae9）、L-13 债·admin 已完成（fcae857）；**P2 四件已完成（2026-10-10，见 §3.1）**；P3 未开始） |
| 最后核对 | 2026-10-10（P2 四件完成后对照 `docs/eval/20260929代码质量分析报告.md` v2 与 `git log` 实况） |
| 作用域 | 框架 + 跨插件 + 插件内部（宿主 / 构建 / 测试 / 各插件） |
| 随包 | 否（`docs/plan/` 层，见 `docs/README.md` §8） |
| 覆盖范围 | 报告结论的**整改状态登记**：P0 C1–C4（含 C1b）、P1 D1–D8、P2 §4.1–§4.4、P3 §5.1/§5.3 |
| 不覆盖 | 审查口径、分级框架与证据正文 → `docs/eval/20260929代码质量分析报告.md`（**证据真源，本文不复述**）；`docs/README.md` §7 的 eval 表行「未实施」标注已随本文状态改为指针；审计新发现的真缺陷 D-1~D-8（无编号冲突的独立清单）以 `docs/archive/20261009规划完成度核对与未完成项汇总.md` §3.1 为准 |
| 关联文档 | `docs/plan/文档体系整改-TODO清单.md`（S12 门禁扫描器与本文 D 组部分同源）；`HANDOFF.md` §4 |

## 0. 速览

| 四段 | 落点 |
| --- | --- |
| 接口契约 | 各表的「修法摘要」= 完成判据（做什么算销项） |
| 实现路径 | 「现状证据」列给出报告小节号与关键 `文件:行` |
| 当前实现状态 | 「状态」列（2026-10-09） |
| 后续优化方向 | 开工时以报告对应小节为执行依据；可选架构决策（如 `jz_tasks`/`jz-ui.js`）按报告 §6.3 默认不做，需规范级拍板 |

> **口径**：每条一行；「现状证据」只引用报告小节与关键位置，完整的 22 项证据链读报告原文。
> 状态三态：未开始 / 进行中 / 已完成（带日期与提交号）。报告 §7 的批次划分仅作排序参考。

## 1. P0：插件独立性与解耦（报告 §2）

| # | 现状证据（报告 §） | 修法摘要 | 状态 |
| --- | --- | --- | --- |
| C1 | §2.2 C1：admin 启动迁移改写其他插件数据文件（原 `plugins/admin/backend/routes.py:308/:959`） | 迁移上移主体 `app.py:100`（行为逐字保留）+ 退场标记 `config/llm_migrated.flag`（`app.py:130`）；admin 侧函数与调用点删除 | ✅ **已完成（2026-10-09，commit 747412d）** |
| C1b | 同 C1，报告建议 3「迁移完成、目标版本全覆盖后**整段删除**」 | **迁移函数删除窗口**：flag 覆盖率 100%（所有目标数据根均出现 `config/llm_migrated.flag`）后，删除 `app.py` 的 `migrate_plugin_legacy_llm()` 与启动调用 | 未开始（依赖 flag 全覆盖，判定口径见 `app.py:125` 注释） |
| C2 | §2.2 C2：`plugins/trajectory-sketch/backend/filter_bridge.py:44-52` 恒真函数与废弃形参；死键 `use_filter_plugin_rules`（`config_store.py:38/:130`、`routes.py:274-275`）；孤儿 `_probe_fix.cpython-314.pyc` | 删恒真函数与废弃形参（同步修调用方）、删死键三处读写、清孤儿 pyc——这是完成解耦，不是卫生清理 | ✅ 已完成（commit `7a14e96`）：恒真函数与废弃形参删除、调用方同修、死键三处读写与模板/注释清理、孤儿 pyc 删除；`file-filter-c-tests` 11/11、`test_filter_llm` 31/31 全绿 |
| C3 | §2.2 C3：帧头常量三份人肉同步（`trajectory-convert:48-51` ↔ `info-transfer:175-178` ↔ `qr-video-decode:38-40`）；编码实现四份 | 协议契约文档（`docs/design/`）+ `testdata/protocol/` 跨实现测试向量；**代码不合并**（四插件各自保留实现） | ✅ 已完成（commit 本次）：契约 `docs/design/二维码传输协议-契约文档.md` + 向量 `testdata/protocol/`（含 `generate.py` 与来源说明）+ 台架 `tools/e2e/qr-protocol-consistency.py`（32/32 通过、1 项如实 skip）；**台架建档过程中查出两处新互操作缺陷已登记契约 §6.1/§6.2，修法需拍板**（m=256 申报溢出；map-marker 生成脚本多组分组与解码端不同构→静默错位） |
| C4 | §2.2 C4：同源副本无台账——`bg_image.py`×2 未声明（file-filter ≡ trajectory-sketch）、`jsqr.min.js`×2 未登记、QR 编解码×4 含未声明分叉（`PAYLOAD_SAFETY`） | 增设**同源副本台账**一节（副本对 / 同源日期 / 演进策略 / 漂移时以谁为准）+ 构建期 md5 漂移提示（warning 不阻断，宿主同 C-11 脚本） | ✅ 已完成（commit 本次）：`docs/design/同源副本台账.md` 覆盖报告 C4 全部副本对（并纠正份数：`bg_image` 实为 ×4、`_viewer` 实为 ×11），逐条给路径对/md5 基准/校验口径/漂移处置；**构建期 md5 漂移提示（报告建议 2）未实施**，已列入台账 §4 后续 |
| L-13 债·admin | 门禁侧新登记（非报告编号）：admin legacy 端点命名债——`/api/account/password` 等（后端 `plugins/admin/backend/routes.py:988` 注册；前端 `frontend/js/admin-common.js:178` 调用；测试 `test_data_migrate.py:574` 覆盖），不符 §16.10「`/api/<自身id>`」惯例 | 现经框架面白名单放行（`tools/build-plugin-package.ps1` `$l13CoreLegacy`，依据 admin 随主体分发、不可卸载；Python 侧 `CORE_LEGACY_FACES` 同口径）；可选清理=改名 `/api/admin/...`（后端+前端+测试三处 + 功能验证） | ✅ 已完成（2026-10-09，commit fcae857：改名 `/api/admin/account/password` + 撤双侧白名单；无白名单 L-13 实测通过、跨插件负向仍拦） |

## 2. P1：数据安全与正确性（报告 §三）

| # | 现状证据（报告 §） | 修法摘要 | 状态 |
| --- | --- | --- | --- |
| D1 | `plugins/admin/backend/routes.py:134-146`（load/save 无锁）；`batch_io.py:273/284` 同 | admin.json 加模块级 `threading.RLock` 包住 load→mutate→save 全程 | ✅ 已完成（L1-3：`902500a` 22 处整段包锁 + `6d83eab` 停卡处两次进锁，23/23 全闭合；并发台架 10/10） |
| D2 | `app.py:525-530`（固定 `.tmp` 名）；`:1073-1116`/`:1148-1184` 无锁读改写 | tools.json 加模块级锁；tmp 改 `tempfile.mkstemp(dir=…)` | ✅ 已完成（tools.json 已随 D1 收口） |
| D3 | `plugins/admin/backend/plugin_admin.py:1365` 传 `None`，函数（`:883-892`）对非 dict 恒返回 `{}` | 修 `plugin_names(None)`——**一行可修** | ✅ 已完成（commit 本次）：`scan_packages` 改为按 `data_root/config/tools.json` 取展示名（`plugin_names(None)` 恒空的缺陷修掉），`data_root=None` 时安全回落 |
| D4 | `app.py:515`（读 tools.json）用普通 utf-8；`jz_deps.py:125…334`、`app.py:122`、`jz_llm.py:811` 用 utf-8-sig | 外部可手工编辑的 JSON 统一 `utf-8-sig`（BOM → 整站空注册表） | ✅ 已完成（commit 本次）：`app.py` 的 tools.json / manifest.json 读取统一 `utf-8-sig`（与 `jz_deps`/`jz_llm` 同口径，容忍记事本 BOM） |
| D5 | 放行版：`character-graph:157-169`、`qr-video-decode:106-118`、`info-transfer:367-376`；收紧版：`trajectory-convert:192-205`、`file-filter:225-236` | 拍板从严（旧任务仅超管可见），三个放行版插件**各自本地**统一；不沉淀公共模块 | ✅ 已完成（L1-2：`f796190`，三处对齐收紧仅超管；test_security 43 通过含 TestTaskOwnership） |
| D6 | v2 出「无。』」（`v2/report.py:222-231`）vs v3 只遍历有窗口数据的用户（`v3/__init__.py:145`） | 核对产品预期后在插件内统一 v2/v3 报告口径 | ✅ 已完成（2026-10-09，commit 8105ae9：v3 按 `dataset.all_usernums` 全集枚举（含被清洗空的号码），无数据号码出「无。」；针对性验证 PASS + test_filter_llm 31/31） |
| D7 | 聚合接口用 manifest.id（`app.py:687`）vs visibility 用 registry.id（`:1142`），无加载期校验 | `register_plugin_backends` 加载时校验一致，不一致记入 `_manifest_errors` | ✅ 已完成（commit 本次）：`load_manifests` 加载期校验 `manifest.id` 与目录名一致（不一致记入 `_manifest_errors` 并跳过该插件，与既有「单插件坏不连坐」口径同）；17 插件实测全过 |
| D8 | 未处理 401：`notice-board`（app.js:35-40）、`file-filter`（:1683-1697）、`trajectory-sketch`；已处理：case-report、shared-docs | 各插件本地补 401 跳转（不引公共 JS 模块） | ✅ 已完成（2026-10-09，commit 8105ae9：notice-board / file-filter / trajectory-sketch 按 knowledge-base 模式补 401（ff 含传文件链路），缓存戳 ts ?v=5 / ff ?v=8 / nb ?v=7） |

## 3. P2 / P3：宿主、构建与插件内部（报告 §四 / §五）

> 分组登记：开工时按报告小节逐条落；P2 去重不受插件独立性约束（报告 §6.3 已撤回/降级的跨插件公共件不在本组）。

### 3.1 P2（报告 §4.1–§4.4）

| # | 范围（报告 §） | 要点摘要 | 状态 |
| --- | --- | --- | --- |
| P2-1 | §4.1 宿主核心重复 | `jz_llm._post`/`test_connection` 约 30 行逐字重复 → `_send_and_parse`；「tmp 写入 + os.replace」4 模块 6 处 → `atomic_write_json`（顺带统一 D4 编码）；删 `_EMOJI_ICON_FILES`/`icon_file` 死代码；`@require_login` 收口三条 LLM 路由等 | ✅ **已完成 2026-10-10**（`268bc32`，详见方案 §B-23） |
| P2-2 | §4.2 构建脚本重复 | `build-deploy.ps1` ↔ `build-deploy-local.py` 整段双实现；包内容清单单一真源数据文件；`tools/_shared.psm1` 统一 `Compare-SemVer` 等；`install.ps1` cfgPairs 死清单改自动发现；spec 两份 19 项列表合一；修 `$vcBundled` 先用后定义与 depCheck 漂移；zip 机制对齐 | ✅ **已完成 2026-10-10**（`d47764c`，含"整段双实现"改单源委派：组装段删除、改调 local.py；详见方案 §B-23） |
| P2-3 | §4.3 测试重复（无 conftest.py） | 根 `conftest.py` 收敛 `load_plugin`/数据根 fixture/USER/PLUGIN_DIRS 四类样板；`test_admin_plugin_manager` 导入期副作用入 fixture | ✅ **已完成 2026-10-10**（`01567c7`，详见方案 §B-23） |
| P2-4 | §4.4 宿主复杂结构 | `app.py` 拆模块（dep_bootstrap / access_log / plugin_loader / LLM Blueprint / tray）；长函数拆分；每请求开销两项（refresher 退避、轻量 `/api/site`） | ✅ **已完成 2026-10-10**（`28f2f3d`+`63c1197`，jz_host/ 五模块拆出、app.py 1476→526 行；详见方案 §B-23） |

### 3.2 P3（报告 §5.1 / §5.3）

| # | 范围（报告 §） | 要点摘要 | 状态 |
| --- | --- | --- | --- |
| P3-1 | §5.1 复杂结构 | admin `routes.py` god file 拆分 + `get_session_user` 请求级缓存；三大 `register()`/`run_filter`/`build_timeline` 拆分；前端长函数拆分 | ◐ 部分完成（2026-10-10 S6b）：**`get_session_user` 请求级缓存已完成**（`session["user"]` 为键挂 `flask.g`，一请求 N 次解析→1 次；`save_admin_config` 写入即作废；test_client 五连调 0 次解析、保存后 1 次）；**其余（admin god file 拆分 / `register()`×2 / `run_filter` / `build_timeline` 拆分 / 前端长函数拆分）未开工**——P3-1 系**纯可维护性重构、无缺陷驱动**，且触及 C 类工件同源副本（如 trajectory-sketch `aggregate.py` → 本地副本重生成+双包重出+重上架）与多插件前端，属方案 §4 明示的"风险最高、单独一轮"；按协议**留后续独立一轮**，起点见承接手册 §B-32 |
| P3-2 | §5.3 健壮性与卫生 | 约 50 处吞异常补日志（优先 admin 与托盘）；knowledge-base 锁内磁盘 I/O 移出；`os._exit`/死代码清理；开发产物移出发布目录；人肉版本戳构建期注入；vendor 补丁脚本化；`main.js` 渲染统一 `esc()` | ✅ 已完成（2026-10-10 P3-2，commit `e5bbda0` 代码 / `be7d648` 包登记；详见承接手册 §B-31）：①吞异常点名站点补日志（tray 启动/KB 迁移/admin 部署标记）②KB 上传/删除改「两段式进锁」重 I/O 出锁 ③清理 admin `_protect_admin_ops` 死代码（`os._exit` 审计后保留）④发布目录开发产物经 payload-rules 已剔除 + 前端静态路由加 `.py` 守卫 ⑤qr `APP_VER` 改单源派生（F-2 门禁拦手同步）⑥vendor 七条补丁加核验脚本 `tools/e2e/kb-vendor-patch-check.py`（**检出半件**；自动重放需上游源，登记待办）⑦`main.js` 分类名与错误串补 `esc()` |

## 4. 纪律与联动

- **证据真源**：`docs/eval/20260929代码质量分析报告.md`（v2，20260930 修订）；本文只登记状态，不复述审查正文与数值。
- **报告状态标注联动**：`docs/README.md` §7 的 eval 表行原标「未实施」；C1 完成后已改为「基本未实施——C1 已完成；整改状态 → `plan/代码质量整改-TODO清单.md`」（本任务更新），后续按该行随本文状态联动。
- **新发现缺陷的承接**：审计 §3.1 的 D-1/D-2 已由 commit `7e4083e` 修复（build-deploy 白名单 / timeout_ms）；D-3/D-5/D-6/D-7 随信息传输与桌面端清单承接，不并入本清单编号。
- **不做项**：报告 §6.3 的「可选架构决策」（扩 `FRAMEWORK_MODULES` 加 `jz_tasks`、前端 `jz-ui.js`）默认不做，需规范级拍板。

*本清单是状态登记面；执行时以报告对应小节为验收依据。*
