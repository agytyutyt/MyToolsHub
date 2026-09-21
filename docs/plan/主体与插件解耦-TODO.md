# 主体与插件解耦 TODO 清单

| 文档属性 | 内容 |
| --- | --- |
| 日期 | 2026-09-19 |
| 依据 | `docs/design/主体与插件解耦-设计文档.md`（设计定稿：§3 边界、§4 管理方式、§5 契约与门控、§7 改动清单、§8 阶段） |
| 用法 | 按 **S0 → S1 → S2** 顺序执行（S3 单独立项）；每项完成后在状态列打勾、回写 `.workbuddy/memory/` 当日日志，并按 `HANDOFF.md` §10.1 同步文档 |
| 口径 | **本文只写"做哪一项、落到哪个文件、用哪条验收"，不复制设计契约**（契约与改动明细见设计文档，避免两处维护） |

| 四段覆盖 | 落点 |
| --- | --- |
| 接口契约 | `docs/design/主体与插件解耦-设计文档.md` §3–§5（本文不复制） |
| 实现路径 | 设计文档 §7 的 23 条改动清单 + §8 阶段划分；本文拆为 T01–T25，逐项标注落点 |
| 当前实现状态 | 下方状态列（**唯一活清单**）。截至 2026-09-21：**S0–S3 全部落地并已通过验收**（目标机：自动验收 27 通过 / 0 失败 / 4 跳过，跳过项为需 Key 与需参数三类，见验收手册 §9）；主包 **v2.3.17**（26.6 MB，仅框架依赖 + admin）、**16 个插件包**与插件集、11 个依赖组件包均已重出并与登记一致；交付侧另有「验收测试数据包」「验收工具包（目标机无需 Python）」「重置回干净机脚本」；自动化回归全绿（`verify-package` 全项、沙箱 E2E 全通过、`decouple-acceptance.py` 15/15、3 个 pytest 文件）；**剩余人工项**见文末清单与 `docs/guide/干净机器部署验收手册.md` |
| 后续优化方向 | 本清单即唯一活清单；S3（依赖组件包）为可选立项，不阻塞 S0–S2 |

> **覆盖范围**：解耦落地的全部待办条目、执行顺序、验收口径与回归要求（唯一活清单）
> ｜**不覆盖**：设计契约与改动明细 → `docs/design/主体与插件解耦-设计文档.md`；插件包链路 → `docs/design/插件独立升级方案-设计文档.md`
> ｜**随包**：否 ｜**最后核对**：2026-09-19

## 总览

| 阶段 | 主题 | 条目 | 前置依赖 | 阶段验收 |
| --- | --- | --- | --- | --- |
| **S0** | 契约、依赖与门控（**不动目录结构**，可独立上线） | T01–T12 | 无 | AC-5 / AC-6 / AC-9 / AC-11 / AC-13~AC-21 |
| **S1** | **物理解耦**（核心交付） | T13–T20 | S0 全部完成 | AC-1~AC-4 / AC-7 / AC-18 |
| **S2** | 插件间独立收口 | T21–T24 | S0、S1 | AC-8 / AC-10 / AC-22 |
| **S3** | （可选）依赖组件包 · 主体瘦身 | T25 | 单独立项 | — |

> 顺序铁律：**S0 必须先于 S1**——没有版本与依赖门控，主包剥离插件后老插件会静默跑坏（设计文档 §0「必须同时补的四件事」）。
> 每阶段结束时跑一次 §回归要求（AC-12）。

---

## S0 · 契约、依赖与门控

