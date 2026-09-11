# -*- coding: utf-8 -*-
"""v2 · 阶段 C：地点簇（把"经常互相切换、地理上很近"的小区合并成一个地点）。

动机
----
基站工参坐标不是用户真实位置：用户站着不动时终端会在相邻小区之间来回重选（乒乓），
坐标随之跳动。若直接用坐标做几何判定，会把切换误判成位移。

解决办法：先从数据中学习"哪些小区属于同一个地点"，以**地点簇**而非小区/坐标作为
判定单位。合并依据三个信号（阈值全部来自 ``params["cluster"]``）：

  1. 地址字符串相同          —— 强信号，工参地址本身是地点级描述；
  2. 坐标距离 < co_site      —— 共址（同站不同小区 / 同小区工参微调）；
  3. 双向切换且距离 ≤ 上限   —— 邻区乒乓。

实现与上游 ``SQLRewrite/src/cluster.py`` **逐条对齐**（并查集 + 相同的合并顺序），
以保证同一份数据的簇划分与对拍结果一致。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ...contract import ClusterInfo, Dataset, Point
from ...geo import haversine, round6
from ...stats import mode_str


def build_clusters(dataset: Dataset, params: Dict[str, Any]
                   ) -> Tuple[Dict[str, str], Dict[str, ClusterInfo], Dict[str, int]]:
    """构建地点簇。

    返回 ``(mapping, table, stats)``：

    - ``mapping``：``{小区键: 簇ID}``
    - ``table``  ：``{簇ID: ClusterInfo}``
    - ``stats``  ：合并原因计数（用于前端展示与数据质量报告）
    """
    ccfg = params["cluster"]
    enabled = bool(ccfg["enable"])
    # 簇建立在**清洗后未去重**的点上（与上游一致）：质心按上报次数加权、
    # 转移边按真实切换次数统计。若改用去重后的点，质心会偏移，
    # 停留点边界随之变化（实测会差 1 个采样点）。
    points = list(dataset.raw_points) or _ordered_points(dataset)

    if not enabled:
        mapping, table = _per_cell_clusters(points)
        return mapping, table, {"by_address": 0, "by_cosite": 0, "by_handover": 0, "disabled": 1}

    # ---- 小区属性：首次出现的位置与地址（与上游 groupby(...).first() 同口径）----
    cell_first: Dict[str, Dict[str, Any]] = {}
    for p in points:
        if p.cell not in cell_first:
            cell_first[p.cell] = {"lon": p.lon, "lat": p.lat, "address": p.address}

    # ---- 相邻转移边统计（同一号码内、相邻两次上报的小区不同）----
    edges: Dict[Tuple[str, str], int] = {}
    for _user, seq in _cell_sequences(points):
        for i in range(len(seq) - 1):
            a, b = seq[i], seq[i + 1]
            if a != b:
                edges[(a, b)] = edges.get((a, b), 0) + 1

    # ---- 并查集 ----
    parent: Dict[str, str] = {c: c for c in cell_first}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    max_m = float(ccfg["handover_max_meters"])
    min_cnt = int(ccfg["handover_min_count"])
    co_site = float(ccfg["co_site_meters"])
    same_addr = bool(ccfg["merge_same_address"])

    stats = {"by_address": 0, "by_cosite": 0, "by_handover": 0}
    for (a, b), n in edges.items():
        if find(a) == find(b):
            continue
        ca, cb = cell_first[a], cell_first[b]
        d = haversine(ca["lon"], ca["lat"], cb["lon"], cb["lat"])
        rev = edges.get((b, a), 0)
        if same_addr and ca["address"] and ca["address"] == cb["address"]:
            union(a, b)
            stats["by_address"] += 1
        elif d < co_site:
            union(a, b)
            stats["by_cosite"] += 1
        elif (n + rev) >= min_cnt and d <= max_m:
            union(a, b)
            stats["by_handover"] += 1

    mapping = {c: find(c) for c in cell_first}
    table = _cluster_attributes(points, mapping)
    return mapping, table, stats


def attach_clusters(dataset: Dataset, mapping: Dict[str, str],
                    table: Dict[str, ClusterInfo]) -> None:
    """就地给每个点挂上 ``clu`` / ``clu_lon`` / ``clu_lat``（无簇时退回原始坐标）。"""
    for pts in dataset.users.values():
        for p in pts:
            clu = mapping.get(p.cell, "")
            info = table.get(clu)
            p.clu = clu
            if info is not None:
                p.clu_lon = info.lon
                p.clu_lat = info.lat
            else:
                p.clu_lon = p.lon
                p.clu_lat = p.lat


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------

def _ordered_points(dataset: Dataset) -> List[Point]:
    """按 (号码, 时间) 顺序展开**去重后的**点（数据集缺失 raw_points 时的兜底）。"""
    out: List[Point] = []
    for user in sorted(dataset.users.keys()):
        out.extend(dataset.users[user])
    return out


def _cell_sequences(points: List[Point]):
    """按号码切分点序列（points 已按 (号码, 时间) 排序，故只需顺序扫一次）。"""
    cur: str = None
    seq: List[str] = []
    for p in points:
        if cur is None:
            cur = p.usernum
        elif p.usernum != cur:
            yield cur, seq
            cur, seq = p.usernum, []
        seq.append(p.cell)
    if cur is not None:
        yield cur, seq


def _per_cell_clusters(points: List[Point]
                       ) -> Tuple[Dict[str, str], Dict[str, ClusterInfo]]:
    """地点簇关闭时的退化模式：每个小区自成一簇。"""
    mapping: Dict[str, str] = {}
    table: Dict[str, ClusterInfo] = {}
    for p in points:
        if p.cell in mapping:
            table[p.cell].n += 1
            continue
        mapping[p.cell] = p.cell
        table[p.cell] = ClusterInfo(clu=p.cell, lon=p.lon, lat=p.lat,
                                    address=p.address, n=1, n_cell=1, jitter_m=0.0)
    return mapping, table


def _cluster_attributes(points: List[Point], mapping: Dict[str, str]
                        ) -> Dict[str, ClusterInfo]:
    """簇属性：按上报次数加权的质心 + 众数地址 + 小区数 + 抖动半径。"""
    acc: Dict[str, Dict[str, Any]] = {}
    for p in points:
        clu = mapping.get(p.cell, p.cell)
        slot = acc.get(clu)
        if slot is None:
            slot = {"sx": 0.0, "sy": 0.0, "n": 0, "addresses": [], "cells": set()}
            acc[clu] = slot
        slot["sx"] += p.lon
        slot["sy"] += p.lat
        slot["n"] += 1
        slot["addresses"].append(p.address)
        slot["cells"].add(p.cell)

    table: Dict[str, ClusterInfo] = {}
    for clu, slot in acc.items():
        n = slot["n"] or 1
        lon = slot["sx"] / n
        lat = slot["sy"] / n
        table[clu] = ClusterInfo(
            clu=clu, lon=round6(lon), lat=round6(lat),
            address=mode_str(slot["addresses"]), n=slot["n"],
            n_cell=len(slot["cells"]), jitter_m=0.0,
        )

    # 抖动半径（簇内点到质心的最大距离）——用于数据质量诊断
    for p in points:
        info = table.get(mapping.get(p.cell, p.cell))
        if info is None:
            continue
        d = haversine(p.lon, p.lat, info.lon, info.lat)
        if d > info.jitter_m:
            info.jitter_m = float(round(d))
    return table
