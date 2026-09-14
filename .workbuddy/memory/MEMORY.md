# JZToolsHub 项目长期记忆

## 离线部署包（2026-09-14 建立，动打包/安装必须连带看这里）

- **产物**：`deploy/JZToolsHub-v<版本>.zip`（v1.7 = **618.4MB**；部署目录 777.5MB / 2886 文件）。
  体积大头是随包分发的第三方安装包：`runtime/` 517.2MB（Chrome 企业版 MSI 159.6MB +
  LibreOffice 26.8.0 MSI 357.5MB）。
- **四件套（改一个必须看另外三个）**：
  `tools/offline-components.json`（清单，入库）→ `tools/fetch-offline-bundle.py`（下载/校验）
  → `tools/offline-runtime/*`（随包脚本，会被复制到部署包的 `runtime/`）
  → `build-deploy.ps1` 的 3.2.2 段（组装 + 按 manifest 校验完整性）。
- **`runtime/` 已 gitignore**；二进制不入库，由获取脚本随时重建。
  **便携解包目录绝不能放进仓库 `runtime/`**（会与 MSI 一起打进包，体积翻倍）。
- **LibreOffice 用 `msiexec /a`（管理安装）**：免管理员、不写注册表，解出即可运行
  （实测 26.8.0：退出码 0 / 95.5s / **解出 1.52GB** / `program\soffice.exe` 直接在 TARGETDIR 根下）。
  调 msiexec 必须 `Start-Process -Wait -PassThru` 取 ExitCode（`&` 会提前返回）。
- **应用侧自动探测便携 soffice（零配置）**，优先级：
  插件配置 `office.soffice_path` > 环境变量 `XHR_SOFFICE` > `<程序目录>/runtime/libreoffice/program/soffice.exe`
  （多套一层时在 `runtime/libreoffice/**` 有限深度 glob）。
- **Chrome 是全机 MSI，需要管理员**；LibreOffice 不需要。所以 `setup-offline-runtime.ps1`
  对 Chrome 提权失败只打印指引、**不报错**（离线组件失败绝不能阻断安装）。
  `install.ps1` 调它必须走**子进程**（该脚本以 `exit` 结尾，dot-source 会终止安装流程）。
- **打包耗时**：全量约 16 分钟（PyInstaller 约 5min，其余为 777MB 文件复制与压缩）；
  只重出 zip 用 `build-deploy.ps1 -ZipOnly` **约 2 分钟**（**先把新文件复制进 `deploy/JZToolsHub/`**）；
  出瘦包用 `-SkipOfflineRuntime`。
- **发版前校准组件**：Chrome 用的是固定 URL `stable`，内容会随 Google 更新，
  冻结的 sha256 会失配 → 重跑 `fetch-offline-bundle.py --pin`。
- 未验证：Chrome 静默安装（需提权，开发会话没有）、真断网机器的完整链路走查。

## 本机（开发机）工具坑速记（2026-09-14 实测新增）

- **PowerShell 变量名不区分大小写**：局部变量不要与 switch 参数同名
  （`$zipOnly` vs `-ZipOnly` 会撞成同一变量，给 SwitchParameter 赋字符串直接抛错）。
- **沙箱会误报，以脚本自身日志为准**：命令里含 `C:\Windows\System32\...` 路径、
  用 `Start-Process`、或长任务收尾时的文件访问，都可能触发 `SandboxError`
  （曾把一次成功的打包任务判成 failed）。**不要把沙箱报错当成事实结论。**
- HTTP 下载走代理 `http://127.0.0.1:49237`（环境变量 `http_proxy`/`https_proxy`），
  Python `urllib` 用 `getproxies()` 即可，无需手工设。
- 大文件下载用 Python 脚本（`urllib` + Range 续传）比 PowerShell 稳；输出用
  `sys.stdout.reconfigure(encoding="utf-8")` 避免乱码。

## Python 版本基线（2026-09-14 实测定论）

- **Win7 目标机支持已取消**（2026-09-14），目标机基线 = **Windows 10+**。
  因此"必须用 3.8 打包"的唯一理由消失，**3.8 支线删除**。
- **不需要升级 Python 版本**：基线已是 **3.14**（最新稳定版），3.15 要到 2026-10-01 才发布。
- 文档口径统一为"**最低 3.12 / 推荐与打包 3.14**"。**明确排除 3.10**
  （2026-10-31 EOL，只差 71 天）。
