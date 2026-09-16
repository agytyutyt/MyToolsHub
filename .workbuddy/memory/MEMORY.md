# JZToolsHub 项目长期记忆

> 改代码 / 打包 / 写文档前先扫本文件。过程细节与实测数据见 `.workbuddy/memory/` 当日日志（先查最新）。
> **打包发版只读 `docs/打包部署手册.md`**（一页流程 + 可复制命令 + 坑清单 + 故障处置），
> 本文件只放"违反即事故"的硬约定，别再让会话去通读 README/HANDOFF。

## A 硬约定（违反即事故）

1. **配置模板必须叫 `config.template.json`**：打包脚本按精确名删插件树内所有 `config.json`，
   同名模板会被一起删 → 部署形态模板同步静默失效。新增带模板的插件要在
   `jztools_data._TEMPLATE_SYNC` 与 `install.ps1 Sync-ConfigTemplates` **两处同时登记**；
   打包后自检 `*.template.json` < 4 即中止。
2. **插件后端路由函数名必须带插件前缀**（`kb_/ts_/ff_/cr_/admin_…`）：Flask 用
   `view_func.__name__` 作 endpoint，重名直接 AssertionError，且无法回滚已注册一半的路由。
3. **打包不传 `-Version` 自动递增 patch；版本号与上一版相同 → `build-deploy.ps1` 中止**
   （版本不变 → 目标机 `sync_templates()` 判未升级 → 模板同步全跳过）。
4. **`bg_image.py` 是刻意双份的共用模块**（file-filter 与 trajectory-sketch 的 `backend/` 各一份，
   逐字节相同；B-7 禁止插件间 import）：改动必须两处同步（`cp` 覆盖），
   `test_bg_image.py` 断言两份 sha256 一致——只改一份就是行为漂移。
5. **插件包出包 = 全仓库干净戳**：`build-plugin-package.ps1` 的 `dirty` 是 `git status --porcelain`
   **全仓库**计数（含未跟踪文件），有并发会话在写仓库（scratch/评估稿）时会被记 `dirty=true`。
   处置顺序：把非本工作的东西**如实提交或本地排除**（`.git/info/exclude`，出包后删段），
   **两次出包之间先提交 `tools/plugin-packages.json`**（否则第二个包看到第一个包的登记）；
   **别用 `git stash -u` 偷懒**——它会删掉别人正在写的未跟踪文件，收尾极麻烦（见 2026-09-16 日志）。

## B 分发物

- **插件升级主流程 = 后台上传即全自动**（admin ≥1.3.0）：管理员上传 zip（**不解压**）→
  校验 → 备份 → 替换 → **自动停服重启** → 页面自动刷新。三个入口共用
  `routes.py:_maybe_auto_restart()`（`/apply`、`/rollback`、`/batch-apply`：批量有失败项则跳过重启）；
  响应回 `restarting` / `restart_message`，前端 `waitAndReload()` 轮询到服务恢复再刷新。
  **顺序是"先替换、后停服"**（替换失败时服务仍活着可回滚），不是先停服。
- **后端用 `errors[]` 给原因的接口，400 响应必须同时给 `error` 摘要**（admin ≥1.3.1）：
  前端通用 `api()` 抛错只取 `data.error`，否则管理员只看到"HTTP 400"（现场无法自助）。
  插件包校验被拒的四大类：同版本重装 / 主程序版本过低或程序目录缺 `version.json` /
  包损坏哈希不符 / 全新安装缺 `tools_entry`。
- **插件依赖白名单的真源 = `JZToolsHub.spec` 的 `PACKAGES`**（出包工具 C-4 校验读它）。
  插件后端 import 了第三方库却报"框架未打包"时，**先往 PACKAGES 登记，不要 `-SkipChecks`**
  （flask / werkzeug 就是这么补进去的）。

- 主包默认瘦身（v1.9.0 = zip 122MB，无 `runtime/`）；`-WithOfflineRuntime` 才含组件。
  组件包独立分发：`JZToolsHub-离线组件-LibreOffice核心-<v>.zip`（免管理员）/
  `…Chrome-<v>.zip`（需管理员）。`install.ps1` 判定看**源包** `runtime\manifest.json`，非目标目录。
