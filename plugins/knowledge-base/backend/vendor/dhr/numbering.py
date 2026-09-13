"""编号与项目符号 —— 对应《设计文档》6.6（T-801/T-802）。

⚠️ 三个最容易写错的点：
  1. ``lvlText`` 的 ``%1`` 是**该级编号**、``%2`` 是**上一级**（不是"第 2 级"），
     ``%n`` 对应 ``counters[n-1]``；
  2. 计数推进：遇到 ilvl=n 时 counters[n]++，**并把 >n 的层级重置为 start** ——
     只 ++ 不重置会让 1.1 → 2.1 变成 2.2；
  3. 中文数字进位：十/十一/二十/一百零一/一千零一，且 legal 样式保留「壹拾」。
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

from .model import ListMarker, NumberingDef, NumberingLevel, RunProps

#: Wingdings 回退映射（目标机器无该字体时使用，TODO 清单 T-802）
WINGDINGS_FALLBACK = {
    "\uF0B7": "\u2022",  # •
    "\uF0A7": "\u25A1",  # □
    "\uF0A8": "\u25A0",  # ■
    "\uF0FC": "\u2713",  # ✓
    "\uF06F": "\u25CF",  # ●
    "\uF0D8": "\u2794",  # ➔
    "\uF0E0": "\u27A4",  # ➤
    "\uF076": "\u2751",  # ❑
}

SYMBOL_FONTS = ("wingdings", "symbol", "webdings", "dings", "opensymbol")

_CN_DIGITS = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
_CN_DIGITAL = ["〇", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
_CN_LEGAL = ["零", "壹", "贰", "叁", "肆", "伍", "陆", "柒", "捌", "玖"]
_CN_UNITS = ["", "十", "百", "千"]
_CN_LEGAL_UNITS = ["", "拾", "佰", "仟"]


def _cn_convert(n: int, digits: List[str], keep_leading_one: bool) -> str:
    units = _CN_LEGAL_UNITS if keep_leading_one else _CN_UNITS
    """通用中文数字转换（支持到 9999）。

    @param keep_leading_one True=「一百一十」（legal 公文）；False=「一百一十」
            的口语形式中两位数开头省「一」→ 十一（普通编号）。
    """
    if n == 0:
        return digits[0]
    out = ""
    need_zero = False
    for pos in (3, 2, 1, 0):
        unit = 10 ** pos
        d = (n // unit) % 10
        if d == 0:
            if out:
                need_zero = True
            continue
        if need_zero:
            out += digits[0]
            need_zero = False
        # 两位数（无百位以上）的一十 → 十（口语）；legal 保留壹拾
        if d == 1 and pos == 1 and not out and not keep_leading_one:
            out += units[pos]
        else:
            out += digits[d] + units[pos]
    return out


def cn_number(n: int, digital: bool = False) -> str:
    """一、二、…十、十一、二十、一百零一（digital=True 用 〇一二三）。"""
    if n < 0:
        return "-" + cn_number(-n, digital)
    if n >= 10000:
        return str(n)  # 超出范围降级为阿拉伯数字
    return _cn_convert(n, _CN_DIGITAL if digital else _CN_DIGITS, keep_leading_one=False)


def cn_legal(n: int) -> str:
    """大写中文：壹拾壹（legal 样式保留「壹拾」）。"""
    if n < 0:
        return "-" + cn_legal(-n)
    if n >= 10000:
        return str(n)
    return _cn_convert(n, _CN_LEGAL, keep_leading_one=True)


def to_roman(n: int) -> str:
    values = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
              (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for v, sym in values:
        while n >= v:
            out.append(sym)
            n -= v
    return "".join(out)


def _enclosed_circle(n: int) -> str:
    if 1 <= n <= 20:
        return chr(0x2460 + n - 1)
    return f"({n})"


def format_number(n: int, fmt: str) -> str:
    """常见 numFmt → 显示（务必覆盖中文场景，6.6.2）。"""
    if n <= 0 and fmt not in ("bullet", "none"):
        return str(n)
    if fmt == "bullet":
        return "\u2022"
    if fmt == "decimal":
        return str(n)
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt == "lowerLetter":
        return chr(96 + ((n - 1) % 26 + 1))
    if fmt == "upperLetter":
        return chr(64 + ((n - 1) % 26 + 1))
    if fmt == "lowerRoman":
        return to_roman(n).lower()
    if fmt == "upperRoman":
        return to_roman(n)
    if fmt in ("chineseCounting", "chineseCountingThousand", "japaneseCounting"):
        return cn_number(n)
    if fmt == "ideographDigital":
        return cn_number(n, digital=True)
    if fmt == "ideographLegalTraditional":
        return cn_legal(n)
    if fmt == "decimalEnclosedCircle":
        return _enclosed_circle(n)
    if fmt == "none":
        return ""
    return str(n)


def expand_lvl_text(
    lvl_text: str,
    counters: List[int],
    num_fmt_of: Callable[[int], str],
    is_lgl: bool,
) -> str:
    """把 "%1.%2." 模板展开成实际文本。⚠️ %n 对应 counters[n-1]；isLgl 全部十进制。"""

    def repl(m: "re.Match[str]") -> str:
        i = int(m.group(1)) - 1
        fmt = "decimal" if is_lgl else (num_fmt_of(i) or "decimal")
        return format_number(counters[i] if 0 <= i < len(counters) else 1, fmt)

    return re.sub(r"%(\d)", repl, lvl_text)


# ─────────────────────────────────────────────────────────────
# numbering.xml 解析（T-801）
# ─────────────────────────────────────────────────────────────


def _ind_px(ind, name: str) -> float:
    from .units import twip_to_px

    v = ind.get(name)
    if v in (None, ""):
        return 0.0
    try:
        return float(twip_to_px(float(v)))
    except ValueError:
        return 0.0


def parse_numbering(numbering_el, rels: Optional[dict] = None) -> List[NumberingDef]:
    """解析 w:numbering → NumberingDef 列表。

    @param numbering_el 去命名空间后的 <numbering> 元素（可为 None）
    @param rels numbering.xml.rels 的 rId → 包内路径（图片项目符号用）
    """
    from xhr.parser_xlsx.xmlutil import child, children

    rels = rels or {}
    if numbering_el is None:
        return []

    # 图片项目符号：w:numPicBullet (numPicBulletId → rId → media 名)
    pic_bullets: Dict[int, str] = {}
    for npb in children(numbering_el, "numPicBullet"):
        try:
            bid = int(float(npb.get("numPicBulletId") or -1))
        except ValueError:
            continue
        pid = child(npb, "pict")
        blip = None
        if pid is not None:
            for e in pid.iter():
                if e.tag in ("blip", "imagedata") and (e.get("embed") or e.get("id")):
                    blip = e.get("embed") or e.get("id")
                    break
        target = rels.get(blip) if blip else None
        if target:
            pic_bullets[bid] = target.rsplit("/", 1)[-1]

    abstracts: Dict[int, Dict[int, NumberingLevel]] = {}
    for an in children(numbering_el, "abstractNum"):
        try:
            aid = int(float(an.get("abstractNumId") or -1))
        except ValueError:
            continue
        levels: Dict[int, NumberingLevel] = {}
        for lvl_el in children(an, "lvl"):
            try:
                ilvl = int(float(lvl_el.get("ilvl") or 0))
            except ValueError:
                continue
            lv = NumberingLevel()
            st_el = child(lvl_el, "start")
            if st_el is not None and st_el.get("val"):
                try:
                    lv.start = int(float(st_el.get("val")))
                except ValueError:
                    pass
            fmt_el = child(lvl_el, "numFmt")
            if fmt_el is not None:
                lv.num_fmt = fmt_el.get("val") or "decimal"
            text_el = child(lvl_el, "lvlText")
            if text_el is not None:
                lv.lvl_text = text_el.get("val") or ""
            jc_el = child(lvl_el, "lvlJc")
            if jc_el is not None:
                lv.jc = jc_el.get("val") or "left"
            if child(lvl_el, "isLgl") is not None:
                lv.is_lgl = True
            ppr = child(lvl_el, "pPr")
            ind = child(ppr, "ind") if ppr is not None else None
            if ind is not None:
                lv.indent_px = _ind_px(ind, "left")
                lv.hanging_px = _ind_px(ind, "hanging")
            rpr = child(lvl_el, "rPr")
            if rpr is not None:
                from .styles import parse_rpr

                lv.props = parse_rpr(rpr, {"colorScheme": {}})
            pic_el = child(lvl_el, "lvlPicBulletId")
            if pic_el is not None and pic_el.get("val"):
                try:
                    lv.pic_bullet_media_id = pic_bullets.get(int(float(pic_el.get("val"))))
                except ValueError:
                    pass
            levels[ilvl] = lv
        abstracts[aid] = levels

    out: List[NumberingDef] = []
    for num_el in children(numbering_el, "num"):
        try:
            num_id = int(float(num_el.get("numId") or -1))
        except ValueError:
            continue
        ref = child(num_el, "abstractNumId")
        try:
            aid = int(float(ref.get("val") or -1)) if ref is not None else -1
        except ValueError:
            aid = -1
        nd = NumberingDef(num_id=num_id, abstract_num_id=aid, levels=dict(abstracts.get(aid, {})))
        for ov in children(num_el, "lvlOverride"):
            try:
                oilvl = int(float(ov.get("ilvl") or 0))
            except ValueError:
                continue
            rec: dict = {}
            so = child(ov, "startOverride")
            if so is not None and so.get("val"):
                try:
                    rec["startOverride"] = int(float(so.get("val")))
                except ValueError:
                    pass
            lvl_el = child(ov, "lvl")
            if lvl_el is not None:
                lv = NumberingLevel()
                st_el = child(lvl_el, "start")
                if st_el is not None and st_el.get("val"):
                    try:
                        lv.start = int(float(st_el.get("val")))
                    except ValueError:
                        pass
                f_el = child(lvl_el, "numFmt")
                if f_el is not None:
                    lv.num_fmt = f_el.get("val") or "decimal"
                t_el = child(lvl_el, "lvlText")
                if t_el is not None:
                    lv.lvl_text = t_el.get("val") or ""
                rec["level"] = {"num_fmt": lv.num_fmt, "lvl_text": lv.lvl_text, "start": lv.start}
            nd.overrides[oilvl] = rec
        out.append(nd)
    return out


# ─────────────────────────────────────────────────────────────
# 计数推进（6.6.3）
# ─────────────────────────────────────────────────────────────


def _is_symbol_font(props: Optional[RunProps]) -> bool:
    if not props:
        return False
    for f in (props.font_latin, props.font_east_asia):
        if f and any(s in f.lower() for s in SYMBOL_FONTS):
            return True
    return False


def _level_start(nd: NumberingDef, ilvl: int) -> int:
    ov = nd.overrides.get(ilvl) or {}
    if "startOverride" in ov:
        return ov["startOverride"]
    lv = nd.levels.get(ilvl)
    return lv.start if lv else 1


def assign_list_markers(
    paragraphs: list,
    numbering: List[NumberingDef],
    warnings: Optional[List[str]] = None,
) -> None:
    """按文档顺序为带 numPr 的段落就地设置 list_marker（计数推进 6.6.3）。

    @param paragraphs 段落对象列表；段落 props 上带 ``_num_id`` / ``_num_ilvl``
           私有键（由 parser 的 pPr 解析写入）。
    """
    by_id: Dict[int, NumberingDef] = {nd.num_id: nd for nd in numbering}
    counters: Dict[int, List[Optional[int]]] = {}  # numId → 各级当前计数（None=未用）

    for para in paragraphs:
        num_id = getattr(para.props, "_num_id", None)
        ilvl = getattr(para.props, "_num_ilvl", None)
        if num_id is None:
            continue
        nd = by_id.get(int(num_id))
        if nd is None:
            if warnings is not None:
                warnings.append(f"编号 numId={num_id} 未在 numbering.xml 中定义，该列表降级为普通段落")
            continue
        lvl_idx = max(0, min(int(ilvl or 0), 8))
        seq = counters.setdefault(num_id, [None] * 9)
        # ⚠️ 深层重置（回到 start）
        for deeper in range(lvl_idx + 1, 9):
            seq[deeper] = None
        cur = seq[lvl_idx]
        start_val = _level_start(nd, lvl_idx) - 1
        if cur is None or cur < start_val:
            cur = start_val
        seq[lvl_idx] = cur + 1

        lv = nd.levels.get(lvl_idx) or NumberingLevel()
        override = nd.overrides.get(lvl_idx) or {}
        ov_level = override.get("level") or {}

        # 展开用计数：未用层级取其 start
        disp: List[int] = []
        for i in range(9):
            if seq[i] is None:
                disp.append(_level_start(nd, i))
            else:
                disp.append(seq[i])

        def fmt_of(i: int) -> str:
            l2 = nd.levels.get(i)
            return l2.num_fmt if l2 else "decimal"

        num_fmt = ov_level.get("num_fmt") or lv.num_fmt
        lvl_text = ov_level.get("lvl_text") if ov_level.get("lvl_text") is not None else lv.lvl_text
        text = expand_lvl_text(lvl_text or "", disp, fmt_of, lv.is_lgl)

        marker = ListMarker(
            text=text,
            num_fmt="decimal" if lv.is_lgl else num_fmt,
            ilvl=lvl_idx,
            props=lv.props.copy() if lv.props else None,
            image_media_id=lv.pic_bullet_media_id,
            indent_px=lv.indent_px,
            jc=lv.jc,
        )
        # ⚠️ 符号字体项目符号：保留原字体；渲染端无字体时按映射表回退
        if marker.text and _is_symbol_font(marker.props):
            marker.text = WINGDINGS_FALLBACK.get(marker.text[0], marker.text)
        para.list_marker = marker
