# JZToolsHub 项目长期记忆（精简版，详录见 .workbuddy/memory/ 当日日志与 docs/ 评估文档）

> 打包发版只读 `docs/guide/打包部署手册.md`；**找任何文档先看 `docs/README.md`（唯一索引 + 覆盖矩阵）**。本文件只放"违反即事故"的硬约定与关键事实。

## A 硬约定（违反即事故）

1. 配置模板必须叫 `config.template.json`（打包按精确名删所有 `config.json`）。新增带模板插件要在 `jztools_data._TEMPLATE_SYNC` 与 `install.ps1 Sync-ConfigTemplates` 两处登记；打包后自检 `*.template.json` < 4 即中止。
2. 插件后端路由函数名必须带插件前缀（`kb_/ts_/ff_/cr_/admin_…`）：Flask 以 `__name__` 作 endpoint，重名 AssertionError 且无法回滚。
3. 打包不传 `-Version` 自动递增 patch；版本与上一版相同 → `build-deploy.ps1` 中止（否则目标机判未升级跳过模板同步）。
4. `bg_image.py` 刻意双份（file-filter 与 trajectory-sketch 各一份，逐字节相同，B-7 禁止插件间 import）：改动必须 `cp` 两处同步，`test_bg_image.py` 断言 sha256 一致。
5. 出包 dirty = 全仓库 `git status --porcelain` 计数：并发会话期间先如实提交/本地排除（`.git/info/exclude`）；两次出包间先提交 `tools/plugin-packages.json`；**禁用 `git stash -u`**（会删别人未跟踪文件，见 2026-09-16 日志）。
6. **只想提交部分文件时必须 `git commit -F - -- <pathspec>`**：`git add <部分文件>` 后直接 `git commit` 会把**索引里所有已 staged 的内容一并入库**（2026-09-18 实测：以为提交 4 个文件，实际 31 个 —— 工作区里 09-17 的 27 个已 staged 重命名被连带提交）。提交前先看 `git diff --cached --name-only`。

## B 分发物要点

