# 知识库（knowledge-base）

在线阅读 PDF / OFD / Word / Excel / Markdown / 纯文本文档的只读知识库插件。

## 功能

- **管理员**（管理员角色 / 超级管理员）：
  - 上传文档（≤20MB）：`.pdf .ofd .docx .xlsx .xls .csv .md .markdown .txt`（旧版 `.doc` 不支持，请另存为 `.docx`）
  - 多级分类管理：新建 / 重命名 / 移动 / 删除（仅空分类可删，同级不重名）
  - 文档管理：改名 / 移动分类 / 删除（在文件卡片上右键）
- **全体登录用户**：浏览分类、阅读、选择复制（tools.json 中 `grant_all: true`）
- **只读保证**：无任何编辑接口；渲染容器不可编辑；不提供 attachment 下载端点

## 技术

- 后端：纯 Python 标准库（无第三方依赖，`requirements.txt` 为空），数据落盘
  数据根目录 `plugins/knowledge-base/data/`（categories.json / files.json / files/<id>.<ext>），
  记录含 `created_by / created_by_name / unit_id / department_id` 归属四字段（规范 9.2 铁律一）。
- 前端：原生 JS + Material 风格，文档渲染全部在浏览器端完成（**不使用任何浏览器内置/插件控件**）：

| 格式 | 渲染库 | 复制实现 |
| --- | --- | --- |
| PDF | pdfjs-dist 3.11.174（legacy） | 「复制本页」getTextContent |
| OFD | easyofd 1.2.1（Canvas + 文本选择层） | 「复制本页」GetPageText |
| Word .docx | mammoth 1.6.0 → DOMPurify | 选择复制 / 复制全文 |
| Excel .xlsx/.xls/.csv | SheetJS CE 0.18.5 | sheet 表格 TSV 复制 |
| Markdown | marked 4.3.0 → DOMPurify | 复制 Markdown 原文 |
| 纯文本 | 原生 textContent | 选择复制 / 复制全文 |

- 第三方库全部 vendor 在 `frontend/vendor/`（离线内网可用，来源/版本/许可证见 `frontend/vendor/VENDOR.md`），
  按需惰性加载（打开对应格式才注入脚本）。

## 接口

前缀 `/api/knowledge-base`，详见主 README「HTTP API」表：
`/status` `/config` `/categories`（GET/POST/PUT/DELETE）`/files`（GET/POST/PUT/DELETE）`/files/<id>/raw`（内联读取）。

## 已知限制

- 旧版二进制 `.doc` 不支持（前端无成熟纯 JS 渲染方案），上传时提示转存 `.docx`。
- OFD 渲染为"尽力而为"：复杂版式（签章、多媒体）可能显示不全，正文文本与图片通常可正常呈现。
- 全文检索、按单位/部门可见范围隔离未实现（数据模型已预留归属字段，见设计文档「遗留/增强」）。

## 设计文档

`docs/知识库插件-设计文档.md`（含开发 TODO 与断点续开发记录）。
