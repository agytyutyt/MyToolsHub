# vendor/ —— Office→HTML 渲染引擎（xhr + dhr）

本目录是**外部项目的原样拷贝**，供知识库插件离线内网部署使用：

| 目录 | 引擎 | 输入 | 说明 |
| --- | --- | --- | --- |
| `xhr/` | XLSX/XLS → HTML 重绘渲染引擎 | .xlsx/.xlsm/.xls | 单元格样式/数字格式/合并/冻结/条件格式/图片 |
| `dhr/` | DOC/DOCX → HTML 渲染引擎 | .docx/.docm/.doc | 标题/段落样式/编号列表/表格/图片/页眉页脚/脚注/修订 |

- **来源**：`D:\TestWorkSpace\xlsx-html-preview\python\`（xhr/dhr 0.1.0，MIT License，
  pyproject 声明核心链路零第三方依赖；`.xls` 兜底通道用 xlrd，`.doc` 归一化用外部
  LibreOffice——两者都是可选依赖）
- **拷贝日期**：2026-09-13；除下述编号补丁外未改动引擎源码（**补丁条数以本文件实际编号为准**，升级时整目录替换后需逐条重打）
- **替换后必做（2026-10-10 P3-2 ⑥）**：跑 `python tools\e2e\kb-vendor-patch-check.py` —— 按本清单 1–7 条逐条核验源码签名，缺哪条即刻报出（漏条通常是静默的，如补丁 1 只在 Python ≤3.13 上炸）。**说明**：本仓不含上游原始源（拷贝自 `D:\TestWorkSpace\xlsx-html-preview\python\`），无法生成权威 diff，故"自动重放补丁"未实现；本脚本负责**检出**，重打仍按下列各条执行。

  1. `xhr/__init__.py` 顶部补 `from typing import Optional`
     —— **上游缺该导入**：第 84 行 `def convert(data: bytes, options: Optional[ConvertOptions] = None)`
     的注解是**运行时立即求值**的，而本模块既没导入 `Optional` 也没写
     `from __future__ import annotations`，因此**在 Python ≤3.13 上导入即
     `NameError: name 'Optional' is not defined`**，引擎整体不可用。Python 3.14 因
     PEP 649 惰性注解而"侥幸可用"，所以这个缺陷在 3.14 上不可见。
     实测：补该行后引擎在 3.8 / 3.13 / 3.14 渲染产物**字节完全一致**
     （同一份 sample.xlsx 输出 HTML sha256 前 16 位 = `d8535d164748e122`）。
     上游 `TestWorkSpace/xlsx-html-preview` 同样存在此缺陷，待同步。
     评估过程与证据见 `docs/eval/Python版本选型评估.md` §1.5。

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

  6. `xhr/normalize/libreoffice.py` **profile 提供者接缝**（本地适配 · P2-21，
     **非上游缺陷**）：新增模块级钩子 `set_profile_provider(provider)` +
     `_profile_provider` + `_acquire_profile()`。不注册时行为与上游**完全一致**
     （每次转换 `mkdtemp` 新建 profile、`finally` 即删）；注册后由提供方返回
     `(profile_dir, release(ok))` 租用**持久 profile** 复用（历史实测冷启动
     37~40s → 热启动 15~18s），取不到或提供方异常自动回退临时路径，转换失败时
     仍由提供方擦除槽位——三条铁律（唯一 profile/输出存在性校验/杀进程树）全部
     保留。使用方：`../office_render.py` 的持久 profile 池（§19）。
     上游同步时如何保留：整目录替换后，按本文件顶部【本地适配 · P2-21】注释 +
     三个新符号（`_profile_provider`/`set_profile_provider`/`_acquire_profile`）+
     `convert_to_xlsx` 内 `lease/owned/release(ok)` 分支**重打本接缝**；
     回归：`backend/test_office_profile_pool.py`，实测：`backend/bench_office_profile_pool.py`。

  7. `dhr/normalize.py` **profile 提供者接缝**（本地适配 · P2-21，**非上游缺陷**）：
     与第 6 条同构（`set_profile_provider` / `_acquire_profile` / lease 分支），
     用于 `.doc → .docx` 归一化复用持久 profile。本文件五条铁律核对：唯一性由
     槽位池保证（池大小=并发闸门数=2，一槽一 profile）、输出存在性校验未动、
     超时杀进程树未动、临时 `work_dir` 清理未动。上游同步时同第 6 条方式重打。
- **接入方式**：`../office_render.py` 在 import 前把本目录插入 `sys.path`
  （dhr 依赖顶层 `xhr` 包，两包必须同为顶层可导入）
- 上游文档：设计文档/API 参考/known-issues 见上游 `docs/`；格式层踩坑见上游
  `python/HANDOFF.md`（Excel）与 `python/HANDOFF-DHR.md`（Word）