- **LibreOffice 组件一键安装 = 纯解压**到 `<程序目录>\runtime\libreoffice\`，对目标机隐身
  零副作用（不写注册表/不建快捷方式/不进"程序和功能"）——**别加注册类动作**。
  装完即生效（`office_render._apply_soffice_env()` 每次重新探测，**别加进程级缓存**）。
- 组件链六件套：`tools/offline-components.json` → `fetch-offline-bundle.py`（`--core`/`--pin`）→
  `build-libreoffice-core.py` → `tools/offline-runtime/*` → `build-offline-component.ps1` → `build-deploy.ps1`。
- 裁剪致命坑：**`presets\` 绝不能删**（全新 profile 下 rc=77）；**`msiexec /a` 忽略 ADDLOCAL**，
  只能全量解包后裁；**测裁剪必须每次全新 profile**，否则假阳性。
- soffice 探测：插件 config `office.soffice_path` > 环境变量 `XHR_SOFFICE` >
  `<程序目录>/runtime/libreoffice/program/soffice.exe`（多套一层时有限深度 glob）。
- 打包耗时全量 5~10min；`-ZipOnly` ≈2min（**先把新文件复制进 `deploy/JZToolsHub/`**）；
   **先 commit 再打包**（否则 version.json.commit 记 `<sha>-dirty`）。
- **主包版本号一律三段式 `X.Y.Z`**（`1.9.0` / `2.0.0`）：`semver_cmp()` 只认 `\d+\.\d+\.\d+`，
  两段式 `2.0` 会让插件包 `min_app_version` 校验退化成"版本号无法比较 → 跳过该项"。
- **本机打包走「两段式 + 增量合并」（2026-09-15 起，工具已入库）**：① 复用完整的 `dist\JZToolsHub`
  （源码没变就不重跑 PyInstaller）；② `python tools\build-deploy-local.py -Version X.Y.Z` 组装
  （**增量合并，从不删库** —— 工具会话 safe-delete 拦批量删除）；③ `build-deploy.ps1 -ZipOnly` 出包；
  ④ `python tools\verify-package.py <zip> --smoke` 校验。压缩与冒烟放**后台**跑、日志写**仓库外**。
  一页流程见 `docs/打包部署手册.md`，实测过程见 `.workbuddy/memory/2026-09-15.md`「v2.0.0 打包与上线」节。
- **PyInstaller 多入口可共享 `_internal`（2026-09-15 实测）**：两个 `Analysis` → 两个 `EXE`
  → **同一个 `COLLECT`**，两个 exe 的 `sys._MEIPASS` 相同，依赖二进制只有一份；
  多一个入口的增量 **3.00 MB / +9.7% 且为一次性**（`_internal` 不增大，增量就是第二个
  exe 的 bootloader + 自带 PYZ）。且 `build-deploy.ps1:132` 是整目录搬运，
  **新增 exe 不用改构建脚本**。冷启动 431ms（stdlib+cryptography 口径）。
  详见 `docs/插件热插拔-容器化改造评估.md` 附录 A。
- **信息传输（info-transfer）链路的几个"改前必知"事实（2026-09-15 评估核实）**：
  ① **桌面端屏幕供给才是头号瓶颈（比手机端分辨率更硬）**：流式预览 `frontend/index.html:171`
  `img.ri-stream { width: min(420px,100%,48vh) }` → 码面在屏上仅 **420px**；放大弹层 860px。
  屏幕没画出来的模块相机放大也补不回来 → 4 码 2×2 在 420px 下每码 210px = 2.0 px/模块，直接不可读。
  **多码/大码的前置条件是"桌面端先做满屏码面舞台（≥1000px）"**；桌面端打包另需 box_size 与画布尺寸配套。
  ② 手机端 `CameraScanner.kt:95-97` 的 `ImageAnalysis` **未设分辨率 → CameraX 默认 640×480**（上限 1080p），
  这是**实时扫码**能读的二维码版本的硬限制（480px 下 v40 需 531px，几何上塞不下）；
  ③ 实时路径只取 `codes.firstOrNull()` + 仅按 `lastText` 去重 → **结构性只支持单码**；
  但 `ImageDecoder.decodeBitmap` **已遍历全部码** → **MP4/帧序列路径今天就能吃多码帧**（改桌面端即可验证）；
  ④ `VideoParseHelper` 要求 `have == n` 才算成功，实时循环模式应改 **k-of-m 早停**（`ZfecCompat` 已实现）；
  ⑤ 单码净载荷（EC L，`_chunk_data` 口径）：v15 377 B / v20 629 / v25 941 / v30 1286 / v35 1712 / **v40 2201 B**；
  ⑥ 协议侧 `idx` 自描述 + 解析按 idx 去重 → **多码同屏协议零改动**；
  ⑦ **多码排布不要用 2×2**：16:9 下 2×2 把长短边同时切半→被迫降版本；同 ppm 下
  **2 行 3 列（6 码）单元格与 2×2 同为 540px 却多带 50% 载荷**，**1 行 2 列（2 码）px/模块更高更稳**。
  盈亏平衡召回率：2 码 0.62 / 6 码 0.68 / **4 码 2×2 0.875（最差）**；
  ⑧ ML Kit（17.3.0）多码实测口径：多码场景检测率 18.9%–40.5%、双码 clip recall 0.5、
  95 码阵 10.5%、**硬上限 10 条码/次调用**；但轮级召回是多帧取或 `1-(1-q)^n`，
  轮长 600ms @8fps（n≈5）时 q=0.5 即可达 0.97 → **要保的是"一轮内 ≥4–5 个分析帧"**。
  可行性评估见 `docs/二维码多码同屏可行性评估.md`（§10 = 4 码专项，§11 = 提速与减少轮次）。
  ⑨ **提速四杠杆（2026-09-15，§11）**：a) **每个 share 都是 base64 承载**（`encode_data`）→
  QR byte 模式 8-bit clean，白扔 25%；改 **Base45 + alphanumeric** 得 **+29%**（v40：2205→2853 B），
  必须两端同步发版；b) **`_pack_data` 对 `fmt=file` 一律不压缩** → "原件传输"传 txt/csv/json 白扔 3–10×，
  对 fmt=file 也试压 zlib 即可（最便宜的最大杠杆）；c) **k-of-m 早停**：收满 m 284 轮 → 凑够 k 58 轮（4.9×），
  轮次 ≈ `k/(N·q)`；d) **码位轮换（零成本必做）**：`base=((r-1)·N)%m` 使 **m 为偶数时 share 的码位奇偶
  永久固定**，坏码位上的 share 永远收不到（实测 +34% 轮次），m 奇数才自愈。
  ⑩ **FEC 冗余不要降（推翻 §6 R8 原建议）**：单向无反馈时 `m < k/q` 会让循环绕回来重发已收到的 share
  （纯空转）；实测 1.39× 起追平最优，q=0.6 时 1.67× 的 62 轮远优于 1.18× 的 89 轮
  → **冗余 = 丢包容错 + 避免空转，两个角色都要它**，现状 40% 几乎最优。
  ⑪ 200 KB 耗时阶梯（q=0.8）：现状 **23.7min** →满屏v40+早停 46s →+N=2 29s →+Base45 23s
  →+码位轮换 ~17s →+6 码 11s →+压缩前置（文本）**4.8s**。

## C Python / 浏览器基线

- 目标机 Win10+；**浏览器基线 Chrome ≥72**（main.js 规避、jz-icon SVG 回退、pdf.js legacy
  的触发条件是"旧浏览器/缺彩色 emoji 字体"，**不是 Win7，别删**）。
  口径：最低 3.12 / 打包 3.14；排除 3.10。
- **动 xlsx 包结构只能走 zip 级字节手术**（`plugins/*/backend/bg_image.py` 是范例）：
  openpyxl `load_workbook`+`save` 会**静默丢掉工作表背景图**（`<picture>`），
  但它重写整个包（样式/宏/透视表全变样）；自己重写 zip 时
  **`ZipInfo` 不能沿用源条目的 `flag_bits`**（带 data descriptor 标志却又不写 descriptor → 产出损坏 zip），
  删图片本体前要**全包扫 `.rels` 反查引用**（同一张图可能还被 drawing 引用，误删就毁用户内容）。
- **zfec 无 cp314 wheel**：必须 `pip install --find-links wheels`（`wheels/zfec-1.6.0.0-cp314-*.whl`，
  重建 `tools/build-zfec-wheel.py`）；GPL-2+ 再分发注意合规。
- **vendor 升级后必须重打补丁**（`vendor/README.md` 现有 5 条：xhr Optional 导入、xhr 空格样式、
  dhr numId=0、dhr 字符缩进、dhr .doc 警告按模式分流）并**递增 `routes.py` PREVIEW_CACHE_VERSION**
  （现为 3），否则旧缓存不重渲染。
- `office_render` 导入失败被静默兜住 → 引擎不可用只在 `/api/knowledge-base/status` 报
  `office_render.available=false`。
- openpyxl：read_only 读不到 merged_cells；data_only 对未计算公式返 None；只存 15 位有效数字。
- **Canvas 画布必须落在整数设备像素上**（相位 `rect.left*dpr % 1` 要为 0）：分数相位会让合成器
  对整张画布做双线性重采样，观感就是"分辨率低、发糊"。位图尺寸取 `Math.round(视口×scale×dpr)`、
  CSS 尺寸**由位图反推**（`位图/dpr`），再用 **<1 设备像素的负边距一次性补偿**（不要迭代累积——
  布局会把小数边距取整，累积会让整页漂出几像素的居中偏差）。页面居中 `margin:0 auto` 与 A4
  自动高度（841.89pt×1.25 = 1052.36px）都会自然算出小数相位：**dpr 处理对了 ≠ 画面清晰**。
  定量口径：同内容画布整数定位 vs 半像素定位，合成后边缘能量 100% vs 80%。

## D 本机限制与工具

- **PowerShell 执行策略 = Restricted**：任何 .ps1 加载即抛 PSSecurityException
  （症状：退出码 1 + stdout 全空 + 日志文件不生成）。正解：
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <脚本>`；
  `Start-Process` 与 Bash→PowerShell 均被安全策略拒绝；后台 run_in_background 默认 120s 被杀
  （长构建走前台 + timeout 600000）。`*>` 重定向是 UTF-16LE，读日志要 decode('utf-16')。
