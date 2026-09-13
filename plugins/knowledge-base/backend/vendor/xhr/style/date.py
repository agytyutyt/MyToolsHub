"""日期处理 —— 对应 TS 版 ``packages/style/src/date.ts``。

⚠️ 这是最容易翻车的地方之一：
  · Excel 错误地认为 1900 年是闰年（序列号 60 = 不存在的 1900-02-29）
  · 存在 1904 日期系统（Mac 版 Excel 传统），workbookPr@date1904="1"

Python 版差异：IR 内日期值就是序列号（number），不存在 Date 反算问题
（TS 版第 13.2 节修正的场景从源头消除）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Tuple

#: 1900 系统的"零点"（已把 Excel 的 1900 闰年 bug 折算进去）
_EPOCH_1900 = datetime(1899, 12, 30, tzinfo=timezone.utc)
#: 1904 系统零点
_EPOCH_1904 = datetime(1904, 1, 1, tzinfo=timezone.utc)
DAY_MS = 86_400_000
DAY = timedelta(days=1)


def serial_to_date(serial: float, date1904: bool = False) -> datetime:
    """Excel 日期序列号 → datetime（UTC，显示语义）。

    序列号 1 = 1900-01-01；59 = 1900-02-28；61 = 1900-03-01。
    """
    if date1904:
        return _EPOCH_1904 + timedelta(days=serial)
    # <60 的序列号要多加一天，才对得上真实日历
    days = serial + 1 if serial < 60 else serial
    return _EPOCH_1900 + timedelta(days=days)


def date_to_serial(d: datetime, date1904: bool = False) -> float:
    """datetime → Excel 序列号（1900 系统）。"""
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    epoch = _EPOCH_1904 if date1904 else _EPOCH_1900
    return (d - epoch).total_seconds() / 86400.0


#: 1904 系统序列号 ↔ 1900 系统序列号的偏移量
DATE1904_OFFSET = 1462


def to_serial_1900(serial: float, date1904: bool) -> float:
    """把任意序列号统一换算到 1900 系统（供 numfmt 使用）。"""
    return serial + DATE1904_OFFSET if date1904 else serial


def is_date_format(code: str) -> bool:
    """判断一个数字格式码是否表示日期/时间。

    做法：先剥离字面量、颜色、条件、转义、占位填充，再看是否含日期占位符。
    ⚠️ 累计时长段 [h]/[m]/[s]（含 [hh] 等宽度写法）本身就是日期时间语义，
       必须在剥括号**之前**判断 —— 否则裸 '[h]' / '[m]' 会被剥成空串而误判为
       数字格式（压力测试发现，Excel 实际显示累计时长）。
    """
    if not code:
        return False
    if re.search(r"\[[hms]+\]", code, re.IGNORECASE):
        return True
    stripped = code
    stripped = re.sub(r"\[[^\]]*\]", "", stripped)   # [Red] [h] [$-409] [>=100]
    stripped = re.sub(r'"[^"]*"', "", stripped)      # "字面量"
    stripped = re.sub(r"\\.", "", stripped)          # \转义
    stripped = re.sub(r"_.", "", stripped)           # _ 留空
    stripped = re.sub(r"\*.", "", stripped)          # * 填充
    if "%" in stripped:
        return False
    return bool(re.search(r"[ymdhs]", stripped, re.IGNORECASE))


def has_time_part(code: str) -> bool:
    stripped = re.sub(r"\[[^\]]*\]", "", code or "")
    stripped = re.sub(r'"[^"]*"', "", stripped)
    stripped = re.sub(r"\\.", "", stripped)
    return bool(re.search(r"[hs]", stripped, re.IGNORECASE) or re.search(r"AM/PM|A/P", stripped, re.IGNORECASE))


def has_date_part(code: str) -> bool:
    stripped = re.sub(r"\[[^\]]*\]", "", code or "")
    stripped = re.sub(r'"[^"]*"', "", stripped)
    stripped = re.sub(r"\\.", "", stripped)
    return bool(re.search(r"[ymd]", stripped, re.IGNORECASE))


def is_text_format(code: str) -> bool:
    """格式码是否表示文本（@）—— 决定是否按文本左对齐。"""
    return bool(re.search(r"@", re.sub(r'"[^"]*"', "", code or "")))


def is_numeric_format(code: str) -> bool:
    """格式码是否表示"数值"（用于 general 对齐推断）。百分比、货币、千分位都算。"""
    if is_date_format(code):
        return False
    stripped = re.sub(r'"[^"]*"', "", code or "")
    stripped = re.sub(r"\\.", "", stripped)
    stripped = re.sub(r"_.", "", stripped)
    return bool(re.search(r"[0#?]", stripped) or re.search(r"%", stripped))
