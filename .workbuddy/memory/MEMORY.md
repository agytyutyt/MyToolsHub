# JZToolsHub 项目长期记忆

> 改代码 / 打包 / 写文档前先扫本文件。过程细节与实测数据见 `.workbuddy/memory/` 当日日志。

## A 三条硬约定（违反即出事故）

1. **配置模板必须叫 `config.template.json`，不能叫 `config.json`**。打包脚本按精确名删插件树内所有
   `config.json`（清 API Key），同名模板会被一起删 → 部署形态模板同步**静默失效**（曾使 6 条登记项
   只剩 `tools.json` 有效）。新增带模板的插件要在 `jztools_data._TEMPLATE_SYNC` 与 `install.ps1` 的
   `Sync-ConfigTemplates` **两处同时登记**；打包后自检 `*.template.json` < 4 即中止。
2. **插件后端路由函数名必须带插件前缀**（`kb_*`/`ts_*`/`ff_*`/`cr_*`/`admin_*`…）。Flask 用
   `view_func.__name__` 作 endpoint，重名直接 `AssertionError`；已有 per-plugin 隔离算失败不拖垮整站，
   但**无法回滚已注册一半的路由**，纪律不能松。
3. **打包不传 `-Version` 也自动递增 patch**；版本号与上一版相同 → `build-deploy.ps1` 报错中止
   （除非 `-Force`）。原因：版本号不变 → 目标机 `sync_templates()` 判未升级 → 模板同步全跳过。

## B 分发物：主包（默认瘦身）+ 独立离线组件包

- **主包 `deploy/JZToolsHub-v<版本>.zip` 默认不含 Chrome / LibreOffice**（第六轮定，约 **122MB**；
  部署目录约 260MB / 2888 文件，**无 `runtime/` 目录**）。`-WithOfflineRuntime` 才回退到胖包
  （约 437MB，含 `runtime/`）；`-SkipOfflineRuntime` 是旧开关，已等价于默认。
  原因：v1.8 的 437.5MB 里 324MB 是组件，而组件只服务部分目标机。
- **组件包（`tools/build-offline-component.ps1` 产出，与主包并列分发）**：
  `JZToolsHub-离线组件-LibreOffice核心-<v>.zip` ≈163MB（**免管理员**）/
  `JZToolsHub-离线组件-Chrome-<v>.zip` ≈160MB（**需管理员**）。
