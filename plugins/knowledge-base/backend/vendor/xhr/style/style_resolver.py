"""样式解析 —— 把解析层给出的原始样式对象规范成 StyleModel。

对应 TS 版 ``packages/style/src/styleResolver.ts``。

⚠️ 本模块是纯函数：不读文件、不抛错（非法输入一律降级为默认值），
   保证"永不崩溃"的降级总原则。

输入形状（由 parser_xlsx 从 XML 归一化而来，与 ExcelJS 的暴露形态对齐）::

    font:      {name?, size?, bold?, italic?, underline?, strike?, color?, vertAlign?, scheme?}
    fill:      {type: 'pattern'|'gradient', pattern?, fgColor?, bgColor?, degree?, stops?}
    border:    {top?/bottom?/left?/right?/diagonal?: {style?, color?, up?, down?}}
    alignment: {horizontal?, vertical?, wrapText?, shrinkToFit?, indent?, textRotation?, readingOrder?}
    numFmt:    'General' 形式的格式码字符串 或 numFmtId 整数
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core import (
    AlignmentStyle,
    BorderStyle,
    BorderSide,
    FillStyle,
    FontStyle,
    NumFmtStyle,
    StyleModel,
)
from .color import ColorCtx, resolve_color
from .numfmt import build_num_fmt_map


@dataclass
class StyleContext:
    """解析上下文：主题色 + 调色板 + 数字格式表 + 默认字体。"""

    color_ctx: ColorCtx
    num_fmt_map: dict[int, str]
    default_font_name: str = "Calibri"
    default_font_size_pt: float = 11.0


def create_style_context(
    theme_colors: list[str],
    indexed: list[str],
    num_fmt_map: Optional[dict[int, str]] = None,
    default_font_name: str = "Calibri",
    default_font_size_pt: float = 11.0,
) -> StyleContext:
    return StyleContext(
        color_ctx=ColorCtx(theme_colors=theme_colors, indexed=indexed),
        num_fmt_map=build_num_fmt_map(num_fmt_map),
        default_font_name=default_font_name,
        default_font_size_pt=default_font_size_pt,
    )


# ─────────────────────────────────────────────────────────────
# 默认样式
# ─────────────────────────────────────────────────────────────


def default_font_style(ctx: StyleContext) -> FontStyle:
    return FontStyle(
        name=ctx.default_font_name,
        size_pt=ctx.default_font_size_pt,
        bold=False,
        italic=False,
        underline=0,
        strike=False,
        color="#000000",
    )


def default_alignment() -> AlignmentStyle:
    return AlignmentStyle(
        h="general",
        v="bottom",  # Excel 默认垂直靠下
        wrap=False,
        shrink_to_fit=False,
        indent=0,
        text_rotation=0,
        reading_order=0,
    )


def create_default_style(ctx: StyleContext) -> StyleModel:
    return StyleModel(
        font=default_font_style(ctx),
        fill=FillStyle(type="none"),
        border=BorderStyle(),
        alignment=default_alignment(),
        num_fmt=NumFmtStyle(id=0, code="General"),
        protection={"locked": True, "hidden": False},
        quote_prefix=False,
        is_default=True,
    )


# ─────────────────────────────────────────────────────────────
# 各子项解析
# ─────────────────────────────────────────────────────────────


def to_raw_color(c: object) -> Optional[dict]:
    """归一化 RawColor：把解析层给出的各种形态统一成 resolve_color 认识的 dict。"""
    if not isinstance(c, dict):
        return None
    if isinstance(c.get("argb"), str) and c["argb"]:
        return {"rgb": c["argb"]}
    if isinstance(c.get("rgb"), str) and c["rgb"]:
        return {"rgb": c["rgb"]}
    if isinstance(c.get("theme"), (int, float)) and not isinstance(c.get("theme"), bool):
        out = {"theme": int(c["theme"])}
        if isinstance(c.get("tint"), (int, float)):
            out["tint"] = float(c["tint"])
        return out
    if isinstance(c.get("indexed"), (int, float)) and not isinstance(c.get("indexed"), bool):
        return {"indexed": int(c["indexed"])}
    if c.get("auto"):
        return {"auto": 1}
    return None


def normalize_underline(u: object) -> int:
    """下划线表示 → 我们的编码（0/1/2/33/34）。"""
    if u is True:
        return 1
    if not u or u == "none" or u is False:
        return 0
    s = str(u)
    return {
        "single": 1,
        "double": 2,
        "singleAccounting": 33,
        "doubleAccounting": 34,
    }.get(s, 1)


def resolve_font(raw: object, ctx: StyleContext) -> FontStyle:
    f = raw if isinstance(raw, dict) else {}
    base = default_font_style(ctx)
    size = f.get("size")
    color = resolve_color(to_raw_color(f.get("color")), ctx.color_ctx, "#000000")
    vert = f.get("vertAlign")
    name = f.get("name")
    scheme = f.get("scheme")
    return FontStyle(
        name=name.strip() if isinstance(name, str) and name.strip() else base.name,
        size_pt=size if isinstance(size, (int, float)) and size > 0 else base.size_pt,
        bold=f.get("bold") is True,
        italic=f.get("italic") is True,
        underline=normalize_underline(f.get("underline")),
        strike=f.get("strike") is True,
        color=color,
        vert_align=vert if vert in ("superscript", "subscript") else None,
        scheme=scheme if scheme in ("major", "minor") else None,
    )


PATTERN_TYPES = {
    "darkDown", "darkGray", "darkGrid", "darkHorizontal", "darkTrellis", "darkUp", "darkVertical",
    "gray0625", "gray125", "lightDown", "lightGray", "lightGrid", "lightHorizontal",
    "lightTrellis", "lightUp", "lightVertical", "mediumGray", "none", "solid",
}


def resolve_fill(raw: object, ctx: StyleContext) -> FillStyle:
    f = raw if isinstance(raw, dict) else {}
    ftype = str(f.get("type") or "none")

    if ftype == "gradient":
        stops_raw = f.get("stops") if isinstance(f.get("stops"), list) else []
        stops = []
        for s in stops_raw:
            o = s if isinstance(s, dict) else {}
            pos = o.get("position")
            stops.append(
                {
                    "pos": pos if isinstance(pos, (int, float)) else 0,
                    "color": resolve_color(to_raw_color(o.get("color")), ctx.color_ctx, "#FFFFFF"),
                }
            )
        degree = f.get("degree")
        if not stops:
            return FillStyle(type="none")
        return FillStyle(type="gradient", gradient={"degree": degree if isinstance(degree, (int, float)) else 0, "stops": stops})

    if ftype == "pattern":
        pattern_raw = str(f.get("pattern") or "none")
        pattern = pattern_raw if pattern_raw in PATTERN_TYPES else "solid"
        fg = to_raw_color(f.get("fgColor"))
        bg = to_raw_color(f.get("bgColor"))

        if pattern == "none":
            return FillStyle(type="none")

        # ⚠️ solid 时 fgColor 即单元格底色；图案时 fgColor 是线条色、bgColor 是底纹
        if pattern == "solid":
            color = resolve_color(fg, ctx.color_ctx) if fg else None
            if not color:
                return FillStyle(type="none")  # 无颜色（如仅 bgColor）视为无填充
            return FillStyle(type="solid", fg_color=color)
        return FillStyle(
            type="pattern",
            pattern=pattern,
            fg_color=resolve_color(fg, ctx.color_ctx) if fg else None,
            bg_color=resolve_color(bg, ctx.color_ctx, "#FFFFFF") if bg else "#FFFFFF",
        )

    return FillStyle(type="none")


BORDER_STYLES = {
    "thin", "medium", "thick", "dashed", "dotted", "double", "hair",
    "mediumDashed", "dashDot", "mediumDashDot", "dashDotDot", "mediumDashDotDot", "slantDashDot",
}


def to_border_side(raw: object, ctx: StyleContext) -> Optional[BorderSide]:
    o = raw if isinstance(raw, dict) else {}
    style = o.get("style")
    if not style or style == "none":
        return None
    norm = str(style) if str(style) in BORDER_STYLES else "thin"
    return BorderSide(style=norm, color=resolve_color(to_raw_color(o.get("color")), ctx.color_ctx, "#000000"))


def resolve_border(raw: object, ctx: StyleContext) -> BorderStyle:
    b = raw if isinstance(raw, dict) else {}
    out = BorderStyle()
    for key in ("top", "bottom", "left", "right"):
        side = to_border_side(b.get(key), ctx)
        if side:
            setattr(out, key, side)

    diag = b.get("diagonal")
    d_side = to_border_side(diag, ctx)
    if d_side and isinstance(diag, dict):
        if diag.get("up"):
            out.diagonal_up = d_side
        if diag.get("down"):
            out.diagonal_down = d_side
        if not diag.get("up") and not diag.get("down"):
            out.diagonal_down = d_side
    return out


H_ALIGN = {
    "general", "left", "center", "right", "fill", "justify", "centerContinuous", "distributed",
}


def normalize_text_rotation(raw: object) -> int:
    """归一化文字旋转角。

    ⚠️ 语义（与 TS 版一致）：正数=逆时针、负数=顺时针、255=竖排。
      1. ExcelJS 用 'vertical' 表示竖排；OOXML 原生用 255。
      2. OOXML 的 91~180 表示"顺时针"（ExcelJS 读回为 90-N，即负角度）。
    """
    if raw == "vertical":
        return 255
    if isinstance(raw, str):
        try:
            raw = float(raw)
        except ValueError:
            return 0
    n = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0
    n = int(n)
    if n == 255:
        return 255
    if -90 <= n <= 90:
        return n
    if 90 < n <= 180:
        return 90 - n  # OOXML 原始值 → 统一语义
    return 0


def resolve_alignment(raw: object) -> AlignmentStyle:
    a = raw if isinstance(raw, dict) else {}
    base = default_alignment()

    h = str(a.get("horizontal") or "general")
    v_raw = str(a.get("vertical") or "bottom")
    if v_raw == "middle":
        v = "center"
    elif v_raw in ("top", "center", "bottom", "justify", "distributed"):
        v = v_raw
    else:
        v = "bottom"

    ro_raw = a.get("readingOrder")
    if ro_raw == "rtl" or ro_raw == 2:
        reading_order = 2
    elif ro_raw == "ltr" or ro_raw == 1:
        reading_order = 1
    else:
        reading_order = 0

    indent = a.get("indent")
    indent_n = min(int(indent), 250) if isinstance(indent, (int, float)) and indent > 0 else 0

    return AlignmentStyle(
        h=h if h in H_ALIGN else "general",
        v=v,
        wrap=a.get("wrapText") is True,
        shrink_to_fit=a.get("shrinkToFit") is True,
        indent=indent_n,
        text_rotation=normalize_text_rotation(a.get("textRotation")),
        reading_order=reading_order,
    )


def resolve_num_fmt(raw: object, ctx: StyleContext, fmt_id: Optional[int] = None) -> NumFmtStyle:
    code = "General"
    if isinstance(raw, str) and raw.strip():
        code = raw
    elif isinstance(fmt_id, int):
        code = ctx.num_fmt_map.get(fmt_id, "General")

    resolved_id = fmt_id if isinstance(fmt_id, int) else 0
    if not isinstance(fmt_id, int):
        for k, v in ctx.num_fmt_map.items():
            if v == code:
                resolved_id = k
                break
    return NumFmtStyle(id=resolved_id, code=code)


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────


def resolve_style(raw: object, ctx: StyleContext) -> StyleModel:
    """把解析层给出的单元格样式规范成 StyleModel。输入非法时逐项降级，绝不抛错。"""
    s = raw if isinstance(raw, dict) else {}
    num_fmt_raw = s.get("numFmt")
    num_fmt_id = s.get("numFmtId")
    protection = s.get("protection") if isinstance(s.get("protection"), dict) else {}
    return StyleModel(
        font=resolve_font(s.get("font"), ctx),
        fill=resolve_fill(s.get("fill"), ctx),
        border=resolve_border(s.get("border"), ctx),
        alignment=resolve_alignment(s.get("alignment")),
        num_fmt=resolve_num_fmt(num_fmt_raw, ctx, num_fmt_id if isinstance(num_fmt_id, int) else None),
        protection={
            "locked": protection.get("locked", True) is not False,
            "hidden": protection.get("hidden", False) is True,
        },
        quote_prefix=s.get("quotePrefix") is True,
    )
