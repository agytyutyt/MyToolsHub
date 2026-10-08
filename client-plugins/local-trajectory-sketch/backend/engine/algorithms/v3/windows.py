# -*- coding: utf-8 -*-
"""v3 · 窗口化：把点序列切成固定墙钟窗口（五态窗口栅格的地基）。

口径（设计文档 §3.3）
--------------------
- 窗口按墙钟对齐：``w = ts // 窗宽秒数``，逐用户独立；
- 窗口表覆盖该用户**首末点之间**的全部窗口，含无点空窗（记 None）；
- 窗口平均位置 = 窗内各点**簇质心**（``clu_lon/clu_lat``）的经纬度平均；
- 首末位置 = 窗内**首点 / 末点**的簇质心——严格窗内取点，不向邻窗借点；
- 单点窗口首末同位，自身票按位移 0 处理（静默）。

本模块不做任何判定（D-6 同 v2 的职责切分）：只产出窗口事实表。
"""
from __future__ import annotations

from typing import Any, Dict

from ...stats import mode_str


def build_windows(dataset, width_seconds: int) -> Dict[str, Dict[str, Any]]:
    """按号码把点序列切窗。

    返回 ``{号码: {"wmin": int, "wmax": int, "width": int, "info": {窗口号: 窗口事实|None}}}``。
    窗口事实字段：

    - ``pos``      平均位置（簇质心均值）
    - ``first`` / ``last``   首 / 末点的簇质心
    - ``ts_first`` / ``ts_last``   首 / 末点时刻（去重后点序列内的时间）
    - ``t0``       窗口起点（墙钟）
    - ``n``        窗内点数
    - ``addr``     窗内地址众数（报告用）
    - ``points``   窗内点列表的引用（聚合层取实际时刻，勿改）
    """
    out: Dict[str, Dict[str, Any]] = {}
    for user, pts in dataset.users.items():
        buckets: Dict[int, list] = {}
        for p in pts:
            buckets.setdefault(p.ts // width_seconds, []).append(p)
        if not buckets:
            out[user] = {"wmin": 0, "wmax": -1, "width": width_seconds, "info": {}}
            continue
        wmin, wmax = min(buckets), max(buckets)
        info: Dict[int, Any] = {}
        for w in range(wmin, wmax + 1):
            g = buckets.get(w) or []
            if not g:
                info[w] = None
                continue
            info[w] = {
                "pos": (sum(p.clu_lon for p in g) / len(g),
                        sum(p.clu_lat for p in g) / len(g)),
                "first": (g[0].clu_lon, g[0].clu_lat),
                "last": (g[-1].clu_lon, g[-1].clu_lat),
                "ts_first": g[0].ts,
                "ts_last": g[-1].ts,
                "t0": w * width_seconds,
                "n": len(g),
                "addr": mode_str([p.address for p in g]),
                "points": g,
            }
        out[user] = {"wmin": wmin, "wmax": wmax, "width": width_seconds, "info": info}
    return out
