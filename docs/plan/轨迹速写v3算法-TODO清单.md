# 轨迹速写 v3 算法落地 TODO 清单

| 文档属性 | 内容 |
| --- | --- |
| 日期 | 2026-09-29 |
| 依据 | `docs/design/轨迹速写v3算法-设计文档.md`（v1.0，口径已定稿 + 探针实测预测）+ `docs/design/轨迹速写插件-设计文档.md` §5（引擎解耦约束 D-1~D-7） |
| 用法 | 按批次顺序执行；每项含**任务 / 具体实现方案 / 预期效果与验收**；完成后在状态列打勾；**跨批次红线见文末，逐批自检** |


| 四段覆盖 | 落点 |
| --- | --- |
| 接口契约 | 批次 2 的注册签名（`run(dataset, params)`）与批次 3 的输出同构要求（键与 v2 完全一致） |
| 实现路径 | 改动面集中在 `plugins/trajectory-sketch/backend/engine/`（algorithms/v3/ 新目录 + params + algorithms/__init__）与 `config.template.json`；前端仅可能加算法选择器 |
| 当前实现状态 | **T1–T15 全部完成（2026-09-29，插件 v1.2.8）**；出包后整文移入 `docs/archive/` |
| 后续优化方向 | 本文即 v3 落地的唯一活清单；完成并出包后整文移入 `docs/archive/` |

> **覆盖范围**：v3 算法的全部实施步骤、验收标准与红线（唯一活清单）
> ｜**不覆盖**：算法口径与结果预测 → `docs/design/轨迹速写v3算法-设计文档.md`（§3/§5）
> ｜**随包**：否 ｜**最后核对**：2026-09-29

## 总览

| 批次 | 主题 | 改动面 | 依赖 |
| --- | --- | --- | --- |
| 1 | 参数层 + 纯函数核心（不注册） | `engine/params.py`、新建 `algorithms/v3/{windows,votes}.py` | 无 |
| 2 | 聚合层 + 编排注册 + 配置样例 | 新建 `algorithms/v3/{aggregate,__init__}.py`、`algorithms/__init__.py`、`config.template.json` | 批次 1 |
| 3 | 报告对接 + 质量区增补 + 前端选择器 | `algorithms/v3/aggregate.py`、`routes.py`（如需）、前端设置区 | 批次 2 |
| 4 | 验收对拍 + 版本与文档 | `selftest.py`、`manifest.json`、三份文档 | 批次 3 |

---

## 批次 1：参数层 + 纯函数核心

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T1 | `params["v3"]` 默认参数节 | `params.py` 增 `DEFAULT_V3`（window_minutes=5 / creep_kmh=10 / fast_kmh=150 / stay_minutes=15 / guard_seconds=60 / noise_floor_m=0 / merge_void_into_stay=true，注释同设计文档 §4.2）；`normalize()` 挂 `sect("v3", DEFAULT_V3)`；`_clamp` 夹取（窗宽 ≥1 分钟、速度 >0、守卫 ≥0、noise_floor ≥0）；`validate` 对 `fast_kmh <= creep_kmh` 出配置警告 | `normalize({"analysis": {"algo": "v3"}})` 返回含全默认 v3 节；非法值被夹取；v2 路径参数对象不受影响（新增键不改变 v2 判定读取） | ✅ |
| T2 | `algorithms/v3/windows.py` 窗口化 | `build_windows(dataset, params) -> {user: {w: WindowInfo}}`：按 `ts // (window_minutes*60)` 分窗；WindowInfo 含平均位置（窗内点簇质心均值）/ 首末簇质心 / 首末点时刻 / 点数 / 主地址（众数）；窗口表覆盖首末点之间的全部窗口含空窗（空窗存 None） | 给定构造点列，窗口边界与墙钟对齐；空窗存在；单点窗 first==last | ✅ |
| T3 | `algorithms/v3/votes.py` 三票 + 判定 | `own_vote(win, noise, guard)`（≤噪声→1；span<guard→2；否则 ÷实际间隔分带）；`pair_vote(a, b, wins, noise)`（两窗有数据 ÷窗宽；恰一窗空且其邻窗非空 → 单空窗桥接 ÷实际间隔；否则 0）；`classify(votes)`（全 0 / ≥2 个 4 / 3+4 计数 ≥2 / 只含 0·1 / 其余）；纯函数无 I/O | 对 §3.4/§3.5 的每条规则各写断言：噪声门限、60 秒守卫、桥接可达 101/202/010、跨 2 空窗不桥接、341→快速、441→极速、000→失联 | ✅ |
| T4 | 单元自检（先挂临时入口） | `engine/selftest.py` 增 `test_v3_votes`：构造微型窗口表直接调 votes 纯函数（不依赖注册）；用例覆盖上表全部分支 | selftest 全量 PASS 且新增用例 ≥8 条断言 | ✅ |

