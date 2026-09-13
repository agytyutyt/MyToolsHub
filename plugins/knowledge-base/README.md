# 知识库（knowledge-base）

在线阅读 PDF / OFD / Word / Excel / Markdown / 纯文本文档的只读知识库插件。

## 功能

- 管理员（管理员角色 / 超级管理员）：
  - 上传文档（≤20MB）：`.pdf .ofd .docx .doc .xlsx .xls .csv .md .markdown .txt`
  - **旧版格式自动转换**：`.doc` → `.docx`、`.xls` → `.xlsx`，服务端转换后落盘
    （库内只保留现代格式）；页面三处提示——上传 Snackbar、卡片"已转换"角标、阅读页顶部说明条。
    转换仅保留正文文字与表格，样式/图片/页眉页脚不迁移。
  - **Word 的 PDF 预览**：上传后由服务端异步转成 PDF，在线阅读走 **pdf.js**，
    **按原文档样式**展示（解决 mammoth 丢样式的问题）；
    转换期间卡片显示"PDF 生成中"角标，就绪后自动切换（详见下方「Word 的 PDF 预览」）。
  - **Excel 的表格预览（替代 PDF 预览，不分页）**：上传后由服务端 `xlsx_render`
    （openpyxl 手绘 HTML，阶段 3 起）生成**连续单页**网页表格——还原合并单元格/列宽/行高/
    边框/填充/字体/对齐/数字与日期显示格式，多 sheet 页签切换；宽表长表不再被打印分页切碎；
    渲染期间卡片显示"预览生成中"角标，就绪后自动切换（详见下方「Excel 表格预览」）。
  - 多级分类管理：新建 / 重命名 / 移动 / 删除（仅空分类可删，同级不重名）
  - 文档管理：改名 / 移动分类 / 删除（在文件卡片上右键）
  - PDF / 表格预览生成失败时可分别用 `POST /files/<id>/pdf-retry`、
    `POST /files/<id>/preview-retry` 手动重转（仅管理员）
- **全体登录用户**：浏览分类、阅读、选择复制、**下载原始文档**（tools.json 中 `grant_all: true`）
- **只读保证**：无任何编辑接口；渲染容器不可编辑；PDF 版仅在线展示，下载一律给原始文档

## 技术

- 后端：核心功能纯 Python 标准库；**旧版格式转换与 Excel 表格预览**依赖
  openpyxl（doc/xls 转换写 xlsx；`xlsx_render.py` 读 xlsx 手绘 HTML）/
  xlrd（读 xls）/ python-docx（写 docx）/ olefile（读 doc OLE2），
  见 `backend/doc_convert.py`、`backend/xlsx_render.py`。依赖缺失时仅自动转换与
  表格预览不可用（`GET /status` 的 `convert_legacy=false` / `xlsx_preview=false`，
  上传接口 422 明确提示、阅读回退 SheetJS），其余功能不受影响（优雅降级，B-4）。
- 数据落盘数据根目录 `plugins/knowledge-base/data/`
  （categories.json / files.json / files/），记录含
  `created_by / created_by_name / unit_id / department_id` 归属四字段（规范 9.2 铁律一）。
  上传后**最多四件套**（按绝对路径去重，不重复落盘）：

  | 文件 | 说明 |
  | --- | --- |
  | `<id>.<original_ext>` | **原件**（所有类型都存），下载端点专用 |
  | `<id>.docx` / `<id>.xlsx` | 降级渲染件（仅旧版 `.doc`/`.xls` 由 doc_convert 生成），供 mammoth/SheetJS 兜底 |
  | `<id>.pdf` | 展示用 PDF（仅 Word 类，服务端转换生成；阶段 3 起 Excel 不再生成） |
  | `<id>.json` | Excel 表格预览（仅 xlsx/xls 类，`xlsx_render` 生成，含各 sheet 的 HTML 片段） |

  元数据字段：`ext`（归一化主格式）/ `original_ext`（上传时扩展名，恒有值）/
  `original_size` / `pdf_status`（none\|pending\|ok\|failed）/ `pdf_size` / `pdf_error` /
  `html_status`（none\|pending\|ok\|failed，Excel 类）/ `html_size` / `html_error`。
- 前端：原生 JS + Material 风格，文档渲染全部在浏览器端完成（**不使用任何浏览器内置/插件控件**）：

