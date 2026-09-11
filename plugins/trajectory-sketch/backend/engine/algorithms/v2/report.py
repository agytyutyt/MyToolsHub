# -*- coding: utf-8 -*-
"""v2 · 阶段 F：报告生成（速写报告）。

职责边界（解耦约束 D-6）：**只负责"把结论写成人话"**，不做任何几何判定。
判定结果由 ``cluster`` / ``staypoint`` / ``trip`` 三个模块产出，本模块只做四件事：

1. 停留点与出行段 → 按时间排序的**条目序列**（``Entry``）；
2. 时间轴闭合：可选把「无定位上报」的空档显式标注出来（``report.fill_holes``）；
3. 句式渲染（``entry_text``）与单位自适应（米 / 公里、分钟 / 小时）；
4. 速写摘要（每个号码一行「停留 N 处、出行 N 次、最远位移…」）。

因此后续要改文案、改单位口径、改分节方式，只需改本文件的模板段；
要改算法口径，只需改另外三个模块——两者互不影响（这正是解耦的收益）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...contract import Dataset, Entry, Stay, Trip, KIND_GAP, KIND_SHIFT, KIND_STAY, KIND_TRIP
from ...timeutil import epoch_to_str, fmt_distance, fmt_duration, human_span


# --------------------------------------------------------------------------
# 条目拼装
# --------------------------------------------------------------------------

def build_entries(stays: List[Stay], trips: List[Trip]) -> List[Entry]:
    """停留点 + 出行段 → 统一条目，并按 (号码, 开始时间, 结束时间) 排序。"""
    rows: List[Entry] = []
    for s in stays:
        rows.append(Entry(usernum=s.usernum, start_ts=s.start_ts, end_ts=s.end_ts,
                          minutes=s.minutes, kind=KIND_STAY, confidence="",
                          from_addr=s.address, from_lon=s.lon, from_lat=s.lat))
    for t in trips:
        rows.append(Entry(usernum=t.usernum, start_ts=t.start_ts, end_ts=t.end_ts,
                          minutes=t.minutes, kind=t.kind, confidence=t.confidence,
                          from_addr=t.from_addr, to_addr=t.to_addr,
                          net_m=t.net_m, speed_kmh=t.speed_kmh,
                          straightness=t.straightness,
                          from_lon=t.from_lon, from_lat=t.from_lat,
                          to_lon=t.to_lon, to_lat=t.to_lat))
    rows.sort(key=lambda e: (e.usernum, e.start_ts, e.end_ts))
    return rows


def fill_holes(entries: List[Entry], gap_seconds: float) -> List[Entry]:
    """把同一号码相邻条目之间超过阈值的空档标注为「无数据」条目（时间轴闭合）。"""
    if not entries or gap_seconds <= 0:
        return entries
    extra: List[Entry] = []
    by_user: Dict[str, List[Entry]] = {}
    for e in entries:
        by_user.setdefault(e.usernum, []).append(e)
    for user, lst in by_user.items():
        lst.sort(key=lambda e: e.start_ts)
        for prev, nxt in zip(lst, lst[1:]):
            hole = int(nxt.start_ts) - int(prev.end_ts)
            if hole >= gap_seconds:
                extra.append(Entry(usernum=user, start_ts=int(prev.end_ts),
                                   end_ts=int(nxt.start_ts), minutes=hole / 60.0,
                                   kind=KIND_GAP,
                                   from_lon=prev.to_lon if prev.to_lon is not None else prev.from_lon,
                                   from_lat=prev.to_lat if prev.to_lat is not None else prev.from_lat))
    if not extra:
        return entries
    out = entries + extra
    out.sort(key=lambda e: (e.usernum, e.start_ts, e.end_ts))
    return out


# --------------------------------------------------------------------------
# 句式模板
# --------------------------------------------------------------------------

def addr_text(addr: str, lon: Optional[float], lat: Optional[float]) -> str:
    """地点描述：优先地址原文，缺失时退回坐标，再缺失则写「未知地点」。"""
    a = (addr or "").strip()
    if a:
        return a
    if lon is not None and lat is not None:
        return "坐标%.5f,%.5f" % (float(lon), float(lat))
    return "未知地点"


def entry_text(e: Entry, start_s: str, end_s: str, idx: int) -> str:
    """单条轨迹描述（句式即本插件的"报告口径"，改动需同步 README 示例）。"""
    head = "    （%d）%s至%s" % (idx, start_s, end_s)
    dur = fmt_duration(e.minutes)
    if e.kind == KIND_STAY:
        return head + "，停留" + dur + "，在【" + addr_text(e.from_addr, e.from_lon, e.from_lat) + "附近】"
    if e.kind == KIND_TRIP:
        return (head + "，运动中" + dur
                + "，从【" + addr_text(e.from_addr, e.from_lon, e.from_lat) + "附近】运动到【"
                + addr_text(e.to_addr, e.to_lon, e.to_lat) + "附近】，距离" + fmt_distance(e.net_m)
                + "，平均速度" + str(int(round(e.speed_kmh))) + "公里/小时")
    if e.kind == KIND_SHIFT:
        return (head + "，位置变动" + dur
                + "，从【" + addr_text(e.from_addr, e.from_lon, e.from_lat) + "附近】到【"
                + addr_text(e.to_addr, e.to_lon, e.to_lat) + "附近】，距离" + fmt_distance(e.net_m)
                + "（速度" + trim1(e.speed_kmh) + "公里/小时，疑似基站切换或步行）")
    return head + "，无定位上报"


def trim1(v: float) -> str:
    return ("%.1f" % float(v)).rstrip("0").rstrip(".")


def user_summary(entries: List[Entry]) -> str:
    """单个号码的速写摘要行。"""
    stays = [e for e in entries if e.kind == KIND_STAY]
    trips = [e for e in entries if e.kind == KIND_TRIP]
    shifts = [e for e in entries if e.kind == KIND_SHIFT]
    if not entries:
        return "【速写】无可用的定位上报记录。"
    parts: List[str] = []
    if stays:
        longest = max(e.minutes for e in stays)
        parts.append("停留 %d 处（最长 %s）" % (len(stays), fmt_duration(longest)))
    if trips:
        parts.append("有效出行 %d 次" % len(trips))
    if shifts:
        parts.append("位置变动 %d 次" % len(shifts))
    disp = [e.net_m for e in entries if e.kind in (KIND_TRIP, KIND_SHIFT)]
    if disp:
        parts.append("最远位移 %s" % fmt_distance(max(disp)))
    span = human_span(min(e.start_ts for e in entries), max(e.end_ts for e in entries))
    parts.append("时间跨度 %s" % span)
    return "【速写】" + "，".join(parts) + "。"


# --------------------------------------------------------------------------
# 报告装配
# --------------------------------------------------------------------------

def run_report(dataset: Dataset, params: Dict[str, Any], stays: List[Stay],
               trips: List[Trip], quality_note: str = "") -> Dict[str, Any]:
    """产出报告正文、条目明细与摘要。

    返回::

        {"entries": [含 序号/开始时间/结束时间/text 的 dict],
         "text_by_user": {号码: 完整正文},
         "user_summaries": {号码: 摘要行},
         "summary": {...全局统计...}}
    """
    rcfg = params["report"]
    gap_s = float(params["clean"]["max_gap_seconds"])
    fill = bool(rcfg["fill_holes"])
    split_day = bool(rcfg["split_by_day"])
    with_summary = bool(rcfg["append_summary"])
    header = str(rcfg["header"] or "")
    footer = str(rcfg["footer"] or "")

    base_entries = build_entries(stays, trips)
    if fill:
        base_entries = fill_holes(base_entries, gap_s)

    by_user: Dict[str, List[Entry]] = {}
    for e in base_entries:
        by_user.setdefault(e.usernum, []).append(e)

    users = _sorted_users(dataset, by_user)
    entries_out: List[Dict[str, Any]] = []
    text_by_user: Dict[str, str] = {}
    user_summaries: Dict[str, str] = {}

    for user in users:
        lst = by_user.get(user, [])
        display = user or "（未标注号码）"
        summary = user_summary(lst)
        user_summaries[user] = summary

        lines: List[str] = []
        cur_day: Optional[str] = None
        for idx, e in enumerate(lst, start=1):
            full_start = epoch_to_str(e.start_ts, "%Y-%m-%d %H:%M")
            full_end = epoch_to_str(e.end_ts, "%Y-%m-%d %H:%M")
            if split_day:
                day = epoch_to_str(e.start_ts, "%Y-%m-%d")
                if day != cur_day:
                    lines.append("    【%s】" % day)
                    cur_day = day
                show_start = full_start[5:]
                show_end = full_end[5:]
            else:
                show_start = full_start
                show_end = full_end
            text = entry_text(e, show_start, show_end, idx)
            lines.append(text)
            entries_out.append({
                "USERNUM": user, "序号": idx,
                "开始时间": full_start, "结束时间": full_end,
                "kind": e.kind, "confidence": e.confidence,
                "minutes": round(float(e.minutes), 2),
                "net_m": round(float(e.net_m)), "speed_kmh": round(float(e.speed_kmh), 2),
                "text": text.strip(),
            })

        body = ("\n".join(lines) if lines else "    无。")
        chunks: List[str] = []
        if header:
            chunks.append(header)
            chunks.append("")
        if with_summary:
            chunks.append(summary)
            chunks.append("")
        chunks.append("    号码%s的活动轨迹如下：" % display)
        chunks.append(body)
        if quality_note:
            chunks.append("")
            chunks.append("    " + quality_note)
        if footer:
            chunks.append("")
            chunks.append("    " + footer)
        text_by_user[user] = "\n".join(chunks)

    summary = _global_summary(dataset, users, by_user, stays, trips, base_entries)
    return {"entries": entries_out, "text_by_user": text_by_user,
            "user_summaries": user_summaries, "summary": summary}


def _sorted_users(dataset: Dataset, by_user: Dict[str, List[Entry]]) -> List[str]:
    """号码顺序：以表格中出现的先后为准，绝不遗漏（无有效数据的号码也出「无。」）。"""
    out: List[str] = []
    for u in dataset.all_usernums:
        if u not in out:
            out.append(u)
    for u in by_user:
        if u not in out:
            out.append(u)
    return out


def _global_summary(dataset: Dataset, users: List[str], by_user: Dict[str, List[Entry]],
                    stays: List[Stay], trips: List[Trip], entries: List[Entry]) -> Dict[str, Any]:
    starts = [e.start_ts for e in entries]
    ends = [e.end_ts for e in entries]
    real_trips = [t for t in trips if t.kind == KIND_TRIP]
    shifts = [t for t in trips if t.kind == KIND_SHIFT]
    return {
        "号码数": len(users),
        "停留点数": len(stays),
        "有效出行次数": len(real_trips),
        "位置变动次数": len(shifts),
        "最远位移_米": int(round(max([t.net_m for t in trips] or [0]))),
        "最长停留_分钟": round(max([s.minutes for s in stays] or [0]), 1),
        "报告条目数": len(entries),
        "时间跨度": human_span(min(starts), max(ends)) if starts else "",
        "时间范围": [epoch_to_str(min(starts)), epoch_to_str(max(ends))] if starts else [],
        "去重后轨迹点数": sum(len(v) for v in dataset.users.values()),
    }