- **OS 基线 ≠ 浏览器基线（易误伤）**：`main.js` 的 Chrome 72 规避、`jz-icon.js`+`static/icons/`
  的 SVG emoji 回退、pdf.js legacy 构建 —— 触发条件是"旧浏览器/缺彩色 emoji 字体"，
  **不是 Win7，不要随 Win7 一起删**。浏览器基线 = **Chrome ≥72**，与 OS 基线并列写在 README §2.1。
- **KB 预览引擎的真凶是一行 bug**（不是版本要求）：
  `plugins/knowledge-base/backend/vendor/xhr/__init__.py` 用了 `Optional` 却既没 import
  也没 `from __future__ import annotations`，而该注解运行时立即求值 →
  **3.8 与 3.13 实测都是导入即 `NameError`**；3.14 能用纯属 PEP 649 惰性注解掩盖。
  **2026-09-14 已修**（补 `from typing import Optional`），实测三版本
  `office_render.availability()` 均为 `{'available': True, ...}`，渲染产物字节一致。
  该补丁属「vendor 升级后必须重打」清单，与 `renderer/table.py` 补丁同级，登记在 `vendor/README.md`。
- **★ zfec 无 cp314 wheel，已用预编译 wheel 缓解**：
  `wheels/zfec-1.6.0.0-cp314-cp314-win_amd64.whl`（随仓库分发），安装依赖必须
  `pip install --find-links wheels ...`，否则退化为源码编译（要求 MSVC）。
  重建用 `python tools/build-zfec-wheel.py`；升级 Python 小版本前先按 `wheels/README.md`
  的检查项确认新 wheel。**zfec 是 GPL-2+，再分发需确认合规口径。**
  wheel 类型速记：`zfec`/`numpy`/`pillow` = `cpXX-cpXX`（版本锁定，每个小版本都要新 wheel）；
  `opencv-python`=cp37-abi3、`cryptography`=cp311-abi3（稳定 ABI，跨版本可用）。
- 影响隐蔽：`office_render` 用 `except Exception` 兜住导入失败 → 引擎不可用时**静默失去原样式预览**，
  只在 `/api/knowledge-base/status` 报 `office_render.available=false`。

## 本机构建 C 扩展的沙箱限制（Windows 开发机）

`pip wheel <sdist>` 在本机会失败，三层原因：
1. setuptools 定位 MSVC 会调 `reg.exe` → **被沙箱程序黑名单拦截**；
2. `cmd.exe` 也被禁 → 无法用 `vcvarsall.bat` / `VsDevCmd.bat` 建标准编译环境；
3. 手工拼 `INCLUDE`/`LIB`/`PATH` 并设 `DISTUTILS_USE_SDK=1`+`MSSdk=1` 后 cl.exe 能被找到并执行，
   但报 `error: [WinError 2]` —— 根因是 **setuptools 在 `DISTUTILS_USE_SDK` 模式下会剔除
   子进程的 `PATH`**，裸名 `cl.exe` 找不到；设 `CC` 全路径**无效**（新版 setuptools
   的 C 编译器模块不读 `CC`）。

可行替代：把**已装好的编译产物重打成标准 wheel**（zip 结构 + dist-info/{METADATA,WHEEL,RECORD}
+ `licenses/`，RECORD 用 url-safe b64 无填充 sha256），见 `tools/build-zfec-wheel.py`。
本机可用的编译器：`C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools`（MSVC 14.51、
SDK 10.0.26100）。

## 三条硬约定（2026-09-14 修复 P0 后确立，改代码务必遵守）

1. **配置模板必须命名 `config.template.json`，绝不叫 `config.json`**。
   打包脚本按精确名删插件树内所有 `config.json`（清本机含 API Key 的运行时配置），
   模板同名会被一起删掉，导致部署形态下模板同步**静默失效**（曾使 6 条登记项只剩
   `tools.json` 有效）。新增带模板的插件要在 `jztools_data._TEMPLATE_SYNC` 与
   `install.ps1` 的 `Sync-ConfigTemplates` **两处同时登记**；打包后脚本会自检
   `*.template.json` 数量（< 4 即中止）。
2. **插件后端路由函数名必须带插件前缀**（`kb_*` / `ts_*` / `ff_*` / `cr_*` / `admin_*` …）。
   Flask 用 `view_func.__name__` 作 endpoint，两个插件各写 `def status()` 会抛
   `AssertionError: View function mapping is overwriting an existing endpoint function`。
   框架已加 per-plugin 隔离（失败记入 `app._plugin_load_errors`，不再拖垮整站），
   但**隔离无法回滚已注册一半的路由**，纪律不能松。
3. **发版打包不传 `-Version` 也会自动递增 patch**；版本号与上一版相同时
   `build-deploy.ps1` **直接报错中止**（除非 `-Force`）。原因：版本号不变 →
   目标机 `sync_templates()` 判定未升级 → 全部模板同步被跳过（"重打包了但配置没变"的真因）。

