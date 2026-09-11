# -*- coding: utf-8 -*-
"""v2 算法：基站工参数据适配的轨迹分析流水线（引擎首个内置实现）。

流水线（与上游 ``SQLRewrite/src/pipeline.py`` 的 v2 分支同序）
-------------------------------------------------------------

    适配与质控（engine.adapter，进本模块前已完成）
      → 地点簇 cluster.build_clusters + attach_clusters
      → 自适应噪声估计 staypoint.estimate_noise
      → 阈值推导 staypoint.thresholds（D_thr / T_thr）
      → 停留点检测 staypoint.detect_stays
      → 出行段构建 trip.build_trips（含 trip chaining 与置信度分级）
      → 数据质量装配
      → 报告生成 report.run_report（含时间轴闭合与速写摘要）

本模块是"编排层"：只串流程与装配对外结果，**不含任何判定逻辑**。
判定逻辑在四个兄弟模块里，改算法时只动它们（见设计文档 §5.3）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ... import registry
from ...contract import Dataset, Stay, Trip
from ...stats import median
from ...timeutil import epoch_to_str, fmt_distance, fmt_duration
from . import cluster, report as report_mod, staypoint, trip as trip_mod

VERSION = "v2"


def run(dataset: Dataset, params: Dict[str, Any]) -> Dict[str, Any]:
    """执行 v2 分析。返回 ``{quality, stays, trips, report, cluster_stats}``。"""
    # ---- 阶段 C：地点簇 ----
    mapping, table, cstats = cluster.build_clusters(dataset, params)
    cluster.attach_clusters(dataset, mapping, table)

    # ---- 阶段 D：停留点 ----
    clustered = bool(params["cluster"]["enable"])
    noise = staypoint.estimate_noise(dataset, params)
    d_thr, t_thr = staypoint.thresholds(noise, params, clustered)
    stays = staypoint.detect_stays(dataset, params, noise, clustered)

    # ---- 阶段 E：出行段（可能合并停留点）----
    stays, trips = trip_mod.build_trips(dataset, stays, params, noise)

    # ---- 阶段 F：报告 ----
    rep = report_mod.run_report(dataset, params, stays, trips)

    quality = _quality(dataset, params, noise, d_thr, t_thr,
                       len(mapping), len(table), clustered, cstats)
    quality["停留点数_合并后"] = len(stays)
    quality["出行段数"] = len(trips)

    return {
        "quality": quality,
        "stays": _stays_json(stays),
        "trips": _trips_json(trips),
        "report": rep,
        "clusters": staypoint.cluster_table_to_json(table),
        "cluster_stats": cstats,
        "warnings": [],
    }


# --------------------------------------------------------------------------
# 结果装配（只做序列化，不改判定）
# --------------------------------------------------------------------------

def _quality(dataset: Dataset, params: Dict[str, Any], noise: Dict[str, Any],
             d_thr: float, t_thr: float, n_cells: int, n_clusters: int,
             clustered: bool, cstats: Dict[str, int]) -> Dict[str, Any]:
    users = dataset.users
    intervals = []
    for pts in users.values():
        for i in range(1, len(pts)):
            intervals.append(float(pts[i].ts - pts[i - 1].ts))
    ts_all = [p.ts for pts in users.values() for p in pts]
    valid = dataset.valid_rows or 1
    drop = dict(dataset.drop_stats)
    w: List[str] = []
    if clustered and n_cells and n_clusters and n_cells > n_clusters * 8:
        w.append("地点簇合并比例较高，说明基站切换频繁，结论已按地点级归并处理。")
    if noise.get("n_samples", 0) < 10:
        w.append("低速样本偏少（%d 个），噪声半径估计的稳定性有限，建议补充多日数据后重标阈值。"
                 % int(noise.get("n_samples", 0)))
    return {
        "读入行数": dataset.raw_rows,
        "有效行数": dataset.valid_rows,
        "丢弃行数": dataset.raw_rows - dataset.valid_rows,
        "有效率": round(dataset.valid_rows / valid, 4) if dataset.raw_rows else 0.0,
        "丢弃原因": drop,
        "号码数": len(dataset.all_usernums),
        "时间范围": [epoch_to_str(min(ts_all)), epoch_to_str(max(ts_all))] if ts_all else [],
        "去重后轨迹点数": sum(len(v) for v in users.values()),
        "采样间隔_秒": {
            # 与上游口径一致：质量报告的采样间隔含 0 间隔（信令突发上报），
            # 而 T_thr 推导用的是"有效间隔"中位数（见 staypoint.estimate_noise）。
            "中位": _round(median(intervals) if intervals else 0.0),
            "最大": _round(max(intervals) if intervals else 0),
        },
        "小区数": n_cells,
        "地点簇数": n_clusters,
        "地址数": dataset.n_addresses,
        "簇合并统计": {"按地址": cstats.get("by_address", 0),
                       "共址": cstats.get("by_cosite", 0),
                       "双向切换": cstats.get("by_handover", 0)},
        "估计定位噪声半径_米": int(round(noise.get("noise_m", 0.0))),
        "低速样本数": int(noise.get("n_samples", 0)),
        "停留半径阈值D_thr_米": int(round(d_thr)),
        "最短停留T_thr_分钟": round(t_thr / 60.0, 1),
        "半径模式": ("固定" if (str(params["staypoint"]["radius_mode"]) == "fixed"
                            or (str(params["staypoint"]["radius_mode"]) == "auto" and clustered))
                   else "自适应"),
        "地点簇启用": clustered,
        "算法版本": params.get("algo", VERSION),
        "质量提示": w,
    }


def _stays_json(stays: List[Stay]) -> List[Dict[str, Any]]:
    out = []
    for s in stays:
        out.append({
            "USERNUM": s.usernum,
            "进入时间": epoch_to_str(s.start_ts),
            "离开时间": epoch_to_str(s.end_ts),
            "停留分钟": round(float(s.minutes), 1),
            "停留时长": _dur(s.minutes),
            "经度": s.lon, "纬度": s.lat,
            "代表地址": s.address,
            "地点簇": s.clu,
            "涉及坐标点数": s.n_points,
            "抖动半径_米": int(round(s.jitter_m)),
        })
    return out


def _trips_json(trips: List[Trip]) -> List[Dict[str, Any]]:
    out = []
    for t in trips:
        out.append({
            "USERNUM": t.usernum,
            "开始时间": epoch_to_str(t.start_ts),
            "结束时间": epoch_to_str(t.end_ts),
            "时长分钟": round(float(t.minutes), 1),
            "时长": _dur(t.minutes),
            "净位移_米": int(round(t.net_m)),
            "累计位移_米": int(round(t.cum_m)),
            "距离": _dist(t.net_m),
            "直线度": round(float(t.straightness), 3),
            "回访次数": int(t.revisit),
            "均速_公里每小时": round(float(t.speed_kmh), 1),
            "判定": t.kind,
            "置信度": t.confidence,
            "起点": t.from_addr, "终点": t.to_addr,
            "起点经度": t.from_lon, "起点纬度": t.from_lat,
            "终点经度": t.to_lon, "终点纬度": t.to_lat,
            "途经点数": t.n_points,
        })
    return out


def _dur(minutes: float) -> str:
    return fmt_duration(minutes)


def _dist(meters: float) -> str:
    return fmt_distance(meters)


def _round(v: Any) -> float:
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return 0.0


registry.register(VERSION, run)