- **★ LibreOffice 组件 = 一键安装、对目标机隐身、零副作用**（用户明确要求，已实测）：
  双击 `安装LibreOffice核心组件.bat` → `install-libreoffice-core.ps1` **纯解压**
  `libreoffice-core.zip` 到 `<程序目录>\runtime\libreoffice\`（恰好命中应用探测链第三条路径），
  **不跑 msiexec、不写注册表、不建快捷方式、不改文件关联、不进"程序和功能"** → 目标机
  Office/WPS 与默认应用不受影响、无 LibreOffice 图标、免管理员；写
  `runtime\libreoffice-core.installed.json` 标记；`-Uninstall` 只删该目录与标记。
  **别再给它加注册/建快捷方式类动作**，否则"隐身"这条就破了。
- **装完即生效、无需重启**：`office_render._apply_soffice_env()` 每次渲染都重新探测（**无缓存**），
  所以 **不要在该链路加进程级缓存**。
- `install.ps1` 判定"是否处理离线组件"看的是**源包** `runtime\manifest.json`
  （`$rtCarried = Test-Path (Join-Path $Source "runtime\manifest.json")`），**不是**目标目录 ——
  否则旧胖装残留的 `runtime\` 会让瘦包路径误触发；瘦包时只打印组件安装指引。
- 实测（模拟目标机）：安装 rc=0 / 12.7s / 557.9MB / 2824 文件 + 内置冒烟通过；随后应用
  `.doc→.docx` 8169B、`.xls→.xlsx` 5933B（内容回环 A2="测试"/B2=42）；卸载 rc=0、目录与标记清除。
- **LibreOffice 只用"裁剪核心包"**：应用只有 `.doc→.docx`（dhr）/ `.xls→.xlsx`（xhr）两条转换，
  不需 UI/帮助/词典/语言包/图标主题。1522.6MB/19418 文件 → **557.9MB/2824 文件**，zip **164.5MB**
  （原 MSI 357.5MB）。重建 `tools/build-libreoffice-core.py`；`-KeepFullLibreOffice` 保留完整 MSI。
- **★ 裁剪两个致命坑**：① **`presets\` 绝不能删**（删了 soffice 在**全新 profile** 下 rc=77
  `Fatal Error 安装无法完成`；`help\`/`readmes\` 反而安全）——应用每次转换都用唯一临时 profile =
  每次首启动，所以**测裁剪安全性必须每次全新 profile，否则是假阳性**。② **`msiexec /a` 忽略
  `ADDLOCAL`**（只点 378 个功能仍解出全量）→ 官方功能选择这路是死的，只能全量解包后裁。
- **组件链路的六件套（改一个看其余五个）**：`tools/offline-components.json`（清单含 `derived` 段，入库）
  → `tools/fetch-offline-bundle.py`（下载/校验，`--core` 顺带出核心包）→
  `tools/build-libreoffice-core.py`（MSI→裁剪→zip）→ `tools/offline-runtime/*`（随包/随组件分发的脚本）
  → `tools/build-offline-component.ps1`（组件包组装器）→ `build-deploy.ps1`（3.2.2 段组装胖包 + 校验）。
- `.NET` 的 `ZipFile.ExtractToDirectory` **没有 `bool` 重载**（传 `$true` 会绑到 `entryNameEncoding`
  抛类型转换错）→ 要覆盖解压用逐条 `ZipFileExtensions::ExtractToFile($e,$p,$true)`；
  解包器用**三级回退**（整档 → 逐条 → `Expand-Archive`）。
- 应用零配置探测 soffice 优先级：插件配置 `office.soffice_path` > 环境变量 `XHR_SOFFICE` >
  `<程序目录>/runtime/libreoffice/program/soffice.exe`（多套一层时有限深度 glob）。
- Chrome 是全机 MSI 需管理员、LibreOffice 不需。胖包下 `setup-offline-runtime.ps1` 对 Chrome 提权失败
  **只打印指引不报错**（离线组件失败绝不能阻断安装）；`install.ps1` 调它必须走**子进程**
  （该脚本以 `exit` 结尾，dot-source 会终止安装流程）。
- `Expand-PayloadZip` 返回 `$true`，裸调用会打出 `True` → 用 `$null = Expand-PayloadZip ...` 抑制。
- 打包耗时：全量约 5~10min（PyInstaller 约 4.3min）；只重出 zip 用 `-ZipOnly` ≈2min
  （**先把新文件复制进 `deploy/JZToolsHub/`**）。**先 commit 再打包**（否则 `version.json.commit`
  记 `<sha>-dirty`）；发版前 `fetch-offline-bundle.py --pin` 校准（Chrome 用固定 URL `stable`）。
- **未实测**：Chrome 静默安装（需提权）、真断网无 Office 机器的完整链路。

## C Python 版本基线

- 目标机 **Windows 10+**（Win7 支持已取消）；**浏览器基线 Chrome ≥72**，与 OS 基线并列写在 README §2.1。
  `main.js` 的 Chrome 72 规避、`jz-icon.js` + `static/icons/` 的 SVG emoji 回退、pdf.js legacy 构建，
  触发条件是"旧浏览器/缺彩色 emoji 字体"，**不是 Win7，别随 Win7 一起删**。
- 口径：**最低 3.12 / 推荐与打包 3.14**，**排除 3.10**（2026-10-31 EOL）。
- `plugins/knowledge-base/backend/vendor/xhr/__init__.py` 曾用 `Optional` 却未 import（3.8/3.13 导入即
  `NameError`，3.14 因 PEP649 惰性注解掩盖）→ **已修**。属「vendor 升级后必须重打」清单，与
  `renderer/table.py` 补丁同级，登记在 `vendor/README.md`。**别用 3.14 的"能跑"判断第三方代码正确性。**
- **★ zfec 无 cp314 wheel**：必须 `pip install --find-links wheels ...`（用随仓库分发的
  `wheels/zfec-1.6.0.0-cp314-*.whl`），否则退化为源码编译（要 MSVC）。重建 `tools/build-zfec-wheel.py`。
  **zfec 是 GPL-2+，再分发需确认合规口径。** wheel 类型：`cpXX-cpXX` 版本锁定（zfec/numpy/pillow）
  vs `abi3` 跨版本可用（opencv-python=cp37-abi3、cryptography=cp311-abi3）。
- `office_render` 用 `except Exception` 兜住导入失败 → 引擎不可用时**静默失去原样式预览**，
  只在 `/api/knowledge-base/status` 报 `office_render.available=false`。

## D 本机限制与工具

- **本机 `pip wheel <sdist>` 必失败**：沙箱禁 `reg.exe`/`cmd.exe`；`DISTUTILS_USE_SDK=1` 模式下
  setuptools 会剔除子进程 PATH，裸名 `cl.exe` 找不到（设 `CC` 全路径无效）。**替代：把已装好的编译
  产物重打成标准 wheel**，见 `tools/build-zfec-wheel.py`；本机 MSVC 14.51 / SDK 10.0.26100 在
  `C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools`。
- **★ PowerShell 执行策略 = `Restricted`**：任何 `.ps1` 在**加载阶段**即抛 `PSSecurityException`，
  症状是**退出码 1 + stdout 全空 + 连重定向的日志文件都不生成**（极易误判为"沙箱拦截"）。
  正解：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File <脚本> ...`；
  `Start-Process` 与 Bash→PowerShell 均被安全策略**直接拒绝**；
  后台模式（`run_in_background`）**默认 120s 就被杀** —— 长构建必须走**前台 + `timeout: 600000`**
  （前台超时只转后台、不杀进程）。脚本里 `*>` 的重定向是 **UTF-16LE**，读日志要 `decode('utf-16')`。
- **★ PowerShell 工具对"长命令"有硬限制，报的却是权限拒绝**：多行/体积大的 PowerShell 命令稳定失败，
  报 `[SandboxError] … CreateProcessW 失败 (win32_err=5, ERROR_ACCESS_DENIED) [target=…powershell.EXE]`
  ——**不是真的权限问题，是命令规模触发的沙箱限制**。短命令正常，长命令必挂；`Set-ExecutionPolicy`
  本身也会触发。对策：长逻辑写进 `.ps1`，命令只留 `powershell.exe … -File <脚本> -短参数`；或拆成多条短命令。
- **Bash 工具 PATH 损坏**（ls/wc/dirname 找不到）；**PowerShell 正常但 stdout 会被吞** →
  探测类命令写临时文件再 Read，用完删。Read/Glob/Grep/Write/Edit 不受影响，优先用它们。
- **沙箱会误报，以脚本自身日志为准**：命令含 `C:\Windows\System32\...` 路径、用 `Start-Process`、
  长任务收尾的文件访问都可能触发 `SandboxError`（曾把一次成功的打包判成 failed）。**别当事实结论。**
  （2026-09-14 那次"构建失败"的真因是**执行策略 + 后台 120s 超时**，不是沙箱 —— 先查这两项。）
- HTTP 走代理 `http://127.0.0.1:49237`（`http_proxy`/`https_proxy`，`urllib.getproxies()` 即可）；
  大文件下载用 Python urllib + Range 续传比 PowerShell 稳，输出前 `sys.stdout.reconfigure(encoding="utf-8")`。
- **venv**（跑源码调试与测试，勿污染系统 Python）：
  `C:\Users\yfjz\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
  （flask 3.1.3 / cryptography / openpyxl 3.1.5）。
- **agent-browser**（`D:\OpenClaw\npm-global\agent-browser.cmd`，**只能用 PowerShell 调**；Chrome 在
  `C:\Users\yfjz\.agent-browser\browsers\`）：调前设 `no_proxy=127.0.0.1,localhost`（curl 访本机必须
  `--noproxy '*'` 否则 502）；**默认视口约 1080×480**，实测前先 `set viewport 1440 1000`（否则真实
  点击会落到遮罩关掉模态框）；**每次 CLI 调用都是新会话**，登录+操作必须放**同一次 `batch`**；
  batch 按空格切参数（eval 写无空格表达式或改用 `get text/count/attr`）；插件页直开
  `/plugin/<id>/index.html`（同源 Cookie 有效）最省事。
- **编码约定**：`.ps1` = UTF-8 BOM + CRLF；`.bat` = GBK + CRLF（**不要 `chcp 65001`**）；脚本写的 JSON 一律 UTF-8 无 BOM。

## E 测试隔离（保护真实数据 `~/.jztoolshub`）

1. 先 `jztools_data.get_data_root = lambda: <临时目录>`，**再** `import app`，复制 `config/tools.json`
   进临时目录，`register_plugin_backends(app)`；**别调 `init_data_root()`**（会触发旧数据迁移）。
2. 优先 `app.test_client()`；需真实 HTTP 用 `app.run(port=5099)`（避开用户的 5000），后台任务用
   Bash `run_in_background`（`&` 会随工具调用结束被回收）。
3. 浏览器走查先把数据根 `copytree` 到临时目录（约 10MB/200 文件），在副本 JSON 注入预置记录与配套
   文件再启服务，真实库全程只读。
4. **外部程序夹具**：临时目录写 `soffice.bat`（`@echo off` + `"<venv python>" fake_soffice.py %*`，
   Windows `subprocess.run` 能跑 .bat 不能跑 .py），脚本解析 `--outdir`/源文件、输出**结构合法最小
   PDF**（xref 偏移要对，pdf.js 能开）；测试把探测函数打桩指向它，失败态用标志文件控退出码。
   验证 LibreOffice 链路时，**每次转换都要用唯一临时 profile**（同一 profile 连跑两次会因
   单实例限制失败，rc=4294967295 —— 也正是应用线上的真实行为）。

## F 其它高频坑

- **数据根指针**：`config/data_root.json` 是 `get_data_root()` 的**备份指针**（主指针
  `~/.jztoolshub.json` 缺失时用），已 gitignore + `git rm --cached`，**别加回版本库**；
  `install.ps1:264` 有"必须含 exe"守卫，别拆。
- **openpyxl 4 坑**：① `read_only=True` 读不到 merged_cells（合并区只有左上角有值）；② `data_only=True`
  对从未被 Excel 计算过的公式返 `None`；③ Excel 只存 **15 位有效数字**（18 位身份证被静默改写，只能
  靠"原始单元格 ≥1e15"识别，显示格式不影响取值）；④ openpyxl 拒写 XML 控制字符，CSV 可携带需清理。
- **插件 backend 跨模块 import 两步式兜底**：`try: from . import X as _X / except ImportError: try:
  import X as _X / except ImportError: _X = None`（否则 importlib/补偿实例永远判"模块不存在"）。
- **LibreOffice headless**：复用固定 profile 15~18s、新建临时 37~40s，失败重置 profile 再试；
  `soffice --version` 在解包版不退出（超时 10s 兜底）；`URE_BOOTSTRAP`/`SAL_*` 实测无效；
  中文 PDF 依赖 Windows 自带字体（宋体/微软雅黑/黑体）。
- **文档同步**：改功能后回写 `README.md` → `plugins/<id>/README.md` → `docs/<功能>-设计文档.md` →
  `HANDOFF.md`（状态头「最后更新」/§2/§3/§7）→ 本目录当日日志。写"当前状态"类内容前先
  `git status --short` / `git log -1` 核实，**别沿用旧段落**。仓库**没有** `prompt.json`（模板已删）但
  `_TEMPLATE_SYNC` 与 `install.ps1` 仍登记 → 升级必告警；改提示词只能改 `llm_client.py` 内置默认值。
- **前端图标**：目标环境含旧浏览器（Chrome 72/78），emoji 渲染不稳（CSS `content` 可能显示方块）→
  新样式用纯文本或自绘 SVG，别依赖 emoji 字体。
- **往包里塞东西前先问"该不该做成组件包"**：可独立分发/升级/卸载、目标机按需安装 → 走组件包，
  别把主包撑大。
