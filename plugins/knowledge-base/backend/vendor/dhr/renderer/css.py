"""样式 → CSS 映射 —— 对应《设计文档》第 7 章映射表（T-703/T-704）。

设计取舍（与 Excel 版一致）：每个段落/span 输出**完整**样式声明，
体积优化靠声明串去重（相同声明串共用一个 class）。
"""
from __future__ import annotations

from typing import List, Optional

from ..model import BorderSpec, Borders, ParaProps, RunProps
from ..styles import HIGHLIGHT_COLORS
from ..units import line_spacing_to_css

CJK_FALLBACK = '"Microsoft YaHei","微软雅黑","PingFang SC","Hiragino Sans GB","Source Han Sans CN","Noto Sans CJK SC",sans-serif'
LATIN_FALLBACK = 'Calibri,Arial,"Segoe UI",Helvetica,sans-serif'
MONO_FALLBACK = 'Consolas,"Courier New",monospace'

#: w:u 的类型 → text-decoration-style
_UNDERLINE_STYLE = {
    "single": None,
    "double": "double",
    "dotted": "dotted",
    "dottedHeavy": "dotted",
    "dash": "dashed",
    "dashHeavy": "dashed",
    "dashLong": "dashed",
    "dashLongHeavy": "dashed",
    "dotDash": "dashed",
    "dotDotDash": "dashed",
    "wave": "wavy",
    "wavyHeavy": "wavy",
    "wavyDouble": "wavy",
}
_UNDERLINE_THICK = {"thick", "dottedHeavy", "dashHeavy", "dotDashHeavy", "dotDotDashHeavy", "wavyHeavy", "dashLongHeavy"}


