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
| 当前实现状态 | 下方状态列（**唯一活清单**）。截至 2026-09-19：**S0 全部落地**、**S1 全部落地**（均沙箱实测）、**S2 的 T21/T22/T24 已完成**；**T20 自动化部分已就绪**（tools/e2e/decouple-acceptance.py，AC-2/3/4/7/18 全绿），真机人工项见文末清单；**T25 第 0 步已完成**（主包 123.8→102.5 MB），第 1 步 PoC 待拍板 |
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
| T04 | **已安装依赖清单**：`build-deploy.ps1` 生成 `config/installed-deps.json`（打包解释器的 `importlib.metadata` 快照：包名 + 版本） | §7 #13、§5.3 | AC-16 / AC-19 | ✅ 已完成 |
| T05 | **判定引擎 `jz_deps.py`**：读清单 + 插件 `requires` → 三态结论（`ok` / `degraded` / `blocked`）；版本比较器（仅 `>=x.y.z` 与精确，自实现，不引入 `packaging`）；供门控 / 后台 / `/api/tools` 三处复用 | §7 #21、§5.3 | AC-14 | ✅ 已完成 |
| T06 | **添加期检测（提示但不阻断）**：后台在 `inspect_package` 阶段返回缺失清单供弹窗（「仍然添加」/「取消」）；离线脚本在只读校验后打印缺失清单、**默认继续**，新增 `-SkipDepCheck` / `-StrictDeps` | §7 #17/#18、§5.3 | AC-20 | ✅ 已完成（脚本侧提示不阻断 + 后台上传时依赖弹窗「仍然添加/取消」；`inspect_package` 返回 dep_check） |
| T07 | **后台依赖展示**：插件列表新增「依赖需求」列（逐依赖徽标：满足绿色 / 不满足红色带 ×）+ 展开明细（声明要求 vs 实际版本）；新增「已安装依赖」区块（框架包 / 外部组件 / 插件自带）；顶部汇总横幅；接口增 `deps` 字段与 `GET /api/admin/deps`；`GET /api/<id>/status` 增 `deps` 段 | §7 #18/#19、§5.3 展示面 | AC-19 / AC-21 | ✅ 已完成 |
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
| T20 | **阶段验收**：跑通 AC-1~AC-4、AC-7、AC-18①（admin 单独升级后主体升级不回退） | §6、§10 | AC-12 | ◐ 自动化部分已就绪（tools/e2e/decouple-acceptance.py：AC-2/3/4/7/18 全绿）；AC-1 需重新出包后复跑；真机人工项见下方清单 |

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
| T25 | **PoC 与立项**：验证 C 扩展（`cv2` / `numpy`）在冻结环境下经 `sys.path` 注入的可行性（DLL 依赖链）；通过后新增「依赖组件包」形态（装到 `<程序目录>/runtime/pylibs/`，`manifest.json` 声明版本并与主清单合并展示） | §3.4 | 单独立项 | ◐ 第 0 步 ✅（pandas 排除）｜**第 1 步 ✅（依赖组件包）**：主包 123.8 → **38.7 MB**；PoC 通过（未装组件 → `--check-deps` 报 ModuleNotFoundError/退出码 1；装组件 → numpy/cv2 import ok）；插件侧提示已加（/status 的 missing_deps + 页面横幅）；待办：并入发布流程与真机演练 |

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
