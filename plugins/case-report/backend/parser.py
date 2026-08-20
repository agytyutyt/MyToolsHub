"""战果录入 —— 本地规则解析器（大模型未配置时的兜底方案）。

从一段收网情况报告中用正则提炼五要素：
案件名 / 时间 / 主办大队 / 抓获人数 / 缴获物品。
规则毕竟无法覆盖所有表述，若抽取不理想，请配置大模型使用智能解析。
"""

from datetime import datetime, timedelta

import re

# 五要素的规范顺序（同时是 JSON 键值对落盘的键名顺序）
FIELD_KEYS = ["案件名", "时间", "主办大队", "抓获人数", "缴获物品"]

# ---------- 主办大队 ----------
# 1) 带标签：主办大队：X  /  主办单位：X  /  主办：X
_UNIT_LABEL_RE = re.compile(
    r"(?:主办|承办|牵头)(?:单位|大队|支队|部门机关)?[:：]?\s*"
    r"(?P<u>[\u4e00-\u9fa5A-Za-z0-9]{1,10}(?:支队|大队|中队|专班|专案组|工作组))"
)
# 2) 内嵌：X大队主办/承办/负责/牵头/实施（前面可有「由/由…」等连接词）
_UNIT_INLINE_RE = re.compile(
    r"(?P<u>(?![主办承办牵头由在向对为之])[\u4e00-\u9fa5A-Za-z0-9]{1,10}"
    r"(?:支队|大队|中队|专班|专案组|工作组))\s*(?:主办|承办|负责|牵头|实施)"
)
# 3) 裸单位名：刑侦一支队 / 禁毒大队 …（避免把「主办大队」这类标签误当单位名）
_UNIT_BARE_RE = re.compile(
    r"(?P<u>(?![主办承办牵头])[\u4e00-\u9fa5]{2,6}(?:支队|大队|中队|专班|专案组|工作组))"
)

# ---------- 时间 ----------
# 相对时间（以「当前年月日」为基准回退）
_REL_YESTERDAY_RE = re.compile(r"(昨天|昨日|昨晚|昨夜)")
_REL_BEFORE_YESTERDAY_RE = re.compile(r"(前天|前日)")
_REL_TODAY_RE = re.compile(r"(今天|今日|今晚|当日|当天|现场)")
# 绝对日期：优先带年份，其次仅月日（缺省补当前年份）
_DATE_ABS_PATTERNS = [
    re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"),
    re.compile(r"(?P<y>\d{4})\s*[./\-]\s*(?P<m>\d{1,2})\s*[./\-]\s*(?P<d>\d{1,2})"),
    re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"),
    re.compile(r"(?P<m>\d{1,2})\s*[./\-]\s*(?P<d>\d{1,2})"),
]

# ---------- 案件名 ----------
_CASE_LABEL_RE = re.compile(
    r"(?:案件名|案名|案件名称)[:：]+\s*(?P<c>[^\s，。,;；]{1,40})"
)
_CASE_QUOTED_RE = re.compile(
    r"[“\"](?P<c>.{1,40}?)[”\"]\s*(?:专案|系列案|案)?"
)
_CASE_TOUCH_RE = re.compile(
    r"(?:侦破|破获|告破|打击)(?:了)?[“\"]?(?P<c>.{2,40}?)[”\"]?\s*案"
)

# ---------- 抓获人数 ----------
_ARREST_RE = re.compile(
    r"(?:抓获|抓捕|到案|落网|刑事拘留|刑拘|带回|审查)"
    r"(?:犯罪嫌疑人|嫌疑人|涉案人员|人员)?"
    r"(?:共|先后|一举|全部)?"
    r"(?P<n>[0-9０-９一二三四五六七八九十百千万两]+)"
    r"名?(?:犯罪嫌疑人|嫌疑人|人员|人)?"
)

# ---------- 缴获物品 ----------
_SEIZE_RE = re.compile(r"缴获(?P<it>[^。；;\n]{1,120})")
_SEIZE_BARE_RE = re.compile(r"缴获(?:涉案|非法)?(?:物品|物资|赃物)?(?P<it>[^。；;\n]{1,120})")

_CH_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def normalize_count(text):
    """把「两名 / 5名 / 二十三 / ２人」等计数规整为阿拉伯数字字符串；失败则原样返回。

    支持 ASCII 与全角（０-９）阿拉伯数字，以及常见中文数字（一…九、十、百、千）。
    """
    t = (text or "").strip()
    # 全角数字 → 半角
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
    """把时间归一化为「YYYY年M月D日」。

    - 仅写「月/日」时默认补**当前年份**；
    - 出现「昨天/前天/今天」等相对时间时，以**当前年月日**为基准回推；
    - 大模型/规则产出的时间均可传入（会去掉「凌晨/下午」等时刻表述）。
    """
    t = (text or "").strip()
    if not t:
        return ""
    now = datetime.now()
    today = now.date()

    if _REL_BEFORE_YESTERDAY_RE.search(t):
        d = today - timedelta(days=2)
        return f"{d.year}年{d.month}月{d.day}日"
    if _REL_YESTERDAY_RE.search(t):
        d = today - timedelta(days=1)
        return f"{d.year}年{d.month}月{d.day}日"
    if _REL_TODAY_RE.search(t):
        return f"{today.year}年{today.month}月{today.day}日"

    for pat in _DATE_ABS_PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        g = m.groupdict()
        year = int(g["y"]) if g.get("y") else now.year
        month, day = int(g["m"]), int(g["d"])
        if 1 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year}年{month}月{day}日"
        break
    return ""


