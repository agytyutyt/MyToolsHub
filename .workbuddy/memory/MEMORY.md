# JZToolsHub 项目长期记忆（精简版）

> 打包发版只读 `docs/guide/打包部署手册.md`；**找任何文档先看 `docs/README.md`**（唯一索引 + 覆盖矩阵）。本文件只放硬约定与关键事实。

## A 硬约定（违反即事故）

1. 配置模板必须叫 `config.template.json`；新增带模板插件要在 `jztools_data._TEMPLATE_SYNC` 与 `install.ps1 Sync-ConfigTemplates` 两处登记；打包后自检 `*.template.json` < 4 即中止。
2. 插件后端路由函数名必须带插件前缀（`kb_/ts_/ff_/cr_/admin_…`），否则 Flask endpoint 重名会抛 AssertionError。
3. 打包不传 `-Version` 自动递增 patch；版本与上一版相同 → `build-deploy.ps1` 中止。
4. `bg_image.py` 刻意双份（file-filter 与 trajectory-sketch 各一份），改动必须 `cp` 同步，`test_bg_image.py` 断言 sha256 一致。
5. 出包 dirty 计数 = 全仓库 `git status --porcelain`；并发会话期间先提交/本地排除；**禁用 `git stash -u`**（会删别人未跟踪文件）。
6. 只想提交部分文件时必须 `git commit -F - -- <pathspec>`；提交前先看 `git diff --cached --name-only`。

## B 分发物要点

- **知识库 Word 预览补丁（阶段 9，2026-09-18）**：三处显示缺陷（仿宋_GB2312 缺失时回退黑体
  导致"莫名加粗"、`<tr>` 的 `overflow:hidden` 无效导致穿模、`<p>` 默认 margin 撑高单元格）
  在 `office_render.py` 的 `_polish_word_html()` 里以 **HTML 后处理**修复，**vendor 零改动（B-7）**。
  改这段必须同时：① 递增 `routes.py` 的 `PREVIEW_CACHE_VERSION`（否则目标机旧缓存让修复不生效）；
  ② 跑 `test_routes_preview.py` 第 0b 节 6 项断言。
  **裸 `"仿宋"` 不可放进 `_FANGSONG_NAMES`**（既是查找键又是插入值，会自我匹配破坏幂等）。
- LibreOffice 一键安装器：`msiexec /a` 解包到 `runtime\libreoffice\`，不写注册表；自检 = 真转换 + 查产物，失败先**自动重解压复检**；退出码 0/1/2/3；超时 240s；日志「安装日志-LibreOffice核心.txt」。
- **VC++ 运行时必须复制到 `program\`**，否则目标机 soffice 崩 `0xC0000142`。`msiexec /a` 会把 VC++ DLL 落到 `System64\`，安装器须用 `Repair-VcRuntime` 把它们搬到 `program\`；`System64\` 不可删。
- soffice 转换首屏/冷启动可能超时；vendor 默认 60s，`office_render` 未覆盖，慢机器需评估上调。
- **知识库「异步下载源」（阶段 10）**：上传可另提供下载文档，落盘 **`<id>.dl.<ext>`**（不得与原件/缓存重名），
  `/download` 优先返回它、缺失回落原件；记录 `download_ext/name/size` **未配置则不写键**；
  校验失败**不阻断主上传**（响应带 `download_error`）。改这块必须跑 `test_routes_preview.py` 第 4b 节。
- **自定义开关（switch）铁律（阶段 11）**：`<label>` 必须**直接包住 `<input>`**（如 `.switch-row` 整行是 label）。
  `.slider` 是 `position:absolute;inset:0` 覆盖层，会盖住隐藏 input——只写 `<label for="x">` 指向它
  **点滑块无效**（曾是"开关点不动"的真因）。`test_routes_preview.py` 第 6 节有静态契约断言，禁止改回 div/span。
- 主包版本一律三段式 `X.Y.Z`。
- 信息传输：**单码口径**，`docx/doc/xlsx/xlsm/xls/csv/txt/md/markdown/ppt/pptx/pdf` 12 个 ext 在册；`ppt`/`pdf` 走精简文本；**多码同屏不采用**。
- 本机打包：先 commit → `tools/build-deploy-local.py -ZipOnly` → `verify-package.py --smoke`。

## C Python / 浏览器基线

- 目标机 Win10+；浏览器 Chrome ≥72；Python 最低 3.12 / 打包 3.14。
- **openpyxl `read_only=True` 读范围取 `<dimension>` 声明，可能失真** → 修法 = `ws.reset_dimensions()` + 补齐行宽，两步成对。
- 动 xlsx 包只能做 zip 级字节手术；Canvas 必须落整数设备像素。
- vendor 升级后须重打补丁并递增 `routes.py` PREVIEW_CACHE_VERSION。

## D 本机限制与工具

- PowerShell 执行策略 Restricted：用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`。
- Bash PATH 损坏 → 全部绝对路径；**且不要 `cd` 进项目目录**（触发 `cd: null directory` 后长命令会被 SIGTERM 截断）。长脚本输出写文件再读。
- **浏览器探针页**（放 `backend/out/`）：引 `frontend/` 必须 `../../frontend/`；`<link>` 要加 `?v=<mtime>` 破 `file://` 缓存；否则整个样式表静默失效（易误判为新 CSS 写错）。探针要跑**真实函数**就从源码抽取，别手抄副本。
- **agent-browser**：`D:\OpenClaw\npm-global\node_modules\agent-browser\bin\agent-browser-win32-x64.exe`；Bash 直接调；多步在同一调用链内 `&&` 串联；eval 禁 `| & >`；先 `set viewport`；原生 click 对插件页无效，用 `eval "el.click()"`。
  **验证 UI 交互必须用真实指针 `click`**（不是 eval 里的 `.click()`）——程序化点击会掩盖"点滑块无反应"这类真实缺陷。
  **会话不跨命令**：任何非浏览器命令（`echo` 等）或跨 `;` 的独立调用都会重置页面（元素变 null）；`open → click → eval → screenshot` 必须同链。
  插件页 E2E 隔离实例须伪造 `jztools_admin.routes.get_session_user` 返管理员，否则 API 全 401 且页面被重定向到登录页。
- **同文件多个 Edit 绝不能并行**（同一消息内并行会静默覆盖）。
- venv：`C:\Users\yfjz\.workbuddy\binaries\python\envs\default\Scripts\python.exe`。
- PyInstaller COLLECT 重建 dist\JZToolsHub 时加 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 前缀。

## E 测试隔离

1. 隔离数据根：`JZTOOLS_DATA_ROOT` 或 `jztools_data.get_data_root = lambda: <临时目录>`。
2. 优先 `app.test_client()`；真实服务用 5100+ 端口。
3. soffice 转换用唯一临时 profile。

## F 其它

- 后端跨模块 import 用两步式兜底（relative → absolute → None）。
- 前端图标别依赖 emoji 字体。

## G 文档体系

- 唯一入口 `docs/README.md`；五层：`guide/`/`design/`（随包）、`eval/`/`plan/`/`archive/`（不随包）。
- 改功能后回写：`plugins/<id>/README.md` → `docs/design/...` → `docs/README.md` + 该文档「最后核对」 → `HANDOFF.md` → 当日日志。
