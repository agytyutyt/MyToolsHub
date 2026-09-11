# -*- coding: utf-8 -*-
"""v2 · 阶段 E：出行段构建（含"位移 vs 基站切换"判别与 trip chaining）。

核心思路
--------
相邻停留点之间是一次"位移候选"，但不直接判为出行，需过三道检验：

  1) **净位移**：用停留点**质心**之间的距离，而不是逐点累加
     （累加会把切换噪声累积放大，是上游 v1 出现 28825 km/h 的根源）；
  2) **回访检验**：窗口内是否出现"离开某簇后又回到该簇"——基站切换的典型特征；
  3) **直线度**：净位移 / 累计位移——停留抖动接近 0，真实移动接近 1。

不通过检验的相邻停留点会被**合并**（trip chaining），视为同一地点内的切换。

判定分级（阈值来自 ``params["trip"]``）::

    净位移 ≥ high_conf_factor × 噪声半径         → 「有效出行」（置信度 高）
    mid_conf_factor × 噪声 ≤ 净位移 < 高置信线    → 「位置变动」（置信度 中，报告注明疑似）
    净位移 < mid_conf_factor × 噪声               → 不做位移，合并两个停留点

速度只用**净位移 / 净时长**，不再逐点累加。首 / 末未纳入停留点的时段同样按此规则
构成出行段（``include_leading_trailing``）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ...contract import Dataset, Point, Stay, Trip, KIND_SHIFT, KIND_TRIP
from ...geo import cumulative_distance, haversine


def build_trips(dataset: Dataset, stays: List[Stay], params: Dict[str, Any],
                noise: Dict[str, Any]) -> Tuple[List[Stay], List[Trip]]:
    """返回 ``(合并后的停留点列表, 出行段列表)``。"""
    tcfg = params["trip"]
    noise_m = float(noise.get("noise_m", 0.0))
    high = float(tcfg["high_conf_factor"]) * noise_m
    mid = float(tcfg["mid_conf_factor"]) * noise_m
    min_str = float(tcfg["straightness_min"])
    walk = float(tcfg["walk_speed_kmh"])
    merge_revisit = bool(tcfg["merge_on_revisit"])
    min_minutes = float(tcfg["min_trip_minutes"])
    revisit_gap_s = float(tcfg["revisit_gap_minutes"]) * 60.0
    use_lead_trail = bool(tcfg["include_leading_trailing"])

    by_user: Dict[str, List[Stay]] = {}
    for s in stays:
        by_user.setdefault(s.usernum, []).append(s)
    for lst in by_user.values():
        lst.sort(key=lambda s: s.start_ts)

    out_stays: List[Stay] = []
    trips: List[Trip] = []

    for user, pts in dataset.users.items():
        n = len(pts)
        if n == 0:
            continue
        lon = [p.path_lon() for p in pts]
        lat = [p.path_lat() for p in pts]
        clu = [p.clu for p in pts]
        addr = [p.address for p in pts]
        ts = [p.ts for p in pts]

        st = list(by_user.get(user, []))

        # ---- 首段：第一个上报点 → 第一个停留点 ----
        if use_lead_trail and st and st[0].i0 > 0:
            seg = _lead_trail(lon, lat, clu, addr, ts, 0, st[0].i0, st[0], user,
                              leading=True, high=high, revisit_gap_s=revisit_gap_s)
            if seg and seg.minutes >= min_minutes and seg.net_m >= mid:
                trips.append(seg)

        # ---- 停留点之间（不达标则合并停留点）----
        i = 0
        while i < len(st) - 1:
            a, b = st[i], st[i + 1]
            i0, i1 = a.i1, b.i0
            path = [(a.lon, a.lat)] + list(zip(lon[i0 + 1:i1], lat[i0 + 1:i1])) + [(b.lon, b.lat)]
            m = _window_metrics(path, clu[i0:i1 + 1], ts[i0:i1 + 1], revisit_gap_s)

            do_merge = (m["net_m"] < mid
                        or (merge_revisit and m["revisit"] > 0)
                        or m["straightness"] < min_str)
            if do_merge:
                st[i] = _merge_stays(a, b)
                st.pop(i + 1)
                continue

            kind = KIND_TRIP if m["net_m"] >= high else KIND_SHIFT
            trips.append(Trip(
                usernum=user, start_ts=int(ts[i0]), end_ts=int(ts[i1]),
                minutes=m["minutes"], net_m=m["net_m"], cum_m=m["cum_m"],
                straightness=m["straightness"], revisit=m["revisit"],
                speed_kmh=m["speed_kmh"], kind=kind,
                confidence="高" if kind == KIND_TRIP else "中",
                from_addr=a.address, to_addr=b.address,
                from_lon=a.lon, from_lat=a.lat, to_lon=b.lon, to_lat=b.lat,
                n_points=i1 - i0 + 1,
            ))
            i += 1

        # ---- 末段：最后一个停留点 → 最后一个上报点 ----
        if use_lead_trail and st and st[-1].i1 < n - 1:
            seg = _lead_trail(lon, lat, clu, addr, ts, st[-1].i1, n - 1, st[-1], user,
                              leading=False, high=high, revisit_gap_s=revisit_gap_s)
            if seg and seg.minutes >= min_minutes and seg.net_m >= mid:
                trips.append(seg)

        out_stays.extend(st)

    out_stays.sort(key=lambda s: (s.usernum, s.start_ts))
    trips.sort(key=lambda t: (t.usernum, t.start_ts))
    return out_stays, trips


# --------------------------------------------------------------------------
# 内部实现
# --------------------------------------------------------------------------

def _window_metrics(path: List[Tuple[float, float]], clu_seq: List[str],
                    ts_seq: List[int], revisit_gap_s: float) -> Dict[str, float]:
    """一段窗口的几何与时间指标。

    ``clu_seq`` 为该窗口内**原始点**的簇序列（用于回访检验），与路径端点无关。
    """
    net = haversine(path[0][0], path[0][1], path[-1][0], path[-1][1]) if len(path) > 1 else 0.0
    cum = cumulative_distance(path)
    minutes = round((ts_seq[-1] - ts_seq[0]) / 60.0, 2) if len(ts_seq) > 1 else 0.0

    revisit = 0
    last_seen: Dict[str, int] = {}
    prev: Optional[str] = None
    for k, c in enumerate(clu_seq):
        t = float(ts_seq[k]) if k < len(ts_seq) else 0.0
        if prev is not None and c != prev and c in last_seen:
            if t - last_seen[c] <= revisit_gap_s:
                revisit += 1
        last_seen[c] = int(t)
        prev = c

    return {
        "net_m": float(round(net)),
        "cum_m": float(round(cum)),
        "straightness": round(net / cum, 3) if cum > 0 else 0.0,
        "minutes": minutes,
        "revisit": revisit,
        "speed_kmh": round(3.6 * net / (minutes * 60.0), 2) if minutes > 0 else 0.0,
    }


def _lead_trail(lon: List[float], lat: List[float], clu: List[str], addr: List[str],
                ts: List[int], i0: int, i1: int, stay: Stay, user: str, leading: bool,
                high: float, revisit_gap_s: float) -> Optional[Trip]:
    """首段 / 末段：一端是普通上报点，另一端是停留点质心。"""
    if i1 < i0:
        return None
    if leading:
        path = list(zip(lon[i0:i1], lat[i0:i1])) + [(stay.lon, stay.lat)]
        from_addr, to_addr = (addr[i0] if i0 < len(addr) else ""), stay.address
        f_lon, f_lat = lon[i0], lat[i0]
        t_lon, t_lat = stay.lon, stay.lat
    else:
        path = [(stay.lon, stay.lat)] + list(zip(lon[i0 + 1:i1 + 1], lat[i0 + 1:i1 + 1]))
        from_addr, to_addr = stay.address, (addr[i1] if i1 < len(addr) else "")
        f_lon, f_lat = stay.lon, stay.lat
        t_lon, t_lat = lon[i1], lat[i1]
    if len(path) < 2:
        return None

    m = _window_metrics(path, clu[i0:i1 + 1], ts[i0:i1 + 1], revisit_gap_s)
    kind = KIND_TRIP if m["net_m"] >= high else KIND_SHIFT
    return Trip(
        usernum=user, start_ts=int(ts[i0]), end_ts=int(ts[i1]),
        minutes=m["minutes"], net_m=m["net_m"], cum_m=m["cum_m"],
        straightness=m["straightness"], revisit=m["revisit"],
        speed_kmh=m["speed_kmh"], kind=kind,
        confidence="高" if kind == KIND_TRIP else "中",
        from_addr=from_addr, to_addr=to_addr,
        from_lon=f_lon, from_lat=f_lat, to_lon=t_lon, to_lat=t_lat,
        n_points=abs(i1 - i0) + 1,
    )


def _merge_stays(a: Stay, b: Stay) -> Stay:
    """合并两个停留点（净位移不足，判为同一地点内的切换），质心按点数加权。"""
    wa, wb = a.n_points, b.n_points
    tot = (wa + wb) or 1
    dominant = a if wa >= wb else b
    return Stay(
        usernum=a.usernum, i0=a.i0, i1=b.i1,
        start_ts=a.start_ts, end_ts=b.end_ts,
        minutes=round((b.end_ts - a.start_ts) / 60.0, 2),
        lon=round((a.lon * wa + b.lon * wb) / tot, 6),
        lat=round((a.lat * wa + b.lat * wb) / tot, 6),
        address=dominant.address, clu=dominant.clu,
        n_points=wa + wb, jitter_m=max(a.jitter_m, b.jitter_m),
    )