def _clean_seized(s):
    """清理缴获物品文本：去首尾标点/空格、去掉结尾「等」等修饰（保留「一批」等原样）。"""
    s = (s or "").strip()
    s = s.strip(" \t，。,;；、").strip()
    s = s.rstrip("等,，、;；。 \t").strip()
    return s


# ===================== 缴获物品拆分 =====================
# 缴获段截取：从「缴获」起，到句号/分号/换行止
_SEIZE_SEGMENT_RE = re.compile(r"缴获(?P<seg>[^。；;\n]{1,200})")

# 数量未知（笼统量词），不参与数字累加
_BATCH_UNITS = ("批", "宗", "堆", "些", "余", "若干")

# 拆分：名称 + 数量 + 单位，如「手机6部 / 冰毒500克 / 现金8万元 / 笔记本电脑3台 / 手机一部」
_ITEM_RE = re.compile(
    r"^(?P<name>[^\d０-９]{1,20}?)"
    r"(?P<num>(?:[0-9０-９]+(?:[.．][0-9０-９]+)?)|(?:[一二两三四五六七八九十百千万]+))"
    r"(?:余)?(?P<unit>[\u4e00-\u9fa5]{0,3}?)(?:整|左右|以上|余元?|多)?$"
)
# 数量在前：如「50克海洛因 / 2部手机」
_ITEM_REV_RE = re.compile(
    r"^(?P<num>(?:[0-9０-９]+(?:[.．][0-9０-９]+)?)|(?:[一二两三四五六七八九十百千万]+))"
    r"(?:余)?(?P<unit>[\u4e00-\u9fa5]{1,3}?)(?P<name>[^\d０-９]{1,20})$"
)

# 物品名常见前缀（修饰语，不计入名称）
_ITEM_PREFIX_RE = re.compile(r"^(涉案|非法|违禁|走私|被盗|盗抢|来历不明|不明|赝品|假冒|高档|名牌|劣质)")
# 连续缴获的连接词（拆分后段首出现时剥掉）
_ITEM_LINK_RE = re.compile(r"^(另有|还有|以及缴获|同时缴获|并缴获|共缴获|再缴获|另缴获|又缴获|以及|及|还有|另|又|共)")

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


def _item_source(segment):
    """优先取括号内明细（如「赃物一批（现金2万元、手机1部）」取括号内容）。"""
    m = re.search(r"[（(](?P<p>[^）)]{1,100})[)）]", segment)
    if m and re.search(r"\d", m.group("p")):
        return m.group("p").strip()
    return segment


def _parse_single_item(seg):
    """解析单个缴获片段 → {category, name, quantity, unit}；解析失败返回 None。"""
    seg = seg.strip()
    if not seg:
        return None
    seg = _item_source(seg)
    seg = _ITEM_LINK_RE.sub("", seg).strip()
    seg = _ITEM_PREFIX_RE.sub("", seg).strip()
    if not seg:
        return None

    m = _ITEM_RE.match(seg)
    if m:
        name = m.group("name").strip().rstrip("的等共计超约近达逾高")
        unit = (m.group("unit") or "").strip()
        num_text = m.group("num")
        if unit in _BATCH_UNITS:
            quantity = None  # 笼统量词，无法累加
        else:
            num_str = normalize_count(num_text)
            try:
                quantity = round(float(num_str), 2)
            except (TypeError, ValueError):
                quantity = None
        if not name:
            return None
        return {
            "category": assign_category(name),
            "name": name,
            "quantity": quantity,
            "unit": unit,
        }

    # 数量在前：如「50克海洛因」
    m = _ITEM_REV_RE.match(seg)
    if m:
        name = m.group("name").strip().rstrip("的等共计超约近达逾高")
        unit = (m.group("unit") or "").strip()
        num_text = m.group("num")
        if unit in _BATCH_UNITS:
            quantity = None
        else:
            num_str = normalize_count(num_text)
            try:
                quantity = round(float(num_str), 2)
            except (TypeError, ValueError):
                quantity = None
        if not name:
            return None
        return {
            "category": assign_category(name),
            "name": name,
            "quantity": quantity,
            "unit": unit,
        }

    # 无数量的笼统写法：如「管制刀具一批 / 涉案物品若干」
    m = re.match(r"^(?P<name>.{1,20}?)(?:一批|一宗|一箱|若干|大量|多件|多台|多部)$", seg)
    if m:
        name = m.group("name").strip()
        if not name:
            return None
        return {
            "category": assign_category(name),
            "name": name,
            "quantity": None,
            "unit": "批",
        }
    return None


