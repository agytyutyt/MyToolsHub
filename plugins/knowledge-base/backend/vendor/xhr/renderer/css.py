"""样式 → CSS 映射 —— 对应 TS 版 ``packages/renderer-dom/src/css.ts``（设计文档第 7 章）。

设计取舍：
  · 每个单元格输出**完整**的样式声明（不依赖继承），因为：
      1) 浏览器与 docx 转换器的继承行为不一致，可预测性更重要；
      2) 真正的体积优化靠"声明串去重"（相同声明串共用一个 class）。
  · 单位统一用 px（列宽行高已是 px），字号由 pt 换算为 px。
"""
from __future__ import annotations

import re
from typing import List, Optional

from ..core import BorderSide, FillStyle, StyleModel, pt_to_px

#: 中文/拉丁字体回退链（Windows 优先）
CJK_FALLBACK = '"Microsoft YaHei","微软雅黑","PingFang SC","Hiragino Sans GB","Source Han Sans CN","Noto Sans CJK SC",sans-serif'
LATIN_FALLBACK = 'Calibri,Arial,"Segoe UI",Helvetica,sans-serif'
MONO_FALLBACK = 'Consolas,"Courier New",monospace'

_FONT_NAME_BAD_RE = re.compile(r"[^\w\s\-_.]", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def sanitize_font_name(name: object) -> str:
    """清理字体名 —— ⚠️ 安全函数，不可省略。

    威胁：字体名来自 xlsx 的 <font><name val="…"/>，完全受文件控制，
    而字体名会被拼进 <style> 标签内容里：

        font-family:"<name>",Calibri,…;

    若不清理，把字体名写成 ``x",sans-serif}</style><script>alert(1)</script><style>a{``
    就能**闭合 style 标签并注入脚本**（真实可利用的 XSS）。

    策略：白名单 —— 只保留「字母（含 CJK）/ 数字 / 空格 / 连字符 / 下划线 / 点」。
    """
    t = name.strip() if isinstance(name, str) else ""
    if not t:
        return ""
    t = _FONT_NAME_BAD_RE.sub("", t)
    t = _SPACES_RE.sub(" ", t).strip()
    return t[:80]


def font_family(name: str, fallback: str = CJK_FALLBACK) -> str:
    """字体族拼接。字体名先经白名单清理，再包引号。"""
    n = sanitize_font_name(name)
    if not n:
        return fallback
    if re.search(r"mono|courier|consolas", n, re.IGNORECASE):
        return f'"{n}",{MONO_FALLBACK}'
    return f'"{n}",{fallback}'


# ─────────────────────────────────────────────────────────────
# 边框
# ─────────────────────────────────────────────────────────────


def border_to_css(side: BorderSide) -> str:
    """边框线型 → CSS 值（不含 border-x: 前缀）。"""
    c = side.color or "#000000"
    style = side.style
    table = {
        "none": "none",
        "hair": f"1px solid {c}",       # 浏览器无法表达 0.5px 时的退化
        "thin": f"1px solid {c}",
        "medium": f"2px solid {c}",
        "thick": f"3px solid {c}",
        "dotted": f"1px dotted {c}",
        "dashed": f"1px dashed {c}",
        "mediumDashed": f"2px dashed {c}",
        "dashDot": f"1px dashed {c}",       # 近似
        "mediumDashDot": f"2px dashed {c}",  # 近似
        "dashDotDot": f"1px dotted {c}",     # 近似
        "mediumDashDotDot": f"2px dotted {c}",  # 近似
        "slantDashDot": f"2px dashed {c}",   # 近似
        "double": f"3px double {c}",
    }
    return table.get(style, f"1px solid {c}")


# ─────────────────────────────────────────────────────────────
# 填充
# ─────────────────────────────────────────────────────────────


def _lin(deg: int, w: int, gap: int, fg: str) -> str:
    return f"repeating-linear-gradient({deg}deg,{fg} 0 {w}px,transparent {w}px {gap}px)"


def pattern_to_css(fill: FillStyle) -> List[str]:
    """图案填充近似 —— 用 repeating-linear-gradient 模拟 18 种 patternType。"""
    fg = fill.fg_color or "#000000"
    bg = fill.bg_color or "#FFFFFF"
    p = fill.pattern or "solid"
    if p == "solid":
        return [f"background-color:{fg}"]
    if p == "darkGray":
        return [f"background-color:{bg}", f"background-image:{_lin(0, 3, 4, fg)}"]
    if p == "mediumGray":
        return [f"background-color:{bg}", f"background-image:{_lin(0, 1, 2, fg)}"]
    if p == "lightGray":
        return [f"background-color:{bg}", f"background-image:{_lin(45, 1, 4, fg)}"]
    if p == "gray125":
        return [f"background-color:{bg}", f"background-image:{_lin(45, 1, 8, fg)}"]
    if p == "gray0625":
        return [f"background-color:{bg}", f"background-image:{_lin(45, 1, 16, fg)}"]
    if p == "darkHorizontal":
        return [f"background-color:{bg}", f"background-image:{_lin(0, 1, 2, fg)}"]
    if p == "darkVertical":
        return [f"background-color:{bg}", f"background-image:{_lin(90, 1, 2, fg)}"]
    if p == "darkDown":
        return [f"background-color:{bg}", f"background-image:{_lin(135, 1, 6, fg)}"]
    if p == "darkUp":
        return [f"background-color:{bg}", f"background-image:{_lin(45, 1, 6, fg)}"]
    if p == "darkGrid":
        return [f"background-color:{bg}", f"background-image:{_lin(0, 1, 2, fg)},{_lin(90, 1, 2, fg)}"]
    if p == "darkTrellis":
        return [f"background-color:{bg}", f"background-image:{_lin(45, 1, 6, fg)},{_lin(135, 1, 6, fg)}"]
    if p == "lightHorizontal":
        return [f"background-color:{bg}", f"background-image:{_lin(0, 1, 4, fg)}"]
    if p == "lightVertical":
        return [f"background-color:{bg}", f"background-image:{_lin(90, 1, 4, fg)}"]
    if p == "lightDown":
        return [f"background-color:{bg}", f"background-image:{_lin(135, 1, 8, fg)}"]
    if p == "lightUp":
        return [f"background-color:{bg}", f"background-image:{_lin(45, 1, 8, fg)}"]
    if p == "lightGrid":
        return [f"background-color:{bg}", f"background-image:{_lin(0, 1, 4, fg)},{_lin(90, 1, 4, fg)}"]
    if p == "lightTrellis":
        return [f"background-color:{bg}", f"background-image:{_lin(45, 1, 8, fg)},{_lin(135, 1, 8, fg)}"]
    return [f"background-color:{fg}"]


def gradient_to_css(fill: FillStyle) -> List[str]:
    """渐变填充（V1 只支持线性双色）。"""
    g = fill.gradient
    if not g or not g.get("stops"):
        return []
    stops = sorted(g["stops"], key=lambda s: s.get("pos", 0))
    stops_str = ",".join(f"{s.get('color', '#FFFFFF')} {round(s.get('pos', 0) * 100)}%" for s in stops)
    # Excel 渐变角度 0° = 从左到右且顺时针；CSS linear-gradient 从上方起算顺时针
    degree = g.get("degree", 0) or 0
    deg = ((90 - degree) % 360 + 360) % 360
    return [f"background-image:linear-gradient({deg}deg,{stops_str})"]


# ─────────────────────────────────────────────────────────────
# 对齐
# ─────────────────────────────────────────────────────────────

_H2CSS = {
    "left": "left",
    "center": "center",
    "right": "right",
    "fill": "left",             # 近似：浏览器无法重复填充
    "justify": "justify",
    "centerContinuous": "center",
    "distributed": "justify",
}

_V2CSS = {
    "top": "top",
    "center": "middle",
    "bottom": "bottom",
    "justify": "middle",        # 近似
    "distributed": "middle",    # 近似
}


def effective_align(h: str, cell_type: str, is_date_fmt: bool) -> str:
    """general 对齐的智能判定：数值/日期右对齐，文本左对齐，布尔/错误居中。"""
    if h != "general":
        return _H2CSS.get(h, "left")
    if cell_type in ("n", "d") or is_date_fmt:
        return "right"
    if cell_type in ("b", "e"):
        return "center"
    return "left"


# ─────────────────────────────────────────────────────────────
# 主映射
# ─────────────────────────────────────────────────────────────


def style_to_css_decls(
    s: StyleModel,
    cell_type: str = "s",
    is_date_fmt: bool = False,
    with_font: bool = True,
) -> List[str]:
    """把 StyleModel 转成完整的 CSS 声明数组（不含选择器）。

    返回值直接 ';'.join(...) 即得一条规则体。
    """
    d: List[str] = []

    # ── 字体 ──
    if with_font:
        d.append(f"font-family:{font_family(s.font.name)}")
        d.append(f"font-size:{num_css(_round2(pt_to_px(s.font.size_pt)))}px")
        if s.font.bold:
            d.append("font-weight:700")
        if s.font.italic:
            d.append("font-style:italic")

        td: List[str] = []
        if s.font.strike:
            td.append("line-through")
        if s.font.underline in (1, 2, 33, 34):
            td.append("underline")
        if td:
            d.append("text-decoration:" + " ".join(td))
            if s.font.underline in (2, 34):
                d.append("text-decoration-style:double")
        if s.font.color:
            d.append(f"color:{s.font.color}")
        # ⚠️ 上下标不能在这里输出 vertical-align：<td> 上该属性表示"单元格垂直对齐"，
        #    会被后面输出的 vertical-align:bottom 覆盖（真实 bug）。
        #    正确做法是把内容包进内联 <span>，见 vertical_align_wrapper()。

    # ── 填充 ──
    if s.fill.type == "solid" and s.fill.fg_color:
        d.append(f"background-color:{s.fill.fg_color}")
    elif s.fill.type == "pattern":
        d.extend(pattern_to_css(s.fill))
    elif s.fill.type == "gradient":
        d.extend(gradient_to_css(s.fill))

    # ── 边框 ──
    for key, prop in (("top", "border-top"), ("right", "border-right"), ("bottom", "border-bottom"), ("left", "border-left")):
        side = getattr(s.border, key)
        if not side or side.style == "none":
            continue
        d.append(f"{prop}:{border_to_css(side)}")

    # ── 对齐 ──
    align = effective_align(s.alignment.h, cell_type, is_date_fmt)
    d.append(f"text-align:{align}")
    d.append(f"vertical-align:{_V2CSS.get(s.alignment.v, 'bottom')}")

    # 逐条 append，方便调用方做声明级去重
    if s.alignment.wrap:
        d.append("white-space:pre-wrap")
        d.append("word-break:break-word")
    else:
        d.append("white-space:nowrap")
        d.append("overflow:hidden")
        d.append("text-overflow:clip")

    if s.alignment.indent:
        d.append(f"padding-left:{s.alignment.indent * 7}px")

    if s.alignment.text_rotation == 255:
        d.append("writing-mode:vertical-rl")
        d.append("text-orientation:upright")
    elif s.alignment.text_rotation:
        # ⚠️ Excel 逆时针为正，CSS 顺时针为正 → 必须取负
        d.append(f"transform:rotate({-s.alignment.text_rotation}deg)")
        d.append("transform-origin:left center")

    if s.alignment.reading_order == 2:
        d.append("direction:rtl")

    if s.alignment.shrink_to_fit:
        d.append("font-size-adjust:0.5")  # 近似占位，V2 精确实现

    return d


def decls_to_rule(decls: List[str]) -> str:
    """声明数组 → 一条 CSS 规则体。"""
    return ";".join(decls)


def vertical_align_wrapper(s: StyleModel) -> Optional[str]:
    """上下标需要的内联包裹样式。

    ⚠️ 必须在 <td> 内部再包一层 <span>：vertical-align 作用在 <td> 上表示
       "单元格垂直对齐"，与上下标语义冲突且会被覆盖。包成行内元素后才正确。
    """
    if s.font.vert_align not in ("superscript", "subscript"):
        return None
    size = num_css(round(pt_to_px(s.font.size_pt) * 0.75 * 100) / 100)
    va = "super" if s.font.vert_align == "superscript" else "sub"
    return f"vertical-align:{va};font-size:{size}px"


def is_blank_style(s: StyleModel) -> bool:
    """样式对象是否"视觉上完全为空"（用于跳过默认样式的输出）。"""
    b = s.border
    return (
        s.fill.type == "none"
        and not b.top and not b.bottom and not b.left and not b.right
        and s.font.bold is False and s.font.italic is False and s.font.strike is False
        and s.font.underline == 0 and s.alignment.wrap is False
        and s.alignment.indent == 0 and s.alignment.text_rotation == 0
        and s.alignment.h == "general"
    )


def _round2(n: float) -> float:
    return round(n * 100) / 100


def num_css(n: float) -> str:
    """数字 → CSS 值字符串：去掉尾随的 .0（对齐 JS 数字语义，12.0 → '12'）。"""
    if isinstance(n, int):
        return str(n)
    s = f"{n:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"