## 数据根目录指针（易踩）

`config/data_root.json` 是 `get_data_root()` 的**备份指针**（不是垃圾文件）：
新机器克隆且主指针 `~/.jztoolshub.json` 缺失时，数据根会被解析成该文件里的路径。
已 `.gitignore` + `git rm --cached`。别再加回版本库。
`install.ps1` 的"既有安装判定"也读它，但 `:264` 有"必须含 exe"守卫，**别拆**。

## 文档体系（2026-09-14 重写后的结构，改文档按此走）

- `README.md`：项目简介 → 环境搭建 → 运行与构建 → 目录结构 → 核心模块解析 →
  使用示例 → HTTP API → 内置插件一览 → 典型业务链路 → 配置模板同步与数据迁移 →
  访问日志 → 故障排查 → 开发者约定速查。
- `HANDOFF.md`：项目定位与架构速记 → **当前 git 实况** → 已完成事项 → 待办（P0/P1/P2）→
  关键架构决策（ADR 表）→ 已知问题 → 踩坑清单（按主题：数据安全/后端/前端/打包/第三方库/工具链）
  → 插件后端速查表 → 移动端 → 文档同步约定。
- `20260914评估报告.md`：插件规范符合性矩阵 + 开发/更新/移除便利性 + 规范 16 条优化建议 + 26 项问题清单。
- **文档滞后是本项目反复出现的风险**：改功能后必须回写 README/HANDOFF；
  写"当前状态"类内容前先用 `git status --short` / `git log -1` 核实，别沿用旧段落。
- 仓库**没有** prompt.json（模板已被删），但 `_TEMPLATE_SYNC` 与 `install.ps1` 仍在登记 → 升级必告警；
  修提示词目前只能改 `llm_client.py` 内置默认值。

## 环境与工具（先看这里，能省很多时间）

- **本会话工具坑（2026-09-11 实测）**：Bash 工具 PATH 损坏（ls/wc/tail/dirname command not found，
  仅内建 echo 可用）；PowerShell 执行正常但 **stdout 被吞**——探测类命令写临时文件再 Read，
  用完删掉；Read/Glob/Grep/Write/Edit 不受影响，优先用它们。
