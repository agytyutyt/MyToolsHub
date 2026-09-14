# 知识库（knowledge-base）

在线阅读 PDF / OFD / Word / Excel / Markdown / 纯文本文档的只读知识库插件。

## 功能

- 管理员（管理员角色 / 超级管理员）：
  - 上传文档（≤20MB）：`.pdf .ofd .docx .doc .xlsx .xls .csv .md .markdown .txt`
  - **旧版格式自动转换**：`.doc` → `.docx`、`.xls` → `.xlsx`，服务端转换后落盘
    （库内只保留现代格式）；页面三处提示——上传 Snackbar、卡片"已转换"角标、阅读页顶部说明条。
    转换仅保留正文文字与表格，样式/图片/页眉页脚不迁移。
  - **Office 原样式预览（Word / Excel，连续单页不分页）**：docx/doc/xlsx/xls 由服务端
    vendor 双引擎（`dhr`=Word、`xhr`=Excel）**按需同步渲染**为 HTML——标题/段落/编号/
    表格/图片/页眉页脚/单元格样式/合并/列宽/数字日期格式全还原，亚秒级完成（无生成中
    状态、无需等待）；渲染失败自动回退 mammoth / SheetJS 简化渲染
    （详见下方「Office 预览（xhr/dhr 双引擎）」）。
  - 多级分类管理：新建 / 重命名 / 移动 / 删除（仅空分类可删，同级不重名）
  - 文档管理：改名 / 移动分类 / 删除（在文件卡片上右键）
  - 预览为按需渲染 + 磁盘缓存，无后台状态机，无需手动重转（重开阅读页即重试）
- **全体登录用户**：浏览分类、阅读、选择复制、**下载原始文档**（tools.json 中 `grant_all: true`）
- **只读保证**：无任何编辑接口；渲染容器不可编辑；PDF 版仅在线展示，下载一律给原始文档

## 技术

- 后端：核心功能纯 Python 标准库；**旧版格式转换**依赖
  openpyxl / xlrd / python-docx / olefile（见 `backend/doc_convert.py`）；
  **Office 预览**用 vendor 双引擎 `backend/vendor/{xhr,dhr}`（原样拷贝自
  TestWorkSpace/xlsx-html-preview 项目，纯标准库零第三方依赖，来源见其 README.md），
  适配层 `backend/office_render.py`。引擎缺失时预览回退前端降级渲染；
  `.xls` 高保真通道与 `.doc` 归一化**可选**依赖外部 LibreOffice（缺失自动走
  xlrd 兜底 / 明确报错回退），其余功能不受影响（优雅降级，B-4）。
- 数据落盘数据根目录 `plugins/knowledge-base/data/`
  （categories.json / files.json / files/），记录含
  `created_by / created_by_name / unit_id / department_id` 归属四字段（规范 9.2 铁律一）。
  上传后落盘（按绝对路径去重，不重复落盘）：

  | 文件 | 说明 |
  | --- | --- |
  | `<id>.<original_ext>` | **原件**（所有类型都存），下载端点专用 |
  | `<id>.docx` / `<id>.xlsx` | 渲染件（仅旧版 `.doc`/`.xls` 由 doc_convert 生成），预览与 mammoth/SheetJS 兜底共用 |
  | `<id>.preview.json` | Office 预览缓存（仅 Word/Excel 类，首次阅读时按需生成） |
  | `<id>.pdf` | （历史遗留）旧版本 PDF 预览产物，阶段 8 起不再生成，阅读端不消费；删除文档时一并清理 |

  元数据字段：`ext`（归一化主格式）/ `original_ext`（上传时扩展名，恒有值）/
  `original_size`。旧版的 `pdf_status`/`html_status` 系列状态字段已废弃
  （预览按需渲染，无后台状态机），历史记录中的残留字段无消费方、无害。
- 前端：原生 JS + Material 风格，文档渲染全部在浏览器端完成（**不使用任何浏览器内置/插件控件**）：

| 格式 | 渲染库 | 复制实现 |
| --- | --- | --- |
| PDF | pdfjs-dist 3.11.174（legacy） | 「复制本页」getTextContent |
| OFD | easyofd 1.2.1（Canvas + 文本选择层） | 「复制本页」GetPageText |
- **Word .docx（含 .doc 自动转换）** | 服务端 dhr 引擎 HTML → DOMPurify（**降级渲染**：mammoth 1.6.0） | 选择复制 / 复制全文
- **Excel .xlsx/.xls/.csv（.xls 上传时已转 xlsx）** | 服务端 xhr 引擎 HTML（自带 sheet 页签）→ DOMPurify（**降级渲染**：SheetJS CE 0.18.5） | sheet 表格 TSV 复制 |
| Markdown | marked 4.3.0 → DOMPurify | 复制 Markdown 原文 |
| 纯文本 | 原生 textContent | 选择复制 / 复制全文 |