| 编号 | 任务 | 落点（设计文档 / 代码） | 验收 | 状态 |
| --- | --- | --- | --- | --- |
| T01 | **依赖倒置**：新增主体模块 `jz_api.py`（`get_session_user()` / `set_operation()` / `require_login()`）；admin 在 `register(app)` 注册 provider；`app.py:278`/`:698` 改从 `jz_api` 导入；10 个插件分批改导入；`jztools_admin.routes` 留 **re-export 垫片**（一个版本窗口） | §7 #21、§8 S0-1；`app.py`、`plugins/*/backend/routes.py` | AC-9 | ✅ 已完成 |
| T02 | **版本契约字段**：`version.json` 增 `plugin_api`（初值 1，`build-deploy.ps1:333-357` 写入、`install.ps1:423-437` 保留）；出包工具把 `api_version` 写进 `plugin-package.json` 与 `index.json` | §7 #8/#9、§5.2 | AC-5 | ✅ 已完成 |
| T03 | **依赖声明补齐**：16 个插件的 `manifest.json` 增 `requires`（三类：`python_packages` / `vendored` / `external`，含版本与 `required`）；`plugins/<id>/README.md` 的依赖清单一节与声明一致 | §7 #16、§5.3 | AC-13 / AC-19 | ✅ 已完成（11 份插件 README 均含「依赖与升级」节：三类依赖清单 + 能否单独升级 + 是否需重启，对应规范 U-2/U-4/U-6） |
| T04 | **已安装依赖清单**：`build-deploy.ps1` 生成 `config/installed-deps.json`（打包解释器的 `importlib.metadata` 快照：包名 + 版本） | §7 #13、§5.3 | AC-16 / AC-19 | ✅ 已完成（**口径已修正**：包集合取自冻结目录 `_internal` 的 dist-info，`scope=shipped`，实测 93 → 9 个；原先记的是构建机全量环境，干净机上会把 cv2/numpy/openpyxl 误判为"已安装"——见设计文档 §11.2d I-11） |
| T05 | **判定引擎 `jz_deps.py`**：读清单 + 插件 `requires` → 三态结论（`ok` / `degraded` / `blocked`）；版本比较器（仅 `>=x.y.z` 与精确，自实现，不引入 `packaging`）；供门控 / 后台 / `/api/tools` 三处复用 | §7 #21、§5.3 | AC-14 | ✅ 已完成 |
| T06 | **添加期检测（提示但不阻断）**：后台在 `inspect_package` 阶段返回缺失清单供弹窗（「仍然添加」/「取消」）；离线脚本在只读校验后打印缺失清单、**默认继续**，新增 `-SkipDepCheck` / `-StrictDeps` | §7 #17/#18、§5.3 | AC-20 | ✅ 已完成（脚本侧提示不阻断 + 后台上传时依赖弹窗「仍然添加/取消」；`inspect_package` 返回 dep_check） |
| T07 | **后台依赖展示**：插件列表新增「依赖需求」列（逐依赖徽标：满足绿色 / 不满足红色带 ×）+ 展开明细（声明要求 vs 实际版本）；新增「已安装依赖」区块（框架包 / 外部组件 / 插件自带）；顶部汇总横幅；接口增 `deps` 字段与 `GET /api/admin/deps`；`GET /api/<id>/status` 增 `deps` 段 | §7 #18/#19、§5.3 展示面 | AC-19 / AC-21 | ✅ 已完成（**已补**：白名单名经 modules 归一（PIL→pillow）+ 依赖组件包逐项列出并标注来源 `framework`/`component`——原先组件包只计入 `other_count`，装了哪些组件看不到；见 §11.2d I-13） |
| T08 | **C-10 交叉校验**：出包工具比对实测 import（`plugin-package.json.deps`）与声明——实测有、声明无 → **拒绝出包**；声明有、实测无 → 警告 | §7 #14、§5.3 | AC-13 | ✅ 已完成 |
| T09 | **门控 + 健壮性**：启动时逐插件求值（版本门控 §5.2 + 依赖三态 §5.3），`blocked` 不加载并记录；`load_manifests()` / `load_registry()` 加异常保护；`/api/tools` 暴露 `plugin_errors`，首页对加载失败插件显示"暂不可用" | §5.2、§5.3；`app.py:390-400`、`:621-627` | AC-5 / AC-6 / AC-14 / AC-15 | ✅ 已完成 |
| T10 | **admin 定位调整**：manifest 增 `"core": true`；`install-plugin.ps1` 拒绝 `-Uninstall admin` / `-PurgeEntry admin`；后台卸载入口对核心插件隐藏 | §7 #17/#18、§3.5 | AC-18② | ✅ 已完成（脚本侧拒卸载 + 后台列表对 core 插件不显示卸载按钮） |
| T11 | **插件间独立 · 第一处修补**：组织架构树提升为主体 API `/api/org/tree`（admin 经 `jz_api` 注册 provider、主体暴露路由）；`notice-board/frontend/app.js:68` 改调主体路由并递增入口页 `?v=N` | §7 #23、§5.4 | AC-22①② | ✅ 已完成 |
| T12 | **顺手清理**：主包 `tools/` 收窄为只带 `plugin-upgrade/`；`wheels/` 移出主包；`tools/plugin-payload-rules.json` 补 `*.mp4` / `yunfu*` / `simulate_*.py` 排除项 | §3.3 | AC-11 | ✅ 已完成 |