- **本机无自带 Flask**。已建隔离 venv（本会话创建）：
  `C:\Users\yfjz\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
  （flask 3.1.3 / cryptography / openpyxl 3.1.5）。跑源码调试与测试都用它，别污染系统 Python。
- **浏览器自动化已安装**：`agent-browser`（npm 全局，PATH 需含 `D:\OpenClaw\npm-global`；
  Chrome 在 `C:\Users\yfjz\.agent-browser\browsers\`）。
  - 调用前先设 `no_proxy=127.0.0.1,localhost`（本机有 `http_proxy` 环境变量会拦本机请求）。
  - `curl` 访问本机服务必须加 `--noproxy '*'`，否则返回 502。
  - **默认视口约 1080×480**：模态框高于视口时，真实鼠标点击会落到遮罩上把浮窗关掉
    （合成 `el.click()` 不受影响）。实测前先 `agent-browser set viewport 1440 1000`。
  - 命令在 `D:\OpenClaw\npm-global\agent-browser.cmd`，**只能用 PowerShell 调**（Bash 缺 sed/uname 跑不了；
    Bash 的 PATH 本来就残废）。PowerShell 里用 `Out-File -Encoding utf8` 落盘再 Read（`>` 出来是 UTF-16）。
  - **每次 CLI 调用都是新会话**：登录态不跨调用，必须把「登录 + 操作」放进**同一次
    `agent-browser batch "cmd1" "cmd2" ...`**（batch 内共享 daemon）。
  - **batch 按空格切分参数**，JS 里的空格和 `|` 会被 cmd 拆掉 → `eval` 脚本要么写成无空格表达式，
    要么改用 `get text/count/attr`。
  - **插件前端跑在 iframe 里**：`/tool/<id>` 是外壳页，`/plugin/<id>/index.html` 才是插件页。
    `click <css>` / `eval` 默认作用于父文档会 "Element not found"，登录后直接开
    `/plugin/<id>/index.html`（同源，Cookie 有效）最省事。

## 给「外部转换程序」做测试夹具（无需真装程序）

要验证调用外部可执行程序（如 LibreOffice soffice）的链路，不必在机器上真装：
在临时目录写 `soffice.bat`（`@echo off` + `"<venv python>" "%~dp0fake_soffice.py" %*`，
Windows 的 `subprocess.run` 能直接跑 .bat 但不能跑 .py），Python 脚本解析 `--outdir` 与源文件参数、
输出一份**最小但结构合法**的 PDF（含正确 xref 偏移，pdf.js 能打开），
测试里把探测函数打桩指向这个 bat 即可。失败态可在脚本里读一个标志文件控制退出码。

## 浏览器走查不要污染真实数据

跑真机走查时**不要把测试数据上传进用户真实的 `~/.jztoolshub`**：
把数据根整目录 `copytree` 到临时目录（约 10MB/200 文件），在副本的 JSON 里直接注入预置记录与
配套文件，再用「patch `jztools_data.get_data_root` → 起 app」的隔离套路启动服务（端口用 5099
避开用户的 5000）。真实库全程只读。

## 隔离测试套路（务必遵守，保护用户真实数据 `~/.jztoolshub`）

1. 先 `import jztools_data; jztools_data.get_data_root = lambda: <临时目录>`，**再** `import app`，
   把项目 `config/tools.json` 复制进临时目录，然后 `register_plugin_backends(app)`。
2. 用 `app.test_client()` 直接打接口。**不要调用 `init_data_root()`**（会触发旧数据迁移）。
3. 需要真实 HTTP 时用 `app.run(port=5099)`（避开用户的 5000）；后台任务必须用 Bash 的
   `run_in_background`，用 `&` 会随工具调用结束被回收。

## 文档同步约定（改功能后必做）

`README.md`（接口表 / 功能说明 / 内置插件一览）→ `plugins/<id>/README.md` →
`docs/<功能>-设计文档.md` → `HANDOFF.md`（状态头「最后更新」、§2 git 状态、§3 插件一览、
§7 踩坑、§7.5 速查表）→ 本目录当日日志。

## 前端图标约定

目标环境含旧版浏览器（Chrome 72/78），**emoji 渲染不稳定**：CSS `content: '🎉'` 可能显示为方块。
新样式优先用纯文本或自绘 SVG，不要依赖 emoji 字体。

## openpyxl 读取表格的 4 个坑（读用户上传的 Excel 必踩）

1. **`read_only=True` 读不到合并单元格**（`ReadOnlyWorksheet` 没有 `merged_cells`），
   而合并区域只有左上角有值、其余是 `None` → 纵向合并的"所属单位"下面几行会集体缺值。
   要处理合并必须用普通模式加载。
2. **`data_only=True` 对"从未被 Excel 计算过"的公式返回 `None`**（只读缓存值）。
   判断"是不是公式"要再用 `data_only=False` 加载一遍比对。
3. **Excel 数字只保留 15 位有效数字**：18 位身份证按数字存会被静默改写
   （读回来是 `1.101011990010112e+17`），而 Excel 里显示的还是原值——
   只能靠"原始单元格是 float/int 且 ≥1e15"识别，不能靠显示值。
   单元格的百分比/货币/日期等**显示格式不影响取值**，取到的是底层值。
4. **openpyxl 拒绝写入 XML 非法控制字符**（`\x00`/`\x07` 抛 `IllegalCharacterError`），
   造测试夹具时别塞；但 **CSV 可以携带**，解析时要清理。

## 插件 backend 共享模块 import 必须两步式兜底

`from . import X` 在主应用包上下文加载时 OK，**但** importlib/补偿测试/重启后的实例可能无包上下文，
裸 `import X` 兜底缺失 + except 太宽（`except Exception` 吞掉 ImportError）会导致实例永远判定「模块不存在」。
**所有插件 backend 内跨模块引用**都写：
```python
try:
    from . import X as _X
except ImportError:
    try:
        import X as _X
    except ImportError:
        _X = None
```
（与项目里 doc_convert 的写法一致）

## LibreOffice headless 转换经验

- **管理安装解包** `msiexec /a <msi> /qn TARGETDIR=D://LibreOffice` —— 非管理员可装 Windows 大型应用，
  解出完整目录可直接运行（soffice.exe），无需注册表注册
- **profile 复用提速**：每次新建临时 profile 37~40s，**复用固定 profile**（`<数据根>/.lo-profile/`）15~18s；
  失败时重置 profile 再试一次（避免「一次损坏、永久失败」）
- `soffice --version` 在解包版会启动后不退出（URE 未注册），超时压到 10s 兜底；版本信息仅展示用，丢失不影响可用性
- `URE_BOOTSTRAP`/`SAL_*` 环境变量无效（实测），不要加
- 中文 PDF 渲染依赖 Windows 系统字体（宋体/微软雅黑/黑体）—— 这些都在 C://Windows//Fonts 自带
