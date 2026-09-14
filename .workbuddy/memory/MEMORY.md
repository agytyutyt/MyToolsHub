# JZToolsHub 项目长期记忆

> 改代码 / 打包 / 写文档前先扫本文件。过程细节与实测数据见 `.workbuddy/memory/` 当日日志（先查最新）。

## A 硬约定（违反即事故）

1. **配置模板必须叫 `config.template.json`**：打包脚本按精确名删插件树内所有 `config.json`，
   同名模板会被一起删 → 部署形态模板同步静默失效。新增带模板的插件要在
   `jztools_data._TEMPLATE_SYNC` 与 `install.ps1 Sync-ConfigTemplates` **两处同时登记**；
   打包后自检 `*.template.json` < 4 即中止。
2. **插件后端路由函数名必须带插件前缀**（`kb_/ts_/ff_/cr_/admin_…`）：Flask 用
   `view_func.__name__` 作 endpoint，重名直接 AssertionError，且无法回滚已注册一半的路由。
3. **打包不传 `-Version` 自动递增 patch；版本号与上一版相同 → `build-deploy.ps1` 中止**
   （版本不变 → 目标机 `sync_templates()` 判未升级 → 模板同步全跳过）。

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
- **PyInstaller 多入口可共享 `_internal`（2026-09-15 实测）**：两个 `Analysis` → 两个 `EXE`
  → **同一个 `COLLECT`**，两个 exe 的 `sys._MEIPASS` 相同，依赖二进制只有一份；
  多一个入口的增量 **3.00 MB / +9.7% 且为一次性**（`_internal` 不增大，增量就是第二个
  exe 的 bootloader + 自带 PYZ）。且 `build-deploy.ps1:132` 是整目录搬运，
  **新增 exe 不用改构建脚本**。冷启动 431ms（stdlib+cryptography 口径）。
  详见 `docs/插件热插拔-容器化改造评估.md` 附录 A。

## C Python / 浏览器基线

- 目标机 Win10+；**浏览器基线 Chrome ≥72**（main.js 规避、jz-icon SVG 回退、pdf.js legacy
  的触发条件是"旧浏览器/缺彩色 emoji 字体"，**不是 Win7，别删**）。
  口径：最低 3.12 / 打包 3.14；排除 3.10。
- **zfec 无 cp314 wheel**：必须 `pip install --find-links wheels`（`wheels/zfec-1.6.0.0-cp314-*.whl`，
  重建 `tools/build-zfec-wheel.py`）；GPL-2+ 再分发注意合规。
- **vendor 升级后必须重打补丁**（`vendor/README.md` 现有 5 条：xhr Optional 导入、xhr 空格样式、
  dhr numId=0、dhr 字符缩进、dhr .doc 警告按模式分流）并**递增 `routes.py` PREVIEW_CACHE_VERSION**
  （现为 3），否则旧缓存不重渲染。
- `office_render` 导入失败被静默兜住 → 引擎不可用只在 `/api/knowledge-base/status` 报
  `office_render.available=false`。
- openpyxl：read_only 读不到 merged_cells；data_only 对未计算公式返 None；只存 15 位有效数字。

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
