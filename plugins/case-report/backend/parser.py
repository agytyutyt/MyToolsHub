"""战果录入 —— 字段规整与战果汇总（已取消本地规则解析，仅大模型解析）。

保留内容：
- FIELD_KEYS：字段顺序
- normalize_case_name / normalize_time / normalize_count：值规整（无正则）
- CATEGORY_RULES + assign_category + known_categories + aggregate_items：战果汇总
"""

from datetime import datetime, timedelta

# 战果要素的规范顺序
FIELD_KEYS = ["案件名", "时间", "主办大队", "主办人", "抓获人数", "涉案价值", "缴获物品"]

# 类似物品 → 统一战果类别（关键词命中任一即归组；同类别数量可累加）
CATEGORY_RULES = [
    ("电脑",   ["电脑", "笔记本", "台式机", "一体机", "平板电脑", "平板", "ipad"]),
    ("电话卡", ["电话卡", "手机卡", "sim卡", "流量卡", "上网卡", "物联卡"]),
    ("手机",   ["手机", "智能手机", "电话机"]),
    ("车辆",   ["轿车", "汽车", "面包车", "货车", "卡车", "客车", "摩托车", "电动车", "机动车"]),
    ("毒品",   ["海洛因", "冰毒", "大麻", "k粉", "摇头丸", "鸦片", "可卡因",
                "甲基苯丙胺", "麻黄碱", "吗啡", "杜冷丁", "毒品"]),
    ("现金",   ["现金", "赃款", "钱款", "人民币", "美元", "港币", "欧元", "泰铢"]),
    ("冻结资金", ["冻结资金", "涉案资金", "非法资金", "资金"]),
    ("银行卡", ["银行卡", "信用卡", "储蓄卡", "银行u盾", "u盾"]),
    ("枪支",   ["枪支", "手枪", "步枪", "猎枪", "气枪", "仿真枪"]),
    ("子弹",   ["子弹", "弹药", "弹壳", "弹匣"]),
    ("刀具",   ["刀具", "匕首", "砍刀", "弹簧刀", "管制刀具"]),
    ("金银饰品", ["金条", "黄金", "金饰", "金器", "银元", "首饰", "戒指", "项链", "耳环", "手镯"]),
    ("名表",   ["手表", "腕表", "名表"]),
    ("烟酒",   ["香烟", "卷烟", "白酒", "洋酒", "红酒", "啤酒", "烟酒", "茅台"]),
    ("赌具赌资", ["赌具", "赌资", "筹码", "麻将机"]),
    ("电子设备", ["对讲机", "摄像头", "u盘", "存储卡", "硬盘", "pos机", "刷卡机", "路由器"]),
    ("证件票据", ["身份证", "驾驶证", "护照", "公章", "印章", "发票", "账本", "票据"]),
    ("制毒原料", ["制毒原料", "化学品", "麻黄素"]),
]
CATEGORY_OTHER = "其他"

_CH_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def normalize_case_name(name):
    """规整案件名以便比较：去引号/书名号/空格（含全角空格）。"""
    n = (name or "").strip()
    quotes = set('""\'\'`\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\u300a\u300b')
    n = "".join(c for c in n if c not in quotes and not c.isspace())
    return n


def normalize_count(text):
    """把「两名 / 5名 / 二十三 / ２人」等计数规整为阿拉伯数字字符串；失败则原样返回。"""
    t = (text or "").strip()
    t = "".join(chr(ord(c) - 0xFEE0) if "０" <= c <= "９" else c for c in t)
    if not t:
        return ""
    if t.isdigit():
        return str(int(t))
    total, num = 0, 0
    for ch in t:
        if ch in _CH_DIGITS:
            num = _CH_DIGITS[ch]
        elif ch == "十":
            total += (num if num else 1) * 10
            num = 0
        elif ch == "百":
            total += (num if num else 1) * 100
            num = 0
        elif ch == "千":
            total += (num if num else 1) * 1000
            num = 0
        elif ch == "万":
            total += (num if num else 1) * 10000
            num = 0
        else:
            return t
    total += num
    return str(total) if total else t


def normalize_time(text):
    """把时间归一化为「YYYY年M月D日」（无正则，纯字符串操作）。

    处理：昨天/前天/今天/当天等相对时间；Y年M月D日；M月D日；Y-M-D/Y/M/D。
    """
    t = (text or "").strip()
    if not t:
        return ""
    now = datetime.now()
    today = now.date()
    for w, delta in (("前天", -2), ("前日", -2), ("昨天", -1), ("昨日", -1),
                     ("昨晚", -1), ("昨夜", -1), ("今天", 0), ("今日", 0),
                     ("今晚", 0), ("当日", 0), ("当天", 0), ("现场", 0)):
        if w in t:
            d = today + timedelta(days=delta)
            return f"{d.year}年{d.month}月{d.day}日"

    def _try_int(s):
        try:
            return int(s.strip())
        except (ValueError, AttributeError):
            return None

    # Y年M月D日
    iy = t.find("年")
    if iy > 0:
        y = _try_int(t[max(0, iy - 4):iy])
        rest = t[iy + 1:]
        im = rest.find("月")
        if im > 0:
            m = _try_int(rest[:im])
            rest2 = rest[im + 1:]
            id_ = rest2.find("日")
            if id_ > 0:
                d = _try_int(rest2[:id_])
                if y and m and d and 1 <= y <= 9999 and 1 <= m <= 12 and 1 <= d <= 31:
                    return f"{y}年{m}月{d}日"

    # M月D日（补当前年份）
    if "年" not in t:
        im = t.find("月")
        if im > 0:
            m = _try_int(t[:im])
            rest = t[im + 1:]
            id_ = rest.find("日")
            if id_ > 0:
                d = _try_int(rest[:id_])
                if m and d and 1 <= m <= 12 and 1 <= d <= 31:
                    return f"{now.year}年{m}月{d}日"

    # Y-M-D / Y/M/D
    for sep in ("-", "/", "."):
        parts = t.split(sep)
        if len(parts) == 3:
            y = _try_int(parts[0])
            m = _try_int(parts[1])
            d = _try_int(parts[2])
            if y and m and d and 1 <= y <= 9999 and 1 <= m <= 12 and 1 <= d <= 31:
                return f"{y}年{m}月{d}日"

    return ""


