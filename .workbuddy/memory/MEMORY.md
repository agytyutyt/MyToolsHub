# JZToolsHub 项目长期记忆（精简版，详录见 .workbuddy/memory/ 当日日志与 docs/ 评估文档）

> 打包发版只读 `docs/打包部署手册.md`。本文件只放"违反即事故"的硬约定与关键事实。

## A 硬约定（违反即事故）

1. 配置模板必须叫 `config.template.json`（打包按精确名删所有 `config.json`）。新增带模板插件要在 `jztools_data._TEMPLATE_SYNC` 与 `install.ps1 Sync-ConfigTemplates` 两处登记；打包后自检 `*.template.json` < 4 即中止。
2. 插件后端路由函数名必须带插件前缀（`kb_/ts_/ff_/cr_/admin_…`）：Flask 以 `__name__` 作 endpoint，重名 AssertionError 且无法回滚。
3. 打包不传 `-Version` 自动递增 patch；版本与上一版相同 → `build-deploy.ps1` 中止（否则目标机判未升级跳过模板同步）。
4. `bg_image.py` 刻意双份（file-filter 与 trajectory-sketch 各一份，逐字节相同，B-7 禁止插件间 import）：改动必须 `cp` 两处同步，`test_bg_image.py` 断言 sha256 一致。
5. 出包 dirty = 全仓库 `git status --porcelain` 计数：并发会话期间先如实提交/本地排除（`.git/info/exclude`）；两次出包间先提交 `tools/plugin-packages.json`；**禁用 `git stash -u`**（会删别人未跟踪文件，见 2026-09-16 日志）。

## B 分发物要点