| 格式 | 渲染库 | 复制实现 |
| --- | --- | --- |
| PDF | pdfjs-dist 3.11.174（legacy） | 「复制本页」getTextContent |
| OFD | easyofd 1.2.1（Canvas + 文本选择层） | 「复制本页」GetPageText |
- **Word .docx（含 .doc 自动转换）** | mammoth 1.6.0 → DOMPurify（**降级渲染**；已生成 PDF 时改走 pdf.js） | 选择复制 / 复制全文
- **Excel .xlsx/.xls/.csv（.xls 上传时已转 xlsx）** | 服务端手绘表格（`html_ready` 时）→ SheetJS CE 0.18.5（**降级渲染**）→ DOMPurify | sheet 表格 TSV 复制 |
| Markdown | marked 4.3.0 → DOMPurify | 复制 Markdown 原文 |
| 纯文本 | 原生 textContent | 选择复制 / 复制全文 |

- 第三方库全部 vendor 在 `frontend/vendor/`（离线内网可用，来源/版本/许可证见 `frontend/vendor/VENDOR.md`），
  按需惰性加载（打开对应格式才注入脚本）。

## Word 的 PDF 预览（原样式）

Word 类上传后，服务端用 **LibreOffice headless**（`soffice --convert-to pdf`）把**原件**
转成 PDF，阅读端直接复用 pdf.js 渲染——翻页/缩放/复制本页全部现成，样式保真接近原生打印。

- **异步管线**（规范 8.5）：上传立即返回（`pdf_status=pending`），转换在后台线程池串行执行
  （`max_workers=1` + 模块级锁），完成后回写 `pdf_status=ok`；前端每 3s 轮询，就绪后自动切到 PDF 视图。
- **断点恢复**：服务重启后自动扫描「Word 类 + 未生成 PDF」的记录重新入队补转；
  `failed` 不自动重试（防坏文件风暴），管理员可经 `/pdf-retry` 手动重转。
- **优雅降级（B-4）**：目标机没装 LibreOffice 时，上传 / 阅读 / 下载**全部照常可用**，
  阅读回退 mammoth / SheetJS（体验同改造前），卡片显示"预览降级"角标（悬停看原因）。
- **不提供 PDF 版下载**：需求明确 Word 下载给原始文档，PDF 仅在线展示。
- **Excel 不再走 PDF**（阶段 3）：PDF 按打印分页，宽表/长表被切成多页不利阅读；
  Excel 预览改由 `xlsx_render` 手绘 HTML（见下节）。历史 Excel 记录的 `pdf_status`
  在启动迁移时复位为 `none`，已生成的 PDF 文件保留但不再被阅读端消费。

## Excel 表格预览（手绘 HTML，连续单页）

Excel 类（xlsx / 自动转换后的 xls）上传后，服务端 `backend/xlsx_render.py` 用 openpyxl
读取工作簿，**Python 手绘**各 sheet 的 `<table>` HTML（参考 GitHub Apkawa/xlsx2html 的思路、
按本项目约束重写，零新增 pip 依赖），以 JSON 落盘 `<id>.json`，前端经
`GET /files/<id>/preview` 拉取后按页签注入展示。

- **样式还原范围**：合并单元格（colspan/rowspan + 外边框）、列宽/行高、隐藏行列、
  13 种边框样式、填充色（含 theme+tint 主题色）、字体（名称/字号/加粗/斜体/下划线/
  删除线/颜色）、对齐（水平/垂直/自动换行/缩进）、数字与日期显示格式（千分位/百分比/
  货币/负数括号与 [Red]/中文日期/时分秒/[h] 经时/科学计数）、网格线开关。
  不还原：批注、浮动图片/图表、条件格式、冻结窗格、富文本局部样式。
- **不分页**：整表连续单页 + 横向滚动，`table-layout:fixed` 锁定列宽不被长文本撑变形。
- **上限熔断（§3.2 降级链）**：单 sheet 超 3000 行 / 120 列 / 12 万单元格 / 4MB 输出即截断，
  表尾提示"下载原件查看全部"；源文件 >15MB 或解析失败 → `html_status=failed`，
  阅读端自动回退 SheetJS 简化渲染，上传/阅读/下载不受影响（B-4）。
- **异步管线与断点恢复**：与 PDF 管线同构（`html_status` pending→ok/failed，
  独立线程池串行渲染；重启后自动补渲，`failed` 不自动重试，可经 `/preview-retry` 手动重转）。
- **只读与安全**：不生成任何编辑控件；单元格文本服务端转义 + 前端 DOMPurify 双重消毒（SEC-5）。

### 安装与配置 LibreOffice（可选）