- 第三方库全部 vendor 在 `frontend/vendor/`（离线内网可用，来源/版本/许可证见 `frontend/vendor/VENDOR.md`），
  按需惰性加载（打开对应格式才注入脚本）。

## Office 预览（xhr / dhr 双引擎，连续单页）

docx/doc/xlsx/xls 的在线阅读由 vendor 双引擎**按需渲染**（阶段 8，替代原
"Word 转 PDF + Excel 手绘 HTML" 两套异步管线）：

- **Excel（xhr 引擎）**：.xlsx/.xlsm 直读；单元格样式/数字格式（日期百分比货币等
  格式化显示文本）/合并/冻结窗格/条件格式（部分）/图片还原；多 sheet 自带页签；
  .xls 自动选通道（LibreOffice 归一化 → xlrd 兜底，内容 100% 保留、降级有提示）。
- **Word（dhr 引擎）**：.docx/.docm 直读；标题/段落样式/编号列表/表格/图片/
  页眉页脚/脚注/修订还原；.doc 经 LibreOffice 归一化（不可用时明确报错回退）。
- **按需渲染 + 缓存**：引擎亚秒级完成（50 页 Word 实测约 0.25s），首次打开阅读页时
  同步渲染并缓存 `<id>.preview.json`，之后直接读缓存——无"生成中"状态、无轮询、
  无重试端点；文件按 id 不可变，缓存无失效问题。
- **优雅降级（B-4）**：引擎未加载 / 渲染失败 → `GET /preview` 返回 404，前端自动
  回退 mammoth（Word）/ SheetJS（Excel）简化渲染；上传/阅读/下载不受影响。
- **连续单页**：Word 流式排版（flow），Excel 整表横向滚动——都没有打印分页。
- **只读与安全**：不生成任何编辑控件；引擎侧全部文本转义 + URL 协议/字体白名单，
  前端侧正文过 DOMPurify（引擎 CSS 为静态内容，摘取后原样挂回，不经消毒）。

### LibreOffice（可选）

仅两条窄路径需要：`.xls` 的高保真归一化通道（缺失时自动 xlrd 兜底）、`.doc` 的
归一化渲染（缺失时该类预览回退降级）。探测顺序：环境变量 `XHR_SOFFICE`（由插件
`config.json` 的 `office.soffice_path` 或旧键 `pdf.soffice_path` 自动注入）→
常见安装路径。未安装不影响其它任何功能。

## 运维与注意事项（阶段 8 实测沉淀）

- **预览缓存与失效**：`<id>.preview.json` 按「文件 id 不可变」设计为长期缓存，服务端不主动
  失效，但缓存内记有**格式版本号**（`routes.py` 的 `PREVIEW_CACHE_VERSION`）：**引擎渲染行为
  变化（vendor 打补丁/升级）时必须递增该常量**，旧缓存会在下次打开阅读页时自动重渲染，
  无需手工清缓存、无需重启服务。排查渲染问题时也可手工删除受影响文件的缓存
  （`data/files/<id>.preview.json`，或整批 `*.preview.json`）。删除文档时缓存自动清理。
- **vendor 引擎升级流程**：① 整目录替换 `backend/vendor/{xhr,dhr}`；② **重打补丁**——按
  `vendor/README.md` 的补丁清单逐条恢复（现有 4 条：xhr 缺格样式、xhr `Optional` 导入、
  dhr numId=0 语义、dhr 字符缩进/形态覆盖）；③ 递增 `routes.py` 的 `PREVIEW_CACHE_VERSION`；
  ④ 重跑 `backend/test_routes_preview.py` 与 `backend/test_dhr_indent_numid.py` 回归；
  ⑤ `python backend/_build_phase8.py` 重建目检页人工核对样式。
- **前端注入约定**：reader.js 对引擎 HTML 的处理是「摘取全部 `<style>` 块 → 正文过
  DOMPurify → CSS 原样挂回」。原因：DOMPurify 会整块丢弃 `<style>` 元素（engine 的 class
  型 CSS 全在里面），而 CSS 是引擎生成的静态内容（字体白名单/无 URL），不经消毒是安全的。
  Excel 页签接线（`.kbsheet-tab` → `aria-selected` + `.kbsheet-sheet[hidden]`）与引擎
  runtime 行为保持一致，两处改动要同步。
