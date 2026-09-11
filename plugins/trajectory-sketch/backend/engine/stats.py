# -*- coding: utf-8 -*-
"""统计工具（纯标准库，算法与 numpy 默认口径对齐）。

上游 SQLRewrite 用 ``numpy.percentile``（默认 ``method="linear"``）。
本模块按同一口径实现，使算法从 pandas/numpy 迁到标准库后**结果保持一致**（对拍前提）。
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """线性插值分位数，等价于 ``numpy.percentile(values, p)``。

    空序列返回 ``None``；``p`` 自动夹到 [0, 100]。
    """
    data = sorted(float(v) for v in values if v is not None and not _isnan(v))
    n = len(data)
    if n == 0:
        return None
    if n == 1:
        return data[0]
    p = min(100.0, max(0.0, float(p)))
    pos = (n - 1) * p / 100.0
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return data[lo]
    frac = pos - lo
    return data[lo] * (1.0 - frac) + data[hi] * frac


def median(values: Sequence[float]) -> Optional[float]:
    """中位数（等价于 ``numpy.median``）。空序列返回 ``None``。"""
    return percentile(values, 50.0)


def mean(values: Sequence[float]) -> float:
    vals = [float(v) for v in values if v is not None and not _isnan(v)]
    return sum(vals) / len(vals) if vals else 0.0


def p95(values: Sequence[float]) -> Optional[float]:
    return percentile(values, 95.0)


def mode_str(values: Sequence[str]) -> str:
    """字符串众数（并列时取**出现顺序最早**者，保证结果确定）。

    对应 pandas 的 ``Series.mode().iloc[0]``：先按首次出现顺序排序再取最高频。
    """
    counts = {}
    order = []
    for v in values:
        s = "" if v is None else str(v)
        if s not in counts:
            counts[s] = 0
            order.append(s)
        counts[s] += 1
    if not order:
        return ""
    best = order[0]
    for s in order:
        if counts[s] > counts[best]:
            best = s
    return best


def _isnan(v) -> bool:
    try:
        return float(v) != float(v)
    except (TypeError, ValueError):
        return False
