# -*- coding: utf-8 -*-
"""v3 · 聚合：把窗口状态栅格切成停留段 / 出行段 / 无数据段。

口径（设计文档 §3.7）
--------------------
窗口状态只负责栅格叙事；**条目由位移切分**，不按状态切分——否则短途出行会被
缓行窗吞掉（与 v2 已修复的缺陷同型，是 v3 立项红线之一）。

1. 位移锚定切分：非失联窗按顺序扫描，开段记锚点（首个非空窗位置）；后续窗口位置
   偏离锚点 ≤ 噪声 → 延续；> 噪声 → 闭段开新段。**桥接空窗（有态无位置）延续当前段
   并计入时长**；失联窗（000）闭段并累计无数据段。
2. 停留段：段时长 ≥ ``stay_minutes``；代表位置 = 段内非空窗位置均值；
   条目时间取段内实际首末点时刻（非窗口边界，避免 0 分钟出行条目）。
3. 跨断档合并：相邻两停留段之间仅隔无数据段/纯桥接段、且位置差 ≤ 噪声 → 合并为
   一次停留（段内断档窗数累计注记）。
4. 出行段：相邻两停留段之间全部窗口（含短段与未桥接空窗），净位移 = 两停留位置差；
   累计位移 = 锚点间窗质心路径长度；含未桥接空窗时记录注记分钟数。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...geo import haversine
from ...stats import mode_str


def build_timeline(wmin: int, wmax: int, info: Dict[int, Optional[Dict[str, Any]]],
                   states: Dict[int, int], noise_m: float, width_s: int,
                   stay_min_s: float, high_m: float) -> Dict[str, Any]:
    """单号码聚合。返回::

        {"events":  [条目序列：{"kind": "stay"/"void", "w0","w1","pos",
                                "first_ts","last_ts","addrs","n_points","void_windows"}],
         "trips":   [{"w0","w1","net_m","cum_m","straightness","start_ts","end_ts",
                      "minutes","speed_kmh","kind","confidence","void_minutes",
                      "from_pos","to_pos"}],
         "state_runs":  [(状态, w0, w1)],          # 连续同态区间（报告附录）
         "bridge_count": int, "state_counter": {状态: 窗数}}
    """
    # ---- 位移锚定切分 ----
    raw: List[list] = []                           # ['seg', w0, w1, positions, anchor, first_ne, last_ne]
    cur: Optional[list] = None
    for w in range(wmin, wmax + 1):
        st = states[w]
        d = info[w]
        if st == 0:                                # 真失联（未桥接空窗）
            if cur:
                raw.append(cur); cur = None
            if raw and raw[-1][0] == "void":
                raw[-1][2] = w
            else:
                raw.append(["void", w, w])
            continue
        if d is None:                              # 桥接空窗：有态无位置 → 延续当前段
            if cur is not None:
                cur[2] = w
            else:                                  # 纯桥接开段（罕见：接在失联后）
                cur = ["seg", w, w, [], None, None, None]
            continue
        pos = d["pos"]
        if cur is None:
            cur = ["seg", w, w, [pos], pos, w, w]
        elif cur[4] is None or haversine(pos[0], pos[1], cur[4][0], cur[4][1]) <= noise_m:
            cur[2] = w; cur[3].append(pos)
            if cur[4] is None:
                cur[4] = pos
            cur[6] = w
        else:
            raw.append(cur)
            cur = ["seg", w, w, [pos], pos, w, w]
    if cur:
        raw.append(cur)

    # ---- 段 → 事件（停留 / 短段 / 无数据）----
    events: List[Dict[str, Any]] = []
    for r in raw:
        if r[0] == "void":
            events.append({"kind": "void", "w0": r[1], "w1": r[2],
                           "minutes": (r[2] - r[1] + 1) * width_s / 60.0})
            continue
        _, w0, w1, positions, _anchor, first_ne, last_ne = r
        dur = (w1 + 1 - w0) * width_s
        pos = None
        if positions:
            pos = (sum(p[0] for p in positions) / len(positions),
                   sum(p[1] for p in positions) / len(positions))
        addrs = [info[w]["addr"] for w in range(w0, w1 + 1)
                 if info.get(w) and info[w].get("addr")]
        events.append({"kind": "stay" if (dur >= stay_min_s and pos) else "short",
                       "w0": w0, "w1": w1, "pos": pos, "first_ts": None, "last_ts": None,
                       "addrs": addrs,
                       "n_points": sum(info[w]["n"] for w in range(w0, w1 + 1)
                                       if info.get(w)),
                       "first_ne": first_ne, "last_ne": last_ne,
                       "void_windows": 0})
    for e in events:                               # 实际首末点时刻（停留条目用）
        if e["kind"] == "void" or e["first_ne"] is None:
            continue
        e["first_ts"] = info[e["first_ne"]]["ts_first"]
        e["last_ts"] = info[e["last_ne"]]["ts_last"]

    # ---- 短段并入相邻停留（两停留位置差 ≤ 噪声）----
    i = 0
    while i < len(events):
        e = events[i]
        if e["kind"] == "short" and 0 < i < len(events) - 1 \
                and events[i - 1]["kind"] == "stay" and events[i + 1]["kind"] == "stay" \
                and events[i - 1]["pos"] and events[i + 1]["pos"] \
                and haversine(events[i - 1]["pos"][0], events[i - 1]["pos"][1],
                              events[i + 1]["pos"][0], events[i + 1]["pos"][1]) <= noise_m:
            events[i - 1]["w1"] = events[i + 1]["w1"]
            events[i - 1]["last_ne"] = events[i + 1]["last_ne"]
            events[i - 1]["last_ts"] = events[i + 1]["last_ts"]
            events[i - 1]["n_points"] += e["n_points"] + events[i + 1]["n_points"]
            events[i - 1]["void_windows"] += e["void_windows"]
            events[i - 1]["addrs"].extend(e["addrs"]); events[i - 1]["addrs"].extend(events[i + 1]["addrs"])
            del events[i:i + 2]
            continue
        i += 1

    # ---- 跨无数据段/短段的相邻停留：位置差 ≤ 噪声 → 合并 ----
    i = 0
    while i < len(events):
        if events[i]["kind"] != "stay":
            i += 1
            continue
        j = i + 1
        absorbed_void = 0
        while j < len(events) and events[j]["kind"] in ("void", "short"):
            if events[j]["kind"] == "void":
                absorbed_void += events[j]["w1"] - events[j]["w0"] + 1
            j += 1
        if j < len(events) and events[j]["kind"] == "stay" and j > i + 1 \
                and events[i]["pos"] and events[j]["pos"] \
                and haversine(events[i]["pos"][0], events[i]["pos"][1],
                              events[j]["pos"][0], events[j]["pos"][1]) <= noise_m:
            events[i]["w1"] = events[j]["w1"]
            events[i]["last_ne"] = events[j]["last_ne"]
            events[i]["last_ts"] = events[j]["last_ts"]
            events[i]["n_points"] += events[j]["n_points"]
            events[i]["void_windows"] += absorbed_void
            events[i]["addrs"].extend(events[j]["addrs"])
            del events[i + 1:j + 1]
            continue
        i = j if j > i else i + 1

    # ---- 出行段：相邻停留之间（跨过其间短段与未桥接空窗），含首尾移动段 ----
    trips: List[Dict[str, Any]] = []
    stay_idx = [k for k, e in enumerate(events) if e["kind"] == "stay" and e["pos"]]

    def _emit_trip(a: Dict[str, Any], b: Dict[str, Any]) -> None:
        """a、b 为两个停留事件：生成其间的一段出行（含注记）。"""
        start_ts = a["last_ts"] if a["last_ts"] is not None else (a["w1"] + 1) * width_s
        end_ts = b["first_ts"] if b["first_ts"] is not None else b["w0"] * width_s
        if end_ts <= start_ts or not a["pos"] or not b["pos"]:
            return
        net = haversine(a["pos"][0], a["pos"][1], b["pos"][0], b["pos"][1])
        span = range(a["w1"] + 1, b["w0"])
        path = [a["pos"]] + [info[w]["pos"] for w in span if info.get(w)] + [b["pos"]]
        cum = sum(haversine(p[0], p[1], q[0], q[1])
                  for p, q in zip(path, path[1:])) if len(path) > 1 else 0.0
        void_minutes = sum((states[w] == 0) * width_s / 60.0 for w in span)
        trips.append({"w0": a["w1"] + 1, "w1": b["w0"] - 1,
                      "net_m": net, "cum_m": cum,
                      "straightness": round(net / cum, 3) if cum > 0 else 1.0,
                      "start_ts": start_ts, "end_ts": end_ts, "minutes": (end_ts - start_ts) / 60.0,
                      "speed_kmh": (3.6 * net / (end_ts - start_ts)) if end_ts > start_ts else 0.0,
                      "kind": "有效出行" if net >= high_m else "位置变动",
                      "confidence": "高" if net >= high_m else "中",
                      "void_minutes": void_minutes,
                      "from_pos": a["pos"], "to_pos": b["pos"],
                      "from_addr": mode_str(a["addrs"]), "to_addr": mode_str(b["addrs"]),
                      "n_points": sum(info[w]["n"] for w in span if info.get(w))})

    for m in range(len(stay_idx) - 1):
        _emit_trip(events[stay_idx[m]], events[stay_idx[m + 1]])

    # 首尾移动段（数据首尾未成停留段的窗口，同 v2 include_leading_trailing 语义）
    if stay_idx:
        first, last = events[stay_idx[0]], events[stay_idx[-1]]
        head = events[:stay_idx[0]]
        head_pos = next((e["pos"] for e in head if e["pos"]), None)
        if head and head_pos and first["pos"]:
            pseudo_head = {"last_ts": next((e["last_ts"] for e in reversed(head)
                                            if e.get("last_ts") is not None), None),
                           "w1": head[-1]["w1"], "pos": head_pos, "addrs":
                           [a for e in head for a in e["addrs"]]}
            _emit_trip(pseudo_head, first)
        tail = events[stay_idx[-1] + 1:]
        tail_pos = next((e["pos"] for e in tail if e["pos"]), None)
        if tail and tail_pos and last["pos"]:
            pseudo_tail = {"first_ts": next((e["first_ts"] for e in tail
                                             if e.get("first_ts") is not None), None),
                           "w0": tail[0]["w0"], "pos": tail_pos, "addrs":
                           [a for e in tail for a in e["addrs"]]}
            _emit_trip(last, pseudo_tail)

    # ---- 连续同态区间（报告附录）与统计 ----
    state_runs: List[List[int]] = []
    counter: Dict[int, int] = {}
    for w in range(wmin, wmax + 1):
        st = states[w]
        counter[st] = counter.get(st, 0) + 1
        if state_runs and state_runs[-1][0] == st and state_runs[-1][2] == w - 1:
            state_runs[-1][2] = w
        else:
            state_runs.append([st, w, w])

    # ---- 报告叙述：按窗口状态聚合（同态连续区间；跨断档合并的停留作单条静默）----
    def _runs_in_span(w0: int, w1: int) -> List[List[int]]:
        runs: List[List[int]] = []
        for w in range(w0, w1 + 1):
            st = states[w]
            if runs and runs[-1][0] == st and runs[-1][2] == w - 1:
                runs[-1][2] = w
            else:
                runs.append([st, w, w])
        return runs

    def _run_item(st: int, w0: int, w1: int) -> Dict[str, Any]:
        wins_local = [info[w] for w in range(w0, w1 + 1) if info.get(w)]
        first_ts = wins_local[0]["ts_first"] if wins_local else w0 * width_s
        last_ts = wins_local[-1]["ts_last"] if wins_local else (w1 + 1) * width_s
        pos_first = wins_local[0]["pos"] if wins_local else None
        pos_last = wins_local[-1]["pos"] if wins_local else None
        net = haversine(pos_first[0], pos_first[1], pos_last[0], pos_last[1]) \
            if (pos_first and pos_last) else 0.0
        if last_ts <= first_ts:                   # 单点/零跨度窗：按窗口覆盖时长呈现
            first_ts = w0 * width_s
            last_ts = (w1 + 1) * width_s
        return {"state": st, "w0": w0, "w1": w1,
                "first_ts": first_ts, "last_ts": last_ts,
                "pos_first": pos_first, "pos_last": pos_last,
                "addr": mode_str([w["addr"] for w in wins_local if w.get("addr")]),
                "addr_from": wins_local[0]["addr"] if wins_local else "",
                "addr_to": wins_local[-1]["addr"] if wins_local else "",
                "net_m": net, "minutes": max(0.0, (last_ts - first_ts) / 60.0),
                "speed_kmh": (3.6 * net / (last_ts - first_ts)) if last_ts > first_ts else 0.0,
                "n_points": sum(w["n"] for w in wins_local),
                "void_windows": 0, "void_minutes": 0.0}

    report_items: List[Dict[str, Any]] = []
    if stay_idx:
        first_stay = events[stay_idx[0]]
        last_stay = events[stay_idx[-1]]
        if first_stay["w0"] > wmin:                   # 头部移动
            for r in _runs_in_span(wmin, first_stay["w0"] - 1):
                report_items.append(_run_item(r[0], r[1], r[2]))
        for m, ia in enumerate(stay_idx):
            e = events[ia]
            # 前后缘的移动窗（出发/到达过渡）拆出为移动叙述，不并入静默行
            p = e["w0"]
            while p <= e["w1"] and states[p] in (2, 3, 4):
                p += 1
            q = e["w1"]
            while q >= p and states[q] in (2, 3, 4):
                q -= 1
            if p > e["w0"]:
                for r in _runs_in_span(e["w0"], p - 1):
                    report_items.append(_run_item(r[0], r[1], r[2]))
            if q >= p:
                item = _run_item(1, p, q)
                item["void_windows"] = sum(1 for w in range(p, q + 1) if states[w] == 0)
                item["void_minutes"] = item["void_windows"] * width_s / 60.0
                if e["addrs"]:
                    item["addr"] = mode_str(e["addrs"])
                report_items.append(item)
            if q < e["w1"]:
                for r in _runs_in_span(q + 1, e["w1"]):
                    report_items.append(_run_item(r[0], r[1], r[2]))
            if m < len(stay_idx) - 1:
                ib = stay_idx[m + 1]
                for r in _runs_in_span(e["w1"] + 1, events[ib]["w0"] - 1):
                    report_items.append(_run_item(r[0], r[1], r[2]))
        if last_stay["w1"] < wmax:                    # 尾部移动
            for r in _runs_in_span(last_stay["w1"] + 1, wmax):
                report_items.append(_run_item(r[0], r[1], r[2]))
    else:                                             # 无停留：全时段状态叙述
        for r in _runs_in_span(wmin, wmax):
            report_items.append(_run_item(r[0], r[1], r[2]))

    # ---- 相邻同态移动条目合并（出发/到达过渡窗与行程窗跨段边界时同态相邻）----
    merged: List[Dict[str, Any]] = []
    for it in report_items:
        if (merged and it["state"] in (2, 3, 4) and merged[-1]["state"] == it["state"]
                and merged[-1]["w1"] + 1 == it["w0"]):
            m = merged[-1]
            m["w1"] = it["w1"]; m["last_ts"] = it["last_ts"]; m["pos_last"] = it["pos_last"]
            m["addr_to"] = it["addr_to"]; m["n_points"] += it["n_points"]
            m["void_windows"] += it["void_windows"]; m["void_minutes"] += it["void_minutes"]
            if m["pos_first"] and m["pos_last"]:
                m["net_m"] = haversine(m["pos_first"][0], m["pos_first"][1],
                                       m["pos_last"][0], m["pos_last"][1])
            m["minutes"] = max(0.0, (m["last_ts"] - m["first_ts"]) / 60.0)
            m["speed_kmh"] = (3.6 * m["net_m"] / (m["last_ts"] - m["first_ts"])
                              ) if m["last_ts"] > m["first_ts"] else 0.0
            continue
        merged.append(it)
    report_items = merged

    return {"events": events, "trips": trips, "state_runs": state_runs,
            "report_items": report_items, "state_counter": counter}


def window_address(info: Dict[int, Optional[Dict[str, Any]]], w0: int, w1: int) -> str:
    """段内地址众数（停留条目代表地址）。"""
    addrs = [info[w]["addr"] for w in range(w0, w1 + 1)
             if info.get(w) and info[w].get("addr")]
    return mode_str(addrs)