---

## S1 · 物理解耦（核心交付）

| 编号 | 任务 | 落点（设计文档 / 代码） | 验收 | 状态 |
| --- | --- | --- | --- | --- |
| T13 | **主包组装收窄**：`build-deploy.ps1:136` 只拷核心插件白名单（一期 = `admin`）+ 断言"包内 `plugins/` 仅含白名单"；`build-deploy.ps1:201-208` 模板自检按包内应带模板清单计算；`tools/build-deploy-local.py:49` 同步；`config/tools.json` 收窄为 `site` + 分类骨架 + admin 条目 | §7 #1/#2/#3/#7 | AC-1 | ✅ 已完成 |
| T14 | **升级不动插件**：`install.ps1:329-354` / `:384-414` 补显式断言——源包无 `plugins\<id>\` 即目标机该目录与其登记**零改动**；沙箱加对应用例 | §7 #5、§6 步骤⑤ | AC-2 | ✅ 已完成 |
| T15 | **主体备份与回滚**：`install.ps1` 升级前备份主体到 `backups\app\<旧版本>-<时间戳>.zip`（不含 `plugins\`、不含数据根，默认保留 1 份）+ 新增 `-Rollback` | §7 #6、§6 | AC-7 | ✅ 已完成 |
| T16 | **插件集包工具** `tools/build-plugin-set.ps1`：产出 `JZToolsHub-插件集-<名称>-v<日期>.zip`（`index.json` + N 个插件包 + `.sha256` + `安装插件集.bat`） | §7 #10、§4.1 | AC-4 | ✅ 已完成 |
| T17 | **批量安装** `install-plugin.ps1 -Set <目录>`：读 `index.json` 逐个应用，跳过已最新/更高版本，任一失败继续其余并汇总；全部成功统一重启一次 | §7 #11、§4.2 | AC-4 | ✅ 已完成 |
| T18 | **校验脚本收口** `tools/verify-package.py`：`TEMPLATE_MIN` 随包内模板清单调整；新增断言"`plugins/` 仅含核心插件""不含 `tools/dev`""不含 `wheels/`""`installed-deps.json` 存在且可解析" | §7 #4/#15 | AC-10 / AC-11 | ✅ 已完成 |
| T19 | **首装引导**：`install.ps1` 安装完成时提示"请安装插件集或逐个安装插件包"；首页工具列表为空时给出同款引导 | §3.5 | AC-17 | ✅ 已完成 |
| T20 | **阶段验收**：跑通 AC-1~AC-4、AC-7、AC-18①（admin 单独升级后主体升级不回退） | §6、§10 | AC-12 | ✅ 已完成：自动化 `tools/e2e/decouple-acceptance.py` 15/15；**目标机整体验收通过**（2026-09-21，`tools/e2e/acceptance-runner.py` 27 通过 / 0 失败 / 4 跳过 + 人工项清单见验收手册 §9.3） |

---

## S2 · 插件间独立收口

| 编号 | 任务 | 落点（设计文档 / 代码） | 验收 | 状态 |
| --- | --- | --- | --- | --- |
| T21 | **第二处修补**：`trajectory-sketch` **自带一份同样的过滤实现**（算法 + `keep_columns` / `post_rules`），删除 `filter_bridge.py` 的 HTTP 路径（`test_client` / 回环 `requests`）与 `tools.json` 读取；递增 `manifest.version`（U-1）并重启 | §7 #22、§5.4 | AC-22① | ✅ 已完成（llm 模式随解耦移除，见设计文档 I-5） |
| T22 | **C-11 构建期拦截**：出包工具扫描 `backend/**/*.py`，import 其他插件模块（`jztools_<其他 id>`）即拒绝出包 | §7 #20、§5.4 | AC-22③ | ✅ 已完成 |
| T23 | **后台「卸载」入口**：遵守规范 §11 顺序（删/停注册条目 → 备份数据 → 删目录），数据默认保留；对 `core:true` 拒绝 | §7 #12、§4.3 | AC-8 | ✅ 已完成（后台卸载入口：规范 §11 顺序 + 对 core:true 拒绝 + 数据默认保留/可选打包） |
| T24 | **收口验证**：grep 断言仓库内零跨插件调用（`jztools_<其他 id>` import / `/api/<其他插件>/` 调用 / 前端跨插件请求）；`jztools_admin.routes` 垫片按窗口移除（RK-5） | §5.4、RK-5 | AC-22② / AC-9 | ✅ 已完成（跨插件 import 零：`jztools_<其他插件>` 全仓零命中；前端跨插件接口调用零；后端跨插件接口调用零；`jztools_admin.routes` 仅 admin 自身与测试/开发工具引用） |

---

## S3 · 可选：依赖组件包（主体瘦身）

| 编号 | 任务 | 落点 | 验收 | 状态 |
| --- | --- | --- | --- | --- |
| T25 | **PoC 与立项**：验证 C 扩展（`cv2` / `numpy`）在冻结环境下经 `sys.path` 注入的可行性（DLL 依赖链）；通过后新增「依赖组件包」形态（装到 `<程序目录>/runtime/pylibs/`，`manifest.json` 声明版本并与主清单合并展示） | §3.4 | 单独立项 | ◐ 第 0 步 ✅（pandas 排除）｜**第 1 步 ✅（依赖组件包）**：主包 123.8 → **38.7 MB**；PoC 通过（未装组件 → `--check-deps` 报 ModuleNotFoundError/退出码 1；装组件 → numpy/cv2 import ok）；插件侧提示已加（/status 的 missing_deps + 页面横幅）；✅ 已并入发布流程并完成真机演练（手册 §5 第 7/8 条覆盖"装组件转绿 / 未装组件提示不阻断"；验收 R 组 R-01~R-08 全绿） |
| T34 | **自动验收工具包（目标机无 Python）**：目标机通常不装 Python，而验收脚本是 .py → 用 PyInstaller 把 `acceptance-runner.py` 冻成单文件 exe（**保持一份实现**，不另写 PowerShell 版），连测试数据与双击入口打成一个 zip；脚本加"冻结根目录识别 / 程序目录自动探测 / testdata 缺省查找" | §11.2d、验收手册 §2.5 | AC-16 / AC-19 | ✅ 已完成：`tools\e2euild-acceptance-kit.ps1` → `deploy\JZToolsHub-验收测试-v20260921.zip`（8.8 MB，exe 内含 Python 运行时）｜实测：**PATH 剥掉 Python 后 exe 全组跑通**（27 通过）；R-06 对旧版主包（本机 v2.1.2 不认识 `--check-deps`）**安全失败**——报版本号并收掉被拉起的进程，不再超时 |
| T33 | **自动验收脚本**：把《验收手册》里可 HTTP 驱动的用例自动化（真实接口，不是"页面能打开"）→ `tools\e2ecceptance-runner.py`：27 条覆盖 F 功能 / R 依赖一致性 / S 安全 / X 边界并发；SKIP 项写明原因不假装通过；退出码 0/1 可接 CI；`--report` 出 markdown | §11.2d、验收手册 §2.5 | AC-16 / AC-19 | ✅ 已完成（实测 27 通过 / 0 失败 / 5 跳过；R-06 在装齐组件时"11 个模块真实 import 成功"；S-01 篡改包被 400 拒绝）｜过程中 15 个初跑失败全部是脚本对接口的假设错误（dry_run 默认 true、/filter 无 ok 字段、staged_id、预览返回 JSON、PDF 走 /raw 等），已按真实契约修正 |
| T32 | **交付验收手册**（总入口）：范围与通过标准、六组用例（D 部署 / F 功能 / R 依赖降级 / U 升级回滚 / S 安全权限 / X 稳定性边界）、记录表与结论模板；部署步骤引用既有手册不重复 → `docs/guide/验收手册.md` | §11.2d、验收手册 | AC-16 / AC-19 | ✅ 已完成（含 F 组 31 条主流程用例，逐条给"操作 → 预期"；配套测试数据包）｜顺带修**分发缺口**：base64/color-picker/json-formatter/map-marker/md5-generator 这 5 个纯前端插件未进任何包 → 已出包并重出插件集（**16 包**），测试数据补这 5 个工具的可粘贴样例与 md5 预期值 |
| T31 | **验收测试数据**（各插件测试文件 + 预生成二维码/视频 + 逐插件检查表）：验收需要贴近业务的输入，临时找文件不可复现也测不全（旧格式 .doc/.xls、缺依赖降级、多张码、纠错等）→ 新增 `tools\e2e\make-acceptance-testdata.py`（`--with-codes` 驱动运行中实例预生成二维码/视频；`--verify` 往返自校验）；产物 `deploy\JZToolsHub-验收测试数据-v<日期>.zip` | §11.2d、验收手册 §2 | AC-16 / AC-19 | ✅ 已完成（32 个文件/406 KB；自校验 3/3 通过：静态码与视频码还原后与原文件 **sha256 一致**、轨迹视频为合法 mp4）｜顺带修 trajectory-convert 视频编码器**无回退**（只试 avc1，缺 OpenH264 时静默产出坏文件）→ 候选 avc1→mp4v + 打开失败即报错，v1.1.5 |
| T30 | **一键扫描安装的逐项进度**：原先一次请求装完只有"安装中…"与最终汇总，看不到装到哪个了 → 改为前端逐项驱动（每项一次请求 + `auto_restart:false`），实时显示 `⬜ 待安装 / ⏳ 正在安装 / ✅ 已安装 / ❌ 失败 / ⏭ 未完成`、进度条与 `n/N`、失败原因与写入文件数，支持中途停止；插件含后端改动时装完统一重启 | §5.3、验收手册 §2.5 | AC-19 | ✅ 已完成（冻结 exe 实测：3 项逐项安装 0.7s/6.5s/0.4s，服务中途不重启，装完 `/api/admin/plugins/restart` 返回 `pending:[file-filter]`） |
| T29 | **重置回干净机（可重复验收）**：干净机装过一次后不再是干净环境（程序/数据/注册表/依赖组件/插件都在），第二轮会因残留而假绿 → 新增 `tools\e2e\reset-clean-machine.ps1`（全清 / 只清依赖 `-Scope Deps` / 只核查 `-VerifyOnly` / 演算 `-DryRun`；删除前列出数据根内容并提醒开发机不要全清；清完做干净度核查并给出退出码） | §11.2d、验收手册步骤 5 | AC-16 | ✅ 已完成（安全实测：假目录 + 临时数据根跑通全清路径，真实指针/注册表/快捷方式未被误动；`-VerifyOnly` 在本机正确报出 4 处真实残留） |
| T28 | **真机再排查（可选依赖 / 冻结标准库 / 免重启生效）+ 一键扫描安装**：① 插件 `*_AVAILABLE` 标记导入时冻结 + pylibs 不存在时 sys.path 未注入 → 装组件后仍显示"未安装"；② `llm_client.py` 裸 `import requests` → 缺组件时整插件加载失败；③ 主包未带组件所需标准库（`http.cookies` / `xml.dom`）→ 组件装了也 import 不了；④ 新增后台「一键扫描安装」 | §5.3、§11.2f | AC-16 / AC-19 | ✅ 已完成（I-17/I-18/I-19）：请求期刷新钩子 + 幂等可重入注入 + 保护性导入 + spec 补标准库（`--check-deps` 真实 import 11/11 通过）；一键扫描安装落地并实测"扫描 → 装 4 包 → 插件依赖不重启即翻转" |
| T27 | **依赖组件多组件共存排障（真机流程演练发现）**：① 组件安装器清空整个 `runtime/pylibs`（装 cv2 再装 numpy → cv2 被删）；② 自检探针写死 numpy/cv2（装非 cv2 组件必然假失败）；③ 组件包不声明组件间依赖（只装 cv2 的提示指向错误方向）；④ 构建工具：`vc_runtime` 写在打包前恒为空、载荷带构建机 `__pycache__`、`-Include` 配 `-LiteralPath` 失效删光载荷 | §3.4、§11.2e | AC-16 / AC-19 | ✅ 已完成（I-14/I-15/I-16）：按组件安装 + 合并登记 + `-Remove`；探针改取已装组件的 modules + `missing_requires`；`UNITS.requires`（cv2→numpy）写进两份清单；11 个组件包按新格式重出并跑通"只装 cv2 → 补 numpy → 共存 → 按组件卸载"全序列 |
| T26 | **依赖检测链路排障（出包复核时发现，按"干净机口径"走查判定链）**：① 清单记了构建机全量环境（93 个包）而非随包分发的 9 个 → 干净机上依赖被误判"已满足"；② 安装器检测不合并 `runtime/pylibs` → 装了组件仍报"缺失"；③ 后台面板白名单名未归一（PIL↔pillow）、组件包不逐项显示；④ `required` 分级在插件间不一致（同类依赖有的标必需 → 插件在目标机被整块隐藏） | §5.3、§11.2d | AC-16 / AC-19 / AC-21 | ✅ 已完成（I-11/I-12/I-13）：生成器改取 `_internal` dist-info（`scope=shipped`）+ 安装器合并组件清单并逐项打印 hint + 面板归一与组件标注 + 规范新增 **U-7a** 统一分级（8 个插件递增版本重出包）；`verify-package` 加断言"清单 ⊆ 随包分发"；沙箱夹具改从仓库 `plugins\` 取源 |

> 收益：主包 zip 有望从约 109 MB 降到约 40 MB 级（`cv2`+`numpy` 约 157 MB 只服务于 2 个插件）。
> 不阻塞 S0–S2；**前置条件是 T03/T04/T05 已落地**（没有声明与判定链，外置依赖即盲拆）。

---

## 回归要求（每阶段结束各跑一次，AC-12）

```powershell
python -m unittest test_plugin_admin test_admin_plugin_manager   # 含"仓库插件目录未被触碰"断言
python -m unittest test_plugin_templates                         # 插件模板补键
powershell -ExecutionPolicy Bypass -File tools\e2e\plugin-upgrade-sandbox-tests.ps1   # 77 项沙箱端到端
python tools\verify-package.py --smoke                            # 出包后：结构 + 隔离数据根冒烟
```

新增用例（S1 交付）：**"主体升级不动插件"**——升级前后对 15 个业务插件目录做递归 sha256 清单，逐字节一致（AC-2）。

---

## 文档同步链（每项完成后按 `HANDOFF.md` §10.1）

```
plugins/<id>/README.md（依赖清单 / 版本 / 是否需重启）
  → docs/design/主体与插件解耦-设计文档.md（改功能时顺手更新「最后核对」与实施记录）
  → docs/README.md（新增/改名文档时更新索引与计数）
  → README.md（§4.1 目录树、插件一览、体积表）
  → HANDOFF.md（状态头「最后更新」、§3 已完成、§4 待办、§6 已知问题）
  → .workbuddy/memory/YYYY-MM-DD.md（当日日志）
