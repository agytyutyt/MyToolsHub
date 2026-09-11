# 知识库（knowledge-base）

在线阅读 PDF / OFD / Word / Excel / Markdown / 纯文本文档的只读知识库插件。

## 功能

- **管理员**（管理员角色 / 超级管理员）：
  - 上传文档（≤20MB）：`.pdf .ofd .docx .doc .xlsx .xls .csv .md .markdown .txt`
  - **旧版格式自动转换**：`.doc` → `.docx`、`.xls` → `.xlsx`，服务端转换后落盘
    （库内只保留现代格式）；页面三处提示——上传 Snackbar、卡片"已转换"角标、阅读页顶部说明条。
    转换仅保留正文文字与表格，样式/图片/页眉页脚不迁移。
  - 多级分类管理：新建 / 重命名 / 移动 / 删除（仅空分类可删，同级不重名）
  - 文档管理：改名 / 移动分类 / 删除（在文件卡片上右键）
- **全体登录用户**：浏览分类、阅读、选择复制（tools.json 中 `grant_all: true`）
- **只读保证**：无任何编辑接口；渲染容器不可编辑；不提供 attachment 下载端点

## 技术

- 后端：核心功能纯 Python 标准库；**旧版格式转换**依赖
  openpyxl（写 xlsx）/ xlrd（读 xls）/ python-docx（写 docx）/ olefile（读 doc OLE2），
  见 `backend/doc_convert.py`。依赖缺失时仅自动转换不可用（`GET /status` 的
  `convert_legacy=false`，上传接口 422 明确提示），其余功能不受影响（优雅降级，B-4）。
  数据落盘数据根目录 `plugins/knowledge-base/data/`
  （categories.json / files.json / files/<id>.<ext>），记录含
  `created_by / created_by_name / unit_id / department_id` 归属四字段（规范 9.2 铁律一）；
  转换来源的文件额外记录 `original_ext`（原扩展名，前端据此提示）。
- 前端：原生 JS + Material 风格，文档渲染全部在浏览器端完成（**不使用任何浏览器内置/插件控件**）：

| 格式 | 渲染库 | 复制实现 |
| --- | --- | --- |
| PDF | pdfjs-dist 3.11.174（legacy） | 「复制本页」getTextContent |
| OFD | easyofd 1.2.1（Canvas + 文本选择层） | 「复制本页」GetPageText |
| Word .docx（含 .doc 自动转换） | mammoth 1.6.0 → DOMPurify | 选择复制 / 复制全文 |
| Excel .xlsx/.xls/.csv（.xls 上传时已转 xlsx） | SheetJS CE 0.18.5 | sheet 表格 TSV 复制 |
| Markdown | marked 4.3.0 → DOMPurify | 复制 Markdown 原文 |
| 纯文本 | 原生 textContent | 选择复制 / 复制全文 |

- 第三方库全部 vendor 在 `frontend/vendor/`（离线内网可用，来源/版本/许可证见 `frontend/vendor/VENDOR.md`），
  按需惰性加载（打开对应格式才注入脚本）。

## 接口

前缀 `/api/knowledge-base`，详见主 README「HTTP API」表：
`/status` `/config` `/categories`（GET/POST/PUT/DELETE）`/files`（GET/POST/PUT/DELETE）`/files/<id>/raw`（内联读取）。

## 已知限制

- **旧版格式转换为"数据保真"**：`.doc` 提取正文段落与表格（olefile 解析 FIB/CLX 分片 +
  控制字符结构切分），样式/图片/页眉页脚/批注丢弃；`.xls` 逐工作表搬迁
  （数字/日期/布尔按类型还原），合并单元格与列宽不迁移。需要完整保真请先在 Office/WPS 中
  另存为 `.docx`/`.xlsx` 后上传（转换后上传同样可读，只是不做格式迁移）。
- `.doc` 若为 Word 6/95 早期格式（nFib < 193）不支持转换，明确报错。
- OFD 渲染为"尽力而为"：复杂版式（签章、多媒体）可能显示不全，正文文本与图片通常可正常呈现。
- 全文检索、按单位/部门可见范围隔离未实现（数据模型已预留归属字段，见设计文档「遗留/增强」）。

## 设计文档

`docs/知识库插件-设计文档.md`（含开发 TODO 与断点续开发记录）。