未安装时功能不缺失（见上「优雅降级」）。需要原样式预览时：

1. 在服务器安装 LibreOffice（25.8+ ~350MB，支持内网离线分发安装包；**不是 pip 包**，
   `backend/requirements.txt` 里没有它）。
   - **非管理员安装**：`msiexec /a LibreOffice_x.msi /qn TARGETDIR=D:\LibreOffice`
     （「管理安装」解包到非系统目录，无需 UAC）。解包后 `D:\LibreOffice\program\soffice.exe` 可直接运行。
2. 插件会自动探测：先读 `<数据根>/plugins/knowledge-base/config.json` 的 `pdf.soffice_path`
   → 注册表 `HKLM\SOFTWARE\LibreOffice\UNO\InstallPath` → 常见安装路径 → `PATH`。
   管理员可打开 `GET /status?refresh=1` 查看探测结果（`pdf_engine.available` / `path` / `version`）。
3. **装在非标准位置时**（如解包版）在**数据根目录**建 `plugins/knowledge-base/config.json` 显式指定：

   ```json
   { "pdf": { "soffice_path": "D:\\LibreOffice\\program\\soffice.exe" } }
   ```

   改完无需重启，下一次转换即生效（探测结果有缓存，`/status?refresh=1` 可强制重探）。
4. **profile 复用提速**：转换用 LibreOffice user profile 复用 `<数据根>/.lo-profile/`
   （与每文件独立临时 profile 相比，实测 37~40s → 15~18s）；profile 损坏时自动重置重试一次。
   运维清理该目录等同于重置转换缓存。

## 接口

前缀 `/api/knowledge-base`，详见主 README「HTTP API」表：
`/status` `/config` `/categories`（GET/POST/PUT/DELETE）`/files`（GET/POST/PUT/DELETE）
`/files/<id>/raw`（内联读取渲染件）、`/files/<id>/pdf`（内联读取展示 PDF，仅 `pdf_status=ok`）、
`/files/<id>/preview`（Excel 表格预览 JSON，仅 `html_status=ok`）、
`/files/<id>/download`（attachment 下载**原件**，全体登录用户）、
`/files/<id>/pdf-retry` 与 `/files/<id>/preview-retry`（重试转换/渲染，仅管理员、仅 `failed` 记录）。

## 已知限制

- **旧版格式转换为"数据保真"**：`.doc` 提取正文段落与表格（olefile 解析 FIB/CLX 分片 +
  控制字符结构切分），样式/图片/页眉页脚/批注丢弃；`.xls` 逐工作表搬迁
  （数字/日期/布尔按类型还原），合并单元格与列宽不迁移。需要完整保真请先在 Office/WPS 中
  另存为 `.docx`/`.xlsx` 后上传（转换后上传同样可读，只是不做格式迁移）。
- `.doc` 若为 Word 6/95 早期格式（nFib < 193）不支持转换，明确报错。
- OFD 渲染为"尽力而为"：复杂版式（签章、多媒体）可能显示不全，正文文本与图片通常可正常呈现。
- **PDF 预览依赖外部程序**（仅 Word 类）：需服务器安装 LibreOffice；未安装时阅读回退降级渲染（不阻断任何功能）。
  大文件（10~20MB Word）转换可能需要数十秒，期间先以降级渲染呈现，最长等待 180s 超时记 `failed`。
- **表格预览的还原范围有限**：批注/浮动图片/图表/条件格式/冻结窗格/富文本局部样式不还原；
  超大表（>3000 行或 >120 列）截断展示（表尾有提示）；>15MB 源文件直接回退 SheetJS。
  旧库里未经过格式转换的裸 `.xls` 记录 openpyxl 读不了，表格预览记 `failed` 并回退 SheetJS（其可读 xls）。
- **Excel 空白页**：仅影响历史 PDF 预览（阶段 3 起 Excel 不再生成 PDF，新上传无此问题）。
- **历史数据原件不可恢复**：本轮改造前上传的 `.doc`/`.xls`，其原件已被转换件覆盖，
  下载拿到的是 `.docx`/`.xlsx`（迁移时按"现有落盘件即原件"回填）。
- 全文检索、按单位/部门可见范围隔离未实现（数据模型已预留归属字段，见设计文档「遗留/增强」）。

## 设计文档

- `docs/知识库插件-设计文档.md`（基线，含开发 TODO 与断点续开发记录）
- `docs/插件库优化方案-设计文档.md`（**本轮改造**：PDF 预览 + 原件下载 + Excel 表格预览，含分阶段 TODO）
