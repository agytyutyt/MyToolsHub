# -*- coding: utf-8 -*-
"""参数层：**所有阈值的唯一外置落点**（解耦约束 D-5）。

职责
----
1. 定义引擎的全部默认参数（列映射、清洗、地点簇、停留点、出行、报告）；
2. ``normalize(config)``：把配置文件的原始结构合并进默认值，产出**规范化参数 dict**
   （幂等：已规范化的 dict 原样返回）；
3. ``validate(params)``：对参数做夹取与校验，产出可读的配置警告列表。

为什么要独立成模块
------------------
上游 SQLRewrite 的全部阈值集中在 ``config.yaml``，代码内不出现魔法数字。本引擎延续该约定：
算法模块（``algorithms/v2/*``）只允许通过 ``params`` 取值，**禁止**写死数字。
这样"算法更新"与"参数调整"完全解耦：调参数不用改代码，换算法不用改配置。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

DEFAULT_ALGO = "v2"

# --------------------------------------------------------------------------
# 列映射：规范列名 → 该列在真实表格中可能出现的表头别名（顺序即优先级）
# --------------------------------------------------------------------------
DEFAULT_COLUMN_MAP = {
    "BEGINTIME": ["BEGINTIME", "BEGIN_TIME", "开始时间", "时间", "起始时间", "上报时间"],
    "USERNUM":   ["USERNUM", "USER_NUM", "MSISDN", "号码", "用户号码", "手机号"],
    "SPCODE":    ["SPCODE", "SP_CODE"],
    "LAI":       ["LAI", "LAC", "位置区"],
    "CI":        ["CI", "小区", "小区号"],
    "ADDRESS":   ["ADDRESS", "GEO_ADDRESS", "ADDRESS_CN", "活动地区", "地址", "逆地理地址", "基站地址"],
    "LONGITUDE": ["LONGITUDE", "LNG", "LON", "经度", "开源经度"],
    "LATITUDE":  ["LATITUDE", "LAT", "纬度", "开源纬度"],
}

DEFAULT_SCHEMA = {
    "required": ["BEGINTIME", "LONGITUDE", "LATITUDE"],
    # 可选列：缺失会降级（无号码→单号码；无 LAI/CI→按地址成簇；无地址→按坐标成簇）。
    # SPCODE（运营商/局码）刻意**不列入**：引擎不参与任何判定，列入只会在每次分析时
    # 产生一条"可选字段未提供"的无谓提示（它仍在 column_map 中，便于将来扩展）。
    "optional": ["USERNUM", "ADDRESS", "LAI", "CI"],
    "column_map": DEFAULT_COLUMN_MAP,
}

DEFAULT_CLEAN = {
    "min_longitude": 1.0,        # 经度下限（上游 SQL：LONGITUDE > 1）
    "enable_min_latitude": True,  # 是否启用纬度下限
    "min_latitude": 0.0,
    "geo_precision": 3,          # 位置指纹精度（经纬度保留位数，3 位 ≈ 100 米）
    "grid_seconds": 300,         # 5 分钟网格
    "max_gap_seconds": 6000,     # 断档阈值（100 分钟），超过则在报告中标注「无数据」
}

DEFAULT_CLUSTER = {
    "enable": True,
    "merge_same_address": True,  # 地址相同 → 必合并（最强信号）
    "co_site_meters": 50.0,      # 共址 / 同站不同小区 → 必合并
    "handover_min_count": 3,     # 双向切换次数下限
    "handover_max_meters": 2000.0,
}

DEFAULT_STAYPOINT = {
    "radius_mode": "auto",           # auto（开簇时用固定小半径）| fixed | adaptive
    "fixed_radius_meters": 800.0,
    "radius_factor": 1.2,
    "radius_min_meters": 300.0,
    "radius_max_meters": 3000.0,
    "min_stay_minutes": 15.0,        # 最短停留 T_thr 下限
    "adaptive_min_stay_factor": 3.0,  # T_thr = max(下限, factor × 中位采样间隔)
    "noise_percentile": 95.0,        # 噪声半径取低速位移的分位数
    "slow_speed_kmh": 5.0,           # 「低速样本」的速度上限
    "max_window_points": 2000,       # 单窗口最大点数（防 O(n²) 退化）
}

DEFAULT_TRIP = {
    "high_conf_factor": 3.0,     # 高置信：净位移 ≥ 3 × 噪声半径
    "mid_conf_factor": 1.5,      # 中置信：净位移 ≥ 1.5 × 噪声半径
    "straightness_min": 0.5,     # 直线度下限
    "walk_speed_kmh": 4.0,       # 低于此速度标为「位置变动」
    "merge_on_revisit": True,    # 窗口内出现回访 → 判为切换，合并停留点
    "revisit_gap_minutes": 60.0,  # 回访时间窗
    "min_trip_minutes": 5.0,
    "include_leading_trailing": True,  # 首 / 末未纳入停留点的时段也算出行段
}

DEFAULT_REPORT = {
    "header": "内部资料 严禁外传",
    "footer": ("（此件为内部资料，严格控制知悉范围，严禁通过互联网、手机、微信等传播使用"
               "和对外发布，严禁向无关人员转发）"),
    "append_summary": True,     # 报告首行附「速写摘要」
    "split_by_day": False,      # 按日分节
    "fill_holes": True,         # 无数据时段显式标注
    "append_quality": False,    # 报告尾部附数据质量摘要
    "time_formats": [],         # 追加的时间格式（优先于内置格式尝试）
}

DEFAULT_PARAMS: Dict[str, Any] = {
    "algo": DEFAULT_ALGO,
    "schema": DEFAULT_SCHEMA,
    "clean": DEFAULT_CLEAN,
    "cluster": DEFAULT_CLUSTER,
    "staypoint": DEFAULT_STAYPOINT,
    "trip": DEFAULT_TRIP,
    "report": DEFAULT_REPORT,
}


# --------------------------------------------------------------------------
# 合并与类型强制
# --------------------------------------------------------------------------

def _coerce(default_val: Any, given: Any) -> Any:
    """按默认值的类型强制转换；转换失败退回默认值。"""
    if default_val is None and given is None:
        return None
    try:
        if isinstance(default_val, bool):
            if isinstance(given, str):
                return given.strip().lower() in ("1", "true", "yes", "on", "是", "开")
            return bool(given)
        if isinstance(default_val, int) and not isinstance(default_val, bool):
            return int(float(given))
        if isinstance(default_val, float):
            return float(given)
        if isinstance(default_val, str):
            return "" if given is None else str(given)
        if isinstance(default_val, list):
            if isinstance(given, (list, tuple)):
                return list(given)
            return copy.deepcopy(default_val)
        if isinstance(default_val, dict):
            if isinstance(given, dict):
                return _merge(default_val, given)
            return copy.deepcopy(default_val)
    except (TypeError, ValueError):
        return copy.deepcopy(default_val)
    return given


def _merge(defaults: Dict[str, Any], given: Any) -> Dict[str, Any]:
    """深度合并：只覆盖 defaults 里已知的键，未知键忽略（防配置漂移）。"""
    out: Dict[str, Any] = {}
    src = given if isinstance(given, dict) else {}
    for key, dval in defaults.items():
        if key in src and src[key] is not None:
            out[key] = _coerce(dval, src[key])
        else:
            out[key] = copy.deepcopy(dval)
    return out


def _merge_aliases(given: Any) -> Dict[str, List[str]]:
    """列映射合并：允许管理员只覆盖部分规范列的别名，其余取默认。"""
    out = {k: list(v) for k, v in DEFAULT_COLUMN_MAP.items()}
    if isinstance(given, dict):
        for k, v in given.items():
            key = str(k).strip()
            if not key:
                continue
            if isinstance(v, (list, tuple)):
                aliases = [str(i).strip() for i in v if str(i).strip()]
            else:
                aliases = [str(v).strip()] if str(v).strip() else []
            # 自定义列名（不在默认映射内）也接受，便于特殊表格
            out[key] = aliases or out.get(key, [])
    return out


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def normalize(config: Any = None) -> Dict[str, Any]:
    """把配置文件（或已规范化的参数）统一为引擎参数 dict。

    接受的输入形态（都是容错的，缺键自动补默认值）::

        {"schema": {...}, "analysis": {"algo": "v2", "cluster": {...}}, "report": {...}}
        {"cluster": {...}, "staypoint": {...}}            # 直接传引擎节
        None                                              # 全部默认值
    """
    if isinstance(config, dict) and config.get("_normalized"):
        return config

    cfg = config if isinstance(config, dict) else {}
    analysis = cfg.get("analysis") if isinstance(cfg.get("analysis"), dict) else {}
    flat: Dict[str, Any] = cfg if not analysis else {}

    def sect(name: str, defaults: Dict[str, Any]) -> Dict[str, Any]:
        src = analysis.get(name)
        if not isinstance(src, dict):
            src = flat.get(name) if isinstance(flat.get(name), dict) else {}
        return _merge(defaults, src)

    schema_src = cfg.get("schema") if isinstance(cfg.get("schema"), dict) else flat.get("schema")
    schema_src = schema_src if isinstance(schema_src, dict) else {}
    schema = {
        "required": [str(c).strip() for c in _coerce(DEFAULT_SCHEMA["required"], schema_src.get("required"))
                     if str(c).strip()],
        "optional": [str(c).strip() for c in _coerce(DEFAULT_SCHEMA["optional"], schema_src.get("optional"))
                     if str(c).strip()],
        "column_map": _merge_aliases(schema_src.get("column_map")),
    }

    algo = analysis.get("algo") if analysis.get("algo") is not None else cfg.get("algo")
    params: Dict[str, Any] = {
        "_normalized": True,
        "algo": str(algo).strip() if str(algo or "").strip() else DEFAULT_ALGO,
        "schema": schema,
        "clean": sect("clean", DEFAULT_CLEAN),
        "cluster": sect("cluster", DEFAULT_CLUSTER),
        "staypoint": sect("staypoint", DEFAULT_STAYPOINT),
        "trip": sect("trip", DEFAULT_TRIP),
    }
    report_src = cfg.get("report") if isinstance(cfg.get("report"), dict) else analysis.get("report")
    params["report"] = _merge(DEFAULT_REPORT, report_src)

    _clamp(params)
    return params


def _clamp(params: Dict[str, Any]) -> None:
    """就地夹取明显越界的参数（不抛异常，保证"配置写错也不崩"）。"""
    sp = params["staypoint"]
    sp["radius_min_meters"] = max(10.0, float(sp["radius_min_meters"]))
    sp["radius_max_meters"] = max(sp["radius_min_meters"], float(sp["radius_max_meters"]))
    sp["fixed_radius_meters"] = min(sp["radius_max_meters"],
                                    max(sp["radius_min_meters"], float(sp["fixed_radius_meters"])))
    sp["noise_percentile"] = min(100.0, max(50.0, float(sp["noise_percentile"])))
    sp["min_stay_minutes"] = max(0.0, float(sp["min_stay_minutes"]))
    sp["adaptive_min_stay_factor"] = max(0.0, float(sp["adaptive_min_stay_factor"]))
    sp["slow_speed_kmh"] = max(0.1, float(sp["slow_speed_kmh"]))
    sp["max_window_points"] = max(10, int(sp["max_window_points"]))

    t = params["trip"]
    t["high_conf_factor"] = max(0.0, float(t["high_conf_factor"]))
    t["mid_conf_factor"] = max(0.0, float(t["mid_conf_factor"]))
    t["straightness_min"] = min(1.0, max(0.0, float(t["straightness_min"])))
    t["walk_speed_kmh"] = max(0.1, float(t["walk_speed_kmh"]))
    t["min_trip_minutes"] = max(0.0, float(t["min_trip_minutes"]))

    c = params["cluster"]
    c["co_site_meters"] = max(0.0, float(c["co_site_meters"]))
    c["handover_min_count"] = max(1, int(c["handover_min_count"]))
    c["handover_max_meters"] = max(0.0, float(c["handover_max_meters"]))

    cl = params["clean"]
    cl["geo_precision"] = min(6, max(0, int(cl["geo_precision"])))
    cl["grid_seconds"] = max(0, int(cl["grid_seconds"]))
    cl["max_gap_seconds"] = max(0.0, float(cl["max_gap_seconds"]))


def validate(params: Dict[str, Any]) -> List[str]:
    """返回配置层面的警告（中文，可直接展示在页面「配置警告」处）。"""
    w: List[str] = []
    req = params["schema"]["required"]
    if not req:
        w.append("未配置必需列（schema.required），任何表都会被视为缺少必需列。")
    if "LONGITUDE" not in req or "LATITUDE" not in req:
        w.append("必需列建议包含经度与纬度，否则无法进行几何判定。")
    if "BEGINTIME" not in req:
        w.append("必需列建议包含时间，否则无法构建轨迹时间轴。")
    cmap = params["schema"]["column_map"]
    for canon in req:
        if canon not in cmap:
            w.append("必需列「%s」未在列映射（column_map）中声明候选别名。" % canon)
    if params["staypoint"]["radius_mode"] not in ("auto", "fixed", "adaptive"):
        w.append("停留半径模式（radius_mode）取值非法，已按 auto 处理。")
    if params["trip"]["mid_conf_factor"] > params["trip"]["high_conf_factor"]:
        w.append("中置信系数大于高置信系数，将导致判定层级颠倒，请检查配置。")
    if not params["cluster"]["enable"] and params["staypoint"]["radius_mode"] == "auto":
        w.append("地点簇已关闭且半径为 auto，将按定位噪声自适应放大停留半径（结果偏保守）。")
    return w


def algorithm_versions() -> List[str]:
    """当前可用的算法版本（延迟导入 registry，避免循环依赖）。"""
    from . import registry
    return registry.versions()
