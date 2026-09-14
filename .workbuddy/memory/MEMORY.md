# JZToolsHub 项目长期记忆

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