def assign_category(name):
    """把物品名映射到统一战果类别（找不到则归「其他」）。"""
    low = name.lower()
    for cat, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw.lower() in low:
                return cat
    return CATEGORY_OTHER


def known_categories():
    """全部内置类别名（含兜底「其他」），供前端下拉/联想。"""
    return [cat for cat, _ in CATEGORY_RULES] + [CATEGORY_OTHER]


def apply_category_overrides(items, overrides):
    """按「用户已学习类别」修正物品类别：overrides = {物品名: 类别}。"""
    items = items or []
    if not overrides:
        return items
    out = []
    for it in items:
        if isinstance(it, dict):
            it = dict(it)
            learned = overrides.get((it.get("name") or "").strip())
            if learned:
                it["category"] = learned
        out.append(it)
    return out


# 计量单位归并（用于跨记录累加时统一到同一单位）
_WEIGHT_TO_GRAM = {"克": 1.0, "g": 1.0, "千克": 1000.0, "公斤": 1000.0,
                   "kg": 1000.0, "斤": 500.0, "两": 50.0, "吨": 1000000.0}
_MONEY_TO_YUAN = {"元": 1.0, "块": 1.0, "千元": 1000.0, "万": 10000.0, "万元": 10000.0}
_FORMAT_UNIT = {"克": "克", "元": "元"}


def aggregate_items(record_items, case_keys=None):
    """跨记录汇总战果：按类别归组、同类数量累加。

    record_items: [[{category,name,quantity,unit}, ...], ...]（每条记录一个列表）。
    case_keys（可选）：与 record_items 等长的案件标识列表（同一案件共用同一标识），
    用于统计「涉及 N 起」时按案件去重；不传时退化为按记录去重。
    单位统一规则：重量类归并到「克」，货币类归并到「元」，其余仅同单位累加；
    quantity 为 None 的笼统量（一批/若干）不计入数字，仅记录出现的案件/记录数。
    返回 [{category, quantity, unit, records, unknown}]，按 records 降序。
    """
    buckets = {}
    for rec_index, items in enumerate(record_items):
        case_id = rec_index
        if case_keys is not None:
            ck = case_keys[rec_index] if rec_index < len(case_keys) else ""
            case_id = ck or str(rec_index)
        for it in items or []:
            if not isinstance(it, dict):
                continue
            cat = it.get("category") or CATEGORY_OTHER
            unit = (it.get("unit") or "").strip() or "-"
            b = buckets.setdefault(cat, {})
            u = b.setdefault(unit, {"sum": 0.0, "records": set(), "unknown": 0})
            u["records"].add(case_id)
            q = it.get("quantity")
            if q is None:
                u["unknown"] += 1
            else:
                try:
                    u["sum"] += float(q)
                except (TypeError, ValueError):
                    u["unknown"] += 1

    out = []
    for cat, units in buckets.items():
        if units and all(u2 in _WEIGHT_TO_GRAM for u2 in units):
            total = sum(u["sum"] for u in units.values())
            records = set().union(*(u["records"] for u in units.values()))
            unknown = sum(u["unknown"] for u in units.values())
            out.append({"category": cat, "quantity": _qty(total, unknown),
                        "unit": "克", "records": len(records), "unknown": unknown})
        elif units and all(u2 in _MONEY_TO_YUAN for u2 in units):
            total = sum(u["sum"] * _MONEY_TO_YUAN[u2] for u2, u in units.items())
            records = set().union(*(u["records"] for u in units.values()))
            unknown = sum(u["unknown"] for u in units.values())
            out.append({"category": cat, "quantity": _qty(total, unknown),
                        "unit": "元", "records": len(records), "unknown": unknown})
        else:
            for u2, u in units.items():
                out.append({"category": cat, "quantity": _qty(u["sum"], u["unknown"]),
                            "unit": u2, "records": len(u["records"]), "unknown": u["unknown"]})
    out.sort(key=lambda x: (-x["records"], x["category"]))
    return out


def _round(v):
    r = round(v, 2)
    return int(r) if r == int(r) else r


def _qty(total, unknown):
    amt = _round(total)
    return None if amt == 0 and unknown else amt