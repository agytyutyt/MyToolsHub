# -*- coding: utf-8 -*-
"""v3 · 三票与判定：自身票、左右对票（含单空窗桥接）、赋权码归类。

口径（设计文档 §3.4 / §3.5 / §3.6）
------------------------------------
- 票值：0 失联 / 1 静默 / 2 缓行 / 3 快速 / 4 极速；
- 静默按**距离**门限（位移 ≤ 噪声即静默，与时间无关）；
- 自身票：窗内首末簇质心距离 ÷ **实际间隔**；间隔 < 守卫秒数时只判静默/缓行
  （短跨度守卫：突发上报不得凭空产生快速/极速票）；空窗 → 0；
- 对票 (i, i+1)：两窗均有数据 → 质心距 ÷ 窗宽；**恰一窗空 → 单空窗桥接**
  （空窗两侧最近非空窗质心 ÷ 实际间隔；另一侧邻窗也空 → 0，即跨 ≥2 空窗不桥接）；
- 判定四行规则（按序）：全 0 失联 / ≥2 个 4 极速 / (3 的个数 + 4 的个数) ≥ 2 快速 /
  只含 0·1 静默 / 其余缓行。码序 [左对， 自身， 右对]，判定与顺序无关。

本模块为纯函数（无 I/O、无状态），便于单元自检。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...geo import haversine

STATE_NAMES = {0: "失联", 1: "静默", 2: "缓行", 3: "快速", 4: "极速"}


def band(distance_m: float, interval_s: float, noise_m: float,
         creep_kmh: float, fast_kmh: float) -> int:
    """距离 + 时间 → 票值。位移 ≤ 噪声直接判静默（先于速度分带）。"""
    if distance_m <= noise_m:
        return 1
    if interval_s <= 0:
        return 4                                  # 距离超噪声而时间塌缩：按最高速处理
    v = 3.6 * distance_m / interval_s
    if v <= creep_kmh:
        return 2
    if v <= fast_kmh:
        return 3
    return 4


def own_vote(win: Optional[Dict[str, Any]], noise_m: float, creep_kmh: float,
             fast_kmh: float, guard_s: float) -> int:
    """自身票：窗内首末簇质心位移。空窗 → 0。"""
    if win is None:
        return 0
    d = haversine(win["first"][0], win["first"][1], win["last"][0], win["last"][1])
    if d <= noise_m:
        return 1
    span = win["ts_last"] - win["ts_first"]
    if span < guard_s:
        return 2                                  # 短跨度守卫
    return band(d, span, noise_m, creep_kmh, fast_kmh)


def pair_vote(i: int, j: int, info: Dict[int, Optional[Dict[str, Any]]],
              width_s: int, noise_m: float, creep_kmh: float, fast_kmh: float
              ) -> Tuple[int, bool]:
    """对票 (i, j=i+1)。返回 ``(票值, 是否走了桥接)``。

    恰一窗空时桥接：空窗另一侧的**紧邻窗**必须非空（否则视为跨 ≥2 空窗 → 0），
    桥接速度 = 空窗两侧最近非空窗质心距 ÷ 两窗实际点时刻差。
    """
    a, b = info.get(i), info.get(j)
    if a is not None and b is not None:
        return band(haversine(a["pos"][0], a["pos"][1], b["pos"][0], b["pos"][1]),
                    width_s, noise_m, creep_kmh, fast_kmh), False
    if a is None and b is None:
        return 0, False
    if a is None:                                 # i 空：左侧紧邻窗必须非空
        left = info.get(i - 1)
        if left is None or b is None:
            return 0, False
        L, R = left, b
    else:                                         # j 空：右侧紧邻窗必须非空
        right = info.get(j + 1)
        if right is None or a is None:
            return 0, False
        L, R = a, right
    interval = R["ts_first"] - L["ts_last"]
    if interval <= 0:
        return 0, False
    vote = band(haversine(L["pos"][0], L["pos"][1], R["pos"][0], R["pos"][1]),
                interval, noise_m, creep_kmh, fast_kmh)
    return vote, True


def classify(votes: Tuple[int, ...]) -> int:
    """赋权码 → 窗口状态（四行计数规则，按序；与码内顺序无关）。"""
    if all(v == 0 for v in votes):
        return 0
    if votes.count(4) >= 2:
        return 4
    if votes.count(3) + votes.count(4) >= 2:
        return 3
    if all(v in (0, 1) for v in votes):
        return 1
    return 2


def window_state(i: int, info: Dict[int, Optional[Dict[str, Any]]], wmin: int, wmax: int,
                 width_s: int, noise_m: float, creep_kmh: float, fast_kmh: float,
                 guard_s: float) -> Tuple[int, Tuple[int, int, int], int]:
    """第 i 窗的三票与状态。返回 ``(状态, (左对, 自身, 右对), 桥接次数)``。

    头尾窗口缺的一侧自然为 0（窗外无窗，对票不可计算）——两票同规则参与判定。
    """
    left, bl = pair_vote(i - 1, i, info, width_s, noise_m, creep_kmh, fast_kmh) \
        if i - 1 >= wmin else (0, False)
    own = own_vote(info.get(i), noise_m, creep_kmh, fast_kmh, guard_s)
    right, br = pair_vote(i, i + 1, info, width_s, noise_m, creep_kmh, fast_kmh) \
        if i + 1 <= wmax else (0, False)
    votes = (left, own, right)
    return classify(votes), votes, int(bl) + int(br)