## 批次 2：聚合层 + 编排注册

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T5 | `algorithms/v3/aggregate.py` 位移锚定切分 | 按设计文档 §3.7：扫窗开段/闭段（锚点距离 ≤噪声延续；桥接空窗延续并计时；000 闭段累计 void）；≥`stay_minutes` 成停留段（代表位置=均值、条目时间取段内实际首末点时刻）；跨 void/短段且位置 ≤噪声的相邻停留合并（记断档注记）；相邻停留间提出行段（净位移=停留位置差，含未桥接空窗时注记"含无数据 N 分钟"）；首尾未成停留段的窗口按移动段口径输出 | 用探针场景构造数据：短途出行不被吞、跨夜停留合并、隧道窗状态无缝 | ✅ |
| T6 | `algorithms/v3/__init__.py` 编排 + 注册 | `run(dataset, params)`：底座（v2 adapter+cluster）→ build_windows → 三票 → classify → aggregate → 组装输出；`registry.register("v3", run)`；`algorithms/__init__.py` 增 `import v3`（本批完成才接入） | `registry.versions()` 含 "v3"；`analyze_rows(rows, algo="v3")` 可出结果；`available_algorithms()` 同步 | ✅ |
| T7 | 输出同构 | 返回键与 v2 一致（quality/stays/trips/report/clusters/cluster_stats/warnings）；stays/trips 字段名复用 v2（`_stays_json/_trips_json` 的字段口径），判定文案映射：停留段→"停留"、移动段按净位移 ≥ `high` 等效线（3×噪声）→"有效出行"否则"位置变动" | 前端与 Excel 导出不改即能消费（同构验证：同一渲染函数吃两算法输出） | ✅ |
| T8 | `config.template.json` 增 v3 节 | `analysis` 增 `"v3": { ...设计文档 §4.2 全键... }`，`"algo": "v2"` 保持缺省 | 新旧配置文件都能加载（normalize 幂等）；缺省行为不变 | ✅ |

## 批次 3：报告对接 + 质量区 + 前端

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T9 | 报告正文附录 | v3 的 `report.text_by_user` 在 v2 句式条目后追加：① 逐窗状态表（时间 / 状态 / 三票码，仅非静默窗全列 + 静默窗按小时抽样，防报告膨胀）；② 断档区间表（起止 / 窗数 / 是否已桥接）；附录开关 `params["v3"]["append_window_table"]`（默认 true） | 报告可读、不爆炸（31 天数据的附录行数可控行为有开关） | ✅ |
| T10 | quality 增补 | `quality` 增 `v3_状态分布`（五态计数）、`v3_断档区间数`、`v3_桥接次数`；数据质量 sheet 随既有管线透出 | 质量区可见断档与桥接统计 | ✅ |
| T11 | 前端算法选择确认 | 检查设置区是否已渲染 `/status` 返回的算法版本列表（`available_algorithms`）；若无则补下拉（v2/v3，缺省 v2），文案标注"v3：窗口状态栅格（实验）" | 页面可选 v3 并完成一次真实分析 | ✅ |

