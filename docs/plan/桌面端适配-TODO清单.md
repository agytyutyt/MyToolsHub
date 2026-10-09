# 桌面端适配 TODO 清单

| 文档属性 | 内容 |
| --- | --- |
| 日期 | 2026-10-03 |
| 依据 | `docs/eval/桌面端适配方案（GLM）.md` v3（三层拓扑 UI/能力/运行时 + 三类插件模型；GD1–GD3 已拍板，GD4/5/7/8 待批次 0 收口） |
| 用法 | 按批次顺序执行；批次 0 为**门控批次**（spike 结论决定后续路线）；每项含**任务 / 具体实现方案 / 预期效果与验收**；完成后在状态列打勾；**跨批次红线见文末，逐批自检** |


| 四段覆盖 | 落点 |
| --- | --- |
| 接口契约 | 批次 3 的路由分区表与会话中继口径、批次 4 的本地插件 manifest 契约（均引自方案 §6，本文不复制） |
| 实现路径 | 改动面：新建客户端工程（内核同源复用 + UI 层壳 + 能力层路由）+ `static/` 与 16 插件前端的流式改造 |
| 当前实现状态 | **批次 0 完成（T01–T05 ✅ 2026-10-03，spike 存档 `demo/desktop-spike/README.md`）**；批次 1–8b 主体完成（遗留见各批次）；**批次 9/10 已大量完成（剩真机/上架项）、批次 11 未开工** |
| 后续优化方向 | 本文即桌面端落地唯一活清单；完成并出包后整文移入 `docs/archive/` |

> **覆盖范围**：桌面客户端全部实施步骤（spike → 内核/UI/中继/插件四层建设 → 流式布局改造 → 验收出包）、门控关系与红线（唯一活清单）
> ｜**不覆盖**：架构口径与选型论证 → `docs/eval/桌面端适配方案（GLM）.md`（§1–§5）；插件包规范 → `docs/design/插件独立升级方案-设计文档.md` §4
> ｜**随包**：否 ｜**最后核对**：2026-10-03

## 总览

| 批次 | 主题 | 改动面 | 依赖 / 门控 |
| --- | --- | --- | --- |
| 0 | P0 spike ×4 + 决策收口 + 基线 | 仓库外 spike 工程；方案文档 §9 回写 | 无；**出口 = GD1 定案** |
| 1 | 宿主内核与客户端骨架（能力层） | 同源内核抽取、客户端 spec、路由分区、单实例、配置/探测/兜底 | 批次 0；T06/T07 门控 GD8 |
| 2 | UI 层（pywebview 壳） | 窗口/托盘/几何/导航锁定/桥/线程专项 | 批次 1 |
| 3 | 会话中继与清单合并 | 登录镜像、类 A 中继、清单合并、jz_relay 门面 | 批次 2；RP7 spike 结论 |
| 4 | 本地插件体系（类 B/C） | manifest 契约（含依赖版本槽）、进程内挂载、子进程执行器、本地插件管理页 | 批次 3；T21 门控 GD7（GD4 已拍板） |
| 5 | 流式布局改造——主界面 | `static/`（断点体系/网格/令牌/收缩鲁棒性） | 可与批次 1–3 并行 |
| 6 | 流式布局改造——插件层 | 16 插件前端统一响应式 + 出包 | 批次 5 |
| 7 | 验收与出包 | resize sweep、干净机器部署、样例插件端到端、手册增补 | 批次 4+6 |
| 8 | 插件商店 S1（O2 落地） | store 插件 + admin 商店管理面 + 客户端安装收口 + 依赖联合安装/出包/Source 抽象 | 设计已拍板（2026-10-05，`docs/design/桌面端插件商店-设计文档.md`）；T-O2-5 槽包部分依赖 O1 触发 |
| 9 | 存量插件适配——框架增补 + 三类试点 | F1 身份注入 / F2 依赖组件包补齐 / F3 resident 台架 / F4 出包工具增强（L-13 静态检查）/ F5 清单降级合并；试点 base64（B1）+ qr-video-decode（B2）+ file-filter（C） | **设计已定稿（2026-10-06，`docs/design/存量插件三类适配-设计文档.md` Q1–Q7 全拍板）；待排期** |
| 10 | 存量插件适配——B 类放量 + A 类上架 | B-light 4 个 + B-heavy 2 个（含懒加载改造）；A 类 4 个出包上架（knowledge-base/shared-docs/notice-board/case-report） | 批次 9 试点通过 |
| 11 | 存量插件适配——C 类收尾 + 全量验收 | trajectory-sketch/character-graph（Q5 过渡口径）+ A 类可选 worker 迁移（Q7 顺序）+ 全量验收（V1–V10） | 批次 10 |
| 12 | 2026-10-09 审计承接（D 类缺陷销项 + 上架/内核链收口 + 残留小项） | 主包与客户端出包链 / 商店上架链 / WebView2 内核链 / 文档与工具残留 | 无 |

---

## 批次 0：P0 spike + 决策收口（门控批次）

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T01 | RP1 spike：PyInstaller×pywebview 打包 | 仓库外最小工程：pywebview 窗口（WebView2/gui=edgechromium）+ pystray 托盘，PyInstaller onedir 打包；专验 pythonnet/clr-loader/WebView2Loader 的隐藏依赖收集 | **打包后双击能开窗且托盘在**；记录 hiddenimports 清单；失败 → 按方案 §4.4 转路线 C/F 并回写方案 §9/GD1 | ✅ 通过（2026-10-03）：Python 3.14 + pywebview 6.2.1 + pythonnet 3.2.0，`--collect-all webview` 单参数即成，onedir 41MB，开窗/托盘/桥/几何事件全过 |
| T02 | RP3 spike：OS 文件拖放 | 打包产物内实测拖文件进 WebView2（`AllowExternalDrop`）；同时验 125%/150% DPI 下 min_size 与几何记忆 | 拖放事件可达页面，或明确「不支持」→ 登记 UI 层「选择文件」兜底需求（批次 2 T11 一并做） | ✅ 部分通过：min_size 钳制与几何**重启恢复**实测通过；拖放=代码层默认开启（pywebview 未触碰该属性），**人工拖拽复核遗留批次 2 T11** |
| T03 | RP7 spike：会话中继原型 | 最小 Flask 中继：登录 POST 透传 + Set-Cookie 镜像、`/api/*` 头透传、**上传流式透传**（chunked，不全量缓冲）、大响应转发；基准用例=对真实主包跑登录→`/api/tools`→info-transfer 大文件上传→会话过期 401 | 登录/取数/过期重登全通；上传内存占用与文件大小解耦；结论回写方案 §9 | ✅ 通过：迷你远程 9/9（200MB 上传字节精确、中继 RSS 增量 0MB、100MB 下载精确）+ 真实主包 A 组 6/6；**教训：请求体须 spool 临时文件按确切 CL 转发**（直传 werkzeug 流 → requests 判未知长度转 chunked → 挂起）；真实主包上传端点 20MB 上限，大流量用例由迷你远程承载 |
| T04 | RP2 探测：WebView2 运行时 | 注册表/目录探测脚本（Evergreen 与 Fixed Version 两口径）；对目标机清单跑一遍摸底 | 产出缺机清单；缺机处理方案定案（随离线组件包补 Standalone 安装器 / 固定版随包 / 回落浏览器） | ✅ 脚本可用并本机实测（Evergreen 154.0.4258.53 FOUND）；**目标机摸底留批次 7 T31** |
| T05 | 决策收口 + 基线 | 拍板 GD4（worker 默认用完退出）、GD5（关窗=隐藏到托盘）、GD7（本地插件管理页装包）、GD8（同源内核双 spec）；spike 结论回写方案 §9/§10；主界面改造前基线截图（§5.4 极值尺寸各一张）存档 | 四项 GD 全部有结论并回写方案；基线图入库（批次 5/7 对拍用） | ✅ 基线 4 张落盘（`demo/desktop-spike/t05_baseline/`，截图直观印证 §5.2 刚性点）；GD4/5/7/8 标注「拟采纳，待需求方复核」；**新发现：`/api/session` 探针 404 已废弃，改 `GET /login`（方案 §7.2 与 T09 已修正）** |

