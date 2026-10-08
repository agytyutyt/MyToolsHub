# -*- coding: utf-8 -*-
"""时间解析与格式化（纯标准库）。

比上游 SQLRewrite 增强的地方（见设计文档 §6.2）：

- 支持三类输入：字符串（多格式尝试）/ Excel 日期序列号（数值或数字串）/ ``datetime`` 对象；
- 支持 14 位「YYYYMMDDHHMMSS」纯数字串与纪元秒纯数字串；
- 解析失败**不抛异常**，返回 ``None`` 由调用方计为「时间无效」并纳入丢弃统计。

时间一律以**本地时区纪元秒（int）**为内部表示，与上游保持一致，方便对拍。
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

# Excel（1900 日期系统）序列号的零点；该基准已内含 1900-02-29 的历史 bug 处理
EXCEL_EPOCH = datetime(1899, 12, 30)

# 内置时间格式（可在配置 report/clean 之外通过 params 追加）
DEFAULT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
    "%d/%m/%Y %H:%M:%S",
)

_NUM_RE = re.compile(r"^\d{4,14}(\.\d+)?$")
_CLEAN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _from_serial(v: float) -> Optional[int]:
    """Excel 日期序列号 → 纪元秒。"""
    if not (40000.0 <= v < 80000.0):        # 约 2009-05 ~ 2119-01
        return None
    try:
        dt = EXCEL_EPOCH + timedelta(days=float(v))
    except (OverflowError, ValueError):
        return None
    return int(time.mktime(dt.timetuple()))


def _from_number(v: float) -> Optional[int]:
    """数值型时间的启发式解析。"""
    if v != v or v in (float("inf"), float("-inf")):     # NaN / inf
        return None
    iv = int(v)
    digits = len(str(iv))
    if digits == 14 and 1e12 <= v < 1e14:                # YYYYMMDDHHMMSS
        return _from_compact(str(iv))
    if 1e9 <= v < 4e9:                                   # 纪元秒（2001-2096）
        return int(v)
    return _from_serial(v)                               # Excel 序列号


def _from_compact(s: str) -> Optional[int]:
    """纯数字紧凑时间串：14 位 YYYYMMDDHHMMSS / 8 位 YYYYMMDD。"""
    fmt = "%Y%m%d%H%M%S" if len(s) == 14 else ("%Y%m%d" if len(s) == 8 else None)
    if not fmt:
        return None
    try:
        return int(time.mktime(datetime.strptime(s, fmt).timetuple()))
    except ValueError:
        return None


def parse_time(value, formats: Optional[Iterable[str]] = None) -> Optional[int]:
    """把任意单元格值解析为本地时区纪元秒；无法解析返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return int(time.mktime(value.timetuple()))
    if isinstance(value, date):
        return int(time.mktime(datetime(value.year, value.month, value.day).timetuple()))
    if isinstance(value, (int, float)):
        return _from_number(float(value))

    s = str(value).strip()
    if not s:
        return None
    # 纯数字：可能是 Excel 序列号 / 纪元秒 / 紧凑格式
    if _NUM_RE.match(s):
        n = float(s)
        r = _from_number(n)
        if r is not None:
            return r
        r = _from_compact(s.split(".")[0])
        if r is not None:
            return r
        return None
    # 带毫秒的字串：截到秒再解析
    m = _CLEAN_RE.match(s)
    candidates = [s]
    if m:
        candidates.insert(0, m.group(1))
    for fmt in tuple(formats or ()) + DEFAULT_FORMATS:
        for cand in candidates:
            try:
                return int(time.mktime(datetime.strptime(cand, fmt).timetuple()))
            except ValueError:
                continue
    return None


def epoch_to_dt(ts: int) -> datetime:
    """纪元秒 → 本地 datetime。"""
    return datetime.fromtimestamp(int(ts))


def epoch_to_str(ts: int, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """纪元秒 → 字符串。"""
    try:
        return epoch_to_dt(ts).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        return ""


def epoch_to_short(ts: int) -> str:
    """纪元秒 → ``YYYY-MM-DD HH:MM``（报告主用形态）。"""
    return epoch_to_str(ts, "%Y-%m-%d %H:%M")


def fmt_duration(minutes: float) -> str:
    """时长自适应：< 60 分钟用分钟，否则用小时（1 位小数，去掉多余的 .0）。"""
    minutes = float(minutes)
    if minutes < 60:
        return "%d分钟" % int(round(minutes))
    return _trim(minutes / 60.0, 1) + "小时"


def fmt_distance(meters: float) -> str:
    """距离自适应：< 1 km 用米，< 10 km 用公里（1 位小数），否则整数公里。"""
    m = float(meters)
    if m < 1000:
        return "%d米" % int(round(m))
    if m < 10000:
        return _trim(m / 1000.0, 1) + "公里"
    return "%d公里" % int(round(m / 1000.0))


def fmt_speed(kmh: float) -> str:
    """速度：整数（与上游报告口径一致）。"""
    try:
        return str(int(round(float(kmh))))
    except (TypeError, ValueError):
        return "0"


def _trim(v: float, digits: int) -> str:
    """定点小数并去掉尾随 0 与小数点（2.0 → "2"，2.50 → "2.5"）。"""
    return (("%." + str(digits) + "f") % float(v)).rstrip("0").rstrip(".")


def human_span(start_ts: int, end_ts: int) -> str:
    """时间跨度的人读描述（用于速写摘要）。"""
    seconds = max(0, int(end_ts) - int(start_ts))
    hours = seconds / 3600.0
    if hours < 1:
        return "%d分钟" % int(round(seconds / 60.0))
    if hours < 48:
        return _trim(hours, 1) + "小时"
    return "%d天" % int(round(hours / 24.0))
