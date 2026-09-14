"""样式继承解析 —— 对应《设计文档》6.4（T-701）与 6.7（T-702）。

★ 唯一正确的合并顺序（6.4.1，照抄）::

    段落最终属性 = docDefaults.pPr ① + 样式链(root→…→style).pPr ② + 段落直接 pPr ③
    字符最终属性 = docDefaults.rPr ① + 段落标记 rPr(pPr/rPr) ② + 样式链.rPr ③ + run 直接 rPr ④

⚠️ 三个必须守住的点（TODO 清单"四类跑偏"里的前两类）：
  1. 切换属性（toggle）：``<w:b/>`` = true；``<w:b w:val="0"/>`` = **显式关闭**，
     必须覆盖继承值 —— 把"节点存在"直接当 true 是第二号 bug；
  2. 合并时只有 ``None`` 才继承，不能 Object.assign（会把 None 盖掉继承值）；
  3. 字符属性链必须包含**段落标记的 rPr**（pPr/rPr），漏掉标题字号会丢。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import (
    PARA_FIELDS,
    RUN_FIELDS,
    BorderSpec,
    Borders,
    ParaProps,
    RunProps,
    StyleDef,
    TabStop,
)
from .units import (
    chars100_to_em,
    eighth_pt_to_px,
    half_point_to_pt,
    parse_line_spacing,
    twentieth_to_pt,
    twip_to_px,
)

# ─────────────────────────────────────────────────────────────
# 高亮 16 色（6.7.1）
# ─────────────────────────────────────────────────────────────

HIGHLIGHT_COLORS: Dict[str, str] = {
    "black": "#000000", "blue": "#0000FF", "cyan": "#00FFFF", "green": "#008000",
    "magenta": "#FF00FF", "red": "#FF0000", "yellow": "#FFFF00", "white": "#FFFFFF",
    "darkBlue": "#00008B", "darkCyan": "#008B8B", "darkGreen": "#006400",
    "darkMagenta": "#800080", "darkRed": "#8B0000", "darkYellow": "#808000",
    "darkGray": "#808080", "lightGray": "#D3D3D3",
}

#: Office 默认主题兜底（clrScheme 文档顺序）
DEFAULT_THEME_COLORS: List[str] = [
    "#000000", "#FFFFFF", "#44546A", "#E7E6E6",
    "#4472C4", "#ED7D31", "#A5A5A5", "#FFC000", "#5B9BD5", "#70AD47",
    "#0563C1", "#954F72",
]

#: w:themeColor 名称 → clrScheme 语义名（注意 text1/background1 的交换与 Excel 一致）
_THEME_KEY_MAP = {
    "dark1": "dk1", "light1": "lt1", "dark2": "dk2", "light2": "lt2",
    "text1": "dk1", "background1": "lt1", "text2": "dk2", "background2": "lt2",
    "accent1": "accent1", "accent2": "accent2", "accent3": "accent3",
    "accent4": "accent4", "accent5": "accent5", "accent6": "accent6",
    "hyperlink": "hlink", "followedHyperlink": "folHlink",
}


def word_theme_tint(hex6: str, tint_hex: str) -> str:
    """Word 的 themeTint：存储值是**保留比例** t=hex/255，c' = c*t + 255*(1-t)。

    实证：accent1 #4472C4 + "浅色 40%" 在文件里是 themeTint="99"（153/255≈0.6），
    68*0.6+255*0.4 = 142.8 → 0x8E ✓。floor 截断与 Excel 版一致。
    """
    t = int(tint_hex, 16) / 255 if tint_hex else 1.0
    out = []
    for i in (0, 2, 4):
        c = int(hex6[i : i + 2], 16)
        v = c * t + 255 * (1 - t)
        out.append(f"{max(0, min(255, int(v))):02X}")
    return "#" + "".join(out)


def word_theme_shade(hex6: str, shade_hex: str) -> str:
    """Word 的 themeShade：存储值是**保留比例** s=hex/255，c' = c*s（向黑收缩）。

    实证：'深色 25%' 在文件里是 themeShade="BF"（191/255=0.75）。
    """
    s = int(shade_hex, 16) / 255 if shade_hex else 1.0
    out = []
    for i in (0, 2, 4):
        c = int(hex6[i : i + 2], 16)
        out.append(f"{max(0, min(255, int(c * s))):02X}")
    return "#" + "".join(out)


def resolve_word_color(attrs: dict, theme: dict, default: Optional[str] = None) -> Optional[str]:
    """w:color / w:shd / pBdr 的颜色解析：RRGGBB / auto / themeColor+tint/shade。

    ⚠️ 调用方必须传**净化后的子集**：w:shd 的 val 是图案类型（clear）、
    pBdr 的 val 是线型（single）——都不是颜色，直接透传会得到 '#CLEAR'。
    """
    val = attrs.get("val") or attrs.get("fill") or attrs.get("color")
    theme_color = attrs.get("themeColor")
    if theme_color:
        key = _THEME_KEY_MAP.get(theme_color, theme_color)
        base = (theme.get("colorScheme") or {}).get(key)
        if not base:
            return default
        base6 = base.lstrip("#")
        tint_hex = attrs.get("themeTint")
        shade_hex = attrs.get("themeShade")
        if tint_hex:
            return word_theme_tint(base6, tint_hex)
        if shade_hex:
            return word_theme_shade(base6, shade_hex)
        return base
    if not val or val == "auto":
        return default
    return "#" + val.upper()


# ─────────────────────────────────────────────────────────────
# XML → 属性部分值（只填出现过的字段，None 表示未设置）
# ─────────────────────────────────────────────────────────────


def _shd_attrs(shd_el) -> dict:
    """w:shd → 颜色解析子集（剔除 val 图案键，保留 fill/themeColor/tint/shade）。"""
    keys = ("fill", "themeColor", "themeTint", "themeShade")
    return {k: shd_el.get(k) for k in keys if shd_el.get(k) is not None}


def _border_attrs(border_el) -> dict:
    """边框元素 → 颜色解析子集（val 是线型不是颜色；颜色在 color 属性）。"""
    keys = ("color", "themeColor", "themeTint", "themeShade")
    return {k: border_el.get(k) for k in keys if border_el.get(k) is not None}


def toggle_attr(el) -> Optional[bool]:
    """切换属性读取：缺失=None（不覆盖）；无 val=... 值=true；val=0/false/off=false。"""
    if el is None:
        return None
    v = el.get("val")
    if v is None:
        return True
    s = str(v).strip().lower()
    if s in ("0", "false", "off", "no"):
        return False
    return True


def _num(el, name: str) -> Optional[float]:
    if el is None:
        return None
    v = el.get(name)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_rpr(rpr_el, theme: dict) -> Optional[RunProps]:
    """w:rPr → RunProps 部分值（只填出现的字段）。"""
    if rpr_el is None:
        return None
    p = RunProps()
    fonts = rpr_el.find("rFonts") if hasattr(rpr_el, "find") else None
    if fonts is not None:
        p.font_latin = fonts.get("ascii") or fonts.get("hAnsi") or None
        p.font_east_asia = fonts.get("eastAsia")
        p.font_cs = fonts.get("cs")
        hint = fonts.get("hint")
        if hint:
            p.font_hint = hint
    sz = rpr_el.find("sz")
    v = _num(sz, "val") if sz is not None else None
    if v is not None:
        p.size_pt = half_point_to_pt(v)
    p.bold = toggle_attr(rpr_el.find("b"))
    p.italic = toggle_attr(rpr_el.find("i"))
    u = rpr_el.find("u")
    if u is not None:
        uv = u.get("val") or "single"
        p.underline = None if uv == "none" else uv
        uc = resolve_word_color(dict(u.attrib), theme)
        if uc:
            p.underline_color = uc
    if toggle_attr(rpr_el.find("strike")) is True:
        p.strike = True
    if toggle_attr(rpr_el.find("dstrike")) is True:
        p.dstrike = True
    color_el = rpr_el.find("color")
    if color_el is not None:
        c = resolve_word_color(dict(color_el.attrib), theme)
        if c:
            p.color = c
    hl = rpr_el.find("highlight")
    if hl is not None and hl.get("val") in HIGHLIGHT_COLORS:
        p.highlight = hl.get("val")
    shd = rpr_el.find("shd")
    if shd is not None:
        fill = resolve_word_color(_shd_attrs(shd), theme)
        if fill:
            p.shd_fill = fill
        p.shd_pattern = shd.get("val") or None
    sp = rpr_el.find("spacing")
    v = _num(sp, "val") if sp is not None else None
    if v is not None:
        p.letter_spacing_pt = twentieth_to_pt(v)
    pos = rpr_el.find("position")
    v = _num(pos, "val") if pos is not None else None
    if v is not None:
        from .units import half_point_to_px

        p.position_pt = v / 2  # 半磅 → pt
    va = rpr_el.find("vertAlign")
    if va is not None and va.get("val") in ("superscript", "subscript"):
        p.vert_align = va.get("val")
    if toggle_attr(rpr_el.find("caps")) is True:
        p.caps = True
    if toggle_attr(rpr_el.find("smallCaps")) is True:
        p.small_caps = True
    if toggle_attr(rpr_el.find("vanish")) is True:
        p.vanish = True
    lang = rpr_el.find("lang")
    if lang is not None:
        p.lang = lang.get("val") or lang.get("eastAsia") or None
    em = rpr_el.find("em")
    if em is not None and em.get("val") in ("emboss", "imprint", "outline", "shadow"):
        p.em = em.get("val")
    if toggle_attr(rpr_el.find("rtl")) is True:
        p.rtl = True
    return p


def parse_pbdr(pbdr_el, theme: dict) -> Optional[Borders]:
    """w:pBdr → Borders。"""
    if pbdr_el is None:
        return None
    borders = Borders()
    for side in ("top", "left", "bottom", "right", "between", "bar"):
        el = pbdr_el.find(side)
        if el is None:
            continue
        color = resolve_word_color(_border_attrs(el), theme) or "#000000"
        spec = BorderSpec(
            style=el.get("val") or "single",
            width_px=eighth_pt_to_px(float(el.get("sz") or 4)),
            space_pt=float(el.get("space") or 0),
            color=color,
        )
        setattr(borders, side, spec)
    return borders


def parse_ppr(ppr_el, theme: dict) -> Optional[ParaProps]:
    """w:pPr → ParaProps 部分值。"""
    if ppr_el is None:
        return None
    p = ParaProps()
    st = ppr_el.find("pStyle")
    if st is not None and st.get("val"):
        p.style_id = st.get("val")
    if toggle_attr(ppr_el.find("keepNext")) is True:
        p.keep_next = True
    if toggle_attr(ppr_el.find("keepLines")) is True:
        p.keep_lines = True
    if toggle_attr(ppr_el.find("pageBreakBefore")) is True:
        p.page_break_before = True
    wc = toggle_attr(ppr_el.find("widowControl"))
    if wc is not None:
        p.widow_control = wc
    sgg = toggle_attr(ppr_el.find("snapToGrid"))
    if sgg is not None:
        p.snap_to_grid = sgg
    numpr = ppr_el.find("numPr")
    if numpr is not None:
        ilvl = numpr.find("ilvl")
        numid = numpr.find("numId")
        p.tabs = p.tabs  # 保持结构
        # numPr 存到临时字段：ilvl/numId 由 parser.py 单独读取（这里只挂标记）
        p.outline_lvl = p.outline_lvl  # no-op，占位
        setattr(p, "_num_ilvl", _num(ilvl, "val") if ilvl is not None else None)
        setattr(p, "_num_id", _num(numid, "val") if numid is not None else None)
    p.borders = parse_pbdr(ppr_el.find("pBdr"), theme)
    shd = ppr_el.find("shd")
    if shd is not None:
        fill = resolve_word_color(_shd_attrs(shd), theme)
        if fill:
            p.shd_fill = fill
    tabs_el = ppr_el.find("tabs")
    if tabs_el is not None:
        tabs: List[TabStop] = []
        for tab in tabs_el.findall("tab"):
            pos = _num(tab, "pos")
            if pos is None:
                continue
            tabs.append(TabStop(pos_px=twip_to_px(pos), val=tab.get("val") or "left", leader=tab.get("leader")))
        p.tabs = tabs
    spacing = ppr_el.find("spacing")
    if spacing is not None:
        b = _num(spacing, "before")
        if b is not None:
            p.space_before_pt = b / 20
        a = _num(spacing, "after")
        if a is not None:
            p.space_after_pt = a / 20
        bl = _num(spacing, "beforeLines")
        if bl is not None:
            setattr(p, "_before_lines_em", bl / 100)
        al = _num(spacing, "afterLines")
        if al is not None:
            setattr(p, "_after_lines_em", al / 100)
        p.line_spacing = parse_line_spacing(dict(spacing.attrib))
    ind = ppr_el.find("ind")
    if ind is not None:
        # ⚠️ left/leftChars（同 right/rightChars）是二选一的两种写法：
        #    解析到哪种就显式清掉另一种，私有键置 None 也会被 merge_para
        #    无条件覆盖 —— 保证「样式 ← 段落直接格式」整组覆盖，否则样式的
        #    字符缩进会压过段落直接写的 twips 缩进（已实测该错配）。
        #    w:start/w:end 是 Strict 别名，仅在 left/right 缺席时兜底。
        left = _num(ind, "left")
        if left is None:
            left = _num(ind, "start")
        if left is not None:
            p.indent_left_px = twip_to_px(left)
            setattr(p, "_left_chars_em", None)
        right = _num(ind, "right")
        if right is None:
            right = _num(ind, "end")
        if right is not None:
            p.indent_right_px = twip_to_px(right)
            setattr(p, "_right_chars_em", None)
        fl = _num(ind, "firstLine")
        if fl is not None:
            p.first_line_px = twip_to_px(fl)
            p.first_line_em = None
            setattr(p, "_fl_form", "px")  # 形态标记：合并时整组覆盖（同 left 的处理）
        hg = _num(ind, "hanging")
        if hg is not None:
            p.hanging_px = twip_to_px(hg)
            p.hanging_em = None
            setattr(p, "_hg_form", "px")
        # ★ Chars 字符单位优先于 twips（中文首行缩进 2 字符）
        flc = _num(ind, "firstLineChars")
        if flc is not None:
            p.first_line_em = chars100_to_em(flc)
            p.first_line_px = None
            setattr(p, "_fl_form", "em")
        hgc = _num(ind, "hangingChars")
        if hgc is not None:
            p.hanging_em = chars100_to_em(hgc)
            p.hanging_px = None
            setattr(p, "_hg_form", "em")
        lc = _num(ind, "leftChars")
        if lc is None:
            lc = _num(ind, "startChars")
        if lc is not None:
            setattr(p, "_left_chars_em", lc / 100)
            p.indent_left_px = None
        rc = _num(ind, "rightChars")
        if rc is None:
            rc = _num(ind, "endChars")
        if rc is not None:
            setattr(p, "_right_chars_em", rc / 100)
            p.indent_right_px = None
    jc = ppr_el.find("jc")
    if jc is not None and jc.get("val"):
        p.align = jc.get("val")
    td = ppr_el.find("textDirection")
    if td is not None and td.get("val") in ("tbRl", "btLr"):
        p.vertical = True
    ol = ppr_el.find("outlineLvl")
    v = _num(ol, "val") if ol is not None else None
    if v is not None:
        p.outline_lvl = int(v)
    if toggle_attr(ppr_el.find("bidi")) is True:
        p.bidi = True
    if toggle_attr(ppr_el.find("contextualSpacing")) is True:
        p.contextual_spacing = True
    p.mark_run_props = parse_rpr(ppr_el.find("rPr"), theme)
    # 清掉全空的属性对象仍返回（携带 _num 私有键）
    return p


def merge_run(base: RunProps, over: Optional[RunProps]) -> RunProps:
    """字符属性合并：**只有 None 才继承**（显式 False 会覆盖 True）。"""
    if over is None:
        return base.copy()
    out = base.copy()
    for k in RUN_FIELDS:
        v = getattr(over, k)
        if v is not None:
            setattr(out, k, v)
    return out


def merge_borders(base: Optional[Borders], over: Optional[Borders]) -> Optional[Borders]:
    if over is None:
        return base
    out = Borders()
    src = base or Borders()
    for side in ("top", "left", "bottom", "right", "between", "bar"):
        setattr(out, side, getattr(over, side) or getattr(src, side))
    return out


def merge_para(base: ParaProps, over: Optional[ParaProps]) -> ParaProps:
    """段落属性合并：None 继承；嵌套对象（borders/mark_run_props/tabs）按字段合并。"""
    if over is None:
        return base.copy()
    out = base.copy()
    for k in PARA_FIELDS:
        v = getattr(over, k)
        if v is None:
            continue
        if k == "borders":
            out.borders = merge_borders(base.borders, v)
        elif k == "mark_run_props":
            out.mark_run_props = merge_run(base.mark_run_props or RunProps(), v)
        else:
            setattr(out, k, v)
    # 私有键（_num_ilvl 等）直接透传
    for k, v in over.__dict__.items():
        if k.startswith("_"):
            setattr(out, k, v)
    return out


# ─────────────────────────────────────────────────────────────
# 样式表与继承链
# ─────────────────────────────────────────────────────────────

_HEADING_NAME_RE = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)


def heading_level_of(props: ParaProps, style_name: Optional[str]) -> Optional[int]:
    """标题层级推导：outlineLvl > 内置样式名 HeadingN/Title/Subtitle（6.4.3）。"""
    if props.outline_lvl is not None:
        lvl = props.outline_lvl + 1
        if 1 <= lvl <= 9:
            return min(lvl, 6)
        return None
    m = _HEADING_NAME_RE.match(style_name or "")
    if m:
        return min(int(m.group(1)), 6)
    name = (style_name or "").strip().lower()
    if name == "title":
        return 1
    if name == "subtitle":
        return 2
    return None


@dataclass
class StyleSet:
    """styles.xml 的解析产物 + 继承解析（带记忆化与防环）。"""

    styles: Dict[str, StyleDef] = field(default_factory=dict)
    defaults_run: RunProps = field(default_factory=RunProps)
    defaults_para: ParaProps = field(default_factory=ParaProps)
    warnings: List[str] = field(default_factory=list)
    _resolved_para: Dict[str, ParaProps] = field(default_factory=dict)
    _resolved_run: Dict[str, RunProps] = field(default_factory=dict)

    def default_paragraph_style(self) -> Optional[StyleDef]:
        for st in self.styles.values():
            if st.type == "paragraph" and st.is_default:
                return st
        return None

    def style_name_of(self, style_id: Optional[str]) -> Optional[str]:
        st = self.styles.get(style_id or "")
        return st.name if st else None

    def chain(self, style_id: str) -> List[StyleDef]:
        """沿 basedOn 上溯到根，返回「根在前」的链；⚠️ 必须防环。"""
        chain: List[StyleDef] = []
        seen = set()
        cur = self.styles.get(style_id)
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            chain.insert(0, cur)
            cur = self.styles.get(cur.based_on) if cur.based_on else None
        if cur is not None:
            self.warnings.append(f"样式 basedOn 链成环：{style_id}")
        return chain

    def resolve_para(self, style_id: Optional[str], direct: Optional[ParaProps]) -> ParaProps:
        """段落最终属性 = docDefaults + 样式链 + 直接格式。"""
        sid = style_id or (self.default_paragraph_style().id if self.default_paragraph_style() else "")
        base = self._resolved_para.get(sid)
        if base is None:
            base = self.defaults_para.copy()
            for st in self.chain(sid):
                if st.para:
                    base = merge_para(base, st.para)
            self._resolved_para[sid] = base
        return merge_para(base, direct)

    def resolve_run(self, style_id: Optional[str], para_mark: Optional[RunProps],
                    direct: Optional[RunProps]) -> RunProps:
        """字符最终属性 = docDefaults + 段落标记 rPr + 样式链 + 直接格式（6.4.1 顺序）。"""
        sid = style_id or (self.default_paragraph_style().id if self.default_paragraph_style() else "")
        base = self._resolved_run.get(sid)
        if base is None:
            base = self.defaults_run.copy()
            for st in self.chain(sid):
                if st.run:
                    base = merge_run(base, st.run)
            self._resolved_run[sid] = base
        out = merge_run(base, para_mark)
        return merge_run(out, direct)
