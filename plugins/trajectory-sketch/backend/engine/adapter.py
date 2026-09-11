# -*- coding: utf-8 -*-
"""适配层：过滤后的二维表 → 规范化轨迹点（引擎入口的第一道工序）。

对应上游 SQLRewrite 的 ``io_utils.load_excel`` + ``pipeline._build_points``，
但**没有任何第三方依赖**（不用 pandas / numpy），且做了三处健壮性增强
（见设计文档 §6.2）：

1. 时间三类输入（字符串 / Excel 序列号 / datetime）由 ``timeutil.parse_time`` 统一处理；
2. 列映射宽容：必需列缺失给出**可操作的中文提示**（告诉管理员去哪补别名）；
3. 号码 / LAI·CI / 地址 缺失时**降级运行**而不是直接失败：
   无号码 → 单号码处理；无 LAI·CI → 以地址为簇键；地址也缺 → 以坐标指纹为簇键。

输出 ``Dataset`` 是全引擎唯一的数据形态；后续算法模块不再接触"表格"概念。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .contract import Dataset, Point, AnalysisError
from .timeutil import parse_time

# 单元格规整（与过滤器插件 core.clean_text 同口径：去 BOM、全角空格转半角、去首尾空白）
_BOM = "\ufeff"

_NUM_IN_TEXT = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

MAX_ROWS = 100000
MAX_COLS = 256

# 丢弃原因（统计口径固定字符串，前端直接展示）
DROP_EMPTY = "整行为空"
DROP_TIME = "时间缺失或无法识别"
DROP_GEO = "坐标缺失或无法识别"
DROP_LON = "经度低于下限"
DROP_LAT = "纬度低于下限"


# --------------------------------------------------------------------------
# 单元格规整
# --------------------------------------------------------------------------

def clean_text(v: Any) -> str:
    """比较 / 表头用文本规整：去 BOM、全角空格转半角、去首尾空白。"""
    if v is None:
        return ""
    s = v if isinstance(v, str) else cell_to_str(v)
    return s.replace(_BOM, "").replace("\u3000", " ").strip()


def cell_to_str(v: Any) -> str:
    """单元格转字符串；浮点整数值不写成 ``x.0``（防大整数号码被 float 化）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if v != v:                       # NaN
            return ""
        if float(v).is_integer():
            return str(int(v))
        return repr(v)
    return str(v)


