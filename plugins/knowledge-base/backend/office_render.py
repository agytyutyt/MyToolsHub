r"""知识库 —— Office(docx/doc/xlsx/xls) → 只读 HTML 预览渲染（xhr/dhr 引擎适配层）。

背景（《插件库优化方案》阶段 8，替代原方案）
--------
原方案两套管线全部废弃：
- Word：LibreOffice → PDF（`pdf_convert.py`，已删除）+ pdf.js 按打印分页渲染；
- Excel：`xlsx_render.py`（已删除）自实现的手绘 HTML 表格。

现改为上游 `D:\TestWorkSpace\xlsx-html-preview` 的**双引擎**（原样 vendor 在
`vendor/` 下，零改动拷贝，核心链路纯标准库、零第三方依赖）：
- `xhr`：.xlsx/.xlsm 直读；.xls 自动选通道（LibreOffice 归一化 → xlrd 兜底，
  内容 100% 保留、样式降级并在 warnings 提示）；
- `dhr`：.docx/.docm 直读；.doc 经 LibreOffice 归一化（不可用时明确报错）。
两引擎自带 IR 中间层、格式化显示文本（不输出原始序列值）、统一错误码
（用户可读文案、不含堆栈，SEC-5 同口径）、安全白名单（URL 协议/字体/图片魔数）。

调用方式
--------
- 引擎包不在包上下文内（dhr 顶层 `from xhr.core import ...`），因此把
  `vendor/` 插到 `sys.path` 后按顶层包导入（上游 demo 同款做法）；
- soffice 路径（三档优先级）：插件 `config.json` 的 `office.soffice_path`（新）或
  `pdf.soffice_path`（兼容旧配置）→ 进程环境变量 `XHR_SOFFICE` →
  随包分发的便携副本 `<程序目录>/runtime/libreoffice/program/soffice.exe`。
  最终写入环境变量 `XHR_SOFFICE`（两引擎的 find_soffice 均在调用时读取该变量）；
  第三档使离线部署包**无需目标机安装 LibreOffice、无需改配置**即可启用高保真通道；
- 渲染亚秒级（50 页 Word 实测约 0.25s），**按需同步渲染**，不再走
  pdf_status/html_status 异步状态机；模块级锁串行防大文件并发挤内存；
- 失败抛 `OfficeRenderError`（文案可直接展示给用户），调用方（routes.py）
  让前端回退 mammoth / SheetJS 降级渲染，上传/阅读/下载不受影响（B-4）。
"""

import glob
import json
import os
import re
import sys
import threading

# ---- vendor 引擎导入（必须在 import xhr/dhr 之前完成 sys.path 注入）----
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

try:
    import dhr as _dhr  # noqa: E402  （dhr 内部依赖顶层 xhr，一并可用）
    import xhr as _xhr  # noqa: E402
except Exception as _e:  # pragma: no cover —— 引擎缺失时插件其余功能不受影响（B-4）
    _dhr = None
    _xhr = None
    _IMPORT_ERROR = str(_e)
else:
    _IMPORT_ERROR = None

try:
    from xhr.normalize.libreoffice import find_soffice as _find_soffice  # noqa: E402
except Exception:  # pragma: no cover
    _find_soffice = None


class OfficeRenderError(Exception):
    """渲染失败，携带**面向用户**的提示文案（不回传堆栈/路径，SEC-5）。"""


# 渲染串行锁：引擎为纯 CPU 解析 + 偶发 LibreOffice 子进程，串行防大文件并发挤内存
_RENDER_LOCK = threading.Lock()

# Excel 渲染单元格上限（xhr 默认 20 万格会产出数十 MB HTML，压到与旧方案同量级；
# 超出由引擎截断并在预览顶部显示提示条）
MAX_CELLS = 120_000

# CSS 前缀：避免与宿主页面（.paper/.sheet-table 等）样式互相污染
WORD_CSS_PREFIX = "kbdoc"
SHEET_CSS_PREFIX = "kbsheet"


# ===================== 配置与探测 =====================