- 插件升级主流程（admin ≥1.3.0）：后台上传 zip 不解压 → 校验→备份→替换→**自动停服重启**。顺序是"先替换、后停服"。
- 后端 `errors[]` 接口的 400 响应必须同时给 `error` 摘要（admin ≥1.3.1，前端只取 `data.error`）。
- 插件依赖白名单真源 = `JZToolsHub.spec` 的 `PACKAGES`；缺库先登记 PACKAGES，别 `-SkipChecks`。
- 主包瘦身（无 runtime/），LibreOffice/Chrome 组件包独立分发；LibreOffice 一键安装=纯解压到 `runtime\libreoffice\`，别加注册动作，别给 soffice 探测加进程缓存。`presets\` 绝不能删（rc=77）；`msiexec /a` 忽略 ADDLOCAL。
- **LibreOffice 一键安装器排障口径（2026-09-18 加固，别再退回旧形态）**：① 自检 = 全新 profile 真转换一次**且必须查产物存在**（只看退出码会假阳性）；② 自检失败且本次未重解压 → 先**自动重解压复检**（旧树残缺是最常见故障；缺了自愈用户会卡死在"已安装→只校验→自检失败"循环里出不来）；③ 日志「安装日志-LibreOffice核心.txt」在脚本同目录（UTF-8 BOM），含环境 + soffice 原始 stdout/stderr + 退出码，**是唯一排障线索**（旧版只报"未产出 xlsx"等于没说）；④ 自检超时默认 **240s**（实测失败那次 173.7s，180s 太紧）；⑤ 开关 `-VerifyOnly`（只自检，不动文件）/`-Force`（清空重解压）/`-SkipSmoke`（环境受限时先装）；退出码 0/1/2/3；⑥ 自检失败**不删组件树**（杀软常只首次拦截，可能是误报）。
- **VC++ 运行时必须在 `program\`，否则目标机必崩 `0xC0000142`（2026-09-18 真机踩出）**：`msiexec /a` 解包时本该进 System32 的 VC++ 2015-2022 运行库**只会落在解包树的 `System64\`**，而 soffice.bin 的 DLL 搜索顺序是「进程主目录(`program\`) → System32 → …」。因此：① 安装器自检前必须用 `Repair-VcRuntime` 把 `System64\` 那 10 个 DLL 补齐到 `program\`；② **`System64\` 不可当死重裁掉**（已进 `build-libreoffice-core.py` 的 MUST_KEEP）；③ **开发机装了完整版 LibreOffice，System32 里就有这套库 → 在开发机上"组件自检通过"一律是假阳性**，必须去干净机器验证。故障指纹（三件套同时出现就别怀疑杀软）：soffice **1~2 秒退出 + stdout/stderr 全空 + profile 目录都不建**；DLL 版本不匹配 → `0xC0000142`，DLL 文件损坏 → `0xC000012F`。
- **soffice 转换耗时与超时风险（待办）**：唯一临时 profile ⇒ **每次**转换都要重新初始化 profile。实测热态 18~22s、刚解压/杀软扫描时 27~72s、组件树损坏时 173.7s。**vendor 默认超时只有 60s**（`vendor/xhr/normalize/libreoffice.py`、`vendor/dhr/normalize.py` 的 `timeout_ms=60_000`；`office_render` 未传 → 走默认）→ 慢机器/冷启动首屏预览可能直接超时失败，是否上调待评估。
- 主包版本一律三段式 `X.Y.Z`（`semver_cmp()` 只认此格式）。
- 本机打包「两段式+增量合并」（2026-09-15 起）：复用 `dist\JZToolsHub` → `tools\build-deploy-local.py` → `-ZipOnly` → `verify-package.py --smoke`；压缩/冒烟放后台、日志写仓库外。先 commit 再打包。
- PyInstaller 多入口可共享 `_internal`（同一 COLLECT），新增 exe 不用改构建脚本。
- **信息传输链路"改前必知"**（详录：`docs/archive/二维码多码同屏可行性评估.md` §10-11、`docs/eval/信息传输格式开销评估与优化方案.md`、`.workbuddy/memory/2026-09-16.md`）：
  - 头号瓶颈是桌面端屏幕供给：流式预览 img 仅 420px、弹层 860px；多码/大码前置条件=桌面满屏码面舞台 ≥1000px + box_size 配套。
  - 手机 CameraScanner 未设分辨率 → CameraX 默认 640×480；实时路径只取 firstOrNull+lastText 去重=结构性单码；但 ImageDecoder 已遍历全部码，MP4/帧序列路径可直接吃多码帧。
  - VideoParseHelper 要求 have==n → 实时循环应改 k-of-m 早停（ZfecCompat 已实现）。
  - 单码净载荷（EC L）：v15 377B / v20 629 / v25 941 / v30 1286 / v35 1712 / v40 2201B。
  - 提速杠杆：share 是 base64 承载白扔 25% → Base45+alphanumeric +29%（两端同步发版）；`_pack_data` 对 fmt=file 一律不压缩白扔 3-10× → zlib 试压；码位轮换 `base=((r-1)·N)%m`（m 偶数必做，+34%）；FEC 冗余 40% 不要降（单向无反馈需防空转，1.39× 起最优）。
  - （不采用）多码排布：2×2 最差（盈亏平衡召回 0.875）；1 行 2 列 px/模块更高更稳，6 码 2 行 3 列多带 50% 载荷。ML Kit 硬上限 10 码/次调用；轮长 600ms@8fps 保 ≥4-5 分析帧即可轮级召回 0.97。**多码同屏方案 2026-09-16 经评审不采用，全部按单码口径设计**。
  - 200KB 耗时阶梯（q=0.8）：现状 23.7min → 满屏v40+早停 46s → +N=2 29s → +Base45 23s → +码位轮换 17s → +6码 11s → +压缩前置(文本) 4.8s。
  - 开销实测（2026-09-16，样例口径）：精简路径全格式 ≤7 码/7s（doc 43×、txt 10×）；原件路径浪费在 fmt=file 不压缩（ppt 10.4×、doc 12×、xls 4.1×、pdf 2.9× 可省，解析端按 zip==1 解压与 fmt 无关、天然兼容）；doc 原件静态 427 页超 200 页上限；原件级二维码通道现实边界 ~100–200KB。
  - **白名单现状（T12 之后，勿再看旧记录）**：`docx/doc/xlsx/xlsm/xls/csv/txt/md/markdown/ppt/pptx/pdf` 12 个 ext 全部在册；`ppt`/`pdf` 走精简提取（`extract=ppt`/`pypdf`，**fmt=text，还原成 .txt**），仅 `pptx` 是 `extract=none` 只支持原件传输。真源 = `routes.py::SUPPORTED_FORMATS`。

## C Python / 浏览器基线

- 目标机 Win10+；浏览器基线 Chrome ≥72（main.js 规避/jz-icon SVG 回退/pdf.js legacy 触发条件是旧浏览器或缺彩色 emoji 字体，不是 Win7，别删）。Python 口径：最低 3.12 / 打包 3.14。
- 动 xlsx 包结构只能走 zip 级字节手术（openpyxl 重写全包丢背景图；ZipInfo 别沿用 flag_bits；删图前全包扫 .rels 反查引用）。
- zfec 无 cp314 wheel：`pip install --find-links wheels`（重建用 `tools/build-zfec-wheel.py`）；GPL-2+ 合规注意。
- vendor 升级后必须重打补丁（`vendor/README.md` 5 条）并递增 `routes.py` PREVIEW_CACHE_VERSION（现 3）。
- openpyxl：read_only 读不到 merged_cells；data_only 公式未计算返 None；只存 15 位有效数字。
- **openpyxl `read_only=True` 的取数范围取自工作表 `<dimension>` 声明（只是"提示"，可能是失真的）**：
  失真声明（如声明 `A1` 而实际 A1:A4）会让 `iter_rows` **只读出首格**——2026-09-18 的真实故障
  （660.xlsx 精简提取只剩一行一列）。修法 = `ws.reset_dimensions()`（≥3.0.4）**且必须配套补齐行宽**
  （清声明后稀疏行变"参差"，只做前者会引入回归）；两步成对才与"普通模式"基准一致。
  **xlrd 无此问题**（nrows 由实际单元格推导，`_dimnrows` 只用于诊断告警）——别去"顺手修"。
  同类隐患仍在 4 处（`file-filter/core.py`、`trajectory-sketch/excel_io.py`、
  `trajectory-sketch/engine/selftest.py`、`trajectory-convert/routes.py`）——**未修，待决策**。
- Canvas 画布必须落整数设备像素（分数相位→合成器双线性重采样发糊）；CSS 尺寸由位图反推 + 一次性负边距补偿，不要迭代累积。

## D 本机限制与工具

- PowerShell 执行策略 Restricted：用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <脚本>`；`*>` 重定向是 UTF-16LE；长命令会因规模触发沙箱拒绝 → 写 .ps1；别加 `2>&1 | Out-File`；成败看产物别只看日志。
- Bash 工具 PATH 损坏 → 全部绝对路径。
- agent-browser exe：`D:\OpenClaw\npm-global\node_modules\agent-browser\bin\agent-browser-win32-x64.exe`，用 Bash 直接调（PowerShell 里会杀宿主）；每次调用=新会话，多步在同一调用内 `;` 串联；eval 禁 `| & >`；先 `set viewport 1440 1000`；插件页直开 `/plugin/<id>/index.html`；`no_proxy=127.0.0.1,localhost`；登录用 eval fetch；点击用 `eval "el.click()"`；写含反引号内容别用 Bash 内联 python -c。**eval/登录态只在同一条 Bash 调用链内有效**，跨调用落 about:blank；原生 click 命令对插件页无效（报 Done 不触发），必须 eval "el.click()"。
- **同文件多个 Edit 绝不能并行**（同一消息里并行发 Edit 调用）：后写回者基于旧快照，会把先写回者的改动静默抹掉。同文件 Edit 必须逐个串行发送（2026-09-16 T06 曾因此丢 4 处改动）。
- 本机 `pip wheel <sdist>` 必失败（沙箱禁 reg.exe）→ 重打已编译产物为 wheel。
- HTTP 代理 `http://127.0.0.1:49237`；端口测试避开 5099。
- venv：`C:\Users\yfjz\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（flask/openpyxl/python-docx 全）。
- 编码约定：.ps1=UTF-8 BOM+CRLF；.bat=GBK+CRLF；JSON=UTF-8 无 BOM。
- 沙箱 safe-delete 拦"一次删 >50 项"→ 验证插件包用 3 文件小包，别拿真包打本地服务；**PyInstaller COLLECT 要重建 dist\JZToolsHub（约 2800 项）必被拦** → 跑打包 ① 时加 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 前缀（仅 dist 构建产物目录可用此旁路）。
- 服务端进程日志重定向到 Windows 路径再读（Bash 的 /tmp 与 python 不通）。

## E 测试隔离（保护真实数据 `~/.jztoolshub`）

1. 先 `jztools_data.get_data_root = lambda: <临时目录>` 再 import app；复制 `config/tools.json`；别调 init_data_root()。或用 `JZTOOLS_DATA_ROOT` 环境变量。
2. 优先 `app.test_client()`；需真实服务用 5100+ 端口。
3. Office 预览链路：本机 soffice 可用（`C:\Program Files\LibreOffice`）；假 admin 会话见 `plugins/knowledge-base/backend/test_routes_preview.py`；soffice 每次转换用唯一临时 profile。

## F 其它

- `config/data_root.json` 是备份指针（主指针 `~/.jztoolshub.json`），已 gitignore；`install.ps1:264` 有"必须含 exe"守卫。
- backend 跨模块 import 两步式兜底（try relative → absolute → None）。
- 前端图标别依赖 emoji 字体，用纯文本或自绘 SVG。
- 往包里塞东西前先问"该不该做成组件包"。

## G 文档体系与随包口径（2026-09-17 重整后）

- **文档唯一入口 = `docs/README.md`**（分层说明 + 「我要做什么 → 看哪份」路由表 + **覆盖矩阵**：
  每个事实只有一个权威落点，其余只写指针）。新手流程看 `docs/项目管理手册.md`。
- **五层**：`guide/`（交付，随包）/ `design/`（设计，随包）/ `eval/`（评估，**不随包**）/
  `plan/`（方案与活清单，**不随包**）/ `archive/`（归档，**不随包**）。
- **随包口径改一处要同步三处**：`tools/build-deploy-local.py` 的 `COPY_DIRS`/`COPY_FILES`
  ↔ `build-deploy.ps1` §3.2 ↔ `tools/verify-package.py`（新 `FORBIDDEN_IN_PACKAGE` 断言
  `docs/(eval|plan|archive)/` 与插件 `test_*.py` 不得随包）。
- **测试/开发脚本一律不随包**：`tools/plugin-payload-rules.json` 的 `exclude_globs` 含
  `test_*.py`/`*_test.py`/`conftest.py`，`exclude_files` 含 `_build_phase8.py`。
- **组装是增量合并（只写不删）**：`docs/` 目录结构变了以后，首次出包前必须清
  `deploy\<部署目录>\docs\`，否则旧副本滞留在包里。
- 文档规范：单份 ≤400 行、不复制契约（只写指针）、不内嵌完整源码（代码给 `文件:行号`）、
  不写滚动流水账；状态三态（现行/已完成/已取代），后两者必须加横幅并链接现行替代。
- **版本号、HEAD、产物清单类事实一律"现取现用"**（`HANDOFF.md` §2.1/§2.2 已改为自检命令），
  固化进文档必然滞后。
- 文档同步链：改功能后回写 `plugins/<id>/README.md` → `docs/design/<主题>-设计文档.md` →
  `docs/README.md`（新增/归档时）+ 该文档的「最后核对」 → `HANDOFF.md` → 当日日志。
- `.workbuddy/` 已改白名单 gitignore（`.workbuddy/*` + `!.workbuddy/memory/`）；验证脚本放 `tools/dev/`。