def parse_float(v: Any) -> Optional[float]:
    """宽容地把单元格解析为 float（容忍 ``111.5526(aaa)`` 这类脏值）；失败返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f
    s = str(v).strip().replace("\u3000", " ").replace(",", "")
    if not s:
        return None
    try:
        f = float(s)
        return None if f != f else f
    except ValueError:
        pass
    m = _NUM_IN_TEXT.search(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 列映射（自检接口与正式分析共用，保证"看到的"与"用到的"一致）
# --------------------------------------------------------------------------

def resolve_columns(headers: Sequence[Any], schema: Dict[str, Any]
                    ) -> Tuple[Dict[str, int], List[str], List[str], Dict[str, str]]:
    """把真实表头映射到规范列名。

    匹配规则（与上游一致）：规整后忽略大小写，按别名列表顺序取第一个命中的表头。

    返回 ``(matched_index, missing_required, missing_optional, matched_header)``；
    ``matched_index`` 为 ``{规范列名: 列下标}``，``matched_header`` 为 ``{规范列名: 实际表头}``。
    """
    column_map = schema.get("column_map") or {}
    lookup: Dict[str, int] = {}
    for i, h in enumerate(headers):
        key = clean_text(h).upper()
        if key and key not in lookup:
            lookup[key] = i

    matched_index: Dict[str, int] = {}
    matched_header: Dict[str, str] = {}
    missing_required: List[str] = []
    missing_optional: List[str] = []

    for group, bucket in ((schema.get("required") or [], missing_required),
                          (schema.get("optional") or [], missing_optional)):
        for canonical in group:
            aliases = column_map.get(canonical) or [canonical]
            hit = None
            for alias in aliases:
                key = clean_text(alias).upper()
                if key in lookup:
                    hit = lookup[key]
                    break
            if hit is None:
                bucket.append(canonical)
            else:
                matched_index[canonical] = hit
                matched_header[canonical] = clean_text(headers[hit])

    return matched_index, missing_required, missing_optional, matched_header


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def build_dataset(rows: Sequence[Sequence[Any]], params: Dict[str, Any]) -> Dataset:
    """把过滤后的二维表（首行表头）构建为 ``Dataset``。

    抛 ``AnalysisError`` 的情形：空表 / 无表头 / 缺必需列 / 行列数超限。
    其余问题（脏数据、缺可选列）一律**降级运行**并记入 ``warnings`` 与 ``drop_stats``。
    """
    if not rows:
        raise AnalysisError("表格为空：没有可分析的数据行。")
    if len(rows) > MAX_ROWS + 1:
        raise AnalysisError("表格行数超过上限（最多 %d 行数据）。" % MAX_ROWS)
    headers = [clean_text(h) for h in rows[0]]
    if len(headers) > MAX_COLS:
        raise AnalysisError("表格列数超过上限（最多 %d 列）。" % MAX_COLS)
    if not any(headers):
        raise AnalysisError("表格缺少表头：首行必须为字段名称。")
    if len(headers) == 1 and len(rows) == 1:
        raise AnalysisError("表格只有表头，没有数据行。")

    schema = params["schema"]
    matched_index, missing_req, missing_opt, matched_header = resolve_columns(headers, schema)
    if missing_req:
        raise AnalysisError(
            "过滤后缺少必需的字段：%s。\n"
            "请在「⚙️ 配置 → 保留字段名单」中补上这些字段，并在「列映射」中为本表的表头"
            "补充别名；当前表格的表头为：%s"
            % ("、".join(missing_req), "、".join([h for h in headers if h]) or "（空）")
        )

    warnings: List[str] = []
    if missing_opt:
        warnings.append("以下可选字段未提供，将按降级方式处理：%s" % "、".join(missing_opt))

    clean = params["clean"]
    min_lon = float(clean["min_longitude"])
    min_lat = float(clean["min_latitude"])
    enable_min_lat = bool(clean["enable_min_latitude"])
    time_formats = params["report"].get("time_formats") or []

    i_time = matched_index["BEGINTIME"]
    i_lon = matched_index["LONGITUDE"]
    i_lat = matched_index["LATITUDE"]
    i_user = matched_index.get("USERNUM")
    i_addr = matched_index.get("ADDRESS")
    i_lai = matched_index.get("LAI")
    i_ci = matched_index.get("CI")
    i_sp = matched_index.get("SPCODE")

    def cell(row: Sequence[Any], idx: Optional[int]) -> Any:
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    drop: Dict[str, int] = {}
    raw_points: List[Point] = []
    all_users: List[str] = []
    seen_users = set()
    data_rows = rows[1:]

    for row in data_rows:
        if row is None:
            drop[DROP_EMPTY] = drop.get(DROP_EMPTY, 0) + 1
            continue
        if not isinstance(row, (list, tuple)):
            drop[DROP_EMPTY] = drop.get(DROP_EMPTY, 0) + 1
            continue
        if all(clean_text(v) == "" for v in row):
            drop[DROP_EMPTY] = drop.get(DROP_EMPTY, 0) + 1
            continue

        user = cell_to_str(cell(row, i_user)).strip()
        if user not in seen_users:
            seen_users.add(user)
            all_users.append(user)

        ts = parse_time(cell(row, i_time), time_formats)
        if ts is None:
            drop[DROP_TIME] = drop.get(DROP_TIME, 0) + 1
            continue
        lon = parse_float(cell(row, i_lon))
        lat = parse_float(cell(row, i_lat))
        if lon is None or lat is None:
            drop[DROP_GEO] = drop.get(DROP_GEO, 0) + 1
            continue
        if lon <= min_lon:
            drop[DROP_LON] = drop.get(DROP_LON, 0) + 1
            continue
        if enable_min_lat and lat <= min_lat:
            drop[DROP_LAT] = drop.get(DROP_LAT, 0) + 1
            continue

        lai = cell_to_str(cell(row, i_lai)).strip()
        ci = cell_to_str(cell(row, i_ci)).strip()
        address = cell_to_str(cell(row, i_addr)).strip()
        spcode = cell_to_str(cell(row, i_sp)).strip()
        raw_points.append(Point(ts=ts, lon=lon, lat=lat, usernum=user,
                                address=address, lai=lai, ci=ci, spcode=spcode,
                                cell=cell_key(lai, ci, address, lon, lat)))

    if i_user is None:
        all_users = [""]
        warnings.append("表格未提供号码列，已按单一号码处理（报告中以「（未标注号码）」呈现）。")

    n_cells = len({p.cell for p in raw_points})
    n_addresses = len({p.address for p in raw_points if p.address})

    users = _dedupe_users(raw_points, clean)
    if not users:
        warnings.append("所有数据行均无效（时间或坐标无法识别），无法生成轨迹结论。")
    elif len(all_users) == 1 and len(next(iter(users.values()))) < 20:
        warnings.append("有效轨迹点偏少，判断阈值可能不适配，结论仅供参考。")

    intervals = _intervals(users)
    dataset = Dataset(
        users=users,
        raw_points=_sort_raw(raw_points),
        all_usernums=all_users or [""],
        raw_rows=len(data_rows),
        valid_rows=len(raw_points),
        drop_stats=drop,
        n_cells=n_cells,
        n_addresses=n_addresses,
        median_interval_s=_median(intervals),
        schema={
            "matched": matched_header,
            "matched_index": matched_index,
            "missing_required": missing_req,
            "missing_optional": missing_opt,
            "headers": [h for h in headers if h],
        },
        warnings=warnings,
    )
    return dataset


def cell_key(lai: str, ci: str, address: str, lon: float, lat: float) -> str:
    """小区键：优先 LAI-CI，无则退化为地址，再退化为坐标指纹。"""
    if lai or ci:
        return "%s-%s" % (lai, ci)
    if address:
        return "ADDR:" + address
    return "GEO:%.3f,%.3f" % (lon, lat)


def _sort_raw(points: List[Point]) -> List[Point]:
    """清洗后的点按 (号码, 时间) 稳定排序。

    地点簇必须建立在**清洗后未去重**的这批行上：上游同样用清洗后的原始帧建簇，
    这样簇质心按"上报次数"加权、转移边按真实切换次数统计——与去重后的点序列
    口径不同，两者不能混用（混用会导致质心偏移、进而影响停留点边界）。
    同号码同秒的多行保持**文件原始顺序**（稳定排序），与上游行为一致。
    """
    return sorted(points, key=lambda p: (p.usernum, p.ts))


def _dedupe_users(raw_points: List[Point], clean: Dict[str, Any]) -> Dict[str, List[Point]]:
    """按号码分组并去重。

    完全复刻上游口径，保证与 SQLRewrite 对拍一致：
      ① 按 (ts 升序, lon 降序) 稳定排序（同时间保留较大经度）；
      ② 按 (ts, lon, lat, cell, address) 精确去重，组内保留最早；
      ③ 按 (网格, 经度指纹, 纬度指纹, ts, lon) 排序后，每个网格+指纹只保留第一条。

    网格与坐标指纹的引入，是为了把"信令突发上报"（同一点位秒级重复几十次）
    折叠成一个有效采样点；组内保留**最早**一条，宁可保守不引入后发值。
    """
    grid = int(clean["grid_seconds"])
    prec = int(clean["geo_precision"])
    buckets: Dict[str, List[Point]] = {}
    for p in raw_points:
        buckets.setdefault(p.usernum, []).append(p)

    out: Dict[str, List[Point]] = {}
    for user, pts in buckets.items():
        pts.sort(key=lambda p: (p.ts, -p.lon))
        seen = set()
        uniq: List[Point] = []
        for p in pts:
            k = (p.ts, p.lon, p.lat, p.cell, p.address)
            if k in seen:
                continue
            seen.add(k)
            uniq.append(p)

        if grid > 0:
            keyed = []
            for p in uniq:
                g = ((p.ts + grid // 2) // grid) * grid
                keyed.append((g, round(p.lon, prec), round(p.lat, prec), p.ts, p.lon, p))
            keyed.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4]))
            picked = {}
            for g, gl, ga, ts, lon, p in keyed:
                k2 = (g, gl, ga)
                if k2 not in picked:
                    picked[k2] = p
            uniq = list(picked.values())

        uniq.sort(key=lambda p: p.ts)
        out[user] = uniq
    return out


def _intervals(users: Dict[str, List[Point]]) -> List[float]:
    """所有相邻采样间隔（秒），用于数据质量报告。"""
    out: List[float] = []
    for pts in users.values():
        for i in range(1, len(pts)):
            out.append(float(pts[i].ts - pts[i - 1].ts))
    return out


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    n = len(data)
    mid = n // 2
    if n % 2:
        return float(data[mid])
    return float((data[mid - 1] + data[mid]) / 2.0)


def max_interval(users: Dict[str, List[Point]]) -> float:
    best = 0.0
    for pts in users.values():
        for i in range(1, len(pts)):
            d = float(pts[i].ts - pts[i - 1].ts)
            if d > best:
                best = d
    return best
