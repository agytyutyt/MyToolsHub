"""styles.xml 全量解析 —— 把 cellXfs 解析成 resolve_style 认识的 raw style dict。

对应 TS 版里 ExcelJS 承担的「样式继承」职责 + 设计文档 4.2 的陷阱说明：

⚠️ 陷阱 1（applyXxx 标志位）：
    applyFont="0"（显式）表示不使用本 xf 的 fontId，应回退到继承的命名样式
    （cellStyleXfs[xfId]）。解析规则：
        · 显式 apply="0"      → 用 parent 的
        · 自身 id 非 0        → 用自身的
        · 显式 apply="1"      → 用自身的（即使为 0）
        · 缺失                → 自身 id 优先，否则 parent
⚠️ 陷阱 2：fills 前两项是约定保留项（0=none、1=gray125），文件不合规时强制补齐。
⚠️ 陷阱 3：patternFill 里 solid 时 fgColor 才是底色（style_resolver 已处理）。
⚠️ 陷阱 4：<alignment> 没有 apply 标志 —— 有子节点就用，没有就回退 parent。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .xmlutil import (
    attr,
    attr_bool,
    attr_int,
    child,
    children,
    parse_color_el,
    parse_xml,
    text_of,
)


@dataclass
class StylesBundle:
    """styles.xml 的解析产物。"""

    #: numFmtId → code（内置表由 style 层合并）
    num_fmts: Dict[int, str] = field(default_factory=dict)
    #: 原始 font dict 列表（ExcelJS 形状）
    fonts: List[dict] = field(default_factory=list)
    fills: List[dict] = field(default_factory=list)
    borders: List[dict] = field(default_factory=list)
    #: 每个 cellXf 解析继承后的 raw style dict（下标 = s 属性值）
    cell_xf_styles: List[dict] = field(default_factory=list)
    #: dxf 差异样式（原始 dict，供条件格式）
    dxfs: List[dict] = field(default_factory=list)
    #: <indexedColors> 覆盖项（无则 None）
    indexed_colors: Optional[List[str]] = None


def _bool_attr(el, name: str) -> Optional[bool]:
    return attr_bool(el, name)


def _flag(el, name: str) -> Optional[str]:
    """applyXxx 属性归一：'1'/'true' → '1'；'0'/'false' → '0'；缺失 → None。"""
    v = _bool_attr(el, name)
    if v is None:
        return None
    return "1" if v else "0"


def parse_font(font_el) -> dict:
    out: dict = {}
    if font_el is None:
        return out
    name_el = child(font_el, "name")
    if name_el is not None and name_el.get("val"):
        out["name"] = name_el.get("val")
    sz = child(font_el, "sz")
    if sz is not None and sz.get("val"):
        try:
            out["size"] = float(sz.get("val"))
        except ValueError:
            pass
    if child(font_el, "b") is not None:
        out["bold"] = True
    if child(font_el, "i") is not None:
        out["italic"] = True
    if child(font_el, "strike") is not None:
        out["strike"] = True
    u = child(font_el, "u")
    if u is not None:
        val = u.get("val")
        out["underline"] = val if val else True
    color = parse_color_el(child(font_el, "color"))
    if color:
        out["color"] = color
    va = child(font_el, "vertAlign")
    if va is not None and va.get("val"):
        out["vertAlign"] = va.get("val")
    scheme = child(font_el, "scheme")
    if scheme is not None and scheme.get("val"):
        out["scheme"] = scheme.get("val")
    return out


def parse_fill(fill_el, for_dxf: bool = False) -> dict:
    if fill_el is None:
        return {"type": "none"}
    pattern = child(fill_el, "patternFill")
    if pattern is not None:
        pattern_type = attr(pattern, "patternType") or ""
        if not pattern_type and for_dxf:
            # ⚠️ dxf 的 patternFill 缺省 patternType 时，只要带颜色就是 solid
            #   （Excel 实际写出 <patternFill><bgColor rgb=.../></patternFill>）。
            #   cellXfs 里的缺省仍然是 none。
            has_color = child(pattern, "bgColor") is not None or child(pattern, "fgColor") is not None
            pattern_type = "solid" if has_color else "none"
        out = {
            "type": "pattern",
            "pattern": pattern_type or "none",
            "fgColor": parse_color_el(child(pattern, "fgColor")),
            "bgColor": parse_color_el(child(pattern, "bgColor")),
        }
        return out
    grad = child(fill_el, "gradientFill")
    if grad is not None:
        stops = []
        for s in children(grad, "stop"):
            stops.append({"position": attr_int(s, "position", 0) or 0, "color": parse_color_el(child(s, "color"))})
        return {"type": "gradient", "degree": attr_int(grad, "degree", 0) or 0, "stops": stops}
    return {"type": "none"}


def parse_border(border_el) -> dict:
    out: dict = {}
    if border_el is None:
        return out
    for side in ("top", "bottom", "left", "right", "diagonal"):
        el = child(border_el, side)
        if el is None:
            continue
        d: dict = {}
        style = attr(el, "style")
        if style:
            d["style"] = style
        color = parse_color_el(child(el, "color"))
        if color:
            d["color"] = color
        if not d:
            continue
        if side == "diagonal":
            # ⚠️ 方向标志是 <border diagonalUp="1" diagonalDown="1"> 上的属性，
            #    不是 <diagonal> 子元素的（写成 up/down 会永远读不到，压力测试发现）
            d["up"] = attr_bool(border_el, "diagonalUp") is True
            d["down"] = attr_bool(border_el, "diagonalDown") is True
        out[side] = d
    return out


def parse_alignment(xf_el) -> Optional[dict]:
    al = child(xf_el, "alignment")
    if al is None:
        return None
    out: dict = {}
    for src, dst in (("horizontal", "horizontal"), ("vertical", "vertical")):
        v = attr(al, src)
        if v:
            out[dst] = v
    # readingOrder 在 XML 里是数字字符串（'0'/'1'/'2'），必须转 int，
    # 否则 resolve_alignment 的 'rtl'/2 判断全部落空（压力测试发现）
    ro = attr(al, "readingOrder")
    if ro:
        try:
            out["readingOrder"] = int(float(ro))
        except ValueError:
            out["readingOrder"] = ro
    if attr_bool(al, "wrapText") is True:
        out["wrapText"] = True
    if attr_bool(al, "shrinkToFit") is True:
        out["shrinkToFit"] = True
    indent = attr_int(al, "indent")
    if indent is not None:
        out["indent"] = indent
    rot = attr(al, "textRotation")
    if rot:
        try:
            out["textRotation"] = int(float(rot))
        except ValueError:
            out["textRotation"] = rot  # 'vertical' 等字符串形式
    return out


def parse_protection(xf_el) -> Optional[dict]:
    p = child(xf_el, "protection")
    if p is None:
        return None
    return {
        "locked": attr_bool(p, "locked"),
        "hidden": attr_bool(p, "hidden"),
    }


def _pick(own: Optional[int], flag: Optional[str], parent: Optional[int]) -> Optional[int]:
    """applyXxx 继承规则（见模块 docstring 陷阱 1）。"""
    if flag == "0":
        return parent
    if own:
        return own
    if flag == "1":
        return own
    return parent if parent else own


def parse_styles(files: dict[str, bytes], styles_path: str = "xl/styles.xml") -> StylesBundle:
    """解析 styles.xml，产出带继承的 cellXf raw style 列表。"""
    bundle = StylesBundle()
    root = parse_xml(files.get(styles_path))
    if root is None:
        # 不合规文件：至少补齐 fills 约定保留项
        bundle.fills = [{"type": "pattern", "pattern": "none"}, {"type": "pattern", "pattern": "gray125"}]
        return bundle

    # ── numFmts ──
    fmts_el = child(root, "numFmts")
    if fmts_el is not None:
        for n in children(fmts_el, "numFmt"):
            try:
                fid = int(float(n.get("numFmtId") or ""))
            except ValueError:
                continue
            code = n.get("formatCode") or ""
            if code:
                bundle.num_fmts[fid] = code

    # ── fonts / fills / borders ──
    fonts_el = child(root, "fonts")
    if fonts_el is not None:
        bundle.fonts = [parse_font(f) for f in children(fonts_el, "font")]

    fills_el = child(root, "fills")
    if fills_el is not None:
        bundle.fills = [parse_fill(f) for f in children(fills_el, "fill")]
    # ⚠️ 陷阱 2：fills[0]=none、fills[1]=gray125 是约定保留项
    while len(bundle.fills) < 2:
        kind = "none" if len(bundle.fills) == 0 else "gray125"
        bundle.fills.insert(0 if len(bundle.fills) == 0 else len(bundle.fills), {"type": "pattern", "pattern": kind})

    borders_el = child(root, "borders")
    if borders_el is not None:
        bundle.borders = [parse_border(b) for b in children(borders_el, "border")]

    # ── cellStyleXfs（命名样式，继承源）──
    parent_xfs: List[dict] = []
    csx_el = child(root, "cellStyleXfs")
    if csx_el is not None:
        for xf in children(csx_el, "xf"):
            parent_xfs.append(
                {
                    "numFmtId": attr_int(xf, "numFmtId", 0),
                    "fontId": attr_int(xf, "fontId", 0),
                    "fillId": attr_int(xf, "fillId", 0),
                    "borderId": attr_int(xf, "borderId", 0),
                    "alignment": parse_alignment(xf),
                    "protection": parse_protection(xf),
                }
            )

    def parent_of(xf) -> dict:
        xf_id = attr_int(xf, "xfId", 0) or 0
        if 0 <= xf_id < len(parent_xfs):
            return parent_xfs[xf_id]
        return {}

    def get(list_, idx: Optional[int], default: dict) -> dict:
        if idx is not None and 0 <= idx < len(list_):
            return list_[idx]
        return default

    def border_of(idx: Optional[int]) -> dict:
        b = get(bundle.borders, idx, {})
        return b if b else {}

    # ── cellXfs ──
    cx_el = child(root, "cellXfs")
    if cx_el is not None:
        for xf in children(cx_el, "xf"):
            parent = parent_of(xf)
            font_id = _pick(attr_int(xf, "fontId", 0), _flag(xf, "applyFont"), parent.get("fontId"))
            fill_id = _pick(attr_int(xf, "fillId", 0), _flag(xf, "applyFill"), parent.get("fillId"))
            border_id = _pick(attr_int(xf, "borderId", 0), _flag(xf, "applyBorder"), parent.get("borderId"))
            num_fmt_id = _pick(attr_int(xf, "numFmtId", 0), _flag(xf, "applyNumberFormat"), parent.get("numFmtId"))

            # ⚠️ 陷阱 4：alignment 无 apply 标志，有子节点就用
            alignment = parse_alignment(xf)
            if alignment is None and _flag(xf, "applyAlignment") != "0":
                alignment = parent.get("alignment")

            protection = parse_protection(xf)
            if protection is None and _flag(xf, "applyProtection") != "0":
                protection = parent.get("protection")

            style: dict = {
                "font": get(bundle.fonts, font_id, {}),
                "fill": get(bundle.fills, fill_id, {"type": "pattern", "pattern": "none"}),
                "border": border_of(border_id),
                "alignment": alignment,
                "protection": protection,
                "numFmtId": num_fmt_id if num_fmt_id is not None else 0,
                "quotePrefix": attr_bool(xf, "quotePrefix") is True,
            }
            bundle.cell_xf_styles.append(style)

    # ── dxfs（条件格式的差异样式）──
    dxfs_el = child(root, "dxfs")
    if dxfs_el is not None:
        for dxf in children(dxfs_el, "dxf"):
            raw: dict = {}
            font_el = child(dxf, "font")
            if font_el is not None:
                raw["font"] = parse_font(font_el)
            fill_el = child(dxf, "fill")
            if fill_el is not None:
                raw["fill"] = parse_fill(fill_el, for_dxf=True)
            border_el = child(dxf, "border")
            if border_el is not None:
                raw["border"] = parse_border(border_el)
            al = child(dxf, "alignment")
            if al is not None:
                raw["alignment"] = {
                    k: al.get(k)
                    for k in ("horizontal", "vertical", "wrapText", "indent", "textRotation", "readingOrder")
                    if al.get(k) is not None
                }
            nf = child(dxf, "numFmt")
            if nf is not None and nf.get("numFmtId"):
                try:
                    raw["numFmt"] = {"numFmtId": int(float(nf.get("numFmtId")))}
                except ValueError:
                    pass
            bundle.dxfs.append(raw)

    # ── indexedColors 覆盖项 ──
    colors_el = child(root, "colors")
    if colors_el is not None:
        indexed = child(colors_el, "indexedColors")
        if indexed is not None:
            from ..style import DEFAULT_INDEXED_PALETTE, normalize_hex6

            out = ["#" + normalize_hex6(c.get("rgb") or "") for c in children(indexed, "rgbColor")]
            if out:
                bundle.indexed_colors = out[:56] if len(out) >= 56 else out + DEFAULT_INDEXED_PALETTE[len(out) :]

    return bundle
