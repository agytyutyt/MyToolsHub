# -*- coding: utf-8 -*-
"""本插件自带的表格过滤实现（硬过滤 + 大模型辅助语义匹配 + 文本后处理）。

为什么自带一份（而不是调用「过滤器」插件）
----------------------------------------
插件之间必须相互独立（《插件设计规范》B-7 / 设计文档《主体与插件解耦》§5.4 D-9）：
过去本插件通过 HTTP 调 ``POST /api/file-filter/apply`` 借用过滤器插件的过滤能力，
还读 ``config/tools.json`` 判断对方是否启用——那是"未声明、无版本校验、不进依赖判定"
的隐式耦合，且对方下线时本插件的字段自检与轨迹分析会直接不可用。

现在把过滤算法与后处理规则**在本插件内自带一份**：两处为**同源副本、各自独立演进**
（不承诺行为一致，见设计文档 §5.4「为什么副本优于引用」；仓库已有先例——
``bg_image.py`` 在 file-filter 与 trajectory-sketch 下各有一份完全相同的副本）。

与「过滤器」插件的分工
------------------------
本文件负责**硬过滤**与**大模型辅助过滤**两条路径：
- 硬过滤（``filter_columns``）与 ``file-filter/backend/core.py`` 的同名实现逐条一致
  （保留列匹配、全空行跳过、表头规整、正则/文本替换、计数口径，由单测钉住）；
- 大模型辅助（``filter_columns_llm``）镜像 ``file-filter/backend/routes.run_filter``
  的 llm 分支口径——**名单内同名字段直接保留**（不把用户点名要的列交给模型判断），
  其余交给模型做语义匹配。模型接入由框架模块 ``jz_llm`` 提供（见 ``llm_client``），
  因此**不产生任何跨插件依赖**。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import llm_client

MAX_ROWS = 100000


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


# ===================== 列过滤（大模型辅助语义匹配） =====================

def filter_columns_llm(headers, rows, keep, session=None):
    """大模型辅助的列过滤：语义匹配替代字面精确匹配。

    与硬过滤的差别（口径镜像 ``file-filter/backend/routes.run_filter`` 的 llm 分支）：

    - **名单内同名字段直接保留**——那是管理员点名要的列，不把判断权交给模型
      （模型偶发返回空值时会让人以为"明明开着却被删"，与硬过滤口径也不一致）；
    - 其余列交给 ``llm_client.match_columns`` 做语义关联（如「采集起始时刻」↔「开始时间」）；
    - 模型返回名单之外的值视为未匹配（防幻觉，见 ``llm_client``）。

    ``session`` 须由调用方在**请求线程**内取得（后台线程读不到会话）；
    未配置大模型时 ``llm_client`` 会抛 ``LLMError``，由调用方转成可展示的提示。

    返回 ``(headers2, rows2, kept, removed)``，与 :func:`filter_columns` 同构。
    """
    mappings = llm_client.match_columns(headers, keep, session=session)
    keep_fold = {clean_text(k).casefold(): clean_text(k)
                 for k in (keep or []) if clean_text(k)}
    idx, kept, removed = [], [], []
    for i, h in enumerate(headers):
        hf = clean_text(h).casefold()
        if not hf:
            removed.append(h)
            continue
        matched = mappings.get(str(h)) or keep_fold.get(hf) or ""
        if matched:
            idx.append(i)
            kept.append((h, matched))
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
                 post_rules: Optional[List[Dict[str, Any]]] = None,
                 session: Any = None) -> Dict[str, Any]:
    """本插件自带的过滤执行入口（与 file-filter 的 /apply 响应结构保持一致）。

    入参：``rows`` 二维表（首行表头）；``mode`` 取 ``hard`` / ``llm``；
    ``session`` 统一大模型的接入配置快照，**仅在 mode=llm 时需要**，且必须由调用方在
    **请求线程**内取得（后台线程读不到会话，见 ``jz_llm`` 模块头的线程纪律）。

    返回：{"rows": [表头]+数据行, "kept": [{"column","matched"}], "removed": [...],
           "replace_count": N, "mode": "hard"|"llm"}
    失败抛 :class:`FilterError`（大模型异常也归一成它，附带可直接展示的中文描述）。
    """
    if not rows or not isinstance(rows[0], (list, tuple)):
        raise FilterError("没有可提交过滤的数据。", 400)
    if len(rows) > MAX_ROWS + 1:
        raise FilterError("行数超过上限（≤%d）" % MAX_ROWS, 400)

    requested = "llm" if str(mode).lower() == "llm" else "hard"

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

    if requested == "llm":
        try:
            headers2, rows2, kept, removed = filter_columns_llm(headers, body, keep,
                                                                session=session)
        except llm_client.LLMError as exc:
            # 大模型异常归一成 FilterError：SEC-5 只给可展示的中文描述，不带堆栈
            raise FilterError("大模型辅助过滤失败：%s" % exc, 400)
    else:
        headers2, rows2, kept, removed = filter_columns(headers, body, keep)

    headers2, rows2, count = post_process(headers2, rows2, post_rules)
    return {
        "rows": [headers2] + rows2,
        "kept": [{"column": c, "matched": m} for c, m in kept],
        "removed": removed,
        "replace_count": count,
        "mode": requested,
    }


def status() -> Dict[str, Any]:
    """本插件过滤能力的可用性摘要（硬过滤与语义匹配都在本插件内实现）。

    只报**本模块自己的**事实；大模型是否已配置属于接入层，由 ``filter_bridge``
    合并 ``llm_client`` 的结论后一并返回（避免本文件耦合 jz_llm）。
    """
    return {
        "available": True,
        "local": True,
        "reason": "",
        "note": "过滤能力为本插件自带（插件间零依赖）；大模型辅助走框架模块 jz_llm。",
    }
