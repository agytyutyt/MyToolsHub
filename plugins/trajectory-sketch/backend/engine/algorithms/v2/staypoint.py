# -*- coding: utf-8 -*-
"""v2 · 阶段 D：自适应噪声估计 + 停留点检测。

与"三态阈值法"（把相邻点距离与固定阈值比较）的三点不同：

1. 在**地点簇质心**序列上检测（簇内切换已被吸收），而不是原始坐标；
2. 半径阈值 ``D_thr`` 由**数据自身估计**（低速样本位移分位数），而不是写死的 120 米；
3. 用「点到窗口质心距离 ≤ D_thr」而不是「相邻点距离 ≤ D_thr」——
   这样乒乓切换（A→B→A）会被吸收进同一个停留点。

自适应阈值
----------
    低速样本 = 相邻点对中 速度 < slow_speed_kmh 的那些
    noise    = percentile(低速样本位移, noise_percentile)
    D_thr    = clamp(noise × radius_factor, radius_min, radius_max)   # 未开簇时
    T_thr    = max(min_stay_minutes, adaptive_min_stay_factor × 中位采样间隔)

阈值与复杂度
------------
- 全部阈值取自 ``params["staypoint"]``，代码内无魔法数字（解耦约束 D-5）；
- 检测为双指针扫描，窗口上限 ``max_window_points`` 防止 O(n²) 退化（沿用上游约定）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ...contract import ClusterInfo, Dataset, Point, Stay
from ...geo import haversine, max_dist_to_centroid
from ...stats import median, mode_str, percentile


def estimate_noise(dataset: Dataset, params: Dict[str, Any]) -> Dict[str, Any]:
    """估计本批数据的**定位噪声半径**（米）。

    必须用**原始坐标**而非簇质心：聚类后同簇点的质心坐标完全相同、距离恒为 0，
    会把噪声估计成 0。这里测的是"用户没动时坐标能跳多远"，用于出行段判定
    （净位移是否超过定位精度）。

    返回 ``{noise_m, n_samples, median_interval_s, median_displacement_m}``。
    """
    sp = params["staypoint"]
    slow_kmh = float(sp["slow_speed_kmh"])
    pct = float(sp["noise_percentile"])

    dists: List[float] = []
    gaps: List[float] = []
    for pts in dataset.users.values():
        for i in range(1, len(pts)):
            a, b = pts[i - 1], pts[i]
            dists.append(haversine(a.lon, a.lat, b.lon, b.lat))
            gaps.append(float(b.ts - a.ts))

    ok_idx = [i for i in range(len(dists)) if gaps[i] > 0]
    speeds = [3.6 * dists[i] / gaps[i] for i in ok_idx]
    sample = [dists[i] for i, v in zip(ok_idx, speeds) if v < slow_kmh]
    if len(sample) < 5:                       # 低速样本不足 → 退回全部位移的分位数
        sample = [dists[i] for i in ok_idx] or [0.0]

    noise = percentile(sample, pct)
    med_dt = median([gaps[i] for i in ok_idx])
    med_d = median([dists[i] for i in ok_idx])
    return {
        "noise_m": float(noise or 0.0),
        "n_samples": int(len(sample)),
        "median_interval_s": float(med_dt or 0.0),
        "median_displacement_m": float(med_d or 0.0),
    }


def thresholds(noise: Dict[str, Any], params: Dict[str, Any],
               clustered: bool = True) -> Tuple[float, float]:
    """返回 ``(停留半径 D_thr 米, 最短停留 T_thr 秒)``。

    半径作用于**簇质心序列**。簇质心本身是稳定的（同簇点坐标相同），故开启聚类时
    用较小的固定半径即可；未开启聚类时才需按定位噪声放大。
    """
    sp = params["staypoint"]
    mode = str(sp["radius_mode"])
    use_fixed = (mode == "fixed") or (mode == "auto" and clustered)
    if use_fixed:
        d = float(sp["fixed_radius_meters"])
    else:
        d = float(min(float(sp["radius_max_meters"]),
                      max(float(sp["radius_min_meters"]),
                          float(noise.get("noise_m", 0.0)) * float(sp["radius_factor"]))))
    t_min = float(sp["min_stay_minutes"]) * 60.0
    t_adapt = float(sp["adaptive_min_stay_factor"]) * float(noise.get("median_interval_s", 0.0))
    return d, max(t_min, t_adapt)


def detect_stays(dataset: Dataset, params: Dict[str, Any], noise: Dict[str, Any],
                 clustered: Optional[bool] = None) -> List[Stay]:
    """在地点簇质心序列上做停留点检测（双指针 + 窗口质心半径）。"""
    if clustered is None:
        clustered = bool(params["cluster"]["enable"])
    d_thr, t_thr = thresholds(noise, params, clustered)
    max_win = int(params["staypoint"]["max_window_points"])

    stays: List[Stay] = []
    for user, pts in dataset.users.items():
        n = len(pts)
        if n == 0:
            continue
        lons = [p.path_lon() for p in pts]
        lats = [p.path_lat() for p in pts]
        ts = [p.ts for p in pts]

        i = 0
        while i < n:
            j = i + 1
            while j < n and (j - i) <= max_win:
                w = list(zip(lons[i:j + 1], lats[i:j + 1]))
                c_lon = sum(t[0] for t in w) / len(w)
                c_lat = sum(t[1] for t in w) / len(w)
                if max_dist_to_centroid(w, c_lon, c_lat) <= d_thr:
                    j += 1
                else:
                    break
            last = j - 1
            if ts[last] - ts[i] >= t_thr:
                w = list(zip(lons[i:last + 1], lats[i:last + 1]))
                c_lon = sum(t[0] for t in w) / len(w)
                c_lat = sum(t[1] for t in w) / len(w)
                jitter = max_dist_to_centroid(w, c_lon, c_lat)
                sub = pts[i:last + 1]
                stays.append(Stay(
                    usernum=user, i0=i, i1=last, start_ts=int(ts[i]), end_ts=int(ts[last]),
                    minutes=round((ts[last] - ts[i]) / 60.0, 2),
                    lon=round(c_lon, 6), lat=round(c_lat, 6),
                    address=_dominant_address(sub),
                    clu=_dominant_cluster(sub),
                    n_points=last - i + 1, jitter_m=float(round(jitter)),
                ))
                i = last + 1
            else:
                i += 1
    return stays


def _dominant_address(points: List[Point]) -> str:
    return mode_str([p.address for p in points])


def _dominant_cluster(points: List[Point]) -> str:
    return mode_str([p.clu for p in points])


def cluster_table_to_json(table: Dict[str, ClusterInfo], limit: int = 500) -> List[Dict[str, Any]]:
    """簇属性导出（前端"地点簇"折叠面板用；limit 防止大表撑爆响应）。"""
    out = []
    for info in list(table.values())[:limit]:
        out.append({"clu": info.clu, "address": info.address, "n_cell": info.n_cell,
                    "n": info.n, "jitter_m": info.jitter_m,
                    "lon": info.lon, "lat": info.lat})
    return out