def _config_paths():
    """候选配置文件路径（数据根优先，其次模块目录；口径同旧 pdf_convert）。"""
    out = []
    try:
        import jztools_data
        out.append(jztools_data.get_data_root_file(
            "plugins", "knowledge-base", "config.json"))
    except Exception:
        pass
    out.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    return out


def _configured_soffice():
    """读配置里显式指定的 soffice 路径（office.soffice_path 新键优先，
    兼容旧 pdf.soffice_path）；未配置返回 None。"""
    for p in _config_paths():
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                conf = json.load(f)
        except Exception:
            continue
        if not isinstance(conf, dict):
            continue
        for section in ("office", "pdf"):
            node = conf.get(section)
            if isinstance(node, dict):
                v = node.get("soffice_path")
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return None


def _program_dir():
    """部署根目录（程序目录）：打包运行时为 exe 同层，源码运行时为仓库根。

    与 app.py::BASE_DIR 口径一致，用于定位随包分发的 runtime/ 目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # 源码运行：<仓库根>/plugins/knowledge-base/backend/office_render.py
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, os.pardir, os.pardir, os.pardir))


#: 随包分发的便携 LibreOffice 候选路径（相对部署根目录）。
#: 离线部署包把 LibreOffice MSI 以「管理安装」方式解包到 runtime/libreoffice/
#: （不写注册表、无需管理员），应用在此自动探测 soffice.exe，实现**零配置**可用。
#: 多列几个候选是为了兼容 msiexec /a 与 7z 两种解包方式、以及人工放在别处的场景。
_BUNDLED_SOFFICE_CANDIDATES = (
    ("runtime", "libreoffice", "program", "soffice.exe"),
    ("runtime", "libreoffice", "LibreOffice", "program", "soffice.exe"),
    ("libreoffice", "program", "soffice.exe"),
)


def _bundled_soffice():
    """随包分发的便携 soffice 路径；不存在返回 None。"""
    base = _program_dir()
    for parts in _BUNDLED_SOFFICE_CANDIDATES:
        p = os.path.join(base, *parts)
        if os.path.isfile(p):
            return p
    # 兜底：不同解包方式（msiexec /a、7z）与不同版本会多套一层目录，
    # 在 runtime/libreoffice 下有限深度搜索 program/soffice.exe。
    root = os.path.join(base, "runtime", "libreoffice")
    if os.path.isdir(root):
        for depth in range(1, 3):
            for hit in sorted(glob.glob(os.path.join(root, *(["*"] * depth),
                                                     "program", "soffice.exe"))):
                if os.path.isfile(hit):
                    return hit
    return None


def _apply_soffice_env():
    """把 soffice 路径注入 XHR_SOFFICE（引擎每次调用都读环境变量，
    改配置无需重启）。返回生效路径或 None。

    优先级：配置显式指定 > 进程环境变量 > 随包分发的便携 LibreOffice。
    最后一级是离线部署的关键：目标机没装 LibreOffice 时，直接使用运行时
    目录里解包好的便携副本，无需改配置、无需注册表。
    """
    explicit = _configured_soffice()
    if explicit and os.path.isfile(explicit):
        # 显式配置优先级最高（setdefault 可能被进程环境里的旧值占位）
        os.environ["XHR_SOFFICE"] = explicit
        return explicit
    env = os.environ.get("XHR_SOFFICE") or None
    if env:
        return env
    bundled = _bundled_soffice()
    if bundled:
        os.environ["XHR_SOFFICE"] = bundled
        return bundled
    return None


def soffice_available():
    """LibreOffice 是否可用（仅 .doc 归一化 / .xls 高保真通道需要）。"""
    if _find_soffice is None:
        return False
    try:
        return bool(_find_soffice(_apply_soffice_env()))
    except Exception:
        return False


def availability():
    """依赖自检（B-4）：引擎包可导入即可用。"""
    if _xhr is None or _dhr is None:
        return {"available": False, "soffice": None,
                "error": "渲染引擎未加载（vendor 缺失或导入失败：%s）" % _IMPORT_ERROR}
    return {"available": True, "soffice": soffice_available() or None, "error": None}


# ===================== 渲染 =====================

def _raise_from_xhr(e):
    """XhrError → OfficeRenderError（message 已是用户可读文案）。"""
    msg = str(e)
    if getattr(e, "code", "") == "TIMEOUT":
        msg = "预览转换超时，文件可能过于复杂，请稍后重试"
    raise OfficeRenderError(msg) from e


def _meta_warnings(result):
    """从引擎结果提取 warnings 列表。

    引擎的 model.meta 是 **dict**（不是对象），此前用 getattr(meta, "warnings")
    永远拿到 None，导致 /preview 的 warnings 字段恒为空——引擎真正的不支持特性
    提示只在 HTML 内嵌提示条里可见。这里兼容 dict / 对象两种形态。
    """
    meta = getattr(result.model, "meta", None)
    if isinstance(meta, dict):
        return list(meta.get("warnings") or [])
    return list(getattr(meta, "warnings", None) or [])


# ===================== 渲染产物修正（不改 vendor 的后处理） =====================
#
# 背景（2026-09-12/09-18 排查，详见 docs/eval/知识库Word预览三问题排查报告.md）
# ------------------------------------------------------------------------------
# 三处显示缺陷全部源自 vendor/dhr 的输出，但按 B-7「vendor 零改动拷贝」约定
# 不在 vendor 内打补丁（升级要重打），而是对 **渲染产物 HTML** 做后处理：
#
# 1) 「仿宋_GB2312 笔画竖过细 / 部分文字莫名加粗」
#    根因：多数客户端（含开发机）**没装 仿宋_GB2312**，引擎 fallback 链跳一格就
#    命中 `Microsoft YaHei`（黑体）。Canvas 像素签名实测墨量：雅黑 ≈ 138k
#    vs 仿宋_GB2312/仿宋 ≈ 54k，**相差约 2.56 倍**——中文整段变黑体，肉眼即
#    「原文没加粗却显示加粗」；数字/英文走 TNR（细）又造成同行粗细不均。
#    修法：在 `"仿宋_GB2312"` 与 `"Microsoft YaHei"` 之间插入仿宋族真实可用名
#    （`"FangSong","仿宋","SimSun"`），优先落回**同类衬线字体**而非黑体。
#
# 2) 「单元格文字穿模」
#    根因 a：引擎在 `<tr style="height:Npx;overflow:hidden">` 上写 overflow:hidden，
#            但 CSS `overflow` 对 `display:table-row` **无效**，精确行高约束失效，
#            内容溢出后与相邻行/边框重叠；
#    根因 b：引擎 base CSS **没有重置 `<p>` 默认 margin**，浏览器 `margin:1em 0`
#            额外撑高单元格，行距被拉坏；
#    根因 c：`td/th` 缺 `word-break/overflow-wrap`，超长不可断行串横向撑出。
#    修法：追加高优先级 CSS 重置 + 补断行规则 + 去掉无效的 overflow:hidden。
#
# 所有补丁都作用于**引擎生成的静态 HTML 字符串**，输入不含用户可控的拼接面
# （字体名已被引擎白名单过滤，CSS 由本模块常量生成），无注入风险。

#: 仿宋族「真实可用」回退名（Windows 自带 simfang.ttf / simsun.ttc）。
#: 顺序即优先级：FangSong（仿宋）→ 仿宋（中文名）→ SimSun（宋体，同为衬线）。
_FANGSONG_FALLBACK = ('FangSong', '仿宋', 'SimSun')

#: 引擎 fallback 链里紧跟目标字体之后的那个「黑体」，用于定位插入点。
_CJK_FALLBACK_HEAD = 'Microsoft YaHei'

#: 需要补上仿宋族回退的 Word 字体名（公文/正式文档常见带 GB2312 后缀的写法）。
#: ⚠️ 不要把裸 `"仿宋"` 放进来：补链后会往链里插入 `"仿宋"`，二次处理会命中自身
#: 导致重复注入（幂等破坏）。裸 `仿宋` 由下面的 `_FANGSONG_ALIASES` 单独兜底。
_FANGSONG_NAMES = ('仿宋_GB2312', '仿宋-GB2312', '仿宋GB2312', 'FangSong_GB2312',
                   'FangSong-GB2312', '仿宋_GB2312_CN')

#: 裸「仿宋」写法：仅在链里**没有**任何仿宋族别名时才需要补（避免自匹配）。
_FANGSONG_ALIASES = ('FangSong', '仿宋', 'SimSun')

#: 补丁标记：已注入过的产物不再重复注入（幂等保证，也便于排查）。
_CSS_PATCH_MARK = "/*kbdoc-patch*/"

#: 追加到首个 `<style>` 的分辨率无关样式重置（preview 场景固定使用 `kbdoc` 前缀）。
_CSS_PATCH = _CSS_PATCH_MARK + """
/* ---- 宿主后处理补丁（office_render.py）：不要手工编辑，见该文件注释 ---- */
/* 1) 段落默认 margin 会撑坏表格单元格行距（Word 无此概念），整体归零 */
.kbdoc p{margin:0}
/* 2) 超长不可断行串（合同编号/数字串）必须能断，否则横向撑出单元格 */
.kbdoc-t td,.kbdoc-t th{word-break:break-all;overflow-wrap:anywhere;word-wrap:break-word}
/* 3) 精确行高的单元格内容裁剪（overflow 对 table-row 无效，改由内层 p 承担） */
.kbdoc-t td>p:only-child{overflow:hidden}
"""

#: 匹配 `<tr ...>` 标签（限定在标签内部，避免误伤 CSS 选择器文本）。
_TR_TAG_RE = re.compile(r"<tr\b[^>]*>")

#: 匹配 `<tr style="...;overflow:hidden">` 里的无效声明（保留 height，去掉 overflow）。
_TR_OVERFLOW_RE = re.compile(r"\s*overflow\s*:\s*hidden\s*;?")


def _patch_font_fallback(html):
    """在仿宋族字体名后插入真实可用的同类回退，避免直接跳到黑体（雅黑）。

    只处理 `font-family:"目标字体","...",...,"Microsoft YaHei",...` 这种由引擎生成的
    声明串。**幂等**：若目标字体与雅黑之间的候选里已出现任一仿宋族别名，即视为已修过
    并跳过——否则 `"仿宋"` 既是查找键又是插入值，二次处理会自我匹配、重复膨胀。
    """
    if _CJK_FALLBACK_HEAD not in html:
        return html

    def _build(pattern, target_literal):
        extra = "".join('"%s",' % n for n in _FANGSONG_FALLBACK
                        if n != target_literal)   # 不重复插入与目标同名的项

        def _sub(m):
            between = m.group(2) or ""
            if any('"%s"' % a in between for a in _FANGSONG_ALIASES):
                return m.group(0)          # 已补过 → 原样返回
            return m.group(1) + between + extra
        return pattern.sub(_sub, html)

    # 先试带后缀的正式写法；都没命中再退回裸「仿宋」（且同样受幂等保护）。
    for name in _FANGSONG_NAMES:
        pattern = re.compile(
            r'("' + re.escape(name) + r'",)'      # 目标字体 + 逗号
            r'((?:"[^"]*",)*)'                    # 中间其它候选（TNR 等）
            r'(?="' + re.escape(_CJK_FALLBACK_HEAD) + r'")'
        )
        out = _build(pattern, name)
        if out != html:
            return out

    pattern = re.compile(
        r'("仿宋",)'                              # 裸「仿宋」写法
        r'((?:"[^"]*",)*)'
        r'(?="' + re.escape(_CJK_FALLBACK_HEAD) + r'")'
    )
    return _build(pattern, '仿宋')


def _patch_table_overflow(html):
    """去掉 `<tr>` 上对 table-row 无效的 `overflow:hidden` 声明。

    只扫 **标签内部**（`<tr ...>`），不碰 CSS 选择器里的同名文本。
    """
    if "overflow:hidden" not in html:
        return html
    out = []
    pos = 0
    for m in _TR_TAG_RE.finditer(html):
        tag = m.group(0)
        if "overflow:hidden" in tag:
            cleaned = _TR_OVERFLOW_RE.sub("", tag)
            # 清掉可能留下的空 style 属性（`style=""` 无意义且会污染 diff）
            cleaned = re.sub(r'\s*style=""', "", cleaned)
            cleaned = re.sub(r'\s*style="\s*"', "", cleaned)
            if cleaned != tag:
                out.append(html[pos:m.start()])
                out.append(cleaned)
                pos = m.end()
    out.append(html[pos:])
    return "".join(out)


def _patch_css(html):
    """把补丁 CSS 追加进第一个 `<style>` 块（引擎输出保证至少有一个）。

    幂等：已带 `_CSS_PATCH_MARK` 的产物直接返回，避免重复注入导致文件膨胀
    （每次预览都会经过本函数，重复注入会在反复请求下发散）。
    """
    if _CSS_PATCH_MARK in html:
        return html
    m = re.search(r"<style[^>]*>", html)
    if not m:
        return html
    return html[:m.end()] + _CSS_PATCH + html[m.end():]


def _polish_word_html(html):
    """Word 预览产物后处理：字体回退补链 + 表格穿模修正 + CSS 重置。"""
    if not html:
        return html
    html = _patch_font_fallback(html)
    html = _patch_table_overflow(html)
    html = _patch_css(html)
    return html


def render_word(data):
    """Word（docx/docm/doc 字节）→ {"kind","html","warnings"}。

    - mode="flow"：流式连续排版（无打印分页，阅读动线与网页一致；.doc 归一化
      场景上游也推荐 flow）；
    - output="fragment"：`<style>` + `<div class="kbdoc">` 片段，便于嵌入宿主容器；
    - 图片内联 base64（离线内网无外链可图床）；
    - 产物经 `_polish_word_html` 修正三处已知显示缺陷（字体回退/表格穿模），
      全部在 HTML 后处理层完成，vendor 保持零改动（B-7）。
    """
    if _dhr is None:
        raise OfficeRenderError("服务器渲染引擎未加载，无法生成 Word 预览")
    if not data:
        raise OfficeRenderError("文件内容为空，无法生成预览")
    opts = _dhr.ConvertOptions(
        mode="flow",
        output="fragment",
        css_prefix=WORD_CSS_PREFIX,
        media_mode="base64",
    )
    _apply_soffice_env()
    try:
        with _RENDER_LOCK:
            result = _dhr.convert(bytes(data), opts)
    except Exception as e:
        _raise_from_xhr(e)
    warnings = _meta_warnings(result)
    return {"kind": "word", "html": _polish_word_html(result.html),
            "warnings": warnings[:5], "truncated": False}


def render_sheet(data):
    """Excel（xlsx/xlsm/xls 字节）→ {"kind","html","warnings","truncated"}。

    - mode="fragment"：`<style>` + `<div class="kbsheet">`（含页签与多个
      `.kbsheet-sheet[hidden]`，页签切换由前端接线）；
    - .xls 由引擎自动选通道（LibreOffice 归一化 → xlrd 兜底，降级进 warnings）。
    """
    if _xhr is None:
        raise OfficeRenderError("服务器渲染引擎未加载，无法生成表格预览")
    if not data:
        raise OfficeRenderError("文件内容为空，无法生成预览")
    opts = _xhr.ConvertOptions(mode="fragment", css_prefix=SHEET_CSS_PREFIX,
                               max_cells=MAX_CELLS)
    _apply_soffice_env()
    try:
        with _RENDER_LOCK:
            result = _xhr.convert(bytes(data), opts)
    except Exception as e:
        _raise_from_xhr(e)
    warnings = _meta_warnings(result)
    return {"kind": "sheet", "html": result.html, "warnings": warnings[:5],
            "truncated": bool(result.truncated)}
