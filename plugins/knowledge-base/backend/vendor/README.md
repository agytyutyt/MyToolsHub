# vendor/ —— Office→HTML 渲染引擎（xhr + dhr）

本目录是**外部项目的原样拷贝**，供知识库插件离线内网部署使用：

| 目录 | 引擎 | 输入 | 说明 |
| --- | --- | --- | --- |
| `xhr/` | XLSX/XLS → HTML 重绘渲染引擎 | .xlsx/.xlsm/.xls | 单元格样式/数字格式/合并/冻结/条件格式/图片 |
| `dhr/` | DOC/DOCX → HTML 渲染引擎 | .docx/.docm/.doc | 标题/段落样式/编号列表/表格/图片/页眉页脚/脚注/修订 |

- **来源**：`D:\TestWorkSpace\xlsx-html-preview\python\`（xhr/dhr 0.1.0，MIT License，
  pyproject 声明核心链路零第三方依赖；`.xls` 兜底通道用 xlrd，`.doc` 归一化用外部
  LibreOffice——两者都是可选依赖）
- **拷贝日期**：2026-09-13；除下述一处补丁外未改动引擎源码（升级时整目录替换后需重打该补丁）：
  - `xhr/renderer/table.py` `_build_cell_attrs`：XML 里不存在的空单元格原本取
    `style_table[0]`——dedup 表按首次出现顺序 intern，[0] 是首个被解析单元格的样式
    而非「默认样式」，导致首个带填充单元格（如蓝底大标题）把整片空白区染色
    （实测「集成测试表格」出现 179 个蓝格子）。补丁：缺格一律按无样式渲染
    （与 Excel 行为一致）。上游 `TestWorkSpace/xlsx-html-preview` 同样存在此 bug，
    已按同位置定位，待同步。
- **接入方式**：`../office_render.py` 在 import 前把本目录插入 `sys.path`
  （dhr 依赖顶层 `xhr` 包，两包必须同为顶层可导入）
- 上游文档：设计文档/API 参考/known-issues 见上游 `docs/`；格式层踩坑见上游
  `python/HANDOFF.md`（Excel）与 `python/HANDOFF-DHR.md`（Word）