- **PowerShell 工具"长命令"稳定失败**（报权限拒绝，实为命令规模触发沙箱限制）→ 长逻辑写 .ps1。
- **跑构建脚本别加 `2>&1 | Out-File`**（NativeCommandError 连带杀内层进程，日志停在半路像失败）；
  成败看产物（deploy/*.zip、version.json）别只看日志尾部。
- PowerShell stdout 偶发被吞 → 探测命令落盘临时文件再 Read（python 解码）。
- **Bash 工具 PATH 损坏**（ls/wc/dirname 找不到、cd 失败）→ 全部绝对路径。
- **agent-browser**（exe `D:\OpenClaw\npm-global\node_modules\agent-browser\bin\agent-browser-win32-x64.exe`，
  **用 Bash 工具直接调**；在 PowerShell 里它退出时会杀宿主，同一会话第二条命令永不执行）。
  **每次工具调用 = 新浏览器会话**，多步操作必须在同一次 Bash 调用里 `;` 串联；
  eval 表达式禁 `|` `&` `>` 字符；先 `set viewport 1440 1000`；插件页直开
  `/plugin/<id>/index.html`；本机访问设 `no_proxy=127.0.0.1,localhost`（curl 需 `--noproxy '*'`）。
  登录态：同一次调用内 `eval "fetch('/api/login',{method:'POST',...})"`；`click` 偶尔不触发，
  **用 `eval "el.click()"` 更稳**。**往日志/文件写含反引号的内容别用 Bash 内联 python -c**
  （反引号被命令替换吞掉）——写脚本文件再跑。
- 本机 `pip wheel <sdist>` 必失败（沙箱禁 reg.exe/cmd.exe）→ 把已编译产物重打成 wheel。
- HTTP 走代理 `http://127.0.0.1:49237`；本机端口测试避开 5099（曾有残留旧进程）。
- venv：`C:\Users\yfjz\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（flask/openpyxl/python-docx 全）。
- 编码约定：.ps1 = UTF-8 BOM + CRLF；.bat = GBK + CRLF（不要 chcp 65001）；JSON = UTF-8 无 BOM。
- 沙箱会误报（System32 路径、Start-Process、长任务收尾都可能触发 SandboxError）——以脚本自身日志/产物为准。
- **沙箱 safe-delete 会拦"一次删 >50 项"**：上传 25 文件的插件包时，服务端清理 staging 会命中
  → 进程被中断、浏览器只看到 `Failed to fetch`。**验证插件包一律用 3 文件的小包**
  （`test_admin_plugin_manager.make_package` 造），别拿真包打本地服务。
- 服务端进程崩时看日志：输出重定向到 **Windows 路径**再读（Bash 的 `/tmp/x.log` 与 python 不通）。

## E 测试隔离（保护真实数据 `~/.jztoolshub`）

1. 先 `jztools_data.get_data_root = lambda: <临时目录>` 再 import app；复制 `config/tools.json`；
   **别调 init_data_root()**（触发旧数据迁移）。或用 `JZTOOLS_DATA_ROOT` 环境变量（admin 插件沙箱同款）。
2. 优先 `app.test_client()`；需真实服务用 5100+ 端口，后台任务用 run_in_background。
3. Office 预览链路：本机 soffice 可用（`C:\Program Files\LibreOffice`）；测试假 admin 会话
   （伪造 `jztools_admin` 模块，见 `plugins/knowledge-base/backend/test_routes_preview.py`）；
   **每次 soffice 转换用唯一临时 profile**（同 profile 连跑撞单实例限制）。

## F 其它

- `config/data_root.json` 是 get_data_root 的**备份指针**（主指针 `~/.jztoolshub.json`），已 gitignore，别加回；
  `install.ps1:264` 有"必须含 exe"守卫，别拆。
- backend 跨模块 import 两步式兜底（try relative → absolute → None），否则 importlib 判"模块不存在"。
- **文档同步**：改功能后回写 README → `plugins/<id>/README.md` → `docs/*设计文档.md` →
  HANDOFF.md 状态头 → 当日日志；写"当前状态"前先 `git status --short` / `git log -1` 核实。
  仓库无 prompt.json（模板已删）但登记仍在 → 升级告警属预期；改提示词只能改 llm_client.py 内置默认。
- 前端图标别依赖 emoji 字体（旧 Chrome CSS content 可能显示方块），用纯文本或自绘 SVG。
- 往包里塞东西前先问"该不该做成组件包"（可独立分发/升级/卸载 → 组件包，别撑大主包）。
