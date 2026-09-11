# -*- coding: utf-8 -*-
"""引擎自测（不依赖 Flask、不依赖插件其他模块）。

用法::

    python -m ...                    # 在插件目录下不便（目录名含连字符），改用路径直跑：
    python "<插件目录>/backend/engine/selftest.py"                       # 只跑单元自检
    python "<插件目录>/backend/engine/selftest.py" --excel <xlsx 路径>   # 另跑真实表格

说明
----
``--excel`` 分支会**延迟导入 openpyxl**（仅用于把 Excel 读成二维表，属诊断入口，
不参与引擎流水线），这是本文件唯一的第三方依赖，且只在命令行显式传参时触发。
引擎本体（analyze_rows 及其全部算法模块）始终只依赖标准库。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if __package__ in (None, ""):                      # 允许直接以脚本方式运行
    sys.path.insert(0, os.path.dirname(_HERE))

from engine import analyze_rows, normalize_params          # noqa: E402  (路径就绪后导入)
from engine.stats import percentile                        # noqa: E402
from engine.timeutil import parse_time                     # noqa: E402
from engine.contract import AnalysisError                  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [PASS] %s" % name)
    else:
        _FAIL += 1
        print("  [FAIL] %s %s" % (name, detail))


# --------------------------------------------------------------------------
# 单元自检
# --------------------------------------------------------------------------

def test_percentile() -> None:
    print("· 统计口径（与 numpy.percentile 线性插值一致）")
    check("percentile([1,2,3,4], 95) = 3.85", abs(percentile([1, 2, 3, 4], 95) - 3.85) < 1e-9)
    check("percentile(1..10, 95) = 9.55", abs(percentile(list(range(1, 11)), 95) - 9.55) < 1e-9)
    check("percentile([], 50) 为空", percentile([], 50) is None)
    check("percentile([5], 50) = 5", percentile([5], 50) == 5)


def test_time() -> None:
    print("· 时间解析（字符串 / Excel 序列号 / datetime）")
    from datetime import datetime
    from engine.timeutil import EXCEL_EPOCH
    check("标准字符串", parse_time("2026-09-10 04:30:38") is not None)
    check("斜杠格式", parse_time("2026/9/10 4:30") is not None)
    check("带毫秒", parse_time("2026-09-10 04:30:38.000") is not None)
    check("紧凑 14 位", parse_time("20260910043038") is not None)
    check("Excel 序列号(数值)", parse_time(46265.5) is not None)
    check("Excel 序列号(数字串)", parse_time("46265.5") is not None)
    check("datetime 对象", parse_time(datetime(2026, 9, 10, 4, 30, 38)) is not None)
    check("非法值返回 None", parse_time("不是时间") is None)
    check("空值返回 None", parse_time(None) is None)
    # 同一时刻的三种表示（字符串 / 紧凑串 / Excel 序列号）应解析为同一时间
    a = parse_time("2026-09-10 04:30:38")
    c = parse_time("20260910043038")
    serial = (datetime(2026, 9, 10, 4, 30, 38) - EXCEL_EPOCH).total_seconds() / 86400.0
    b = parse_time(serial)
    check("三种格式结果一致", abs(a - c) < 120 and abs(a - b) < 120,
          "a=%s b=%s c=%s" % (a, b, c))


def test_schema_guard() -> None:
    print("· 缺必需列应抛出可读错误")
    rows = [["时间", "地址"], ["2026-09-10 04:30:38", "某地"]]
    try:
        analyze_rows(rows)
        check("缺经度/纬度时报错", False, "未抛异常")
    except AnalysisError as exc:
        check("缺经度/纬度时报错", "经度" in str(exc) or "LONGITUDE" in str(exc), str(exc)[:60])
    except Exception as exc:                                  # noqa: BLE001
        check("缺经度/纬度时报错", False, "异常类型非 AnalysisError：%r" % (exc,))


def test_empty_and_dirty() -> None:
    print("· 空表 / 全脏数据")
    try:
        analyze_rows([])
        check("空表报错", False, "未抛异常")
    except AnalysisError:
        check("空表报错", True)

    rows = [["BEGINTIME", "LONGITUDE", "LATITUDE"],
            ["2026-09-10 04:30:38", 0, 0],
            ["坏时间", 109.9, 21.6],
            ["2026-09-10 04:35:38", "x", "y"]]
    res = analyze_rows(rows)
    check("全脏数据不崩溃且无结论", res["ok"] and not res["stays"] and not res["trips"])
    check("丢弃统计有记录", sum(res["quality"]["丢弃原因"].values()) == 3,
          str(res["quality"]["丢弃原因"]))


def test_static_user() -> None:
    print("· 全程静止 → 1 个长停留、0 次出行")
    rows = [["BEGINTIME", "USERNUM", "LAI", "CI", "ADDRESS", "LONGITUDE", "LATITUDE"]]
    lon, lat = 109.92401, 21.626743
    base = 1786000000
    for i in range(30):                                  # 30 个点，间隔 10 分钟，坐标微抖
        jitter = 0.0004 * (i % 3)
        rows.append(["", "15700000007", "1335731", "51645403137", "青平镇中垌村村口",
                     lon + jitter, lat - jitter])
    rows = _retime(rows, 0, base, 600)
    res = analyze_rows(rows)
    check("静止数据不产生出行段", len(res["trips"]) == 0, str(res["trips"])[:120])
    check("静止数据产生停留点", len(res["stays"]) >= 1)
    if res["stays"]:
        check("停留点覆盖全部时长", res["stays"][0]["停留分钟"] >= 280,
              str(res["stays"][0]["停留分钟"]))


def test_pingpong() -> None:
    print("· 乒乓切换 → 收敛为单一停留点")
    rows = [["BEGINTIME", "USERNUM", "LAI", "CI", "ADDRESS", "LONGITUDE", "LATITUDE"]]
    base_lon, base_lat = 109.92, 21.62
    cells = [("1335731", "1001", 0.0, 0.0), ("1335731", "1002", 0.004, 0.0),
             ("1335732", "1003", 0.0, 0.004)]
    for i in range(36):
        lai, ci, dx, dy = cells[i % 3]
        rows.append(["", "15700000007", lai, ci, "青平镇中垌村村口",
                     base_lon + dx, base_lat + dy])
    rows = _retime(rows, 0, 1786000000, 600)
    res = analyze_rows(rows)
    check("乒乓切换不产生出行段", len(res["trips"]) == 0, str(res["trips"])[:120])
    check("簇数被归并（≤2）", res["quality"]["地点簇数"] <= 2,
          str(res["quality"]["地点簇数"]))


def test_idempotent() -> None:
    print("· 幂等性（同输入两次结果完全一致）")
    rows = _moving_rows()
    a = analyze_rows(rows)
    b = analyze_rows(rows)
    check("停留点一致", a["stays"] == b["stays"])
    check("出行段一致", a["trips"] == b["trips"])
    check("报告一致", a["report"]["text_by_user"] == b["report"]["text_by_user"])


def test_moving_and_report() -> None:
    print("· 真实位移 → 有效出行 + 报告成型")
    rows = _moving_rows()
    res = analyze_rows(rows)
    check("产出出行段", len(res["trips"]) >= 1, str(res["trips"])[:120])
    trips = [t for t in res["trips"] if t["判定"] == "有效出行"]
    check("存在有效出行", len(trips) >= 1)
    text = res["report"]["text_by_user"].get("15700000007", "")
    check("报告含抬头", "内部资料" in text)
    check("报告含号码", "号码15700000007的活动轨迹如下" in text)
    check("报告含速写摘要", "【速写】" in text)
    check("报告含落款", "严禁向无关人员转发" in text)
    check("时间轴闭合（无空洞残留）", all(e["序号"] == i + 1
                                     for i, e in enumerate(res["report"]["entries"])))
    check("无哨兵速度值", all(0 <= t["均速_公里每小时"] < 1000 for t in res["trips"]))


def test_multi_user_and_no_user() -> None:
    print("· 多号码与无号码列")
    rows = [["BEGINTIME", "USERNUM", "LONGITUDE", "LATITUDE"]]
    for u, off in (("13800000001", 0.0), ("13800000002", 0.05)):
        for i in range(10):
            rows.append(["", u, 109.9 + off + 0.0002 * i, 21.6 + 0.0002 * i])
    rows = _retime(rows, 0, 1786000000, 600)
    res = analyze_rows(rows)
    check("两个号码各自成报", set(res["report"]["text_by_user"].keys()) == {"13800000001", "13800000002"})

    rows2 = [["BEGINTIME", "LONGITUDE", "LATITUDE"]]
    for i in range(10):
        rows2.append(["", 109.9 + 0.0002 * i, 21.6 + 0.0002 * i])
    rows2 = _retime(rows2, 0, 1786000000, 600)
    res2 = analyze_rows(rows2)
    check("无号码列时降级成功", res2["ok"] and len(res2["report"]["text_by_user"]) == 1)
    check("降级有警告提示", any("号码" in w for w in res2["warnings"]), str(res2["warnings"]))


def test_params_guard() -> None:
    print("· 参数层（默认值 / 非法值夹取 / 未知算法）")
    p = normalize_params(None)
    check("默认算法为 v2", p["algo"] == "v2")
    check("默认必需列含经纬度时间",
          set(["BEGINTIME", "LONGITUDE", "LATITUDE"]).issubset(set(p["schema"]["required"])))
    weird = normalize_params({"analysis": {"algo": "v2", "staypoint": {"noise_percentile": 999,
                                                                     "min_stay_minutes": -5},
                                           "trip": {"straightness_min": 9}}})
    check("分位数被夹到 100", weird["staypoint"]["noise_percentile"] == 100.0)
    check("最短停留不为负", weird["staypoint"]["min_stay_minutes"] == 0.0)
    check("直线度被夹到 1", weird["trip"]["straightness_min"] == 1.0)
    try:
        analyze_rows([["BEGINTIME", "LONGITUDE", "LATITUDE"], ["2026-09-10 04:30:38", 109.9, 21.6]],
                     algo="v9")
        check("未知算法报错", False, "未抛异常")
    except AnalysisError as exc:
        check("未知算法报错", "v9" in str(exc))


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _retime(rows, time_col: int, base: int, step: int):
    """把第 time_col 列填成递增时间字符串（避免手写 30 行时间）。"""
    from engine.timeutil import epoch_to_str
    out = [rows[0]]
    for i, r in enumerate(rows[1:]):
        r = list(r)
        r[time_col] = epoch_to_str(base + i * step)
        out.append(r)
    return out


def _moving_rows():
    """构造"先停留 → 长途移动 → 再停留"的数据，用于验证出行判定与报告。"""
    rows = [["BEGINTIME", "USERNUM", "LAI", "CI", "ADDRESS", "LONGITUDE", "LATITUDE"]]
    # 起点：停留 2 小时（20 个点，坐标微抖）
    for i in range(20):
        rows.append(["", "15700000007", "1335731", "1001", "甲地",
                     109.900 + 0.0002 * (i % 2), 21.600 + 0.0002 * (i % 2)])
    # 移动：10 个点，每步 2 km（东移约 0.02 度）
    for i in range(1, 11):
        rows.append(["", "15700000007", "1335731", "2000", "途经地",
                     109.900 + 0.02 * i, 21.600])
    # 终点：停留 2 小时（20 个点）
    for i in range(20):
        rows.append(["", "15700000007", "1335731", "3001", "乙地",
                     110.100 + 0.0002 * (i % 2), 21.600 + 0.0002 * (i % 2)])
    return _retime(rows, 0, 1786000000, 600)


def run_excel(path: str) -> None:
    print("\n· 真实表格：%s" % path)
    try:
        import openpyxl                                   # 诊断入口专用，见模块文档
    except Exception:
        print("  [SKIP] 当前环境没有 openpyxl，无法读取 Excel")
        return
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    while rows and all(v is None for v in rows[-1]):
        rows.pop()
    res = analyze_rows(rows)
    q = res["quality"]
    print("  算法=%s 号码数=%s 去重点数=%s" % (res["algo"], q["号码数"], q["去重后轨迹点数"]))
    print("  噪声半径=%s m  D_thr=%s m  T_thr=%s 分  簇=%s"
          % (q["估计定位噪声半径_米"], q["停留半径阈值D_thr_米"],
             q["最短停留T_thr_分钟"], q["地点簇数"]))
    print("  停留点 %d 个，出行段 %d 段" % (len(res["stays"]), len(res["trips"])))
    for s in res["stays"]:
        print("    停留 %s ~ %s（%s）%s"
              % (s["进入时间"], s["离开时间"], s["停留时长"], s["代表地址"][:24]))
    for t in res["trips"]:
        print("    %s %s ~ %s %s 净位移 %s 均速 %s km/h 直线度 %s 回访 %s"
              % (t["判定"], t["开始时间"], t["结束时间"], t["时长"],
                 t["距离"], t["均速_公里每小时"], t["直线度"], t["回访次数"]))
    print("  ---- 报告正文 ----")
    for _u, text in res["report"]["text_by_user"].items():
        print(text)
        print()


def main(argv) -> int:
    print("=== 轨迹分析引擎自测 ===")
    for fn in (test_percentile, test_time, test_schema_guard, test_empty_and_dirty,
               test_static_user, test_pingpong, test_idempotent, test_moving_and_report,
               test_multi_user_and_no_user, test_params_guard):
        fn()
    if "--excel" in argv:
        idx = argv.index("--excel")
        if idx + 1 < len(argv):
            run_excel(argv[idx + 1])
    print("\n结果：PASS=%d FAIL=%d" % (_PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