```

**改打包/安装链路时另需同步**（`HANDOFF.md` §10.1 第二段）：`build-deploy.ps1` ↔ `install.ps1` ↔
`tools/plugin-payload-rules.json` ↔ `docs/guide/离线部署包说明.md`（体积与组成数字）↔ `tools/verify-package.py`。

---

## 断点续开发清单（按序执行）

```
□  1. T01 依赖倒置（jz_api.py + 垫片）——它是 T03/T07/T11 的前置
□  2. T02 版本契约字段（plugin_api / api_version）
□  3. T04 已安装依赖清单 → T03 依赖声明 → T05 判定引擎
□  4. T08 C-10 交叉校验（出包工具）→ T06 添加期检测 → T07 后台展示
□  5. T09 门控 + 健壮性（blocked 不加载、坏 manifest 隔离）
□  6. T10 admin 定位调整 → T11 org API 提升主体（第一处修补）
□  7. T12 顺手清理 → 跑 S0 阶段验收（AC-5/6/9/11/13~21）+ 回归
□  8. T13 主包组装收窄 → T18 校验脚本收口 → T14 升级不动插件
□  9. T15 主体备份/回滚 → T16 插件集工具 → T17 -Set → T19 首装引导
□ 10. 跑 S1 阶段验收（AC-1~4/7/18）+ 回归；真机演练"主体升级不动插件"
□ 11. T21 第二处修补 → T22 C-11 拦截 → T23 后台卸载 → T24 收口验证
□ 12. 跑 S2 阶段验收（AC-8/10/22）+ 回归
□ 13. 规范回写：《插件设计规范.md》增 M-4 / U-7 / B-4 修订 / B-7 重申 / S-9（设计文档 §5.3 规范增量表）
□ 14. 文档同步链收尾（README / HANDOFF / docs/README / memory）
□ 15. （可选）T25 依赖组件包 PoC 立项
```


---

### T20 真机人工验收清单（自动化跑不到的项）

自动化：`python tools\e2e\decouple-acceptance.py`（沙箱内验证 AC-2/AC-3/AC-4/AC-7/AC-18，含递归 sha256 比对）。
以下必须在**真实目标机**上人工确认：

| # | 步骤 | 通过口径 |
| --- | --- | --- |
| 1 | `build-deploy.ps1` 出主包 → `python toolserify-package.py <zip> --smoke` | 全绿；zip 内 `plugins/` 仅 `admin/`（AC-1）；解压冒烟首页 200、匿名 /api/* 401 |
| 2 | 干净机器跑 `一键安装.bat` → 登录后台 | 装完提示"只有核心插件，请安装插件集"；后台「插件管理」列出 admin，依赖徽标正常（AC-17） |
| 3 | 解压插件集 → 双击 `安装插件集.bat` | 逐包校验并安装；已是最新版的跳过；结束提示"统一重启"；刷新首页出现工具卡片（AC-4） |
| 4 | 目标机**已装**若干业务插件（先手工放一份旧版本），再跑主包升级 | 控制台打印"目标机独有插件（源包不含，保持原样不动）"；升级后业务插件版本与内容不变、数据不变（AC-2/AC-3） |
| 5 | 升级后立即 `install.ps1 -Rollback` | 主体版本与文件回到升级前；业务插件与数据不受影响（AC-7） |
| 6 | 把 admin 单独升级到更高版本，再跑主包升级 | 跳过覆盖并打印提示；状态登记保持较高版本（AC-18①） |
| 7 | 后台「插件管理」尝试卸载 admin | 列表对 admin 显示"核心"标签、无卸载按钮；命令行 `-Uninstall admin` 被拒（AC-18②） |
| 8 | 断开网络，重复第 3 步 | 全流程离线可完成（D-4） |


---

### 真机缺陷修复（2026-09-20）

| 缺陷 | 根因 | 修复 | 状态 |
| --- | --- | --- | --- |
| 已装 LibreOffice 仍显示「降级」（假降级） | `jz_deps._probe_external()` 漏读插件配置里显式指定的 `office.soffice_path`/`pdf.soffice_path`（插件侧优先级最高的一档） | 新增 `_configured_soffice_paths()`，与 `office_render._configured_soffice()` 同口径并插入探测链第二档；实测插件与框架判定一致 | ✅ 已修（需重新出包后进入冻结实例） |
