# JZToolsHub 项目长期记忆

## 环境与工具（先看这里，能省很多时间）

- **本机无自带 Flask**。已建隔离 venv（本会话创建）：
  `C:\Users\yfjz\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
  （flask 3.1.3 / cryptography / openpyxl 3.1.5）。跑源码调试与测试都用它，别污染系统 Python。
- **浏览器自动化已安装**：`agent-browser`（npm 全局，PATH 需含 `D:\OpenClaw\npm-global`；
  Chrome 在 `C:\Users\yfjz\.agent-browser\browsers\`）。
  - 调用前先设 `no_proxy=127.0.0.1,localhost`（本机有 `http_proxy` 环境变量会拦本机请求）。
  - `curl` 访问本机服务必须加 `--noproxy '*'`，否则返回 502。
  - **默认视口约 1080×480**：模态框高于视口时，真实鼠标点击会落到遮罩上把浮窗关掉
    （合成 `el.click()` 不受影响）。实测前先 `agent-browser set viewport 1440 1000`。
  - 登录态不跨 CLI 调用保持：**登录 + 后续操作要在同一次 Bash 调用内链式执行**。

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