## 批次 1：宿主内核与客户端骨架

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T06 | 宿主内核范围划定（GD8 已拍板） | 按方案 §7.3 三档口径：产出内核文件清单（正面 + 禁带：业务插件、admin、数据权威、`jz_llm`、共享盘升级链不进）+ `app.py` 机制函数最小抽取清单（行为保持，端侧差异一律走 `register_providers` 扩展点） | 内核清单评审通过；抽取后主包全量自检 + smoke **双绿**（红线 4） | ✅ 清单产出并**已冻结（2026-10-03 需求方评审通过）**；**批次 1 零抽取、主包零改动**（git diff 空），抽取推迟到消费批次 T19/T20 |
| T07 | 客户端出包链路 | 新建客户端 spec（onedir、`console=False`，复用主包 spec 模式 + T01 验证的 `--collect-all webview` 口径；**含子进程运行时目录 embeddable Python，GD4**）+ 出包脚本（`build-deploy-local.py` 模式另立，**不改主包命令真源**）+ 校验脚本（verify 模式另立：结构断言/禁带清单/`--smoke` 起服务探针）。**命名/版本/兜底策略走构建配置单源**（`tools/client/client-build-config.json`：产品名、exe 名、显示名、程序目录、数据目录、版本号、`webview2_fallback=fixed`（已定案）、`webview2_version`（100.0.1185.x 线最后补丁版）——spec、出包脚本、install.ps1、壳窗口标题统一从它读取）；fixed 目录获取=官方下载页 Fixed Version 下拉（100.x 可选性待核验，备选 Microsoft Update Catalog）解包入 `runtime\webview2\`，**删多余语言包（留 zh-CN/en-US）+ NTFS compact LZX 压缩**随出包脚本执行；**部署分件解耦**：程序包（exe+_internal+static+插件）/ 内核包（webview2 fixed 独立分件，先例=Chrome MSI/LibreOffice 离线组件）/ 组件包，install.ps1 按 `webview2_version` 探测已装则跳过——程序更新不重下内核；初值：exe `JZClient.exe`、版本 **0.0.1-preview**、数据目录 `%LOCALAPPDATA%\JZClient\` | 双击产物起能力层服务并监听 127.0.0.1 随机端口；产物含 `runtime\python.exe` 且可派生插件子进程；**改配置文件即可改名/改版本/换内核，出包链无硬编码**；100 代 fixed 目录实占 ≈180–250MB（裁语言包+LZX 后）；**程序包更新用例：覆盖程序目录后内核目录原样未动**；校验脚本红灯项与主包同风格 | ✅ 程序包分件落地（构建配置单源 + spec + build-client.py + verify-client.py），实际出包 **36.6MB**（批次 1 无 UI 层），结构断言与 smoke 全过；**遗留**：fixed 内核 100.x 可得性核验与内核分件、install.ps1（T31 前）；子进程运行时目录**已销项（2026-10-08）**：`tools/client/build-client-runtime.py` → `runtime/python`（embeddable cp314 + Flask 栈），verify-client 增「打包态 worker 往返」断言 |
| T08 | 能力层路由分区 | 按方案 §6.1 表实现四路裁决（本地直出/清单合并/中继远程/兜底）；`/api/local/<id>/` 挂载机制；127.0.0.1 随机空闲端口；**单实例互斥**（命名 mutex + 第二实例激活已有窗口） | 四路路由用例各 1 条过；二次启动激活已有窗口不开新进程 | ✅ 源码态+打包态全过（本地直出/中继透传 401→登录→200/兜底 503/`/api/local` 占位/单实例防重入退出）；**「第二实例激活已有窗口」随批次 2 T10 窗口接入**。补丁（2026-10-04，应无窗阶段运维反馈）：**`port.txt`/`client.pid` 落盘 + exe `--stop` 参数**（带镜像名校验防误杀）——当前端口不再靠翻追加式日志 |
| T09 | 配置/探测/兜底 | 客户端配置存 `%LOCALAPPDATA%\<客户端>\`（本地插件清单、客户端配置文件；不存凭据）。**远程地址以配置文件为准（2026-10-03 拍板）**：两层——构建配置给默认地址（出包定型）→ 安装时落地到客户端配置文件（可按机器改）→ 运行时读客户端配置文件，**无首启输入向导**；兜底页「改地址」= 编辑该文件后重连；健康探测用匿名探针（**`GET /login`；`/api/session` 当前版本 404 已废弃，2026-10-03 修正**）；本地兜底页（原因+改地址+重试+用浏览器打开） | 断网/错地址/慢响应三场景兜底页可达；改配置文件地址即重连；**装完即起，无首启向导** | ✅ 源码态+打包态全过（smoke：兜底页 503/health/写地址落盘 config.json）；探针口径 T03 已实测 |

#### T06 实施记录：宿主内核边界清单（2026-10-03 产出，**已冻结**）

**内核正面清单**（随客户端 spec 打包；GD8 三档复用方式）：

| 内核件 | 复用档位 | 实际启用批次 |
| --- | --- | --- |
| `jz_deps.py`（依赖三态判定） | 档1 直接 import | 批次 4 T19（requires 求值） |
| `jz_api.py`（门面；`register_providers`=端侧差异唯一扩展点） | 档1 直接 import | 批次 3 T14（会话提供者注册） |
| `jztools_data.py`（数据目录约定） | 档1 直接 import（客户端启动将自有数据目录设为 `JZTOOLS_DATA_ROOT`） | 批次 4 |
| `app.py` 机制函数 `_load_backend_module` / `register_plugin_backends`（**参数化改造**：插件目录与核心插件清单入参） | 档2 行为保持抽取 | **T19 前**（批次 4），抽取提交以主包自检+smoke 双绿背书 |
| `app.py` 机制函数 `_setup_dep_components` / `dep_components_status` | 档2 行为保持抽取 | **T20 前**（批次 4） |
| 静态路由（`/`、`/tool/<id>`、`/plugin/<id>/`、`/static`） | 档3 两端各写 | **批次 1 已实现**（`client/capability_app.py`，客户端组装含路由分区） |

**禁带清单**：`plugins/*`（业务插件与 admin）、`config/admin.json`、`jz_llm`、`tools/`（共享盘升级链）、数据根、`wheels/`。

**批次 1 决定：零抽取、主包零改动**（`git diff app.py jz_deps.py jz_api.py jztools_data.py` 为空）。依据：插件加载在 `__main__`（`app.py:1342`）而非模块级、装配层薄——抽取动作推迟到**消费它的批次**（T19/T20）执行，避免无消费方的提前重构；本批内核复用为「源码随包 + import 位预留」。客户端源码落在 `client/`（能力层装配/配置/单实例/兜底页），出包工具在 `tools/client/`。

**批次 1 验收步骤（2026-10-03 交付，供需求方复核）**：

| # | 步骤 | 预期 |
| --- | --- | --- |
| V1 | 纸面评审本清单（正面/禁带/档2 抽取推迟至 T19/T20） | 确认或改判；确认后清单状态改「已冻结」 |
| V2 | `git diff app.py jz_deps.py jz_api.py jztools_data.py` | 空输出（主包零改动红线） |
| V3 | `python tools/client/build-client.py` | `deploy/桌面端/JZClient/` 生成，~36MB，build-info.json 版本=0.0.1-preview |
| V4 | `python tools/client/verify-client.py` | 结构断言过（含禁带：无 plugins/ 目录） |
| V5 | `python tools/client/verify-client.py --smoke` | 临时目录启动→兜底页 503→health→写地址落盘→退出码 0 |
| V6 | 改 client-build-config.json 的 exe 名与版本 → 重出包 | 产物名/版本随之（单源验证），验完改回 |
| V7 | 起真实主包（`JZTOOLS_PORT=5001`）+ 客户端配置文件写地址 → 浏览器开 client.log 里的端口 | 主界面出现→登录→工具列表来自远程→类 A 插件页可用 |
| V8 | 停远程 → 刷新页面 | 兜底页 503；改地址/重试可用；再启远程恢复 |
| V9 | 再启动一个 JZClient.exe | 第二实例自动退出（防重入） |

> 通过标准：V1 确认 + V2–V9 全过 → 批次 1 验收通过，T06 清单冻结，批次 2 开工。
>
> **验收结论（2026-10-03）：V1 清单确认 + V2–V9 通过（含实测补丁：`port.txt`/`client.pid`/`--stop`；需求方以 YFJZ_Client 改名自出包验证了构建配置单源）——批次 1 验收通过，批次 2 解锁。**

## 批次 2：UI 层（pywebview 壳）

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T10 | 窗口与生命周期 | `webview.start()` 占主线程；`min_size` 720×560；几何记忆（resize/move 300ms 防抖落盘，重启恢复）；**关窗=隐藏到托盘（GD5 已拍板 A）+ 首次关窗托盘气泡提示**（核验 pywebview `closing` 事件可取消性，不可取消则 winforms FormClosing 兜底）；托盘菜单「打开窗口/用浏览器打开/退出」（复用 `app.py:1204` 口径），退出前保存几何+收割 worker | 三场景过：关窗→托盘→再开窗（页面状态不丢）；首次关窗有气泡；托盘退出无残余进程；重启恢复上次几何 | ✅ 落地+自动化验证：几何恢复源码态（900×620@100,80）与打包态（820×580@40,40）**精确恢复**；托盘启动/退出通道就绪；`closing` 可取消性经 pywebview 6.2.1 源码侦察证实（winforms `args.Cancel`）。**人工复核项：关窗→托盘→再开窗、首次关窗气泡、托盘菜单三项** |
| T11 | 导航锁定与拖放兜底 | 新导航/新窗仅放行 127.0.0.1；外链转系统浏览器；WebView2 用户数据目录指向客户端自有目录；T02 结论落地（拖放不可达则做「选择文件」兜底入口）；**Fixed Version 实测**：`WEBVIEW2_BROWSER_EXECUTABLE_FOLDER` 指向 100.0.1185.x 解包目录跑 T01 壳（开窗/桥/拖放全过）——确认 pywebview 6.2.1 与 100 代运行时兼容 | 伪造外链被拦；用户数据目录不落程序目录；拖放或兜底二选一可用；**壳在 100 代内核上功能完整** | ✅ 落地：外链/新窗=pywebview 默认转系统浏览器+页面侧拦截（shell-bridge）+`loaded` 守卫兜底（JS 跳转漂移）；用户数据目录=storage_path+private_mode=False **落客户端数据目录**；fixed 内核=pywebview 原生 `WEBVIEW2_RUNTIME_PATH` 已接构建配置。**遗留：100 代 fixed 包实测（待内核包可得性核验）、拖放人工复核** |
| T12 | 桥与结构类 | `static/js/shell-bridge.js`（页面侧适配器 <100 行，随主界面 static 下发）：探测壳并暴露 `window.jzShell`（`version`/`engine`/`openExternal` 三项，调用点全部判空）；壳注入 `html.jz-shell` 结构类 | 浏览器路径行为与现状一致（判空失效）；壳内「关于」可见引擎版本 | ✅ `static/js/shell-bridge.js`（≤60 行）+ index/tool 两处挂载（`?v=1`）+ `jz-shell` 注入 + 站外链接/新窗拦截；浏览器路径判空无感（源码态验证 import 面零污染）。**人工：站外链接点击实测** |
| T13 | 线程纪律专项（RP5） | 专项验收脚本+人工：UI 主线程×waitress×托盘×worker 四方并发；「worker 运行中关窗」「远程中断时再开窗」 | 全部场景无死锁/无残余进程；问题修完才进批次 3 | ◐ 自动化部分过：打包态启停×服务线程×UI 线程共存无死锁、`--stop`/托盘退出通道无残余；worker 场景等批次 4。**人工专项：关窗→托盘→再开窗、远程中断再开窗（并入下方复核清单）** |

#### 批次 2 实施记录（2026-10-03）

**pywebview 6.2.1 源码侦察四结论（实现依据）**：① `closing` 事件可取消（handler 返回 False → winforms `args.Cancel`，GD5 关窗隐藏的落点）；② `confirm_close=False` 关窗无确认框；③ `OPEN_EXTERNAL_LINKS_IN_BROWSER` 默认已把新窗转系统浏览器；④ `WEBVIEW2_RUNTIME_PATH` 原生支持 fixed 内核 + `storage_path`/`private_mode=False` 把 WebView2 profile 钉进客户端数据目录（会话跨重启持久）。

**出包**：54.4MB（+UI 层 pywebview/pystray/pillow）。**窗口契约实测**：几何恢复源码态与打包态均精确复原；最小 720×560（T01 spike 已验钳制）。

**人工复核清单（需求方实机，约 10 分钟）**：① 关窗 → 托盘 → 再开窗（页面状态不丢）；② 首次关窗气泡一次；③ 托盘菜单三项（打开窗口/用浏览器打开/退出，退出无残余进程）；④ 页面内点击站外链接 → 转系统浏览器；⑤ 文件拖放进窗口（T02 结论：默认开启）；⑥ 起主包→客户端用→停主包→兜底页→再恢复（批次 1 V7/V8 复跑）。

> **批次 2 验收结论（2026-10-04）：人工复核清单通过（含"停主包卡加载中"缺陷的修复复核）——批次 2 闭合，批次 3 解锁。**

**补丁（2026-10-04，应"停主包后卡加载中"反馈）**：v3 拓扑下主界面为本地直出，远程挂死时页面仍会加载、前端 API 卡「加载中」——兜底页原先只覆盖远程页面导航。修复：**导航探针门控**（`_remote_alive`：GET /login、2s 超时、5s 缓存）——主界面/工具页导航在探针失败时返回本地兜底页（503），远程恢复自动放行；`/tool/local-*` 不受门控（本地插件离线可用）；兜底页增「本地插件管理」入口。**教训入工艺**：出包组装必须与 dist 同源（--skip-build 曾把旧 dist 覆盖进 deploy 造成 Frankenstein 包）；**smoke 增加 UI 层存活断言**（能力层启动 3 秒后进程必须存活 + 日志无「UI 层异常退出」）——UI 层导入失败类缺陷自此必被 smoke 拦截。

## 批次 3：会话中继与清单合并

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T14 | 登录会话镜像 | 登录请求经中继 POST 远程；镜像会话（供中继与 jz_relay 用）+ **加密持久化到客户端自有目录**（类 C 升级触发能力层秒级重启后免重登，方案 §6.7）；401/过期统一信号 → 前端引导重登（对齐 admin 滑动续期口径） | 登录→取数→过期→重登全链路通；能力层重启后免重登；**只持久化会话 Cookie（加密），凭据不落盘** | ✅ 台架全过（T14a–e）：登录捕获/滑动更新/删除型 Set-Cookie 与 401 清除/DPAPI 持久化恢复（secure_store.py=crypt32） |
| T15 | 类 A 中继通道 | 其余 `/api/*` 与类 A `/plugin/<id>/*` 转发远程：请求/响应头透传、上传流式透传（T03 方案产品化）、大响应不全量缓冲 | 类 A 插件在客户端内全部可用（抽查 5 个含上传/下载/大表格）；与浏览器直连行为一致 | ✅ T03 spike 已验传输层（200MB 上传/100MB 下载/内存平坦）；本批并入了会话镜像维护，登录→取数→登出闭环台架全过 |
| T16 | 工具清单合并与本地直出 | `/api/tools` = 中继远程 ⊕ 本地注册表（`local-` 前缀；id 冲突拒装提示）；`/tool/<local-id>` 与 `/plugin/local-*/<entry>` 本地直出（工具外壳页用客户端自带 `tool.html`） | 主页网格混排两类入口可点开；断网时本地入口仍可开 | ✅ 台架全过（T16a–f）：远程+本地条目混排、categories 自动补「本地插件」类目、本地元数据/外壳页/前端直出（local_registry.py 字段与主包 get_aggregated_tools 同构） |
| T17 | jz_relay 门面 | 能力层提供 `jz_relay.get/post(框架级路径,…)`：自动附用户会话；**仅放行框架级 API 与数据接口**（白名单）；其他插件端点不可达；`server_data: true` 插件安装时明示「数据出机」。**白名单初版口径（2026-10-03 拍板）**：= 当前 HTML 版插件实际调用的框架级 API 面，**从现有代码调用关系机械提取**（方案 §6.3），不加新面、不删现用面 | 白名单外路径 403；**提取清单与 HTML 版实际调用面一致（抽查 3 个插件核对）**；类 C 样例经门面取数成功且权限=用户本人 | ✅ 台架全过（T17b–d）：白名单内 200（镜像会话）/白名单外 RelayError/写操作被 methods 限定排除；白名单固化 `client/relay_whitelist.json`（provenance 内置），`sys.modules["jz_relay"]` 注册 |

#### 批次 3 实施记录（2026-10-04）

- **白名单 provenance**：`client/relay_whitelist.json` 内置提取说明——grep `plugins/*/frontend+backend` 对 `/api/*` 的全部引用，剔除插件自有命名空间后仅剩 `/api/tools*` 与会话闭环（会话闭环由中继自身承载、不开放给插件）；`/api/org/tree` 为框架级只读数据接口纳入；`/api/llm/` 预留（主包当前无 HTTP 推理面）。
- **新增源码**：`client/{secure_store,session_mirror,local_registry,jz_relay}.py` + `relay_whitelist.json`（spec datas 随包，verify 结构断言覆盖）。
- **测试台架**：`demo/desktop-spike/b3_test.py`（Flask test_client 直驱能力层）+ `b3-data/local-plugins/local-demo` 假插件——**16/16 全过**。教训：测试台架的桩服务必须与真实服务端响应形态同构（mini_remote 曾因缺 categories 键导致误判）。
- **类 C 离线语义（2026-10-04 需求方明确）**：本意=简单逻辑本地化运算、完成后交由服务器做复杂/敏感操作；三档离线语义=类 A 不可用/类 B 完全可用/类 C 降级可用。jz_relay 已备 post()，操作提交型 POST 白名单规则随服务器侧端点真实提供时增补。
- 出包回归：54.4MB，结构断言 + smoke（含 UI 层存活断言）全绿。

## 批次 4：本地插件体系（类 B/C）

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T18 | 本地插件 manifest 契约与装包校验 | `runtime:"local"` + `kind: local/hybrid` + `entry{frontend,backend,mount}` + `requires`（同主包格式；**版本不符=三态判定提示**——多版本共存/版本化槽已评估列后续优化 O1，方案 §6.6，本期不做）；装包校验：哈希、`mount` 前缀冲突检查、`local-` 命名空间、禁 `shell=True` 口径（方案 §6.5） | 合规包装得上、四类违规包各 1 个被拒；依赖缺失/版本不符两类门控提示正确；契约已回写《插件设计规范.md》**§16 本地插件规范（L-1~L-12）** | ✅ 台架全过：四类违规包各 1 个被拒（非 zip/缺 manifest/非 local- id/mount 不合规）+ 合规包安装成功；契约与 requires 口径回写本清单 T18 行与方案 §6.4 |
| T19 | 类 C 进程内挂载 | `backend/routes.py` 以 `register(app)` 注册到能力层（与主包同构），端点统一挂 `mount` 前缀；重依赖走 `runtime/pylibs` 组件包注入（复用 `app.py:51` 口径，装完即生效） | 类 C 样例（进程内）页面+API 全通；缺依赖时按 requires 三态提示 | ✅ 台架全过：hybrid 子应用派发（进程内）ping/前端/jz_relay 全通；缺依赖 → blocked 503+缺失清单（打包态语义 installed-deps）；Blueprint mount 前缀由能力层挂载 |
| T20 | 子进程执行器（GD4 已拍板，保留） | **默认 spawn 源码包子进程**（`runtime\python.exe plugin_host.py`：注入共享 pylibs → 加载插件源码 → mini-WSGI 派发其 register(app) 路由）；entry 指向 exe 走例外通道；stdio-json-v1（request_id 并发对账、`timeout_ms` 超时即杀、stderr 归日志、大结果走临时文件）；默认用完退出，manifest 可声明常驻；**崩溃检测→错误回传（不炸页面）→常驻 worker 自动拉起→审计日志** | 源码与 exe 各一样例调用通；超时被杀且能力层不挂；并发 5 路不串号；常驻 worker 杀死后自动拉起；**热插拔 L3：升级后收割替换生效（方案 §6.7）** | ✅ 台架全过：源码 worker 子进程调用 200；超时即杀 → 504；其他 worker 不受影响；stderr 归 plugin-logs、审计落 plugin-audit.log。**打包态 worker 已销项（2026-10-08）**：`runtime/python` 运行时分件（`tools/client/build-client-runtime.py`）+ 构建配置 `paths.runtime_python_dir` + spec 补 `jz_plugin_host.py`→`kernel/` + verify-client smoke 增「打包态 worker 往返」断言（此前打包态 worker 因 exe 当不了脚本解释器整体哑火，源码态台架查不出来） |
| T21 | 本地插件管理页（GD7 已拍板 A+C） | `/local-admin` 本地页：装包/列表/卸载/启用停用 + **「检查共享盘更新」**（读本地插件索引，`plugin_admin.py:1095` 模式客户端化）；**商店适配四接缝随本任务落地（方案 §6.8）**：①索引=版本化 JSON 且与商店目录 schema 同构；②更新源可配置 `plugin_source`（共享盘/HTTP）；③安装收口为 `install(pkg_source)` 单入口；④索引带 `runtime` 类别中立字段；依赖三态提示与组件包安装入口；主页「本地插件管理」入口（仅桌面端可见，`jz-shell` 结构类控制） | 装卸全流程通；**共享盘检查更新→一键升级→L3 即时生效闭环**；热插拔验收：类 B 装/卸/升级即时生效，类 C 走既定口径（§6.7）；浏览器路径看不到该入口 | ✅ 台架全过：安装/卸载/启停/列表/检查更新接口 + local_admin.html 管理页 200 + 主页入口（jz-shell 门控）；热插拔：worker 全即时、hybrid 装卸即时+升级走重启口径（§6.7） |

#### 批次 4 实施记录（2026-10-04）

- **主包抽取（GD8 档2）**：新建 `jz_plugin_host.py`（setup_dep_components / dep_components_status / load_backend_module，BASE_DIR 与插件目录参数化）；`app.py` 改为薄包装。**主包启动回归绿**：`/`→302、`/login`→200、`/api/tools`→401、插件注册零失败；全量 `verify-package --smoke` 留待主包出包门（红线 4 时点）。
- **类 C 派发形态（实现关键决策）**：Flask 2.3+ 禁止首请求后 `register_blueprint`——进程内热挂载改用**子应用派发**（每个 hybrid 插件独立 Flask 子应用，能力层剥 `/api/local/<id>` 前缀派发），装卸/升级=字典操作，真热插拔（L1/L2/L3 全满足）；Blueprint 由能力层挂 mount 前缀，register(app) 形态做 mount 合规校验。
- **worker 细节**：默认剥规范前缀 `/api/local/<id>`；stderr 归 `plugin-logs/<id>.log`；审计 `plugin-audit.log`；超时即杀且不串扰其他 worker。
- **装包校验（T18）**：zip 完整性/路径安全（防 ../）/manifest/runtime/kind/id 前缀/mount 冲突/前端存在 六连断言；升级=覆盖替换。
- **管理页**：`client/local_admin.html`（列表/安装/卸载/启停/检查更新）+ `/local-api/plugins/*` 五接口 + 主页入口（`html.jz-shell` 门控，浏览器路径不可见）。
- **台架**：`demo/desktop-spike/b4_test.py` **21/21 全过**（含四类违规拒装、blocked 门控、超时 504、worker 隔离）。
- **遗留**：①~~打包态 worker 需 embeddable Python 随包（构建配置 `paths.runtime_python_dir`，T07 遗留项）~~ **已销项 2026-10-08**（运行时分件 `runtime\python` + 构建配置 `paths.runtime_python_dir` + smoke 断言）；②全量主包 smoke 留待主包出包门。

## 批次 5：流式布局改造——主界面（可与批次 1–3 并行）

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T22 | 断点体系与容器令牌 | `style.css` 末段重写：五档宽度（480/767/1023/1440+）+ `max-height:640px` 一档；`.content` LG 1200/XL 1440；内边距 24→16→12；`?v=` 递增 | 基线图对比：极值尺寸无横向滚动/裁切；600px 私有档废除 | ✅ 落地+三态截图实证（720×560/2560×640/1440×900 对比基线改善显著，横向滚动条消失） |
| T23 | 工具网格自适应 | 固定 4 列 → `repeat(auto-fill, minmax(240px,1fr))`；XS 强制 2 列、MD ≤3 列 | 1200px 仍 4 列、1440px 起 5–6 列、窄窗自然换列不挤压 | ✅ 1440 实测 5 列；720 实测 2 列可读 |
| T24 | 收缩鲁棒性 + 高度口径 | 横向 flex/grid 子项补 `min-width:0`；固定宽改 `flex:0 1 Xpx`+下限；`style.css:321` 等处 `100vh` 追加 `@supports (height:100dvh)` 增强 | 连续缩放无元素撑出视口；矮窗走内部滚动不裁切 | ✅ 主界面关键行 min-width:0 + 100dvh 增强落地 |
| T25 | 壳预留与剪贴板兜底 | 视口 <480×480 显「窗口过小」提示层（`jz-shell` 作用域 + shell-bridge 视口守卫）；剪贴板兜底：4 个内联脚本（base64/color-picker/json-formatter/md5）接入 `jzCopyText`（execCommand 兜底，jz-copy.js 同款实现内联），其余 8 处原有 fallback 守卫 | 过小窗口出提示不出破版；`http://<IP>` 直连时复制可用 | ✅ 落地 |

## 批次 6：流式布局改造——插件层

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T26 | 零断点插件补档（9 个） | admin（8 页+login）+ base64/color-picker/json-formatter/md5-generator/qr-video-decode/map-marker/character-graph/shared-docs，按统一模板补 1023/767/480 + 高度档（每插件约 40 行，自包含不跨插件引用） | 逐插件在极值尺寸可用；admin 后台表格横滚不破 | ✅ 16 插件统一档补齐（脚本批量+逐插件定制块）；抽检 notice-board 480 宽收敛正常 |
| T27 | 画布/地图专项 | `map-marker`：面板 300px→38%+min 260，≤767 上下分区，补 `invalidateSize()` 200ms 防抖；`character-graph`：侧栏 320px→chips/抽屉，100dvh 增强 | 两插件在缩放/旋转窗口中无白屏错位 | ✅ map-marker（面板收缩+≤767 上下分区；AMap 2.0 自带容器自适应，原 invalidateSize 口径按 Leaflet 语义、本插件无需）+ character-graph（dvh/侧栏 260/≤767 顶部横排） |
| T28 | B 类工作台 + 触控 | knowledge-base/trajectory-sketch/case-report/shared-docs/file-filter/notice-board 按方案 §5.3 档位收口；hover-only 补 click/focus、热区扩 44px | 六插件三档+矮窗全过；触屏可操作 | ✅ B 类 480 档补齐；主界面触控已修（悬浮球 click 开关 + cat-nav 767 档）；480 宽抽检通过 |
| T29 | 逐插件出包 | 每插件 `?v=` 递增（C-3）→ `tools/build-plugin-package.ps1` → C-1/C-3/C-7 校验 → 登记 `tools/plugin-packages.json`；主界面改动随客户端包发版 | 16 插件包全部登记；旧主程序上插件不因缺失文件报错 | ✅ **15/15 出包登记成功**（2026-10-04，版本 +0.0.1，C 系校验全过，登记见 tools/plugin-packages.json；admin 为核心插件随主包、不在插件包批次）。**主界面 static/admin.css/jz_plugin_host.py 随下次主包发版** |

#### 批次 5/6 实施记录（2026-10-04）

**批次 5（主界面）**：`style.css` 追加统一响应式层（XL 1440 放宽/内边距三档/高度 640/min-width:0/100dvh/`jz-shell` 过小守卫）；`.tool-grid` 改 auto-fill minmax(240px,1fr)；600px 私有档废除拆入 767/480；`shell-bridge.js` 增视口守卫（<480×480 置类显提示层）；版本戳 style.css v17、main.js v15。**三态截图实证**：720×560 两列可读、2560×640 无横滚、1440 五列（对比基线图 b5-verify/）。
**批次 6（插件层）**：16 插件（admin+15 业务）统一档补齐——外部 css 7 份追加、内联样式 8 处注入、character-graph ?v 递增；剪贴板兜底 `jz-copy.js` + 4 内联脚本接入（其余 8 处原有 fallback 守卫，实际缺口比盘点小）；主界面触控（悬浮球 click 开关，修正按钮 id=edit-fab）。**抽检**：notice-board 480 宽收敛正常（b6-notice-480.png）。
**T27 口径修正**：map-marker 用高德 AMap 2.0（自带容器尺寸自适应），原 invalidateSize 需求按 Leaflet 语义写就，本插件无需 JS 补丁。
**未做**：T29 逐插件出包（先 commit 再出包惯例；admin.css 引用无缓存戳的既有现状未改）。**后续完成（2026-10-05）**：T29 15/15 出包登记；**打包部署手册增补「出包体系总览（按 11 块映射）」**——块→出包链映射表、双端联动规则五条（红线 4 操作化）、频次速记。

## 批次 7：验收与出包

| # | 任务 | 具体实现方案 | 预期效果与验收 | 状态 |
| --- | --- | --- | --- | --- |
| T30 | 布局验收 | 按方案 §5.4：resize sweep（四边四角+最大化往返 3 次）、极值采样（720×560/640×1024/2560×640/1920×1080）、DPI 矩阵（100/125/150/200%）、边界用例（弹窗/拖卡/地图/上传中缩放）。**注：批次 0 基线图截于 Evergreen 154 内核；fixed 定版（100.0.1185.x）后须以目标内核重截基线作为对拍真源** | 全过且与目标内核基线图对比无回归 | ✅ resize sweep/极值采样/窄高 640×1024 均过（Evergreen 154；**fixed-100 定版后重截基线**） ☑ |
| T31 | 干净机器部署验收 | 按 `docs/guide/干净机器部署验收手册.md` 模式另立客户端清单：运行时自足、pylibs 组件包、**WebView2 fixed（100.0.1185.x）装载与内核/程序解耦验证（程序更新不触碰内核目录，T07）**、单实例、断网降级 | 干净机一次装成；程序包更新后内核目录未动、客户端正常启动 | ✅ 解耦语义实证（KERNEL_MARKER 保留）+ smoke；**真干净机验收留需求方** ☑ |
| T32 | 样例插件端到端 | 三类各一：类 A（现网插件经中继）、类 B-heavy（worker）、类 C（进程内+jz_relay 取数+LLM 中继）；验证跨类禁调用（白名单外 403）与断网行为 | 三样例全过；红线 1/2/6 逐条核验 | ✅ 类 A 真实端到端（登录/清单/公告板经中继，截图存档）；类 B/C 台架全过；LLM 中继预留（主包无 HTTP 推理面）；**类 C 离线语义已明确=降级可用**（本地简单运算照常、服务器复杂/敏感步骤降级，方案 §1.2） ☑ |
| T33 | 文档与手册回写 | `docs/guide/打包部署手册.md` 增客户端出包命令；`离线部署包说明.md` 增 WebView2 运行时组件（如适用）；`docs/README.md` 索引状态更新；方案文档 §9 风险表销项 | 文档与产物一致；索引三处（路由表/plan 表/eval 表）同步 | ✅ 手册/离线说明/README/方案 §9 已回写 ☑ |

---

## 批次 8：插件商店 S1（O2 落地，2026-10-05 拍板）

> 详细实现方案与验收标准见 `docs/design/桌面端插件商店-设计文档.md` §5/§6（本文不复制）；跨批次红线同文末六条，商店另加两条：数据契约单一写方（§3.4）/ 白名单正交（R2，商店不得放宽 jz_relay 面）。

| # | 任务 | 锚点 | 状态 |
| --- | --- | --- | --- |
| T-O2-0 | 拍板项收口 | 设计文档 §7 / §1.3 | ✅（14 项+需求⑧⑨⑩全落定；审核纪律暂按建议） |
| T-O2-1 | 数据契约冻结 + `数据根\store\` 落地（内核零改动 + 可选一行随迁） | §3.1 / §3.4 / §7 Q8 | ✅（Q8 拍板做：`_DATA_SUBDIRS` 追加 store，jztools_data.py 一行；主包启动回归绿） |
| T-O2-2 | store 插件（读面 + 全生命周期操作矩阵 + 合并清单隐藏已停用条目修正 + server 前端包本地伺服路由） | §3.2 / §3.5 / §3.5.1 | ✅（`plugins/store/`；CORE_PLUGINS 两处 + verify 断言 + tools.json 条目 grant_all；停用过滤修正随 `/api/tools` 合并落地；sidebar 商店入口激活 ?v2→v3） |
| T-O2-3 | admin 商店管理面（§3.7 状态机 + 台账 + 统计；部署运行面板=supervisor 控制接口薄客户端；E1 index-server.json 投影） | §3.3 / §3.7 / §3.9 | ✅（`store_admin.py` + `admin-store.html/js` + 7 端点；部署运行面板按**当前形态**=inspect/apply 链（重新部署/已部署版本/待重启徽标），supervisor 编排待 T-O2-11 接管；E1 随发布/下架同步投影） |
| T-O2-4 | 客户端安装收口（pkg_install 单入口 + preflight/install + 上游拉包；install 按 runtime 分派：local=完整插件 / server=前端包 / dep-slot=依赖包）——先行 | §3.5 / §3.5.1 / §4.3 | ✅（`client/pkg_install.py` 唯一链（本地上传同改走）；`/local-api/store/preflight\|install\|download`；server 前端包落 `server-frontends\` + 清单圈定本地伺服、停用/卸载回退中继；下载接口增 `artifact=frontend` 参数区分双包工件） |
| T-O2-5 | 依赖联合安装（组件包 .pending 语义；槽包部分依赖 O1 触发） | §3.5 | ✅（组件包部分：preflight 缺依赖→provides 映射依赖包→一键同装；纯 Python 立即生效/DLL 类留 `.pending` 下次启动生效；槽包部分随 O1 触发） |
| T-O2-6 | 本地包出包工具（JZClient-本地插件-*.zip + sha256 旁挂） | §4.5 | ✅（`tools/client/build-local-plugin-package.py`，实测过；含登记片段输出） |
| T-O2-7 | Source 抽象 + `plugin_source` 配置链（ServerSource 默认 / ShareSource 兜底） | §3.6 / §4.3 | ✅（`client/plugin_source.py`；`/local-api/config/plugin-source` GET/POST 免重启；旧构建配置 `plugin_store.index_path` 自动映射 share；check-update 经源抽象） |
| T-O2-8 | 验收（C1–C12，含红线 1/2/3/5/6 与商店特有两条） | §6 | ◐（台架 `tools/e2e/store-batch8-tests.py` 57 项全过：A 读面/B 状态机+占号+下架回退+E1+部署/C 六连校验/D 能力层端到端；C6 解耦 grep 干净；主包启动回归绿；**待人工**：真机浏览器路径走查、装机冒烟、干净机切源演示） |
| T-O2-9 | 服务器插件出包链增产前端包工件（与后端包同版本成对） | §3.5.1 | ✅（build-plugin-package.ps1 增产 `JZToolsHub-前端-*.zip` + frontend-manifest.json 文件清单 + sha256 旁挂；登记/索引/-Publish 同步带出） |
| T-O2-10 | jz_relay 白名单扩展：自身插件服务器端点（类 C 动态规则，id 去 local- 前缀映射） | §3.5.1 | ✅（`jz_relay.own(<id>)` 返回命名空间视图；白名单增 `own_namespace` 节（enabled 开关）；框架级面与其他插件端点不变） |
| T-O2-11 | 管理进程 supervisor（启动编排/健康探针/熔断拉起/升级停换起+回滚/孤儿收割/日志归集/控制接口/Windows 服务化；**独立安装包**） | `服务端插件运行架构-设计文档.md` §2 | ✅（批次 8b，2026-10-06：`supervisor/` 包——core（注册表+端口分配一次持久化+探针+双计数器熔断+Job Object）/apply_chain（停换起同端口+回滚，不打 restart_pending）/control_api（stdlib+令牌）/`__main__` 入口；install-supervisor.ps1（NSSM+服务恢复策略）；真机服务化验收留出包门） |
| T-O2-12 | worker 运行形态（自有 Flask 实例 + 会话自验 provider + 懒启动 + venv 拉起可选 + 主应用反代与统一 errorhandler） | `服务端插件运行架构-设计文档.md` §3–§4 | ✅（批次 8b，2026-10-06 收口：`jz_worker.py` 两级会话 provider（fail-closed 拍板）+ 主应用 `--worker` 分支/worker 化跳过/反代桥/统一 errorhandler（补课项）；**venv 建环境落地**——`venv_mgr.py`（--system-site-packages 基线随主包 + wheel 闭包离线安装 + marker 幂等重建），apply 自动 venv 化，出包链 `-VenvWheels` 参数，上架机器校验同源复用；**试点迁移演练通过**（`tools/e2e/worker-migration-demo.py`：真实主应用+真实 notice-board 全链 8 项——真实登录/反代/worker 会话回环取真实身份/隔离 503 主应用照常）。懒启动首期不做（拍板）。**剩真机项**：服务化安装（NSSM）+ 逐插件真机切换） |

### 批次 8 实施记录（2026-10-05，T-O2-0~10）

- **改动面**：`jztools_data.py`（一行）/ 新建 `plugins/store/`（manifest+backend+frontend 三件套）/ `plugins/admin/backend/{store_admin.py,routes.py,frontend/admin-store.html,frontend/admin-store.js}` / `client/{pkg_install.py,plugin_source.py,capability_app.py,jz_relay.py,relay_whitelist.json,local_admin.html}` / `tools/{build-deploy-local.py,verify-package.py,build-plugin-package.ps1}` / 新建 `tools/client/build-local-plugin-package.py`、`tools/e2e/store-batch8-tests.py` / `config/tools.json`（store 条目 grant_all）/ `static/js/sidebar.js`（商店入口激活，?v2→v3 共 11 处 HTML 同步）。
- **设计落定外的两个增量**：① 商店下载接口增 `artifact=frontend` 查询参数（server 条目双包工件选择，客户端装 server 条目取前端包必须）——已记入设计文档 §4.2；② `local_state` 停用状态改读磁盘最新配置（原读启动快照，停用后 rescan 不生效的隐性 bug 顺带修复）。
- **契约要点**：catalog schema 2 原样落地（versions[]/status/submit/review/history 台账）；flat 投影 = 每 id 最新 published，附加字段 icon/deps_mode/provides/frontend_*（向后兼容，老客户端忽略）；E1 index-server.json 仅 server 条目 published 版本。
- **遗留**：T-O2-11/12（supervisor/worker）；真机人工项见 T-O2-8 ◐ 注记；商店页暂未在主包 config.template 层面做模板同步测试（tools.json 经 merge-tools 链随主包发版合并，出包门验证）。

### 批次 8b 实施记录（2026-10-06，T-O2-11/12 主体）

- **改动面**：新建 `supervisor/`（core / apply_chain / control_api / `__main__` / mainapp_bridge / install）+ `jz_worker.py`；`app.py`（`--worker` 分支、worker 化跳过进程内加载、桥安装于 register_plugin_backends 之后）；`JZToolsHub.spec`（hiddenimports：jz_worker + supervisor.*）；`store_admin.deploy_backend_package` 双形态分派（worker 化→控制接口 apply，未 worker 化→既有进程内链）；台架 `tools/e2e/supervisor-tests.py`（36 项全过：端口持久化/两级会话/熔断/apply 回滚/控制接口/反代/C14 全场景）。
- **台架抓出的四个真 bug（均已修）**：① apply 编排与监控线程打架——停换起窗口的探针失败被误熔断，加 `_applying` 抑制集；② 监控把人工 stop 计入崩溃连击（proc=None ≠ 死亡）——未托管单元不探针不计数；③ 同一具死进程在重拉失败时被重复计数——记账后即摘出 `_procs`；④ 端口占用探测用 bind，Windows TIME_WAIT 残留误报占用——改 connect 探测（只认真实监听者）。
- **两个设计级收获**：熔断需**双计数器**（探针连败 + 快速崩溃连击——路由触发型毒插件每次重启后探针正常，单一计数器永远到不了阈值，已回写设计文档 §2）；apply 顺序必须是「备份→停旧→验端口→替换→起」（先验端口会被自己的 worker 占住）。
- **遗留**：venv 建环境（wheel 清单闭包安装，T-O2-12c 后半）；试点插件真机迁移（迁移流程：注册表加单元 → 重启主应用 → 验证反代 → 逐个切）；supervisor 独立安装包真机服务化验收（NSSM 脚本已备）；懒启动（拍板首期不做）。
- **同日收口（批次 8b 续）**：①venv 建环境全链落地——`venv_mgr.py`（`--system-site-packages` 基线随主包、wheel 绝对路径离线安装、marker 幂等重建）+ apply 自动 venv 化（`deps_mode=venv` 包→建环境→单元切 venv 解释器）+ `validate_venv_node` 上架校验同源复用 + 出包链 `-VenvWheels`；台架 D5 组（6 项：venv apply/单元登记/健康/业务 API/私有依赖 dbgmod 生效/wheel sha 不符拒装旧版照常），**实施修正：wheel tag 校验须按「末三段」解析且分两类放行（cp314*-cp314*-win_amd64 C 扩展 / py3-none-any 纯 Python）——整名匹配与 "none" 一刀切都会误杀（已回写 §4）**。②试点迁移演练 `tools/e2e/worker-migration-demo.py` 8 项全过——真实主应用 + 真实 notice-board（注册表加单元→重启跳过进程内→supervisor 拉起→真实登录→反代访问公告板 API（worker 回环取**真实** admin 身份 dict）→停 worker 该插件 503 而主应用/登录态/工具清单照常）。③`tools/build-supervisor-package.ps1` 分件组装实测通过（supervisor/ + jz_worker.py + 安装脚本 + 注册表模板）。

## 批次 9–11：存量插件适配（设计已定稿；**批次 9/10 已大量完成（剩真机/上架项）、批次 11 未开工**，2026-10-06 开工）

### 批次 10 实施记录（2026-10-06，提交 89367f2）

| 项 | 状态 |
| --- | --- |
| B-light 4 个本地包 | ✅ 派生出包（color-picker/json-formatter/md5-generator/map-marker，零 api 引用） |
| B-heavy 2 个懒加载改造 | ✅ info-transfer/trajectory-convert cv2/numpy → 真值哨兵 "lazy" + _load_cv2/_load_numpy 闸门首用装载；/status 旗标改 jz_deps 判定数据源（**发现 None 哨兵会被 jz_deps 刷新器请求期探测清零——哨兵必须真值**）；实测请求期零装载 |
| B-heavy 2 个本地包 | ✅ 派生出包（info-transfer 改写 17 处 / trajectory-convert 7 处，resident+timeout） |
| A 类 4 个双包重打 | ✅ -Force 补前端工件（knowledge-base/shared-docs/notice-board/case-report，C-13 全过——**顺带落地注释剥离**：notice-board 解耦迁移说明里的历史路径不算违规） |
| 待办 | 真机项：admin 商店管理面上架（local 包×7 + 服务器双包）→ 装包走查 → 批次 11（C 类收尾 + worker 迁移 Q7 顺序） |

### 客户端重新出包（2026-10-07，提交 7043c2d）

- **触发**：批次 9/10 改动（F1 身份注入/F5 降级合并/查询串透传/executor 崩溃修复/同源内核 jz_api 兜底）尚未进冻结包。
- **过程抓到环境漂移**：首次构建 smoke 失败——`ModuleNotFoundError: webview`（pywebview/pythonnet 从构建环境消失，PyInstaller 静默少收集；批次 2 补丁③ UI 存活断言按设计拦截）。修复=按批次 0 定案版本装回（pywebview==6.2.1 + pythonnet==3.2.0；**本机代理 7890 宕，pip 走直连**）。
- **加固**：build-client.py 增出包前置断言（webview/pythonnet/pystray 缺失即拒打，失败从 smoke 前移到打包前，7043c2d）。
- **产物**：`deploy/桌面端/YFJZ_Client` 85.3 MB；结构断言 9 项过；smoke 通过（兜底页/本地接口/配置落盘）；F2 依赖清单随包（9 包，含 pywebview）；runtime\ 子树保留（内核/程序解耦）。
- **待真机**：Windows 实机开窗/托盘/拖拽人工复核 + 批次 9/10 的商店上架与装包走查。

### 主包 v2.3.23 出包（2026-10-07）——上架前置

- **触发**：真机验证发现桌面端「插件商店」点击报「工具不存在或未启用」→ 服务器跑的是 9 月 v2.3.22 部署包（store 是批次 8 才进主包的）。
- **产物**：deploy/服务端/JZToolsHub **v2.3.23**（增量组装：覆盖 544/新增 20；plugins/ 含 admin+store；tools.json store 条目 grant_all；含批次 9/10 的 jz_api 内核改动与 file-filter v1.6.0 源）；**verify-package --smoke 全绿**（启动/登录页/匿名 401/数据根隔离）。
- **待目标机**：按打包部署手册覆盖部署 → 重启 → 验证侧栏「插件商店」可开 → 商店管理面上架三试点与依赖组件包 → 装包走查。

> 权威设计：`docs/design/存量插件三类适配-设计文档.md`（2026-10-06 定稿，Q1–Q7 全部拍板）。分类 A×6/B-light×5/B-heavy×3/C×3、四条改造模式（M-A/M-B1/M-B2/M-C）、逐插件方案（§3）、框架增补 F1–F6（§2.5）、验收 V1–V10（§5）均以该文为准——本节只登记批次骨架，任务分解随排期展开。后续优化已列 O3–O6。

| 批次 | 任务骨架（任务号排期时分配） | 设计依据 |
| --- | --- | --- |
| 9 | F1 身份注入（provider + X-JZ-User）；F2 组件包盘点补齐（qrcode/olefile 等）；F3 resident 台架用例；F4 出包工具增强（-From 派生 / --api-rewrite / L-13 静态检查+服务器出包链扫描）；F5 清单降级合并；试点三类各一：base64（M-B1）+ qr-video-decode（M-B2）+ file-filter（M-C 全套，含服务器增补端点与客户端工件）；试点中目标机复测 worker 内存定值（Q6） | §2.5 / §3.7 / §3.14 / §3.15 / §5 V6/V8/V9 |
| 10 | B-light 4 个（color-picker/json-formatter/md5-generator/map-marker）+ B-heavy 2 个（info-transfer/trajectory-convert，含懒加载改造）出包上架；A 类 4 个出包上架（knowledge-base/shared-docs/notice-board/case-report）；B-light 双条目并存口径落地（显示名后缀/浏览器降权显示） | §3.3–§3.6 / §3.8–§3.13 / §5 V2/V3/V7 |
| 11 | C 类收尾：trajectory-sketch、character-graph 拆分（Q5：file-filter 试点已先行，此二者按过渡口径处理）；A 类可选 worker 迁移按 Q7 顺序（knowledge-base → shared-docs → case-report）；全量验收 V1–V10 + 跨批次红线复检 | §3.16–§3.17 / §4 / §5 |
| ↳ | **trajectory-sketch 拆分 ✅（2026-10-09）**：服务器增补 `POST /api/trajectory-sketch/llm/match-columns`（无状态·不落库）+ `/status` 增 `llm_configured`（客户端网关探测用）；客户端工件 `client-plugins/local-trajectory-sketch/`（llm_client=own() 网关、config=服务器权威+本地缓存兜底、保存透传服务器、适配脚本 tools/e2e/adapt-ts-client.py）+ 本地 manifest（hybrid/mount=`/api/local/local-trajectory-sketch`/server_data，requires=openpyxl/xlrd）；双包出包 **v1.3.0**（服务器后端+前端成对 + 本地包，`-FrontendFrom`+`--api-rewrite` 改写 3 处零残留）并装客户端实测：上传自检 → **hard 档分析 done**（停留点结果正常）→ **llm 档明示**「需先配置大模型/改用硬过滤」（不静默）。顺带加固：本地包漏开 `--api-rewrite` 且前端含 `api/<base>` 引用 → 出包即红（此前仅打印计数，人工易漏）。**剩**：上架（本地包 + server 双包）+ llm 联网实测（需先在管理后台配模型） | 本行 |

### 批次 9 任务分解（2026-10-06 开工）

| # | 任务 | 内容 | 状态 |
| --- | --- | --- | --- |
| T-O9-1 | F4 出包工具增强 | `build-local-plugin-package.py`：`--derive-local`（manifest 变体派生，entry 字符串/对象双形态兼容）、`--api-rewrite`（幂等改写+边界断言）、L-13 静态检查（5 类禁用模式逐行定位+跨插件端点+改写计数核对）、`-FrontendFrom`；sha256 旁挂/登记片段沿用既有。**✅（2026-10-06）**：base64/qr-video-decode 两试点包实测通过（B1 manifest 变体正确、B2 resident/timeout/backend 入口正确、改写 3 处零残留）；负路径 4 类违规全红。**遗留：服务器出包链 C 系同款扫描（`build-plugin-package.ps1`）未做，随 T-O9-8 前补** | ✅（遗留 1 子项） |
| T-O9-2 | 试点 M-B1：base64 | **出包 ✅**（`deploy/插件包/本地插件/JZClient-本地插件-local-base64-v1.0.1.zip`，3.1KB，零 api 引用） | 出包完成；**装包走查待客户端台架** |
| T-O9-3 | 试点 M-B2：qr-video-decode | **出包 ✅**（`…local-qr-video-decode-v1.1.5.zip`，resident=true/timeout=600000/entry.backend 正确，改写 3 处零残留，requires=flask+zfec 原样） | 出包完成；**zfec dep-slot 组件包联动待 T-O9-6；装包/任务轮询走查待台架** |
| T-O9-4 | F1 身份注入 | capability_app：镜像钩子（更新/清除→身份失效）+ /api/session 惰性缓存（TTL 300s）+ 派发链 X-JZ-User 头 + jz_api.register_providers（hybrid）；根 jz_api（同源内核，GD8）get_session_user 增无 provider 时请求上下文读头兜底（懒 import flask，服务器 provider 路径不变）。**✅（2026-10-06）台架 `tools/e2e/identity-inject-tests.py` 10/10**：匿名/登录双链（provider+头）/登出清空/provider 优先 | ✅ |
| T-O9-5 | F5 清单降级合并 | api_tools_merged 两处 502 兜底 → `_local_only_tools_response`（200 仅本地条目 + degraded 标记 + 仅本地分类，停用过滤照旧）。**✅（2026-10-06）台架 5a–5e**：死端口 200/仅本地出格/本地元数据可取/停用隐藏。main.js **无需改动**（200 走正常渲染路径，site 字段显式判空） | ✅ |
| T-O9-6 | F2 依赖组件包盘点 | **✅（2026-10-06）盘点结论：零缺项**——B/C 插件 requires 全集（cv2/numpy/zfec/zxingcpp/qrcode/olefile/openpyxl/xlrd/docx/pypdf/requests）在 `deploy/依赖包/` 全部有组件包且版本满足各插件下限；flask 走客户端运行时判定。**盘点抓出真缺口并修复**：客户端包内无 installed-deps.json → B-heavy 的 flask（required:true）三态判定误报缺失——build-client.py 接入同源 `gen-installed-deps.py --internal <客户端 _internal>`（产物落 kernel/config/，服务器 spec 白名单警告按「产物可解析」判据兼容），**现有 dist 已补生成 + evaluate 探针验证 flask→ok(3.1.3)、zfec 未装组件时正确 degraded**。偏差记录：客户端 _internal 无 requests/waitress dist-info（客户端工件 requires 不含 requests，无影响） | ✅ |
| T-O9-7 | F3 resident 台架用例 | **✅（2026-10-06）台架 `tools/e2e/resident-tests.py` 9/9**：resident 状态跨请求存活+同进程复用 / 非 resident 每请求新进程 / 超时 504+击杀后恢复 / 崩溃 502+自动拉起（状态重置）/ 查询串透传。**顺带修两处真缺陷**：①本地派发丢查询串（worker_path 只取 request.path，file-filter 前端 `mappings?limit=` 在用）→ 补 query_string 透传；②executor 崩溃后常驻表不弹出——Windows 下子进程句柄状态滞后（管道 EOF 先到、poll() 迟报退出码），死进程被复用连环 502 → 崩溃路径对齐超时路径同步弹出。**台架隔离坑**：local_state 经 client_config 落真实用户目录（load_client_config 不吃 data_dir 参数）跨运行残留——两台架已用官方 `JZCLIENT_DATA_DIR` 覆盖口隔离，误写目录已清 | ✅ |
| T-O9-8 | 试点 M-C：file-filter 全套 | **阶段 1+2 ✅（2026-10-06）**：①前置 PS1 侧 L-13 扫描落地（build-plugin-package.ps1 新增 C-13 段，单引号 PS 串规避转义坍缩，桩验证 4/4 违规全红+base64/file-filter 真插件过；**教训：ps1 中文必须 UTF-8 BOM（批次 8b 已知）+ 双引号串内正则字符类转义不可靠**）；②服务器工件两端点已入 routes.py 并冒烟过——`POST /mappings/lookup`（批量查表 confirmed/suggested/remap，键归一化全在服务器）、`POST /llm/match-columns`（无状态 LLM 匹配+record_suggestions 落库+valid_targets/is_rejected 预过滤，Key 不出服务器）。**阶段 3 ✅（出包完成）**：客户端工件落地——`client-plugins/local-file-filter/`（manifest hybrid/mount/server_data=true/requires 仅 openpyxl+xlrd；backend=core+bg_image 同源副本 + **三个 relay 垫片**（llm_gateway=jz_llm 网关/mapping_store=查表垫片/llm_client=匹配垫片）+ routes.py 适配拷贝（adapt-ff-client.py 补丁：load_config/save_config→own 代理、10 个 mappings 管理端点→_passthrough 透明转发、过滤流本地化））；出包 `-FrontendFrom`+`--api-rewrite`（4 处改写零残留）。**顺带修真约定偏差**：entry.backend 按 pkg_install 六连口径应为**末段文件名**（routes.py）而非 GLM 草图的 backend/routes.py——工具推导/校验、file-filter manifest、两台架假插件已同步修正，qr-video-decode 包已重出。**待办**：混合装包冒烟台架（本地 status/config + 透传 + 过滤流离线降级）、双工件上架 | **阶段 4 ✅（2026-10-06）冒烟台架 `tools/e2e/file-filter-c-tests.py` 11/11**：本地 status/config（own 代理）+ mappings 透传 + 过滤全链（confirmed 档 relay 查表命中/exact 本地/LLM 建议删除）+ **敏感数据零出机**（stub 全量请求体无单元内容，V5 取证）+ **离线降级**（mode=llm 任务内 LLM 段失败 → 自动回退 hard 重跑并明示 degraded 原因）。**顺带修两处框架真缺陷**：①jz_relay.request 硬编码 timeout 不许覆盖 → kw.pop 缺省；②**jz_plugin_host 热升级缓存缺陷**——load_backend_module 的子模块 import_module 命中 sys.modules 旧缓存，升级后跑旧代码 → 装载前清同名父/子模块。**批次 9 台架全绿（15/15+9/9+11/11）**；包已按降级修复重出。剩：三试点双工件上架（admin 商店管理面操作）+ 真机装包走查 → T-O9-9 验收销项 | **✅（2026-10-06，提交 20d31bd+ab2bc4a）**：代码/文档/台架已提交；双工件包全部就绪——file-filter v1.6.0（后端+前端成对，C-13 真链首过）+ base64 v1.0.1（-Force 重打补前端工件）+ 三个本地包（deploy/插件包/本地插件/）。qr-video-decode 服务器版无改动（1.1.5 既有包沿用）。**剩真机项**：目标机/部署服务器上经 admin 商店管理面上架三试点（local 包×3 + file-filter 双包）→ 发布即部署 → 真机装包走查（含离线降级/数据不出机抽验）→ 销项 | ✅（剩真机项） |
| T-O9-9 | 批次 9 验收 | 试点三件按 V2/V4/V6/V8/V9 抽验 + 台架回归 + 回写实施记录 | 待办 |

**跨批次纪律**：既有红线 1–6 全部适用（红线 2 为过渡期口径，退役规划见 O6）；适配设计文档 §5 V1–V10 为批次验收真源；F1–F5 先于一切插件适配（F4 是 B 类唯一卡点，F1 是 B-heavy 任务归属与 C 类会话取数前置）。

---

## 批次 12：2026-10-09 审计承接

> 依据：`docs/archive/20261009规划完成度核对与未完成项汇总.md`（§3.1/§3.2/§3.4/§6）；X-1~X-7 逐条登记，状态如实。

| # | 任务 | 内容 | 状态 |
| --- | --- | --- | --- |
| X-1 | D-1 出包白名单缺 store | `build-deploy.ps1` 核心插件白名单缺 store（静默产出无商店残包），与 `build-deploy-local.py`/`verify-package.py` 口径不一 | ✅ 已完成（commit `7e4083e`）；端到端验证待下次整包 |
| X-2 | D-2 B-heavy 本地包丢 timeout_ms | 重出包时漏传 `-TimeoutMs` → 能力层超时上限回落 60s | ✅ 已完成（commit `53bab50`）；三插件升补丁号重出：info-transfer 1.2.10 / qr-video-decode 1.1.7 / trajectory-convert 1.2.1<br>2026-10-09 后续：info-transfer→1.2.11、qr→1.2.0 已重出（含服务器包），待上架（Q3） |
| X-3 | D-8 store 无 README | store 插件缺 README（接口唯一真源）；出包链对 README 缺失只 Warn | ✅ 已完成（commit `95c219d`）：README 门禁升格为「必须/Die」+ 上架机器校验兜底；store 登记 `8e14e5c` |
| X-4 | C1 迁移上移 | 插件历史 LLM 迁移上移主体 `app.py`（代码质量报告 C1 整改） | ✅ 已完成（commit `747412d`：app.py 落点 + `llm_migrated.flag` 退场标记；删除窗口=flag 全覆盖后，已写入代码注释） |
| X-5 | D-4 商店上架链收口 | 含 C 类服务器工件、trajectory-sketch 新条目、catalog 数据修复（S13） | 未开始（本轮已排期）；2026-10-09：本地包 10 个已重出（含 README）待上架；catalog 修复待 Q4 |
| X-6 | D-5 WebView2 fixed 内核链 | 备选阶梯 100.x→109.x→92.x（2026-10-09 拍板） | 未开始（本轮已排期） |
| X-7-1 | 残留小项① | admin manifest 版本递增（admin 代码 `747412d` 有改动） | 未开始 |
| X-7-2 | 残留小项② | `tools/e2e/plugin-upgrade-sandbox-tests.ps1` 依赖旧路径 `deploy\JZToolsHub\version.json`（现为 `deploy\服务端\JZToolsHub`）需修 | 未开始 |
| X-7-3 | 残留小项③ | 本地派生包未拷 README（上架告警将长期误报） | 未开始 |
| X-7-4 | 残留小项④ | `tools/client/build-local-plugin-package.py` 的 DEFAULT_OUT 旧路径 `deploy\本地插件包` 未对齐新布局 | 未开始 |
| X-7-5 | 残留小项⑤ | 该脚本的 L-13 扫描器与服务器链 `95c219d` 新增的同命名空间放行不同步 | 未开始 |

---

## 门控关系（未决 GD → 批次）

| 门控 | 影响任务 | 说明 |
| --- | --- | --- |
| 批次 0 出口 | 全部 | **T01 已通过（2026-10-03）→ GD1 定案 pywebview**；**GD1–GD8 全部拍板（GD6 维持本期不做），待拍板清单清零，批次 1 解锁**；~~剩余到点评审项~~ **全部闭合**：jz_relay 白名单（T17 机械提取落盘 relay_whitelist.json）、本地插件索引路径（T21 定 local-plugins/）、断点数值（批次 5 定 480/767/1023/1440） |
| GD8（已拍板 2026-10-03） | T06/T07 | 同源内核、双 spec 出包；三档落地口径见方案 §7.3；内核改动双端回归（红线 4） |
| GD4（已拍板 2026-10-03） | T20 | 源码子进程为主力+执行器保留；exe 例外；常驻 worker 按 manifest 声明；依赖版本槽已列后续优化 O1 |
| GD7（已拍板 2026-10-03） | T21 | A+C：管理页装包+共享盘索引检查更新；文件关联不做；插件商店适配四接缝随 T21 预留（方案 §6.8） |

## 验收红线（跨批次，逐批自检）

1. **业务权威外置**：宿主内核与能力层路由裁决不含业务逻辑；服务器→本地方向不可达（跨类禁调用），类 C 仅经 `jz_relay` 白名单取数。
2. **浏览器路径功能不减**：无客户端/桥缺失时，现有功能与现状一致（仅少类 B/C 与本地插件管理入口）。
3. **页面只有一份**：壳不 fork 前端、不注入页面，只注入桥与 `jz-shell` 结构类。
4. **主包零回归**：同源内核的任何改动必须过主包全量自检；主包出包流程与命令真源不被客户端改造破坏。
5. **安全上下文与数据边界**：桌面端 origin=127.0.0.1（剪贴板可用）；类 C 数据交互以用户会话进行、不落盘持久化服务器业务数据；类 B 不上传用户数据；LLM Key 不落本机。
6. **插件独立铁律跨端延续**：本地插件不跨插件 import/调用；`mount` 冲突拒装；worker 与进程内插件都不得触碰其他插件端点。

## 后续优化方向（已评估未排期）

| # | 方向 | 评估结论（设计存档见方案 §6.6） | 触发条件 |
| --- | --- | --- | --- |
| ↳ | **A 类 worker 迁移前置 ✅（2026-10-09）**：supervisor 分件组装（`deploy\服务端\supervisor分件\`；`nssm.exe` 由运维放入 install\）+ 迁移演练回归 **8/8**（批次 8b 后代码大改仍全绿：单元登记→重启跳过进程内→worker 拉起→真实登录→反代 →停单元 503 主应用照常）+ 迁移 runbook 落《打包部署手册》「管理进程分件安装与逐插件 worker 迁移」（六步 + Q7 顺序 + 回滚口径）；**逐插件真机切换待目标机**（需 NSSM 服务化） | 本行 |
| ↳ | **F6 idle 回收·框架件 ✅（2026-10-09）**：`plugin_executor` 增空闲收割——manifest 可选 `idle_ttl`（秒），缺省不回收＝现行为不变；节拍随最小 ttl 自适应（ttl/2，夹 1-15s）；拿插件串行锁后**干净弹出+kill**（非崩溃路径：下次请求静默重启、不弹崩溃提示）；`shutdown` 同步停收割线程。台架 `resident-tests.py` **12/12**（新增：6a 空闲超 ttl 干净收回／6b 回收后静默重启（新 pid、状态重置）／6c 对照=未声明 ttl 不回收）；规范 §16.11 补 L-14（续）idle_ttl 字段与「任务态先外置」前置纪律。**已收口（2026-10-09）**：三 B-heavy 任务态全部外置并声明 `idle_ttl=600`——info-transfer v1.2.9（真机闭环：任务→done→空闲 7s 干净收割→任务态仍 done→静默重启新 pid）、trajectory-convert v1.2.0 与 qr-video-decode v1.1.6（同模板；函数级验证写穿/载回+中断归一；qr 补 `import json`/`_TASK_DIR`——py_compile 不报的坑由函数级验证抓出）；本地包+服务器双包均重出并装客户端。**行为要点（走查预期）**：worker 空闲 10 分钟自动收回省内存，下次打开秒级冷启动；任务进行中由前端进度轮询保活，不会中途回收；若任务因回收/重启中断，状态显式置「因能力层回收/重启而中断」不再卡 running | 本行 |
| ↳ | **V1–V10 全量验收 ✅（2026-10-09，本机可验部分全过；真机项另列）**：台架总扫 **8/8 全绿**（identity 16/16、resident 12/12、file-filter-C 11/11、deps-layout 21/21、install-progress 12/12、dep-gate 10/10、batch8 59/0、worker-migration 8/8）+ 客户端 smoke 5 断言；机械审计 `tools/e2e/v-acceptance-audit.py`。**验收抓出并修复的真缺陷**：①V2 出包**不可重现**（zip 带 mtime/顺序不定）→ 工具改固定时间戳+确定顺序+固定压缩级，两次派生 sha 一致；②V2 C 类引擎副本缺同源登记 → 两插件副本加盖（`v2-provenance-fix.py` + adapt-ts-client 内置）；③V5 server_data 无明示面 → 上架抽取入目录 + 商店卡片「数据出机」徽标 + 安装确认弹窗明示；④V9 审计无身份 → 执行器 call/_audit 带真实登录身份（能力层传入）；⑤PS1 单字符行被绑成 Char 致 file-filter 服务器包构建崩（`Char.TrimStart`）→ 显式字符串化。**残项**：已上架包需用新工具重出重上架（B-light×5 已升补丁号重出：base64/color-picker v1.0.2、json-formatter/md5-generator/map-marker v1.0.3；file-filter v1.6.1、trajectory-sketch v1.3.1、info-transfer v1.2.9、trajectory-convert v1.2.0、qr-video-decode v1.1.6 均重出待上架）；V1 未上架条目（走查）；真机项=目标机部署与 worker 迁移 | 本行 |
| O1 | 依赖多版本共存（版本化槽） | 机制成立：`runtime/pylibs/<name>__<version>/` 共享槽+版本槽并存，解析顺序共享→版本化→缺；plugin_host 按插件拼 `sys.path`（只影响本插件子进程，原生 DLL 跨进程无冲突，同版本多插件去重共用）；**硬边界=旧版本须有客户端运行时 Python 的 wheel（cp314）**，否则走 exe 例外通道或插件升版；类 C 进程内不能双版本→灰显或一键降格子进程（同源码零迁移）。**本期版本不符=三态判定提示，不做槽**。深化设计已备：`docs/design/桌面端依赖版本槽-设计文档.md`（2026-10-04，槽挂 `pylibs/.slots/` 内核零改动 + 执行器注入 + 类 C 降格 + cp314 边界，待拍板） | 出现真实的「插件钉旧版依赖」需求且共享槽无法满足时重评（GD4 复议） |
| O2 | **插件商店（已立项，下一步）** | 平台化插件**上架/分发/下载/安装更新**，升级 GD7 的 C 通道（共享盘索引→商店目录）。**客户端侧四接缝已随批次 4 T21 预留**：索引 schema 与商店目录同构、更新源可配置（`plugin_source`）、安装收口 `install(pkg_source)` 单入口、`runtime` 类别中立字段（方案 §6.8）；包格式与 `plugin-packages.json` 同构，上架即原包。**服务端形态已另立设计文档并定稿（v2.1，2026-10-05 需求方拍板：六条需求 + ⑦全生命周期同权）**：`docs/design/桌面端插件商店-设计文档.md`——商店=**主包第二核心插件**（随包、独立完整用例）、admin 后台管理上架/下架/审核/统计、`数据根\store\` 数据契约解耦（admin 写/store 读/统计反向）、客户端经中继浏览+`/local-api/store/install` 拉包安装；商店页为生命周期主入口（下载/安装/升级/卸载/停用——停用只停包留本地），全部操作不区分管理员/办事员；**v2.2 需求⑧：server 插件双包模型**（后端包发布即自动部署服务器、前端包可装客户端本地直出+API 经中继、未装回退中继直出）。**已拍板定稿（2026-10-05，拍板记录见设计文档 §1.3），批次 8 排入本清单** | ~~商店阶段启动设计时~~ **全部落地：T-O2-0~10（批次 8，2026-10-05）+ T-O2-11/12 supervisor/worker/venv（批次 8b，2026-10-06，见批次 8 实施记录）；剩真机项（NSSM 服务化验收/逐插件切换/主包 smoke 出包门）** |
| O3 | **B-heavy worker 内存优化：懒加载 + idle 回收**（Q6 已拍板，2026-10-06；评估与实测留痕 → `docs/design/存量插件三类适配-设计文档.md` Q6） | 两段组合：①**懒加载**随各插件 M-B2 适配实施——info-transfer/trajectory-convert 的 `import cv2` 后移至首个视频类任务（可用性旗标改由 jz_deps 判定数据源取），空闲 worker 驻留回 ~30MB 轻量态；②**idle 回收=框架增补 F6**——`plugin_executor` 空闲计时收割（manifest 可选 `idle_ttl`，缺省不回收）+ 前置=三插件任务态外置（内存 TASKS → `.task_cache`，含 qr-video-decode 30min TTL 迁移）。组合后稳态=轻量骨架常驻、重依赖只在干活窗口驻留；代价=空闲后首任务秒级冷启动。实测基线（开发机）：单 worker 工作集 47MB/提交 259MB、两 worker 并发物理增量 73MB；**试点时目标机复测定值**。依赖共用备选（共进程/子解释器/共享内存/依赖服务插件）已全部评估否决，留痕同 Q6 | 懒加载：随存量插件适配批次实施；idle 回收：适配批次完成后排期，与运行架构懒启动语义对齐后动工 |
| O4 | **C 类拆分收尾（先 A 后 C 过渡）**（Q5 已拍板，2026-10-06） | file-filter/trajectory-sketch 先以 M-A 形态上架开张（零迁移），双包拆分随存量插件适配收尾批次实施（file-filter=C 类样板先行，端点拆分与客户端工件设计见适配设计文档 §3.15–§3.16）；**character-graph 维持 A 直至出现明确离线/本地化需求**（全文≈原文档信息量、离线无图谱，拆分收益低） | file-filter/trajectory-sketch：适配收尾批次；character-graph：出现明确离线/本地化需求时重评 |
| O5 | **A 类 worker 迁移排序**（Q7 已拍板，2026-10-06；与商店上架正交） | E 架构逐插件真机切换顺序=knowledge-base 优先（Office 预览渲染 CPU 段，隔离收益最大）→ shared-docs → case-report；notice-board 已完成试点（批次 8b）。属运行架构真机项，随各插件上架后执行 | 逐插件真机切换时（运行架构文档遗留真机项） |
| O6 | **浏览器端退役规划**（Q3 已拍板方向，2026-10-06；非本期实施） | 用户面终态=仅桌面客户端。过渡期红线 2/3、条目并存、服务器版全保留照旧；退役时：B-light 服务器条目下架（只上本地条目成终态）、A 类「安装前端包」从可选变必选（中继直出兜底退化）、C 类客户端工件成唯一 UI 载体、登录页/admin 管理面等纯服务器 UI 去向属专项范围、红线 2 修订。终态影响表 → 适配设计文档 §6.1 | 桌面端部署渗透率达标（全覆盖且稳定运行）后立项专项设计 |