def _num(v: float) -> str:
    if isinstance(v, int):
        return str(v)
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def sanitize_font_name(name: object) -> str:
    """字体名白名单 —— 安全函数（字体名会拼进 <style>）。与 Excel 版同一策略。"""
    import re

    t = name.strip() if isinstance(name, str) else ""
    if not t:
        return ""
    t = re.sub(r"[^\w\s\-_.]", "", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()[:80]


def font_family_of(p: RunProps, cjk: bool = True) -> str:
    """rFonts 四槽位 → font-family 列表。

    ⚠️ eastAsia 必须排在 latin **前面**，否则中文会用拉丁字体渲染（设计文档 7.5）。
    """
    parts: List[str] = []
    ea = sanitize_font_name(p.font_east_asia)
    latin = sanitize_font_name(p.font_latin)
    if cjk and ea:
        parts.append(f'"{ea}"')
    if latin and latin != ea:
        parts.append(f'"{latin}"')
    if "mono" in latin.lower() or "courier" in latin.lower() or "consolas" in latin.lower():
        parts.append(MONO_FALLBACK)
    else:
        parts.append(CJK_FALLBACK if cjk else LATIN_FALLBACK)
    return ",".join(parts)


def border_spec_css(spec: BorderSpec) -> str:
    """Word 边框线型 → CSS border 值。"""
    style = spec.style
    color = spec.color or "#000000"
    width = max(1, spec.width_px)
    if style == "double" or style == "triple":
        return f"{width}px double {color}"
    if style in ("dashed", "dashedSmall", "dashLong", "dashLongHeavy"):
        return f"{width}px dashed {color}"
    if style in ("dotted", "dotDash", "dotDotDash"):
        return f"{width}px dotted {color}"
    if style == "wave" or style == "wavyDouble":
        return f"{width}px solid {color}"
    if style in ("inset",):
        return f"{width}px inset {color}"
    if style in ("outset",):
        return f"{width}px outset {color}"
    if style in ("thick",):
        return f"{max(3, width)}px solid {color}"
    return f"{width}px solid {color}"


def run_props_to_css(p: RunProps) -> List[str]:
    """RunProps → CSS 声明数组（7.1 映射表）。"""
    d: List[str] = []
    d.append(f"font-family:{font_family_of(p)}")
    if p.size_pt is not None:
        d.append(f"font-size:{_num(p.size_pt)}pt")
    if p.bold:
        d.append("font-weight:700")
    elif p.bold is False:
        d.append("font-weight:400")
    if p.italic:
        d.append("font-style:italic")
    elif p.italic is False:
        d.append("font-style:normal")

    deco: List[str] = []
    deco_style: Optional[str] = None
    deco_thickness = False
    if p.underline:
        deco.append("underline")
        st = _UNDERLINE_STYLE.get(p.underline)
        if st:
            deco_style = st
        if p.underline in _UNDERLINE_THICK:
            deco_thickness = True
    if p.strike:
        deco.append("line-through")
    if p.dstrike:
        deco.append("line-through")
        deco_style = "double"
    if deco:
        d.append("text-decoration:" + " ".join(deco))
        if deco_style:
            d.append(f"text-decoration-style:{deco_style}")
        if deco_thickness:
            d.append("text-decoration-thickness:2px")
        if p.underline_color:
            d.append(f"text-decoration-color:{p.underline_color}")

    if p.color:
        d.append(f"color:{p.color}")
    if p.highlight:
        d.append(f"background-color:{HIGHLIGHT_COLORS.get(p.highlight, '#FFFF00')}")
    elif p.shd_fill:
        d.append(f"background-color:{p.shd_fill}")

    if p.letter_spacing_pt is not None:
        d.append(f"letter-spacing:{_num(p.letter_spacing_pt)}pt")
    if p.vert_align == "superscript":
        d.append("vertical-align:super")
        d.append("font-size:0.65em")
    elif p.vert_align == "subscript":
        d.append("vertical-align:sub")
        d.append("font-size:0.65em")
    elif p.position_pt is not None and p.position_pt != 0:
        d.append(f"position:relative;top:{_num(-p.position_pt)}pt")
    if p.caps:
        d.append("text-transform:uppercase")
    if p.small_caps:
        d.append("font-variant:small-caps")
    if p.rtl:
        d.append("direction:rtl")
        d.append("unicode-bidi:embed")
    if p.vanish:
        d.append("display:none")
    return d


def para_props_to_css(p: ParaProps, default_tab_px: float = 48.0) -> List[str]:
    """ParaProps → CSS 声明数组（7.2 映射表）。"""
    d: List[str] = []
    if p.align:
        mapping = {"left": "left", "start": "left", "center": "center", "right": "right",
                   "end": "right", "both": "justify", "distribute": "justify"}
        v = mapping.get(p.align)
        if v:
            d.append(f"text-align:{v}")
            if p.align == "distribute":
                d.append("text-justify:inter-character")
    # ⚠️ 缩进用 margin（不用 padding —— 会和边框/底纹冲突）
    # ★ 字符单位（leftChars/rightChars）用 em：随字号缩放，与 Word 一致；
    #   与 px 二选一（styles.parse_ppr 保证同组已互相清空）
    left_em = getattr(p, "_left_chars_em", None)
    if left_em is not None:
        d.append(f"margin-left:{_num(left_em)}em")
    elif p.indent_left_px is not None:
        d.append(f"margin-left:{_num(p.indent_left_px)}px")
    right_em = getattr(p, "_right_chars_em", None)
    if right_em is not None:
        d.append(f"margin-right:{_num(right_em)}em")
    elif p.indent_right_px is not None:
        d.append(f"margin-right:{_num(p.indent_right_px)}px")
    # ★ 首行/悬挂缩进：px 与 em（Chars）二选一。带形态标记时按标记取
    #   （styles.parse_ppr 写入、merge_para 透传），避免样式的字符缩进压过
    #   段落直接写的 twips 缩进；无标记走历史优先级（em 优先）
    fl_form = getattr(p, "_fl_form", None)
    if fl_form == "px":
        if p.first_line_px is not None:
            d.append(f"text-indent:{_num(p.first_line_px)}px")
    elif fl_form == "em":
        if p.first_line_em is not None:
            d.append(f"text-indent:{_num(p.first_line_em)}em")
    elif p.first_line_em is not None:
        d.append(f"text-indent:{_num(p.first_line_em)}em")
    elif p.first_line_px is not None:
        d.append(f"text-indent:{_num(p.first_line_px)}px")
    hg_form = getattr(p, "_hg_form", None)
    if hg_form == "px":
        if p.hanging_px is not None:
            d.append(f"text-indent:-{_num(p.hanging_px)}px")
            d.append(f"padding-left:{_num(p.hanging_px)}px")
    elif hg_form == "em":
        if p.hanging_em is not None:
            d.append(f"text-indent:-{_num(p.hanging_em)}em")
            d.append(f"padding-left:{_num(p.hanging_em)}em")
    elif p.hanging_em is not None:
        d.append(f"text-indent:-{_num(p.hanging_em)}em")
        d.append(f"padding-left:{_num(p.hanging_em)}em")
    elif p.hanging_px is not None:
        d.append(f"text-indent:-{_num(p.hanging_px)}px")
        d.append(f"padding-left:{_num(p.hanging_px)}px")
    if p.space_before_pt is not None:
        d.append(f"margin-top:{_num(p.space_before_pt)}pt")
    if p.space_after_pt is not None:
        d.append(f"margin-bottom:{_num(p.space_after_pt)}pt")
    ls_css = line_spacing_to_css(p.line_spacing)
    if ls_css:
        d.append(f"line-height:{ls_css}")
    if p.borders:
        b: Borders = p.borders
        for side in ("top", "left", "bottom", "right"):
            spec = getattr(b, side)
            if spec and spec.style not in ("nil", "none"):
                d.append(f"border-{side}:{border_spec_css(spec)}")
                d.append(f"padding-{side}:3px")
        if b.bar:
            d.append(f"border-left:{border_spec_css(b.bar)}")
            d.append("padding-left:4px")
    if p.shd_fill:
        d.append(f"background-color:{p.shd_fill}")
    if p.vertical:
        d.append("writing-mode:vertical-rl")
        d.append("text-orientation:mixed")
    if p.keep_next:
        d.append("break-after:avoid")
    if p.keep_lines:
        d.append("break-inside:avoid")
    if p.page_break_before:
        d.append("break-before:page")
    if p.widow_control:
        d.append("orphans:2")
        d.append("widows:2")
    if p.bidi:
        d.append("direction:rtl")
    return d