def parse_items(text, max_items=50):
    """从缴获物品文本中逐项拆分，返回 [{category, name, quantity, unit}, ...]。

    每项单列：物品名 + 数量 + 单位；类似物品（如电脑/笔记本）映射为同一 category，
    供「战果汇总」跨记录累加数量。
    """
    if not text or not str(text).strip():
        return []
    segment = str(text).strip()
    # 若是整段报告，先截取「缴获」后的部分
    m = _SEIZE_SEGMENT_RE.search(segment)
    if m:
        segment = m.group("seg")

    items = []
    for part in re.split(r"[、，,;；\s]+", segment):
        item = _parse_single_item(part)
        if item:
            items.append(item)
        if len(items) >= max_items:
            break
    return items


# 计量单位归并（用于跨记录累加时统一到同一单位）
_WEIGHT_TO_GRAM = {"克": 1.0, "g": 1.0, "千克": 1000.0, "公斤": 1000.0,
                   "kg": 1000.0, "斤": 500.0, "两": 50.0, "吨": 1000000.0}
_MONEY_TO_YUAN = {"元": 1.0, "块": 1.0, "千元": 1000.0, "万": 10000.0, "万元": 10000.0}
_FORMAT_UNIT = {"克": "克", "元": "元"}


def aggregate_items(record_items):
    """跨记录汇总战果：按类别归组、同类数量累加。

    record_items: [[{category,name,quantity,unit}, ...], ...]（每条记录一个列表）。
    单位统一规则：重量类归并到「克」，货币类归并到「元」，其余仅同单位累加；
    quantity 为 None 的笼统量（一批/若干）不计入数字，仅记录出现记录数。
    返回 [{category, quantity, unit, records, unknown}]，按 records 降序。
    """
    buckets = {}  # category -> {unit: {"sum": float, "records": set, "unknown": int}}
    for rec_index, items in enumerate(record_items):
        for it in items or []:
            if not isinstance(it, dict):
                continue
            cat = it.get("category") or CATEGORY_OTHER
            unit = (it.get("unit") or "").strip() or "-"
            b = buckets.setdefault(cat, {})
            u = b.setdefault(unit, {"sum": 0.0, "records": set(), "unknown": 0})
            u["records"].add(rec_index)
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
        # 尝试归并到可统一单位（重量→克，货币→元）；否则保留原单位
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
    """金额/重量数值四舍五入，去除浮点尾差。"""
    r = round(v, 2)
    return int(r) if r == int(r) else r


def _qty(total, unknown):
    """纯「一批/若干」等笼统量（无可累加数字）时以 null 表达。"""
    amt = _round(total)
    return None if amt == 0 and unknown else amt


def parse_by_rules(text):
    """规则提取五要素，返回 {字段: 值}，未命中则为空字符串。"""
    fields = {k: "" for k in FIELD_KEYS}
    if not text:
        return fields
    text = text.strip()
    if len(text) > 20000:
        text = text[:20000]

    # 主办大队
    for pat in (_UNIT_LABEL_RE, _UNIT_INLINE_RE, _UNIT_BARE_RE):
        m = pat.search(text)
        if m:
            u = m.group("u").strip()
            if u:
                fields["主办大队"] = u
                break
    # 案情单位名里常带「队」，但『收网』不一定是单位，裸匹配再做一次轻校验
    if fields["主办大队"] and not any(k in fields["主办大队"]
                                      for k in ("大队", "支队", "中队", "专班", "专案组", "工作组")):
        fields["主办大队"] = ""

    # 时间（仅月/日自动补当前年份；昨天/前天等以当前年月日回推）
    fields["时间"] = normalize_time(text)

    # 案件名：标签 > 引号 > 侦破式
    for pat in (_CASE_LABEL_RE, _CASE_QUOTED_RE, _CASE_TOUCH_RE):
        m = pat.search(text)
        if not m:
            continue
        c = m.group("c").strip()
        if len(c) >= 2 and not (len(c) == 2 and c[0] in "本该个此这"):
            fields["案件名"] = c
            break

    # 抓获人数
    m = _ARREST_RE.search(text)
    if m:
        cnt = normalize_count(m.group("n"))
        if cnt:
            fields["抓获人数"] = cnt

    # 缴获物品
    for pat in (_SEIZE_RE, _SEIZE_BARE_RE):
        m = pat.search(text)
        if m:
            it = _clean_seized(m.group("it"))
            if it:
                fields["缴获物品"] = it
                break

    return fields