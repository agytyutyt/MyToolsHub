"""条件格式（Conditional Formatting）—— 对应 TS 版 ``packages/style/src/cf.ts``。

职责：
  1. 把 dxf 差异样式规范成 StylePatch（字段粒度的部分覆盖）
  2. 按 rules 逐格求值，命中的把 patch 叠加到单元格样式上
  3. 支持 stopIfTrue；不支持的规则类型跳过并记 warning（绝不抛错）

⚠️ 三个必须守住的语义（TS 版实测得来，Python 版同样适用）：
  · 优先级方向：**priority 数字小的先应用**，后应用的覆盖先应用的。
  · 相对引用平移：公式以规则范围左上角为原点存，逐格求值必须平移（expr.shift_formula）。
  · dxf 的 fill 语义：Excel 在 dxf 里把纯色填充色存在 **bgColor**，
    而 cellXfs 里存在 fgColor。沿用 resolve_fill 会导致条件格式底色全丢失。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..core import CfRuleModel, Rect, SheetModel, StyleModel, a1_to_rc
from .color import resolve_color
from .expr import CellGetter, eval_bool, shift_formula
from .style_dedup import StyleTable
from .style_resolver import StyleContext, to_raw_color

# ─────────────────────────────────────────────────────────────
# dxf → StylePatch
# ─────────────────────────────────────────────────────────────


def _has_values(o: object) -> bool:
    """判断一个 dxf 子项是否"真的给了值"。

    解析层会把没出现的项写成 None（或全空对象），这些必须视为"不覆盖"，
    否则条件格式会把单元格的字号、对齐等属性抹成默认值。
    """
    if not isinstance(o, dict):
        return False
    return any(v is not None and v is not False for v in o.values())


def _dxf_font(raw: object, ctx: StyleContext) -> Optional[dict]:
    """dxf 的 font → Partial 字典（只取显式给出的字段）。"""
    if not _has_values(raw):
        return None
    f = raw  # type: ignore[assignment]
    out: dict = {}
    if isinstance(f.get("size"), (int, float)) and f["size"] > 0:
        out["size_pt"] = f["size"]
    if f.get("bold") is True:
        out["bold"] = True
    if f.get("italic") is True:
        out["italic"] = True
    if f.get("strike") is True:
        out["strike"] = True
    u = f.get("underline")
    if u is True or (isinstance(u, str) and u != "none" and u):
        out["underline"] = 1
    color = resolve_color(to_raw_color(f.get("color")), ctx.color_ctx, "")
    if color:
        out["color"] = color
    return out or None


def _dxf_fill(raw: object, ctx: StyleContext) -> Optional[dict]:
    """dxf 的 fill。

    ⚠️ 关键差异：dxf 里纯色填充的颜色存在 bgColor，普通 fill 存在 fgColor。
       实测：条件格式"红底"规则的 dxf 是
         {'type':'pattern','pattern':'solid','bgColor':{'rgb':'FFFFC7CE'}}
    """
    if not _has_values(raw):
        return None
    f = raw  # type: ignore[assignment]
    ftype = str(f.get("type") or "")
    if ftype and ftype not in ("pattern", "solid"):
        return None

    pattern_raw = str(f.get("pattern") or "solid")
    if pattern_raw == "none":
        return {"type": "none"}

    bg = resolve_color(to_raw_color(f.get("bgColor")), ctx.color_ctx, "")
    fg = resolve_color(to_raw_color(f.get("fgColor")), ctx.color_ctx, "")

    if pattern_raw == "solid":
        color = bg or fg  # dxf 语义：bgColor 优先，fgColor 兜底
        if not color:
            return None
        return {"type": "solid", "fg_color": color}

    color = fg or bg
    if not color:
        return None
    return {"type": "pattern", "pattern": pattern_raw, "fg_color": color, "bg_color": bg or "#FFFFFF"}


def _dxf_border(raw: object, ctx: StyleContext) -> Optional[dict]:
    """dxf 的 border → 只覆盖给出的边。"""
    if not _has_values(raw):
        return None
    b = raw  # type: ignore[assignment]
    out: dict = {}
    for side in ("top", "bottom", "left", "right", "diagonal_up", "diagonal_down"):
        s = b.get(side)
        if not _has_values(s):
            continue
        out[side] = {
            "style": str(s.get("style") or "thin"),
            "color": resolve_color(to_raw_color(s.get("color")), ctx.color_ctx, "#000000"),
        }
    return out or None


def _dxf_align(raw: object) -> Optional[dict]:
    if not _has_values(raw):
        return None
    a = raw  # type: ignore[assignment]
    out: dict = {}
    if isinstance(a.get("horizontal"), str):
        out["h"] = a["horizontal"]
    if isinstance(a.get("vertical"), str):
        out["v"] = "center" if a["vertical"] == "middle" else a["vertical"]
    return out or None


def _dxf_numfmt(raw: object, ctx: StyleContext) -> Optional[dict]:
    if not _has_values(raw):
        return None
    n = raw  # type: ignore[assignment]
    fmt_id = n.get("numFmtId")
    if isinstance(fmt_id, int):
        code = ctx.num_fmt_map.get(fmt_id)
        if code:
            return {"id": fmt_id, "code": code}
    return None


def resolve_dxf_patch(raw: object, ctx: StyleContext) -> Optional[dict]:
    """dxf 原始对象 → StylePatch 字典。无有效字段时返回 None（调用方跳过该规则）。"""
    if not isinstance(raw, dict):
        return None
    patch: dict = {}
    for key, fn in (
        ("font", _dxf_font),
        ("fill", _dxf_fill),
        ("border", _dxf_border),
        ("alignment", _dxf_align),
        ("numFmt", _dxf_numfmt),
    ):
        try:
            val = fn(raw.get(key), ctx) if key != "alignment" else fn(raw.get(key))
        except Exception:  # noqa: BLE001 —— dxf 局部异常不拖垮整条规则
            val = None
        if val:
            patch[key] = val
    return patch or None


# ─────────────────────────────────────────────────────────────
# 样式叠加
# ─────────────────────────────────────────────────────────────


def apply_style_patch(base: StyleModel, patch: dict) -> StyleModel:
    """把样式补丁叠加到基础样式上（字段粒度覆盖，dxf 的核心语义）。"""
    from dataclasses import replace

    from ..core import BorderSide, NumFmtStyle

    out = replace(base)
    if patch.get("font"):
        font = replace(out.font)
        for k, v in patch["font"].items():
            setattr(font, k, v)
        out.font = font
    if patch.get("fill"):
        out.fill = replace(out.fill, **patch["fill"])
    if patch.get("border"):
        border = replace(out.border)
        for k, v in patch["border"].items():
            setattr(border, k, BorderSide(style=v["style"], color=v["color"]))
        out.border = border
    if patch.get("alignment"):
        out.alignment = replace(out.alignment, **patch["alignment"])
    if patch.get("numFmt"):
        out.num_fmt = NumFmtStyle(id=patch["numFmt"]["id"], code=patch["numFmt"]["code"])
    out.protection = base.protection
    out.quote_prefix = base.quote_prefix
    out.is_default = False
    return out


# ─────────────────────────────────────────────────────────────
# 规则求值
# ─────────────────────────────────────────────────────────────

#: 本版本不实现（色阶/数据条/图标集需要图形层）
UNSUPPORTED_TYPES = {
    "colorScale", "dataBar", "iconSet", "top10", "aboveAverage",
    "timePeriod", "duplicateValues", "uniqueValues",
}


def _compare(a: object, b: object) -> int:
    """比较两个值（数字按数值比，其它按字符串比）。"""
    na = a if isinstance(a, (int, float)) and not isinstance(a, bool) else None
    nb = b if isinstance(b, (int, float)) and not isinstance(b, bool) else None
    if na is not None and nb is not None:
        return 0 if na == nb else (-1 if na < nb else 1)
    if na is not None and isinstance(b, str):
        try:
            nb2 = float(b)
            return 0 if na == nb2 else (-1 if na < nb2 else 1)
        except ValueError:
            pass
    if nb is not None and isinstance(a, str):
        try:
            na2 = float(a)
            return 0 if na2 == nb else (-1 if na2 < nb else 1)
        except ValueError:
            pass
    sa = "" if a is None else str(a)
    sb = "" if b is None else str(b)
    return 0 if sa == sb else (-1 if sa < sb else 1)


_QUOTED_RE = re.compile(r'"(.*)"')


def _needle_of(formula: str) -> str:
    """取公式里的文本字面量（containsText 系规则：formula 形如 NOT(ISERROR(SEARCH("警",B2)))）。"""
    s = (formula or "").strip()
    m = _QUOTED_RE.search(s)
    if m:
        return m.group(1)
    return s.strip('"')


@dataclass
class _CellLike:
    v: object
    w: str


def match_rule(
    rule: CfRuleModel,
    cell: _CellLike,
    get_cell: CellGetter,
    d_row: int,
    d_col: int,
) -> Optional[bool]:
    """单条规则在某个单元格上是否命中。

    @returns True 命中 / False 未命中 / None 无法判定（调用方视为未命中）
    """
    formulas = rule.formulas or []

    if rule.type == "cellIs":
        op = rule.operator
        if not op or not formulas:
            return None
        a = cell.v
        cmp_b = _compare(a, formulas[0])
        if op == "greaterThan":
            return cmp_b > 0
        if op == "greaterThanOrEqual":
            return cmp_b >= 0
        if op == "lessThan":
            return cmp_b < 0
        if op == "lessThanOrEqual":
            return cmp_b <= 0
        if op == "equal":
            return cmp_b == 0
        if op == "notEqual":
            return cmp_b != 0
        if op == "between":
            return len(formulas) > 1 and cmp_b >= 0 and _compare(a, formulas[1]) <= 0
        if op == "notBetween":
            return len(formulas) > 1 and (cmp_b < 0 or _compare(a, formulas[1]) > 0)
        return None

    if rule.type == "expression":
        if not formulas:
            return None
        # ⚠️ 必须平移：公式以规则范围左上角为原点
        shifted = shift_formula(formulas[0], d_row, d_col)
        return eval_bool(shifted, get_cell)

    if rule.type in ("containsText", "notContainsText", "beginsWith", "endsWith"):
        if not formulas:
            return None
        needle = _needle_of(formulas[0])
        if not needle:
            return None
        hay = cell.w or ""
        if rule.type == "containsText":
            return needle in hay
        if rule.type == "notContainsText":
            return needle not in hay
        if rule.type == "beginsWith":
            return hay.startswith(needle)
        return hay.endswith(needle)

    return None


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────


@dataclass
class ApplyCfResult:
    #: 被条件格式改过样式的单元格数（按单元格去重）
    affected: int = 0
    #: 跳过的规则数（不支持的类型）
    skipped_rules: int = 0


#: patch 字段 → 子键粒度（冲突判定单位）
def _merge_patch_fields(merged: dict, claimed: set, patch: dict) -> None:
    """把一条规则的 patch 并入累计 patch —— **高优先级先占位**。

    ⚠️ 冲突语义（Python 版修正，附实证）：
      TS 版按「priority 小的先应用，后应用的覆盖先应用的」实现 —— 即低优先级
      规则覆盖高优先级。经 LibreOffice 26.8 实测（pri1=红底 >5、pri2=绿底
      6~10，值 8 两者皆命中 → Excel 语义显示**红底**），真实语义是：
      **priority 数字小的先应用且获胜**；后应用的规则只补未冲突的属性。
      这与 ECMA-376 18.3.1.49「lower numeric values are higher priority」
      及 Excel「Manage Rules 列表顶部规则优先」一致。
    """
    font = patch.get("font")
    if isinstance(font, dict):
        slot = merged.setdefault("font", {})
        for k, v in font.items():
            key = f"font:{k}"
            if key not in claimed:
                claimed.add(key)
                slot[k] = v
    if "fill" in patch and "fill" not in claimed:
        claimed.add("fill")
        merged["fill"] = patch["fill"]
    border = patch.get("border")
    if isinstance(border, dict):
        slot = merged.setdefault("border", {})
        for side, v in border.items():
            key = f"border:{side}"
            if key not in claimed:
                claimed.add(key)
                slot[side] = v
    alignment = patch.get("alignment")
    if isinstance(alignment, dict):
        slot = merged.setdefault("alignment", {})
        for k, v in alignment.items():
            key = f"alignment:{k}"
            if key not in claimed:
                claimed.add(key)
                slot[k] = v
    if "numFmt" in patch and "numFmt" not in claimed:
        claimed.add("numFmt")
        merged["numFmt"] = patch["numFmt"]


def apply_conditional_formatting(
    sheet: SheetModel,
    style_table: StyleTable,
    ctx: StyleContext,
    on_warn: Optional[Callable[[str], None]] = None,
) -> ApplyCfResult:
    """把条件格式应用到工作表上的单元格（**就地修改** cell.styleId）。"""
    rules = sheet.conditional_formattings or []
    if not rules:
        return ApplyCfResult()

    # 值索引：'r,c' → 单元格
    by_rc: Dict[Tuple[int, int], _CellLike] = {}
    row_map: Dict[int, object] = {}
    for row in sheet.rows:
        row_map[row.index] = row
        for c, cell in row.cells.items():
            by_rc[(row.index, c)] = _CellLike(v=cell.v, w=cell.w)

    def get_cell(a1: str) -> object:
        try:
            rc = a1_to_rc(a1)
            item = by_rc.get((rc.row, rc.col))
            return item.v if item else None
        except Exception:
            return None

    # ⚠️ priority 小的先应用且获胜（冲突时先占位，见 _merge_patch_fields）
    sorted_rules = sorted(rules, key=lambda r: r.priority or 0)

    skipped = 0
    touched: Set[Tuple[int, int]] = set()
    locked: Set[Tuple[int, int]] = set()  # stopIfTrue 命中后的锁定标记
    merged_patches: Dict[Tuple[int, int], dict] = {}
    claimed: Dict[Tuple[int, int], set] = {}

    for rule in sorted_rules:
        if str(rule.type) in UNSUPPORTED_TYPES:
            skipped += 1
            if on_warn:
                on_warn(f"条件格式规则类型「{rule.type}」暂不支持，已跳过")
            continue
        if not rule.patch:
            skipped += 1
            continue

        for rng in rule.ranges or []:
            for r in range(rng.r0, rng.r1 + 1):
                row = row_map.get(r)
                if row is None:
                    continue
                for c in range(rng.c0, rng.c1 + 1):
                    if (r, c) in locked:
                        continue
                    cell = row.cells.get(c)  # type: ignore[union-attr]
                    if cell is None:
                        continue
                    if cell.merge == "covered":
                        continue

                    d_row = r - rng.r0
                    d_col = c - rng.c0
                    hit = match_rule(rule, _CellLike(v=cell.v, w=cell.w), get_cell, d_row, d_col)
                    if hit is not True:
                        continue

                    _merge_patch_fields(
                        merged_patches.setdefault((r, c), {}),
                        claimed.setdefault((r, c), set()),
                        rule.patch,
                    )
                    touched.add((r, c))
                    if rule.stop_if_true:
                        locked.add((r, c))

    # 叠加到基础样式并生成新样式项
    for (r, c), patch in merged_patches.items():
        row = row_map.get(r)
        if row is None:
            continue
        cell = row.cells.get(c)  # type: ignore[union-attr]
        if cell is None:
            continue
        base = style_table.at(cell.style_id) or style_table.at(0)
        if base is None:
            continue
        cell.style_id = style_table.intern(apply_style_patch(base, patch))

    return ApplyCfResult(affected=len(touched), skipped_rules=skipped)
