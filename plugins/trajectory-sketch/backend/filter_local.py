# -*- coding: utf-8 -*-
"""本插件自带的表格过滤实现（硬过滤 + 文本后处理）。

为什么自带一份（而不是调用「过滤器」插件）
----------------------------------------
插件之间必须相互独立（《插件设计规范》B-7 / 设计文档《主体与插件解耦》§5.4 D-9）：
过去本插件通过 HTTP 调 ``POST /api/file-filter/apply`` 借用过滤器插件的过滤能力，
还读 ``config/tools.json`` 判断对方是否启用——那是"未声明、无版本校验、不进依赖判定"
的隐式耦合，且对方下线时本插件的字段自检与轨迹分析会直接不可用。

现在把过滤算法与后处理规则**在本插件内自带一份**：两处为**同源副本、各自独立演进**
（不承诺行为一致，见设计文档 §5.4「为什么副本优于引用」；仓库已有先例——
``bg_image.py`` 在 file-filter 与 trajectory-sketch 下各有一份完全相同的副本）。

与「过滤器」插件的差异（有意为之）
--------------------------------
- **大模型辅助匹配（mode=llm）不再提供**：它依赖过滤器插件自己的大模型配置
  （base_url / api_key / model），那正是本次要切断的跨插件配置依赖。本插件没有自己的
  大模型配置节，因此 ``mode="llm"`` 一律**回退为硬过滤**并在响应里给出 ``warning``。
- 其余行为（保留列匹配、全空行跳过、表头规整、正则/文本替换、计数口径）与
  ``file-filter/backend/core.py`` 的同名实现逐条一致（由单测钉住）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_ROWS = 100000

# 模块级提示：llm 模式不可用时的说明（前端与 API 都会看到）
LLM_REMOVED_NOTE = ("大模型辅助匹配已随插件解耦移除（它依赖「过滤器」插件的大模型配置）。"
                    "已按硬过滤处理：如需调整保留字段，请在本插件配置里修改 keep_columns。")


class FilterError(Exception):
    """过滤错误。``message`` 为可直接展示的中文描述（SEC-5）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ===================== 单元格规整（与 file-filter/backend/core.py 同源） =====================

def cell_to_str(v):
    """单元格转字符串（表头/文本比较用）；None → 空串。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def clean_text(v):
    """比较用文本规整：去 BOM、全角空格转半角、去首尾空白。"""
    s = cell_to_str(v)
    s = s.replace("\uFEFF", "").replace("\u3000", " ")
    return s.strip()


# ===================== 列过滤（硬过滤） =====================

def filter_columns(headers, rows, keep):
    """只保留与 keep 名单匹配的列（规整后精确匹配），返回 (headers2, rows2, kept, removed)。"""
    keep_clean = [(clean_text(k), clean_text(k).casefold()) for k in (keep or []) if clean_text(k)]
    fold = {kf: k for k, kf in keep_clean}
    idx, kept, removed = [], [], []
    for i, h in enumerate(headers):
        hf = clean_text(h).casefold()
        if hf and hf in fold:
            idx.append(i)
            kept.append((h, fold[hf]))
        else:
            removed.append(h)
    rows2 = [[(r[i] if i < len(r) else None) for i in idx] for r in rows]
    return [headers[i] for i in idx], rows2, kept, removed


# ===================== 文本后处理（脱敏 / 正则替换） =====================

def compile_rules(rules):
    """把规则列表编译为 [(regex_or_None, pattern, replacement)]；无效规则跳过。"""
    out = []
    for rule in (rules or []):
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        replacement = rule.get("replacement")
        replacement = "" if replacement is None else str(replacement)
        if rule.get("is_regex"):
            try:
                out.append((re.compile(pattern), pattern, replacement))
            except re.error:
                continue                      # SEC-6：非法正则包裹处理，跳过而非中断
        else:
            out.append((None, pattern, replacement))
    return out


def _apply_text(s, compiled):
    """对一段文本依次应用全部规则，返回 (新文本, 替换次数)。"""
    count = 0
    for regex, pattern, replacement in compiled:
        if regex is not None:
            s, n = regex.subn(replacement, s)
            count += n
        elif pattern in s:
            count += s.count(pattern)
            s = s.replace(pattern, replacement)
    return s, count


def post_process(headers, rows, rules):
    """对表头与所有字符串单元格应用文本/正则替换（脱敏）。返回 (headers2, rows2, count)。"""
    compiled = compile_rules(rules)
    if not compiled:
        return list(headers), [list(r) for r in rows], 0
    count = 0
    headers2 = []
    for h in headers:
        s, n = _apply_text(cell_to_str(h), compiled)
        count += n
        headers2.append(s)
    rows2 = []
    for r in rows:
        new_row = []
        for v in r:
            if isinstance(v, str):
                s, n = _apply_text(v, compiled)
                count += n
                new_row.append(s)
            else:
                new_row.append(v)
        rows2.append(new_row)
    return headers2, rows2, count


# ===================== 对外入口 =====================

def apply_filter(rows: Sequence[Sequence[Any]], mode: str = "hard",
                 columns: Optional[List[str]] = None,
                 post_rules: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """本插件自带的过滤执行入口（与 file-filter 的 /apply 响应结构保持一致）。

    入参 ``rows``：二维表（首行表头）。
    返回：{"rows": [表头]+数据行, "kept": [{"column","matched"}], "removed": [...],
           "replace_count": N, "mode": "hard", "warning": "…"（仅当请求 llm 模式时）}
    失败抛 :class:`FilterError`。
    """
    if not rows or not isinstance(rows[0], (list, tuple)):
        raise FilterError("没有可提交过滤的数据。", 400)
    if len(rows) > MAX_ROWS + 1:
        raise FilterError("行数超过上限（≤%d）" % MAX_ROWS, 400)

    requested = "llm" if str(mode).lower() == "llm" else "hard"
    warning = ""
    if requested == "llm":
        warning = LLM_REMOVED_NOTE          # 见模块头：llm 依赖对方配置，已随解耦移除

    keep = [clean_text(c) for c in (columns or []) if clean_text(c)]
    if not keep:
        raise FilterError("保留字段名单为空：请在插件配置里设置 keep_columns。", 400)

    headers = [clean_text(h) for h in rows[0]]
    body: List[List[Any]] = []
    for r in rows[1:]:
        if not isinstance(r, (list, tuple)):
            continue
        row = list(r[:len(headers)])
        if len(row) < len(headers):
            row.extend([None] * (len(headers) - len(row)))
        if all(clean_text(v) == "" for v in row):
            continue                        # 跳过全空行（与对方口径一致）
        body.append(row)

    headers2, rows2, kept, removed = filter_columns(headers, body, keep)
    headers2, rows2, count = post_process(headers2, rows2, post_rules)
    out: Dict[str, Any] = {
        "rows": [headers2] + rows2,
        "kept": [{"column": c, "matched": m} for c, m in kept],
        "removed": removed,
        "replace_count": count,
        "mode": "hard",
    }
    if warning:
        out["warning"] = warning
    return out


def status() -> Dict[str, Any]:
    """本插件过滤能力的可用性摘要（自带实现，恒可用；llm 模式不再提供）。"""
    return {
        "available": True,
        "local": True,
        "llm": False,
        "reason": "",
        "note": "过滤能力为本插件自带（插件间零依赖）；大模型辅助匹配已随解耦移除。",
    }