- 插件升级主流程（admin ≥1.3.0）：后台上传 zip 不解压 → 校验→备份→替换→**自动停服重启**。顺序是"先替换、后停服"。
- 后端 `errors[]` 接口的 400 响应必须同时给 `error` 摘要（admin ≥1.3.1，前端只取 `data.error`）。
- 插件依赖白名单真源 = `JZToolsHub.spec` 的 `PACKAGES`；缺库先登记 PACKAGES，别 `-SkipChecks`。
- 主包瘦身（无 runtime/），LibreOffice/Chrome 组件包独立分发；LibreOffice 一键安装=纯解压到 `runtime\libreoffice\`，别加注册动作，别给 soffice 探测加进程缓存。`presets\` 绝不能删（rc=77）；`msiexec /a` 忽略 ADDLOCAL。
- 主包版本一律三段式 `X.Y.Z`（`semver_cmp()` 只认此格式）。
- 本机打包「两段式+增量合并」（2026-09-15 起）：复用 `dist\JZToolsHub` → `tools\build-deploy-local.py` → `-ZipOnly` → `verify-package.py --smoke`；压缩/冒烟放后台、日志写仓库外。先 commit 再打包。
- PyInstaller 多入口可共享 `_internal`（同一 COLLECT），新增 exe 不用改构建脚本。
- **信息传输链路"改前必知"**（详录：`docs/二维码多码同屏可行性评估.md` §10-11、`docs/信息传输格式开销评估与优化方案.md`、`.workbuddy/memory/2026-09-16.md`）：
  - 头号瓶颈是桌面端屏幕供给：流式预览 img 仅 420px、弹层 860px；多码/大码前置条件=桌面满屏码面舞台 ≥1000px + box_size 配套。
  - 手机 CameraScanner 未设分辨率 → CameraX 默认 640×480；实时路径只取 firstOrNull+lastText 去重=结构性单码；但 ImageDecoder 已遍历全部码，MP4/帧序列路径可直接吃多码帧。
  - VideoParseHelper 要求 have==n → 实时循环应改 k-of-m 早停（ZfecCompat 已实现）。
  - 单码净载荷（EC L）：v15 377B / v20 629 / v25 941 / v30 1286 / v35 1712 / v40 2201B。
  - 提速杠杆：share 是 base64 承载白扔 25% → Base45+alphanumeric +29%（两端同步发版）；`_pack_data` 对 fmt=file 一律不压缩白扔 3-10× → zlib 试压；码位轮换 `base=((r-1)·N)%m`（m 偶数必做，+34%）；FEC 冗余 40% 不要降（单向无反馈需防空转，1.39× 起最优）。
  - （不采用）多码排布：2×2 最差（盈亏平衡召回 0.875）；1 行 2 列 px/模块更高更稳，6 码 2 行 3 列多带 50% 载荷。ML Kit 硬上限 10 码/次调用；轮长 600ms@8fps 保 ≥4-5 分析帧即可轮级召回 0.97。**多码同屏方案 2026-09-16 经评审不采用，全部按单码口径设计**。
  - 200KB 耗时阶梯（q=0.8）：现状 23.7min → 满屏v40+早停 46s → +N=2 29s → +Base45 23s → +码位轮换 17s → +6码 11s → +压缩前置(文本) 4.8s。
  - 开销实测（2026-09-16，样例口径）：精简路径全格式 ≤7 码/7s（doc 43×、txt 10×）；原件路径浪费在 fmt=file 不压缩（ppt 10.4×、doc 12×、xls 4.1×、pdf 2.9× 可省，解析端按 zip==1 解压与 fmt 无关、天然兼容）；ppt/pdf/pptx 不在白名单连原件都被拒；doc 原件静态 427 页超 200 页上限；原件级二维码通道现实边界 ~100–200KB。

## C Python / 浏览器基线

- 目标机 Win10+；浏览器基线 Chrome ≥72（main.js 规避/jz-icon SVG 回退/pdf.js legacy 触发条件是旧浏览器或缺彩色 emoji 字体，不是 Win7，别删）。Python 口径：最低 3.12 / 打包 3.14。
- 动 xlsx 包结构只能走 zip 级字节手术（openpyxl 重写全包丢背景图；ZipInfo 别沿用 flag_bits；删图前全包扫 .rels 反查引用）。
- zfec 无 cp314 wheel：`pip install --find-links wheels`（重建用 `tools/build-zfec-wheel.py`）；GPL-2+ 合规注意。
- vendor 升级后必须重打补丁（`vendor/README.md` 5 条）并递增 `routes.py` PREVIEW_CACHE_VERSION（现 3）。
- openpyxl：read_only 读不到 merged_cells；data_only 公式未计算返 None；只存 15 位有效数字。
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
- 沙箱 safe-delete 拦"一次删 >50 项"→ 验证插件包用 3 文件小包，别拿真包打本地服务。
- 服务端进程日志重定向到 Windows 路径再读（Bash 的 /tmp 与 python 不通）。

## E 测试隔离（保护真实数据 `~/.jztoolshub`）

1. 先 `jztools_data.get_data_root = lambda: <临时目录>` 再 import app；复制 `config/tools.json`；别调 init_data_root()。或用 `JZTOOLS_DATA_ROOT` 环境变量。
2. 优先 `app.test_client()`；需真实服务用 5100+ 端口。
3. Office 预览链路：本机 soffice 可用（`C:\Program Files\LibreOffice`）；假 admin 会话见 `plugins/knowledge-base/backend/test_routes_preview.py`；soffice 每次转换用唯一临时 profile。

## F 其它

- `config/data_root.json` 是备份指针（主指针 `~/.jztoolshub.json`），已 gitignore；`install.ps1:264` 有"必须含 exe"守卫。
- backend 跨模块 import 两步式兜底（try relative → absolute → None）。
- 文档同步：改功能后回写 README → `plugins/<id>/README.md` → `docs/*设计文档.md` → HANDOFF.md → 当日日志；写"当前状态"前先核实 git 状态。
- 前端图标别依赖 emoji 字体，用纯文本或自绘 SVG。
- 往包里塞东西前先问"该不该做成组件包"。