## 批次 4：验收对拍 + 版本与文档

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T12 | selftest 端到端用例 | `test_v3_end_to_end` 四例：① 静默抖动（全月静默为主、无出行）；② 通勤双址（600 米、9/23 类短途 → 聚合层必须出出行段，红线）；③ 单空窗隧道（行驶中闪断 1 窗 → 空窗判快速）；④ 多窗失联（≥2 空窗 → 000，出行段注记断档） | 四例全过；`analyze_rows(algo="v3")` 与 `algo="v2"` 互不干扰 | ✅ |
| T13 | 真实数据对拍设计文档 §5 | 两份 xlsx 跑 `analyze_rows(algo="v3")`，对拍 §5.1 分布（±2% 容差）与 §5.3 的 9/23 条目（停留 605min@卫健局、出行 649m×2、跨夜合并）；人工复核逐窗表 | 数字与设计文档 §5 一致或差异可解释并回写文档 | ✅ |
| T14 | v2 回归红线 | `algo` 缺省（v2）下 selftest 47 项全过 + 两份数据 v2 输出与 1.2.7 提交（202fafe）逐字节一致 | v2 行为零变化 | ✅ |
| T15 | 版本与文档同步 | `manifest.json` 1.2.7→1.2.8；插件 README 增 v3 小节（口径摘要 + 指针）；v2 设计文档 §5.1 提一句"v3 同底座另见 v3 设计文档"；`docs/README.md` 索引状态列更新；本清单状态列回写 | 文档与代码一致；出包另行按流程登记 `tools/plugin-packages.json` | ✅ |

---

## 批次 5：报告改版（2026-09-29 二次修订，需求方定稿）

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T16 | 报告改版：状态聚合叙述 | 删除【逐窗状态】/【断档统计】附录与 `append_window_table` 参数；`aggregate.build_timeline` 产出 `report_items`（同态连续区间一条，跨段边界同态移动合并，停留前后缘过渡窗拆出为移动叙述）；`v3/__init__` 以 `_state_report` 取代 v2 渲染层——五态命名句式，速度口径不变（净位移÷实际时间），跨断档停留加"其间失联"注记；`report.summary` 保留 v2 键名按状态语义映射（停留点数=静默段数等），另增极速/失联段数 | selftest 74/74（新增"报告为状态叙述且无逐窗附录"等 4 项断言）；9/23 报告实测：静默→缓行 576m→静默→缓行 649m→静默→失联→静默，时间轴无缝 | ✅ |

| T17 | 表达修订（需求方定稿） | 状态名 静止→**静默**、蠕动→**缓行**（`votes.STATE_NAMES`，报告/质量/摘要全链路生效）；失联条目仅报失联，删除"无定位上报"后缀；文档同步（设计文档 §2.1/§3/§5、README、本清单） | selftest 74/74；报告与 `v3_状态分布` 键值为新表达；历史决策引用（§1.2 原始要点）保持原文 | ✅ |

| T18 | 参数逐项说明写入插件 README | 需求方定稿：不单开文档。`plugins/trajectory-sketch/README.md` §6 增「分析参数逐项说明（分析阈值 · v2 / v3 分立）」：通用底座（clean/cluster/噪声）/ v2 判定（staypoint 半径组 + trip）/ v3 专属七参数，逐项给默认值、功能、调大/调小预期；另附「v3 会读到哪些 v2 参数」与「调参速查（症状 → 参数）」；两份设计文档挂指针 | README §6 即参数唯一真源；walk_speed_kmh 如实标注"预留未使用" | ✅ |

| T19 | 算法切换胶囊（需求方定稿） | 字段自检卡顶部「算法」胶囊（switch 形态，v2↔v3 分段高亮）为**唯一切换入口**，办案员/管理员均可点；新端点 `POST /algorithm` 仅写 `analysis.algo`（办案员即可，无管理员门禁）；设置面板撤算法下拉、`/config` POST 不再收 `analysis.algo`；上传响应带 `algorithms`；样式 `style.css`（.algo-switch）+ 资源版本 v=3/v=4 | 烟囱：切换 200 生效、未知算法 400、config POST 不再改 algo（另有 403 门禁）；selftest 74/74 | ✅ |

## 验收红线（跨批次）

1. **缺省不变**：`analysis.algo` 缺省 v2；v2 在全部既有用例下输出与 1.2.7 完全一致（幂等）。
2. **短途不吞**：v3 聚合层必须让 600 米级短途以出行段进报告（T12 例② / T13 对拍），这是 v3 立项的原始诉求。
3. **引擎纯净**：v3 只用标准库，不引入新依赖；不触碰其他插件（独立铁律）。
4. **失联如实**：桥接不掩盖断档事实（质量区必有断档区间列表）。