- **渲染 bug 排查路径**：先用 `office_render.render_sheet/render_word` 在模块级复现并对照
  openpyxl 原始样式（填充/合并/列宽逐项比对），再查引擎 CSS 类与 `<td>` 的对应关系——
  「样式张冠李戴」类问题（如蓝格子）大概率在引擎的样式解析/去重层，接入层只负责注入。
- **soffice 配置**：`config.json` 键 `office.soffice_path`（兼容旧键 `pdf.soffice_path`），
  经环境变量 `XHR_SOFFICE` 下发给引擎，改配置免重启；仅影响 `.xls` 高保真与 `.doc` 归一化
  两条窄路径（见上文「LibreOffice（可选）」）。
- **打包 / 依赖**：引擎零 pip 依赖（纯标准库），`JZToolsHub.spec` 的 PACKAGES **无需**
  为 xhr/dhr 加条目；`backend/vendor/` 由 build-deploy.ps1 随 plugins 整树自动进部署包；
  开发机 `backend/config.json` 不会入包（目标机在数据根配置 soffice 路径，升级持久）；
  `backend/out/` 测试产物已在打包清理清单。打包后验收点与完整说明见 HANDOFF §5.3.1。
- **上游已知问题**：① `style_table[0]` 缺格染色 bug（已在本插件 vendor 打补丁，上游待同步）；
  ② LibreOffice `.doc→docx` 归一化对微型表格会退化为制表符段落（内容保留，真实链路不走
  此通道）；③ 条件格式色阶/数据条/图标集、Word 公式/SmartArt/图表为占位渲染（上游 V2 项）。

## 接口

前缀 `/api/knowledge-base`，详见主 README「HTTP API」表：
`/status` `/config` `/categories`（GET/POST/PUT/DELETE）`/files`（GET/POST/PUT/DELETE）
`/files/<id>/raw`（内联读取渲染件）、`/files/<id>/preview`（Office 预览 JSON：
`{kind, html, warnings, truncated}`，按需渲染 + 缓存，失败 404）、
`/files/<id>/download`（attachment 下载**原件**，全体登录用户）。
旧版 `/pdf`、`/pdf-retry`、`/preview-retry` 端点已随状态机一并移除。

## 已知限制

- **旧版格式转换为"数据保真"**：`.doc` 提取正文段落与表格（olefile 解析 FIB/CLX 分片 +
  控制字符结构切分），样式/图片/页眉页脚/批注丢弃；`.xls` 逐工作表搬迁
  （数字/日期/布尔按类型还原），合并单元格与列宽不迁移。需要完整保真请先在 Office/WPS 中
  另存为 `.docx`/`.xlsx` 后上传（转换后上传同样可读，只是不做格式迁移）。
- `.doc` 若为 Word 6/95 早期格式（nFib < 193）不支持转换，明确报错。
- OFD 渲染为"尽力而为"：复杂版式（签章、多媒体）可能显示不全，正文文本与图片通常可正常呈现。
- **Office 预览的还原范围有限**（引擎已知边界，详见 vendor/README.md 引的上游
  known-issues）：Excel 条件格式的色阶/数据条/图标集为占位渲染；Word 公式/SmartArt/
  图表占位；列宽换算固定 `mdw=7px`（非 Calibri 默认字体的文件略有偏差）；
  `.xls` xlrd 兜底通道样式保真有限（内容 100% 保留、页面有降级提示）；
  加密文件不支持（返回友好提示后回退降级渲染）。
- **历史 PDF 预览产物**：阶段 8 前生成的 `<id>.pdf` 不再被阅读端消费（保留在盘上，
  删除文档时一并清理）；历史记录中的 `pdf_status`/`html_status` 字段同理废弃。
- **Excel 空白页**：仅影响历史 PDF 预览（阶段 3 起 Excel 不再生成 PDF，新上传无此问题）。
- **历史数据原件不可恢复**：本轮改造前上传的 `.doc`/`.xls`，其原件已被转换件覆盖，
  下载拿到的是 `.docx`/`.xlsx`（迁移时按"现有落盘件即原件"回填）。
- 全文检索、按单位/部门可见范围隔离未实现（数据模型已预留归属字段，见设计文档「遗留/增强」）。

## 设计文档

- `docs/知识库插件-设计文档.md`（基线，含开发 TODO 与断点续开发记录）
- `docs/插件库优化方案-设计文档.md`（**本轮改造**：原件下载 + Office 预览引擎
  演进（PDF→手绘→xhr/dhr 双引擎），含分阶段 TODO）
