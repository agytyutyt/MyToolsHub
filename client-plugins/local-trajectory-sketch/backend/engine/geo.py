# -*- coding: utf-8 -*-
"""几何工具（纯标准库）。

只提供引擎需要的两件事：两点球面距离、以及一组点的质心距离统计。
不引入 numpy / geopy 等依赖（见设计文档 §5.1 D-2）。
"""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

# 地球半径取 WGS84 长半轴（6378137），与上游 SQLRewrite/src/geo.py 及原始 SQL
# 的 ``6378137 * 2 * ASIN(SQRT(...))`` 完全一致 —— 这是"可对拍"的前提，请勿改成
# 平均半径 6371008.8，否则阈值边界上会与上游结果出现一个采样点级的偏差。
EARTH_RADIUS_M = 6378137.0


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """两点球面距离（米，**四舍五入到整米**）。参数为十进制度。

    取整同样是为了与上游保持一致：上游用 ``np.round`` 输出整米，
    停留窗口判定（``<= D_thr``）会因此出现边界差异。
    """
    p = math.pi / 360.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.sin((lat2 - lat1) * p)
    dlon = math.sin((lon2 - lon1) * p)
    a = dlat * dlat + math.cos(p1) * math.cos(p2) * dlon * dlon
    if a < 0.0:
        a = 0.0
    elif a > 1.0:
        a = 1.0                      # 浮点误差可能让 a 略大于 1，导致 asin 域外
    return float(round(2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(a))))


def centroid(points: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """一组 (lon, lat) 的算术质心。空序列返回 (0.0, 0.0)。"""
    n = len(points)
    if n == 0:
        return 0.0, 0.0
    sx = 0.0
    sy = 0.0
    for lon, lat in points:
        sx += lon
        sy += lat
    return sx / n, sy / n


def max_dist_to_centroid(points: Sequence[Tuple[float, float]],
                         c_lon: float, c_lat: float) -> float:
    """一组点到给定质心的最大距离（米）。"""
    best = 0.0
    for lon, lat in points:
        d = haversine(lon, lat, c_lon, c_lat)
        if d > best:
            best = d
    return best


def cumulative_distance(points: Sequence[Tuple[float, float]]) -> float:
    """按顺序累计的相邻点距离之和（米）。共用同一实现，避免各处重复推导。"""
    total = 0.0
    for i in range(len(points) - 1):
        (x1, y1), (x2, y2) = points[i], points[i + 1]
        total += haversine(x1, y1, x2, y2)
    return total


def bounding_span(points: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """返回 (最大跨度距离米, 最大相邻距离米)，用于数据质量诊断。"""
    if len(points) < 2:
        return 0.0, 0.0
    span = 0.0
    step = 0.0
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            d = haversine(points[i][0], points[i][1], points[j][0], points[j][1])
            if d > span:
                span = d
    for i in range(len(points) - 1):
        d = haversine(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        if d > step:
            step = d
    return span, step


def round6(v: float) -> float:
    return round(float(v), 6)
