# -*- coding: utf-8 -*-
"""v3 算法：五态窗口栅格（引擎第二内置实现，与 v2 并列，``analysis.algo`` 可选）。

流水线（设计文档 `docs/design/轨迹速写v3算法-设计文档.md` §3）
--------------------------------------------------------------

    底座（复用 v2）：adapter 清洗去重 → cluster 地点簇 + attach（位置一律取簇质心）
      → windows.build_windows：5 分钟墙钟窗口（含空窗表）
      → votes.window_state：自身票 + 左/右对票（含单空窗桥接）→ classify 四行判定
      → aggregate.build_timeline：位移锚定切分 → 停留段/出行段/无数据段
      → _state_report：报告正文按**窗口状态聚合叙述**（五态命名，同态连续区间一条）

与 v2 的输出契约差异
--------------------
- ``stays`` / ``trips`` / ``report`` / ``clusters`` / ``cluster_stats`` 键与 v2 同构，
  前端与 Excel 导出无需适配；``trips`` 增补可选键 ``含无数据_分钟``；
- ``report`` 的条目与正文改为**状态聚合叙述**：失联 / 静默 / 缓行 / 快速 / 极速，
  同状态连续窗口聚合为一条（跨断档合并的停留含"其间失联"注记）；
  速度口径不变（净位移 ÷ 实际时间）；
- ``quality`` 增补 v3 专属键（``v3_状态分布`` / ``v3_断档区间数`` / ``v3_桥接次数`` 等）。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from ... import registry
from ...contract import Dataset, Stay, Trip
from ...timeutil import epoch_to_str, fmt_distance, fmt_duration, human_span
from ..v2 import cluster, staypoint
from ..v2 import report as report_mod          # 仅复用 addr_text 的地址文案规则
from . import aggregate, votes, windows

VERSION = "v3"


def run(dataset: Dataset, params: Dict[str, Any]) -> Dict[str, Any]:
    """执行 v3 分析。返回 ``{quality, stays, trips, report, clusters, cluster_stats, warnings}``。"""
    v3 = params["v3"]
    width = int(float(v3["window_minutes"]) * 60)
    creep = float(v3["creep_kmh"])
    fast = float(v3["fast_kmh"])
    guard = float(v3["guard_seconds"])
    stay_min_s = float(v3["stay_minutes"]) * 60.0

    # ---- 底座（复用 v2 阶段 A/B/C，位置一律取簇质心）----
    mapping, table, cstats = cluster.build_clusters(dataset, params)
    cluster.attach_clusters(dataset, mapping, table)
    noise = float(v3["noise_floor_m"]) or \
        float(staypoint.estimate_noise(dataset, params).get("noise_m", 0.0))
    high_m = float(params["trip"]["high_conf_factor"]) * noise   # 出行/位置变动分级线（同 v2 语义）

    # ---- 窗口化 + 三票 + 聚合（逐用户）----
    wins = windows.build_windows(dataset, width)
    stays: List[Stay] = []
    trips: List[Trip] = []
    per_user_items: Dict[str, List[Dict[str, Any]]] = {}
    state_counter_total: Dict[int, int] = {}
    bridge_total = 0
    void_seg_total = 0
    void_win_total = 0
    window_total = 0

    for user, uw in wins.items():
        wmin, wmax, info = uw["wmin"], uw["wmax"], uw["info"]
        if wmax < wmin:
            continue
        states: Dict[int, int] = {}
        for w in range(wmin, wmax + 1):
            st, _votes, br = votes.window_state(w, info, wmin, wmax, width,
                                                noise, creep, fast, guard)
            states[w] = st
            bridge_total += br
        window_total += wmax - wmin + 1

        tl = aggregate.build_timeline(wmin, wmax, info, states, noise, width,
                                      stay_min_s, high_m)
        for st, c in tl["state_counter"].items():
            state_counter_total[st] = state_counter_total.get(st, 0) + c
        void_runs = [r for r in tl["state_runs"] if r[0] == 0]   # 失联区间按状态栅格统计
        void_seg_total += len(void_runs)
        void_win_total += sum(r[2] - r[1] + 1 for r in void_runs)
        per_user_items[user] = tl["report_items"]

        for e in tl["events"]:
            if e["kind"] != "stay" or not e["pos"]:
                continue                      # 纯桥接段（无位置）：仅存在于病态边界，不入条目
            start_ts = e["first_ts"] if e["first_ts"] is not None else e["w0"] * width
            end_ts = e["last_ts"] if e["last_ts"] is not None else (e["w1"] + 1) * width
            stays.append(Stay(
                usernum=user, i0=e["w0"], i1=e["w1"],
                start_ts=int(start_ts), end_ts=int(end_ts),
                minutes=round(max(0.0, (end_ts - start_ts) / 60.0), 2),
                lon=round(e["pos"][0], 6), lat=round(e["pos"][1], 6),
                address=aggregate.window_address(info, e["w0"], e["w1"]),
                n_points=e["n_points"], jitter_m=0.0,
            ))
        for t in tl["trips"]:
            trip = Trip(
                usernum=user, start_ts=int(t["start_ts"]), end_ts=int(t["end_ts"]),
                minutes=round(t["minutes"], 2), net_m=round(t["net_m"], 1),
                cum_m=round(t["cum_m"], 1), straightness=t["straightness"], revisit=0,
                speed_kmh=round(t["speed_kmh"], 2), kind=t["kind"],
                confidence=t["confidence"],
                from_addr=t["from_addr"], to_addr=t["to_addr"],
                from_lon=round(t["from_pos"][0], 6), from_lat=round(t["from_pos"][1], 6),
                to_lon=round(t["to_pos"][0], 6), to_lat=round(t["to_pos"][1], 6),
                n_points=t["n_points"],
            )
            trip.含无数据_分钟 = round(float(t["void_minutes"]), 1)
            trips.append(trip)

    # ---- 报告：按窗口状态聚合叙述 ----
    rep = _state_report(dataset, params, per_user_items, width)

    quality = _quality(dataset, params, table, noise, cstats,
                       window_total, state_counter_total, bridge_total,
                       void_seg_total, void_win_total)
    quality["停留点数_合并后"] = len(stays)
    quality["出行段数"] = len(trips)

    return {
        "quality": quality,
        "stays": _stays_json(stays),
        "trips": _trips_json(trips),
        "report": rep,
        "clusters": staypoint.cluster_table_to_json(table),
        "cluster_stats": cstats,
        "warnings": _warnings(state_counter_total, window_total),
    }


# --------------------------------------------------------------------------
# 报告：按窗口状态聚合叙述（同态连续区间一条，五态命名）
# --------------------------------------------------------------------------

def _state_report(dataset: Dataset, params: Dict[str, Any],
                  per_user_items: Dict[str, List[Dict[str, Any]]], width: int) -> Dict[str, Any]:
    rcfg = params["report"]
    header = str(rcfg.get("header") or "")
    footer = str(rcfg.get("footer") or "")
    append_summary = bool(rcfg.get("append_summary", True))
    split_day = bool(rcfg.get("split_by_day"))

    users = list(per_user_items.keys())
    entries_out: List[Dict[str, Any]] = []
    text_by_user: Dict[str, str] = {}
    user_summaries: Dict[str, str] = {}
    state_counter: Counter = Counter()
    first_ts_all: List[int] = []
    last_ts_all: List[int] = []

    for user in users:
        items = per_user_items[user]
        display = user or "（未标注号码）"
        user_summaries[user] = _user_summary(items)
        lines: List[str] = []
        cur_day: Any = None
        for idx, it in enumerate(items, start=1):
            state_counter[it["state"]] += 1
            first_ts_all.append(it["first_ts"]); last_ts_all.append(it["last_ts"])
            full_start = epoch_to_str(it["first_ts"], "%Y-%m-%d %H:%M")
            full_end = epoch_to_str(it["last_ts"], "%Y-%m-%d %H:%M")
            if split_day:
                day = epoch_to_str(it["first_ts"], "%Y-%m-%d")
                if day != cur_day:
                    lines.append("    【%s】" % day)
                    cur_day = day
                show_start, show_end = full_start[5:], full_end[5:]
            else:
                show_start, show_end = full_start, full_end
            text = _state_line(idx, it, show_start, show_end)
            lines.append(text)
            entries_out.append({
                "USERNUM": user, "序号": idx,
                "开始时间": full_start, "结束时间": full_end,
                "kind": votes.STATE_NAMES[it["state"]], "confidence": "",
                "minutes": round(float(it["minutes"]), 2),
                "net_m": round(float(it["net_m"])),
                "speed_kmh": round(float(it["speed_kmh"]), 2),
                "text": text.strip(),
            })
        chunks: List[str] = []
        if header:
            chunks += [header, ""]
        if append_summary:
            chunks += [user_summaries[user], ""]
        chunks.append("    号码%s的活动轨迹如下：" % display)
        chunks.append("\n".join(lines) if lines else "    无。")
        if footer:
            chunks += ["", "    " + footer]
        text_by_user[user] = "\n".join(chunks)

    span = human_span(min(first_ts_all), max(last_ts_all)) if first_ts_all else ""
    summary = {
        "号码数": len(users),
        "停留点数": state_counter.get(1, 0),          # 静默段数（v3 状态口径）
        "有效出行次数": state_counter.get(3, 0),       # 快速段数
        "位置变动次数": state_counter.get(2, 0),       # 缓行段数
        "极速段数": state_counter.get(4, 0),
        "失联段数": state_counter.get(0, 0),
        "最远位移_米": int(round(max([it["net_m"] for its in per_user_items.values()
                                      for it in its if it["state"] in (2, 3, 4)] or [0]))),
        "最长停留_分钟": round(max([it["minutes"] for its in per_user_items.values()
                                    for it in its if it["state"] == 1] or [0]), 1),
        "时间跨度": span,
        "时间范围": [epoch_to_str(min(first_ts_all)), epoch_to_str(max(last_ts_all))]
                    if first_ts_all else [],
        "报告条目数": len(entries_out),
    }
    return {"entries": entries_out, "text_by_user": text_by_user,
            "user_summaries": user_summaries, "summary": summary}


def _state_line(idx: int, it: Dict[str, Any], show_start: str, show_end: str) -> str:
    """单条状态叙述（速度口径与窗口票一致：净位移 ÷ 实际时间）。"""
    head = "    （%d）%s至%s" % (idx, show_start, show_end)
    name = votes.STATE_NAMES[it["state"]]
    dur = fmt_duration(it["minutes"])
    if it["state"] == 0:
        return head + "，失联" + dur + "。"
    if it["state"] == 1:
        line = head + "，静默" + dur + "，在【" + _addr(it) + "附近】"
        if it.get("void_minutes"):
            line += "，其间失联 %.0f 分钟" % it["void_minutes"]
        return line + "。"
    dist = "距离 " + fmt_distance(it["net_m"])
    speed = "平均速度 %s 公里/小时" % _trim_speed(it["speed_kmh"])
    addr_from = report_mod.addr_text(it.get("addr_from"), None, None) \
        if it.get("addr_from") else _addr_at(it, "pos_first")
    addr_to = report_mod.addr_text(it.get("addr_to"), None, None) \
        if it.get("addr_to") else _addr_at(it, "pos_last")
    if it["net_m"] < 1:                           # 单点过渡窗：位移不可估
        return head + "，" + name + dur + "（单点定位，位移不可估），在【" + addr_from + "附近】。"
    if addr_from == addr_to:
        return head + "，" + name + dur + "，在【" + addr_from + "附近】移动，" + dist + "，" + speed + "。"
    return head + "，" + name + dur + "，从【" + addr_from + "附近】到【" + addr_to + "附近】，" + dist + "，" + speed + "。"


def _addr(it: Dict[str, Any]) -> str:
    return report_mod.addr_text(it.get("addr"), None, None)


def _addr_at(it: Dict[str, Any], key: str) -> str:
    pos = it.get(key)
    if pos is None:
        return report_mod.addr_text(it.get("addr"), None, None)
    return report_mod.addr_text("", pos[0], pos[1])


def _trim_speed(v: float) -> str:
    s = ("%.1f" % float(v)).rstrip("0").rstrip(".")
    return s or "0"


def _user_summary(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "【速写】无可用的定位上报记录。"
    cnt = Counter(it["state"] for it in items)
    parts: List[str] = []
    for st in (0, 1, 2, 3, 4):
        if cnt.get(st):
            parts.append("%s %d 段" % (votes.STATE_NAMES[st], cnt[st]))
    span = human_span(min(it["first_ts"] for it in items),
                      max(it["last_ts"] for it in items))
    parts.append("时间跨度 %s" % span)
    return "【速写】" + "，".join(parts) + "。"


# --------------------------------------------------------------------------
# 质量与序列化
# --------------------------------------------------------------------------

def _quality(dataset: Dataset, params: Dict[str, Any], table: Dict[str, Any],
             noise: float, cstats: Dict[str, int], window_total: int,
             state_counter: Dict[int, int], bridge_total: int,
             void_seg_total: int, void_win_total: int) -> Dict[str, Any]:
    ts_all = [p.ts for pts in dataset.users.values() for p in pts]
    valid = dataset.valid_rows or 1
    return {
        "读入行数": dataset.raw_rows,
        "有效行数": dataset.valid_rows,
        "丢弃行数": dataset.raw_rows - dataset.valid_rows,
        "有效率": round(dataset.valid_rows / valid, 4) if dataset.raw_rows else 0.0,
        "丢弃原因": dict(dataset.drop_stats),
        "号码数": len(dataset.all_usernums),
        "时间范围": [epoch_to_str(min(ts_all)), epoch_to_str(max(ts_all))] if ts_all else [],
        "去重后轨迹点数": sum(len(v) for v in dataset.users.values()),
        "小区数": dataset.n_cells,
        "地点簇数": len(table),
        "地址数": dataset.n_addresses,
        "簇合并统计": {"按地址": cstats.get("by_address", 0),
                       "共址": cstats.get("by_cosite", 0),
                       "双向切换": cstats.get("by_handover", 0)},
        "估计定位噪声半径_米": int(round(noise)),
        "算法版本": params.get("algo", VERSION),
        "v3_窗口数": window_total,
        "v3_状态分布": {votes.STATE_NAMES[k]: v
                        for k, v in sorted(state_counter.items(), reverse=True)},
        "v3_桥接次数": bridge_total,
        "v3_断档区间数": void_seg_total,
        "v3_断档窗数": void_win_total,
        "质量提示": [],
    }


def _warnings(state_counter: Dict[int, int], window_total: int) -> List[str]:
    w: List[str] = []
    if window_total:
        void_ratio = state_counter.get(0, 0) / float(window_total)
        if void_ratio > 0.5:
            w.append("无数据窗口占比 %.0f%%，数据较稀疏，结论仅覆盖有上报的区间。"
                     % (void_ratio * 100))
    return w


def _stays_json(stays: List[Stay]) -> List[Dict[str, Any]]:
    """与 v2 ``_stays_json`` 字段完全同构（前端 / Excel 导出零适配）。"""
    out = []
    for s in stays:
        out.append({
            "USERNUM": s.usernum,
            "进入时间": epoch_to_str(s.start_ts),
            "离开时间": epoch_to_str(s.end_ts),
            "停留分钟": round(float(s.minutes), 1),
            "停留时长": fmt_duration(s.minutes),
            "经度": s.lon, "纬度": s.lat,
            "代表地址": s.address,
            "地点簇": s.clu,
            "涉及坐标点数": s.n_points,
            "抖动半径_米": int(round(s.jitter_m)),
        })
    return out


def _trips_json(trips: List[Trip]) -> List[Dict[str, Any]]:
    """与 v2 ``_trips_json`` 字段同构；增补可选键 ``含无数据_分钟``。"""
    out = []
    for t in trips:
        out.append({
            "USERNUM": t.usernum,
            "开始时间": epoch_to_str(t.start_ts),
            "结束时间": epoch_to_str(t.end_ts),
            "时长分钟": round(float(t.minutes), 1),
            "时长": fmt_duration(t.minutes),
            "净位移_米": int(round(t.net_m)),
            "累计位移_米": int(round(t.cum_m)),
            "距离": fmt_distance(t.net_m),
            "直线度": round(float(t.straightness), 3),
            "回访次数": int(t.revisit),
            "均速_公里每小时": round(float(t.speed_kmh), 1),
            "判定": t.kind,
            "置信度": t.confidence,
            "起点": t.from_addr, "终点": t.to_addr,
            "起点经度": t.from_lon, "起点纬度": t.from_lat,
            "终点经度": t.to_lon, "终点纬度": t.to_lat,
            "途经点数": t.n_points,
            "含无数据_分钟": round(float(getattr(t, "含无数据_分钟", 0.0) or 0.0), 1),
        })
    return out


registry.register(VERSION, run)
