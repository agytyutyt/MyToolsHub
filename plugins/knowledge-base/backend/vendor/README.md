# vendor/ —— Office→HTML 渲染引擎（xhr + dhr）

本目录是**外部项目的原样拷贝**，供知识库插件离线内网部署使用：

| 目录 | 引擎 | 输入 | 说明 |
| --- | --- | --- | --- |
| `xhr/` | XLSX/XLS → HTML 重绘渲染引擎 | .xlsx/.xlsm/.xls | 单元格样式/数字格式/合并/冻结/条件格式/图片 |
| `dhr/` | DOC/DOCX → HTML 渲染引擎 | .docx/.docm/.doc | 标题/段落样式/编号列表/表格/图片/页眉页脚/脚注/修订 |

- **来源**：`D:\TestWorkSpace\xlsx-html-preview\python\`（xhr/dhr 0.1.0，MIT License，
  pyproject 声明核心链路零第三方依赖；`.xls` 兜底通道用 xlrd，`.doc` 归一化用外部
  LibreOffice——两者都是可选依赖）
- **拷贝日期**：2026-09-13；除下述四处补丁外未改动引擎源码（升级时整目录替换后需重打这四处补丁）：

  1. `xhr/__init__.py` 顶部补 `from typing import Optional`
     —— **上游缺该导入**：第 84 行 `def convert(data: bytes, options: Optional[ConvertOptions] = None)`
     的注解是**运行时立即求值**的，而本模块既没导入 `Optional` 也没写
     `from __future__ import annotations`，因此**在 Python ≤3.13 上导入即
     `NameError: name 'Optional' is not defined`**，引擎整体不可用。Python 3.14 因
     PEP 649 惰性注解而"侥幸可用"，所以这个缺陷在 3.14 上不可见。
     实测：补该行后引擎在 3.8 / 3.13 / 3.14 渲染产物**字节完全一致**
     （同一份 sample.xlsx 输出 HTML sha256 前 16 位 = `d8535d164748e122`）。
     上游 `TestWorkSpace/xlsx-html-preview` 同样存在此缺陷，待同步。
     评估过程与证据见 `docs/Python版本选型评估.md` §1.5。

  2. `xhr/renderer/table.py` `_build_cell_attrs`：XML 里不存在的空单元格原本取
     `style_table[0]`——dedup 表按首次出现顺序 intern，[0] 是首个被解析单元格的样式
     而非「默认样式」，导致首个带填充单元格（如蓝底大标题）把整片空白区染色
     （实测「集成测试表格」出现 179 个蓝格子）。补丁：缺格一律按无样式渲染
     （与 Excel 行为一致）。上游 `TestWorkSpace/xlsx-html-preview` 同样存在此 bug，
     已按同位置定位，待同步。

  3. `dhr/numbering.py` `assign_list_markers`：**numId=0 误报**。OOXML 里
     `<w:numId w:val="0"/>` 是「取消编号」的合法值（Word/WPS 用它清除段落从样式
     继承的编号），并非「未定义编号」。原实现查不到 numId=0 的定义就告警
     「编号 numId=0.0 未在 numbering.xml 中定义，该列表降级为普通段落」，用户可见
     的文档顶部提示条被这条假警告污染（用户实测反馈）。补丁：numId=0 静默按普通
     段落处理；真未定义的 numId 仍告警，且消息中的 numId 以整数格式输出
     （原为 float 的 "3.0"）。上游同缺陷待同步。

  4. `dhr/styles.py` `parse_ppr` + `dhr/renderer/css.py` `para_props_to_css`：
     **字符缩进丢失/错误覆盖**（用户实测反馈「部分段落开头缩进不显示」）：
     a) `w:ind` 的 `leftChars`/`rightChars`/`startChars`/`endChars`（中文文档常用
        「缩进 N 字符」写法）解析后**无任何消费方**，margin 整个丢失——补丁在
        `para_props_to_css` 以 `margin-left/right:{n}em` 输出（em 随字号缩放）；
     b) `w:start`/`w:end`（Strict 别名）原本被忽略——补丁在 `left`/`right` 缺席时兜底；
     c) 首行/悬挂缩进的 px 与 em（Chars）两形态在「样式 ← 段落直接格式」合并时
        各自独立继承，样式的字符缩进会压过段落直接写的 twips 缩进（反之亦然）——
        实测「样式 leftChars=200 + 段落 left=420」输出 2em 而非 28px。补丁：
        `parse_ppr` 记录形态标记 `_fl_form`/`_hg_form`/`_left_chars_em`（私有键由
        `merge_para` 无条件透传），渲染时按「最近来源整组覆盖」取用。
     回归自测：`backend/test_dhr_indent_numid.py`（14 项断言，含覆盖矩阵）。
     上游同缺陷待同步。

  5. `dhr/__init__.py` `normalize_doc`：`.doc` 归一化警告**按渲染模式分流文案**
     （用户实测反馈）。原文案"分页位置可能不精确，建议使用流式模式"对**所有**
     .doc 一律插入，但本插件自阶段 8 起 `office_render.render_word` 恒以
     `mode="flow"` 渲染——用户已在流式模式下，"建议使用流式模式"是误导。
     补丁：`normalize_doc` 增加 `mode` 参数，flow 模式改用
     "…个别复杂版式（分栏/文本框等）可能与原件存在细微差异"，非 flow 保留原文案；
     `convert()` 透传 `opts.mode`。上游待同步。
- **接入方式**：`../office_render.py` 在 import 前把本目录插入 `sys.path`
  （dhr 依赖顶层 `xhr` 包，两包必须同为顶层可导入）
- 上游文档：设计文档/API 参考/known-issues 见上游 `docs/`；格式层踩坑见上游
  `python/HANDOFF.md`（Excel）与 `python/HANDOFF-DHR.md`（Word）
