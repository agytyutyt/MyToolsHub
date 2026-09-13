"""数字格式引擎 —— 对应 TS 版 ``packages/style/src/numfmt.ts``（SSF 的位置）。

TS 版用 SheetJS 的 SSF 库；Python 生态没有等价物，这里按《设计文档》6.5.3
的"自研子集"路线实现，覆盖：

  · 段结构      pos;neg;zero;text（含 [>100] 条件段与 [Red] 颜色段）
  · 数字占位    0 # ? . ,（千分位与"以千为单位缩放"）
  · 百分比      %（值 ×100）
  · 科学计数    0.00E+00 / ##0.0E+0（工程计数）
  · 分数        # ?/? 与 # ??/??
  · 日期时间    y/m/d/h/s 全套，含 ★ m 的月/分歧义、[h] 累计时长、AM/PM
  · 字面量      "..." 引号、\\x 转义、_x 留空格、*x 丢弃填充、裸货币符号（¥ $ €）
  · General     仿 Excel 的 11 位有效数字 + 大小阈值科学计数

Excel 的显示规则比 SSF 更宽容：裸的未知字符一律按字面量输出（这正是
TS 版 13.4 节"自动补引号加固"想要的效果），因此本实现无需 repair 逻辑。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional, Tuple, Union

from .date import is_date_format, to_serial_1900

#: 内置格式 id（0-49）→ 格式码 —— 依据《设计文档》6.5.2
BUILTIN_NUMFMT: dict[int, str] = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    5: "$#,##0_);($#,##0)",
    6: "$#,##0_);[Red]($#,##0)",
    7: "$#,##0.00_);($#,##0.00)",
    8: "$#,##0.00_);[Red]($#,##0.00)",
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    12: "# ?/?",
    13: "# ??/??",
    14: "m/d/yy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yy h:mm",
    37: "#,##0_);(#,##0)",
    38: "#,##0_);[Red](#,##0)",
    39: "#,##0.00_);(#,##0.00)",
    40: "#,##0.00_);[Red](#,##0.00)",
    41: '_(* #,##0_);_(* \\(#,##0\\);_(* "-"_);_(@_)',
    42: '_("$"* #,##0_);_("$"* \\(#,##0\\);_("$"* "-"_);_(@_)',
    43: '_(* #,##0.00_);_(* \\(#,##0.00\\);_(* "-"??_);_(@_)',
    44: '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)',
    45: "mm:ss",
    46: "[h]:mm:ss",
    47: "mmss.0",
    48: "##0.0E+0",
    49: "@",
}

#: 内置日期格式 id（这些 id 的行为与 locale 相关，用 code 更稳）
BUILTIN_DATE_IDS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

_MAX_DECIMALS = 30


@dataclass
class FormatValueResult:
    """显示文本 + 是否发生过降级（调用方可记 warning）。"""

    text: str
    degraded: bool = False


# ─────────────────────────────────────────────────────────────
# 段（section）解析
# ─────────────────────────────────────────────────────────────


@dataclass
class _Section:
    raw: str
    condition: Optional[Tuple[str, float]] = None  # (op, num)
    has_at: bool = False


def _split_sections(code: str) -> List[_Section]:
    """按 ';' 切段；引号与转义内的 ';' 不切。"""
    sections: List[str] = []
    buf: List[str] = []
    in_quote = False
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        if ch == '"':
            in_quote = not in_quote
            buf.append(ch)
        elif ch == "\\" and i + 1 < n:
            buf.append(code[i : i + 2])
            i += 1
        elif ch == ";" and not in_quote:
            sections.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    sections.append("".join(buf))

    out: List[_Section] = []
    for raw in sections:
        sec = _Section(raw=raw, has_at="@" in raw)
        m = re.match(r"^\s*\[(>=|<=|<>|>|<|=)\s*([0-9.]+)\]", raw)
        if m:
            sec.condition = (m.group(1), float(m.group(2)))
        out.append(sec)
    return out


def _cond_match(op: str, num: float, value: float) -> bool:
    if op == ">":
        return value > num
    if op == "<":
        return value < num
    if op == ">=":
        return value >= num
    if op == "<=":
        return value <= num
    if op == "=":
        return value == num
    if op == "<>":
        return value != num
    return False


def _pick_section(sections: List[_Section], serial: float) -> Tuple[Optional[_Section], float]:
    """选段并返回该段应使用的值（负数段取绝对值）。"""
    conds = [s for s in sections if s.condition]
    for s in conds:
        assert s.condition
        if _cond_match(s.condition[0], s.condition[1], serial):
            return s, serial
    plain = [s for s in sections if not s.condition]
    if not plain:
        return None, serial
    if len(plain) == 1:
        s = plain[0]
        if serial < 0 and not _is_date_like(s):
            return s, serial  # 单段 + 负数：格式化后自动加 '-'
        return s, serial
    if serial < 0:
        return plain[1], abs(serial)
    if serial == 0 and len(plain) >= 3:
        return plain[2], serial
    return plain[0], serial


def _is_date_like(sec: _Section) -> bool:
    return is_date_format(sec.raw)


# ─────────────────────────────────────────────────────────────
# 字面量扫描
# ─────────────────────────────────────────────────────────────


def _round_half_away(value: float, decimals: int) -> Decimal:
    """Excel 显示用四舍五入（half away from zero；Python 的 round 是银行家舍入）。"""
    d = Decimal(str(value))
    q = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    return d.quantize(q, rounding=ROUND_HALF_UP)


@dataclass
class _NumericShape:
    prefix: str = ""
    suffix: str = ""
    int_part: str = ""        # 0#? 占位符序列（不含千分位逗号）
    frac_part: str = ""       # 小数点后的 0#? 占位符
    has_dot: bool = False
    thousands: bool = False   # ',' 出现在占位符之间 → 千分位
    scale_commas: int = 0     # 尾部逗号数量 → ×1/1000^n
    percent: int = 0          # '%' 出现次数 → ×100^n
    exp_part: str = ""        # 指数占位符
    exp_plus: bool = False    # E+ / E-


def _scan_numeric(sec_raw: str) -> _NumericShape:
    """把一个数字段扫描成结构化形状。未知字符一律当字面量（宽容模式）。"""
    shape = _NumericShape()
    # literal 累积到 prefix（还没出现占位符）或 suffix（出现过占位符/小数点后）
    state = {"seen_placeholder": False}
    buf: List[str] = []

    def flush(target: str) -> None:
        text = "".join(buf)
        buf.clear()
        if target == "pre":
            shape.prefix += text
        elif target == "suf":
            shape.suffix += text

    i = 0
    n = len(sec_raw)
    comma_marks: List[int] = []  # 逗号出现时已累积的数字占位符数
    digits_seen = 0

    def append_literal(s: str) -> None:
        buf.append(s)

    while i < n:
        ch = sec_raw[i]
        if ch == '"':
            j = sec_raw.find('"', i + 1)
            if j < 0:
                append_literal(sec_raw[i + 1 :])
                i = n
            else:
                append_literal(sec_raw[i + 1 : j])
                i = j
        elif ch == "\\":
            if i + 1 < n:
                append_literal(sec_raw[i + 1])
                i += 1
        elif ch == "_":
            if i + 1 < n:
                append_literal(" ")
                i += 1
        elif ch == "*":
            if i + 1 < n:
                i += 1  # 填充字符：CSS 无对应，直接丢弃
        elif ch == "[":
            j = sec_raw.find("]", i)
            i = j if j >= 0 else n  # 颜色/条件：文本输出时丢弃
        elif ch in "0#?":
            flush("pre" if not state["seen_placeholder"] else "suf")
            state["seen_placeholder"] = True
            if shape.has_dot:
                shape.frac_part += ch
            else:
                shape.int_part += ch
            digits_seen += 1
        elif ch == ".":
            flush("pre" if not state["seen_placeholder"] else "suf")
            if not shape.has_dot:
                shape.has_dot = True
                state["seen_placeholder"] = True
            else:
                append_literal(".")
        elif ch == ",":
            if digits_seen > 0:
                comma_marks.append(digits_seen)
            else:
                append_literal(",")  # 占位符之前的逗号：字面量
        elif ch == "%":
            flush("pre" if not state["seen_placeholder"] else "suf")
            state["seen_placeholder"] = True
            shape.percent += 1
            append_literal("%")  # % 既是 ×100 的指令，也要出现在输出里
        elif ch in "Ee" and i + 1 < n and sec_raw[i + 1] in "+-":
            flush("pre" if not state["seen_placeholder"] else "suf")
            shape.exp_plus = sec_raw[i + 1] == "+"
            i += 1
            j = i + 1
            while j < n and sec_raw[j] in "0#?":
                shape.exp_part += sec_raw[j]
                j += 1
            i = j - 1
        else:
            append_literal(ch)
        i += 1

    flush("pre" if not state["seen_placeholder"] else "suf")

    # 逗号语义：之后不再有数字占位符的逗号是"以千为单位缩放"，
    # 占位符之间的是千分位分隔符
    if comma_marks:
        total_digits = len(shape.int_part) + len(shape.frac_part)
        trailing = sum(1 for dpos in comma_marks if dpos == total_digits)
        inner = len(comma_marks) - trailing
        if inner:
            shape.thousands = True
        shape.scale_commas = trailing

    return shape


# ─────────────────────────────────────────────────────────────
# 数字格式化
# ─────────────────────────────────────────────────────────────


def _group_thousands(int_str: str) -> str:
    out = []
    for idx, ch in enumerate(reversed(int_str)):
        if idx and idx % 3 == 0:
            out.append(",")
        out.append(ch)
    return "".join(reversed(out))


def _format_fixed(value: float, shape: _NumericShape) -> str:
    # 小数位 = frac_part 全部占位符个数；'0' 强制补零，'#'/'?' 可省略
    decimals = len(shape.frac_part)
    min_frac = sum(1 for ch in shape.frac_part if ch == "0")

    scaled = value
    if shape.percent:
        scaled = scaled * (100 ** shape.percent)
    if shape.scale_commas:
        scaled = scaled / (1000 ** shape.scale_commas)

    q = _round_half_away(scaled, decimals)
    neg = q < 0
    q = -q if neg else q
    s = format(q, "f")
    if "." in s:
        int_str, frac_str = s.split(".", 1)
    else:
        int_str, frac_str = s, ""

    # 去掉多余的小数尾随 0（保留 min_frac 位）
    if len(frac_str) > min_frac:
        frac_str = frac_str[:min_frac] + frac_str[min_frac:].rstrip("0")
        if len(frac_str) < min_frac:
            frac_str = frac_str.ljust(min_frac, "0")

    # 整数部分：补足 '0' 占位符要求的位数
    min_int = sum(1 for ch in shape.int_part if ch == "0")
    if len(int_str) < min_int:
        int_str = int_str.rjust(min_int, "0")
    if int_str == "0" and (shape.int_part and "0" not in shape.int_part):
        # int part 只有 # 占位且值为 0 → 输出空（Excel 行为）
        int_str = ""

    # '?' 的空格填充：整数部分前导空格
    width_int = len(shape.int_part)
    if width_int > len(int_str):
        pad = width_int - len(int_str)
        spaces = "".join(" " if ph == "?" else "" for ph in shape.int_part[:pad])
        int_str = spaces + int_str
    # 小数部分尾随空格（'?' 占位）
    width_frac = len(shape.frac_part)
    if width_frac > len(frac_str):
        tail = shape.frac_part[len(frac_str):]
        spaces = "".join(" " if ph == "?" else "" for ph in tail)
        frac_str = frac_str + spaces

    if shape.thousands and int_str:
        int_str = _group_thousands(int_str)

    body = int_str
    if shape.has_dot:
        body += "." + frac_str

    prefix = shape.prefix
    suffix = shape.suffix
    if neg:
        # 无独立负数段时 Excel 在整个输出外加 '-'
        if prefix.startswith("(") and suffix.endswith(")"):
            pass  # 会计式括号由段结构给出
        prefix = "-" + prefix
    return prefix + body + suffix


def _format_scientific(value: float, shape: _NumericShape) -> str:
    frac_digits = len(shape.frac_part)
    exp_digits = len(shape.exp_part) or 1
    engineering = "#" in shape.int_part
    x = value
    if shape.percent:
        x = x * (100 ** shape.percent)
    if shape.scale_commas:
        x = x / (1000 ** shape.scale_commas)

    exp = 0
    mant = x
    if x != 0:
        exp = int(math.floor(math.log10(abs(x))))
        if engineering:
            exp = exp - (exp % 3) if exp >= 0 else exp - ((exp % 3) - 3) % 3
        mant = x / (10 ** exp)
        # 四舍五入到 frac 位后可能进位（如 9.999 → 10.00）
        q = _round_half_away(mant, frac_digits)
        if abs(q) >= 10 ** (1 if not engineering else len(shape.int_part)):
            exp += 3 if engineering else 1
            mant = x / (10 ** exp)
        else:
            mant = float(q)

    mant_str = f"{abs(mant):.{frac_digits}f}" if frac_digits else str(int(round(abs(mant))))
    int_len = len(shape.int_part) or 1
    if len(mant_str.split(".")[0]) > int_len and engineering:
        # 工程计数 mantissa 不应超过整数占位宽度
        exp += 3
        mant = x / (10 ** exp)
        mant_str = f"{abs(mant):.{frac_digits}f}" if frac_digits else str(int(round(abs(mant))))

    sign = "-" if (mant < 0 or (mant == 0 and math.copysign(1, mant) < 0)) else ""
    exp_str = str(abs(exp)).rjust(exp_digits, "0")
    exp_sign = ("+" if exp >= 0 else "-") if shape.exp_plus or exp < 0 else ""
    return f"{shape.prefix}{sign}{mant_str}E{exp_sign}{exp_str}{shape.suffix}"


def _literalize(text: str) -> str:
    """把一段格式码里的字面量提取出来：引号 / 转义 / _x / *x / [..] 按规则处理。"""
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = text.find('"', i + 1)
            if j < 0:
                out.append(text[i + 1 :])
                i = n
            else:
                out.append(text[i + 1 : j])
                i = j
        elif ch == "\\":
            if i + 1 < n:
                out.append(text[i + 1])
                i += 1
        elif ch == "_":
            if i + 1 < n:
                out.append(" ")
                i += 1
        elif ch == "*":
            if i + 1 < n:
                i += 1
        elif ch == "[":
            j = text.find("]", i)
            i = j if j >= 0 else n  # 颜色/条件：丢弃
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _format_fraction(value: float, sec_raw: str, shape: _NumericShape) -> str:
    # 分数模板可能带整数占位（'# ?/??'）也可能不带
    m = re.search(r"([0#?]+)\s+([0#?]+)\s*/\s*([0#?]+)", sec_raw)
    int_ph = ""
    if m:
        int_ph, num_ph, den_ph = m.group(1), m.group(2), m.group(3)
    else:
        m = re.search(r"([0#?]+)\s*/\s*([0#?]+)", sec_raw)
        if not m:
            return _format_fixed(value, shape)
        num_ph, den_ph = m.group(1), m.group(2)

    max_den = 10 ** len(den_ph) - 1
    scaled = value
    if shape.percent:
        scaled = scaled * (100 ** shape.percent)
    if shape.scale_commas:
        scaled = scaled / (1000 ** shape.scale_commas)

    from fractions import Fraction

    frac = Fraction(scaled).limit_denominator(max_den)
    whole = abs(frac.numerator) // frac.denominator if frac.denominator != 1 else abs(frac.numerator)
    rem = abs(frac) - whole
    sign = "-" if frac < 0 else ""

    # ⚠️ SSF/Excel 的空格语义（对照 SSF 真值校准）：
    #   · 整数为 0 且整数占位只含 '#'/'?' → 整数部分输出空串（占位符留白）
    #   · num 右对齐、den 左对齐到同一宽度 W = max(len(num_ph), len(den_ph))
    #   验证：'# ?/?' 0.125→' 1/8'；'# ??/??' 0.125→'  1/8 '；42.5→'42  1/2 '
    #         '# ?/??' 0.5→'  1/2 '；5.125 '# ?/?'→'5 1/8'
    prefix = shape.prefix
    suffix = _literalize(sec_raw[m.end():])
    if whole == 0:
        whole_str = "0" if int_ph and "0" in int_ph else ""
    elif int_ph:
        whole_str = str(whole)
    else:
        whole_str = ""  # 无整数占位：不输出整数部分

    if frac.denominator == 1 or rem == 0:
        body = whole_str or "0"
        return f"{prefix}{sign}{body}{suffix}"

    width = max(len(num_ph), len(den_ph))
    num_str = str(rem.numerator).rjust(width)
    den_str = str(rem.denominator).ljust(width)
    frac_str = f"{num_str}/{den_str}"
    # 整数占位与分数之间的字面量空格始终保留（whole 为 0 留白时也要有），
    # SSF 真值：'# ??/??' 0.125 → '  1/8 '（1 个分隔空格 + num 右对齐 1 个空格）
    body = f"{whole_str} {frac_str}" if int_ph else frac_str
    return f"{prefix}{sign}{body}{suffix}"


# ─────────────────────────────────────────────────────────────
# 日期格式化
# ─────────────────────────────────────────────────────────────


def _scan_date_tokens(sec_raw: str) -> List[Tuple[str, str]]:
    """扫描日期段的 token。返回 [('lit', text) | ('tok', code), ...]。"""
    tokens: List[Tuple[str, str]] = []
    i = 0
    n = len(sec_raw)
    while i < n:
        ch = sec_raw[i]
        if ch == '"':
            j = sec_raw.find('"', i + 1)
            if j < 0:
                tokens.append(("lit", sec_raw[i + 1 :]))
                i = n
            else:
                tokens.append(("lit", sec_raw[i + 1 : j]))
                i = j
        elif ch == "\\":
            if i + 1 < n:
                tokens.append(("lit", sec_raw[i + 1]))
                i += 1
        elif ch == "_":
            if i + 1 < n:
                tokens.append(("lit", " "))
                i += 1
        elif ch == "*":
            if i + 1 < n:
                i += 1
        elif ch == "[":
            j = sec_raw.find("]", i)
            content = sec_raw[i + 1 : j if j >= 0 else n]
            if re.fullmatch(r"\[?h+|m+|s+", content, re.IGNORECASE):
                tokens.append(("tok", "[" + content.lower() + "]"))
            # 其余（颜色/条件/区域码）丢弃
            i = j if j >= 0 else n
        elif ch in "Aa" and sec_raw[i : i + 5].upper() == "AM/PM":
            tokens.append(("tok", "AM/PM"))
            i += 4
        elif ch in "Aa" and sec_raw[i : i + 3].upper() == "A/P":
            tokens.append(("tok", "A/P"))
            i += 2
        else:
            matched = False
            for pat, code in (
                ("yyyy", "yyyy"), ("yy", "yy"), ("y", "yy"),
                ("mmmmm", "mmmmm"), ("mmmm", "mmmm"), ("mmm", "mmm"), ("mm", "mm"), ("m", "m"),
                ("dddd", "dddd"), ("ddd", "ddd"), ("dd", "dd"), ("d", "d"),
                ("hh", "hh"), ("h", "h"), ("ss", "ss"), ("s", "s"),
            ):
                if sec_raw[i : i + len(pat)].lower() == pat:
                    tokens.append(("tok", code))
                    i += len(pat) - 1
                    matched = True
                    break
            if not matched:
                if ch == "." and i + 1 < n and sec_raw[i + 1] == "0":
                    j = i + 1
                    while j < n and sec_raw[j] == "0":
                        j += 1
                    tokens.append(("tok", "." + "0" * (j - i - 1)))  # 小数秒
                    i = j - 1
                else:
                    tokens.append(("lit", ch))
        i += 1
    return tokens


def _resolve_m_tokens(tokens: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """★ m 歧义消解：前一个时间 token 是 h（或后一个是 s）→ 分钟，否则月份。

    分钟 token 改写成 'mi' / 'mmi'（内部标记，保留宽度信息；
    不能用 '[m]' 形式 —— 那会与真正的 elapsed 段 [h]/[m]/[s] 混淆）。
    """
    out = list(tokens)
    tok_idx = [i for i, (k, _) in enumerate(out) if k == "tok"]
    for pos, i in enumerate(tok_idx):
        kind, code = out[i]
        if code in ("m", "mm"):
            prev = out[tok_idx[pos - 1]][1] if pos > 0 else ""
            nxt = out[tok_idx[pos + 1]][1] if pos + 1 < len(tok_idx) else ""
            prev_is_h = prev in ("h", "hh", "[h]", "[hh]")
            next_is_s = nxt in ("s", "ss")
            if prev_is_h or next_is_s:
                out[i] = ("tok", "mmi" if code == "mm" else "mi")
    return out


def _format_date(serial: float, sec_raw: str) -> str:
    from .date import serial_to_date

    tokens = _resolve_m_tokens(_scan_date_tokens(sec_raw))
    tok_codes = [c for k, c in tokens if k == "tok"]

    # elapsed 只有在格式里真的写了 [h]/[m]/[s] 时才算（'mi' 是歧义消解的内部标记）
    elapsed_set = {"[h]", "[hh]", "[m]", "[mm]", "[s]", "[ss]"}
    has_elapsed = any(c in elapsed_set for c in tok_codes)
    has_meridiem = any(c in ("AM/PM", "A/P") for c in tok_codes)
    has_ymd = any(
        c in ("yyyy", "yy", "m", "mm", "mmm", "mmmm", "mmmmm", "d", "dd", "ddd", "dddd")
        for c in tok_codes
    )
    has_hms = any(c in ("h", "hh", "s", "ss", "mi", "mmi") for c in tok_codes) or has_elapsed

    parts: List[str] = []

    if has_elapsed:
        # 累计时长语义：[h] = 不进位的总小时数（如 2.5 天 → 60:00:00）
        total_sec = int(round(abs(serial) * 86400))
        for kind, code in tokens:
            if kind == "lit":
                parts.append(code)
                continue
            if code == "[h]":
                parts.append(str(total_sec // 3600))
            elif code == "[hh]":
                parts.append(str(total_sec // 3600).rjust(2, "0"))
            elif code == "[m]":
                parts.append(str(total_sec // 60))
            elif code == "[mm]":
                parts.append(str(total_sec // 60).rjust(2, "0"))
            elif code == "[s]":
                parts.append(str(total_sec))
            elif code == "[ss]":
                parts.append(str(total_sec).rjust(2, "0"))
            elif code in ("h", "hh"):
                rem = (total_sec // 3600) % 24
                parts.append(str(rem).rjust(2 if code == "hh" else 1, "0"))
            elif code in ("mi", "mmi"):
                rem = (total_sec % 3600) // 60
                parts.append(str(rem).rjust(2 if code == "mmi" else 1, "0"))
            elif code in ("s", "ss"):
                rem = total_sec % 60
                parts.append(str(rem).rjust(2 if code == "ss" else 1, "0"))
            else:
                parts.append("")
        return "".join(parts)

    if serial < 0:
        return "####"

    days = int(math.floor(serial))
    frac = serial - days
    total_sec = int(round(frac * 86400))
    if total_sec >= 86400:  # 23:59:59.5+ 进位到次日
        days += 1
        total_sec -= 86400
    dt = serial_to_date(days)
    h24 = total_sec // 3600
    minute = (total_sec % 3600) // 60
    second = total_sec % 60
    frac_second = frac * 86400 - math.floor(frac * 86400)

    hour_out = h24
    if has_meridiem:
        hour_out = h24 % 12
        if hour_out == 0:
            hour_out = 12

    for kind, code in tokens:
        if kind == "lit":
            parts.append(code)
            continue
        if code == "yyyy":
            parts.append(f"{dt.year:04d}")
        elif code == "yy":
            parts.append(f"{dt.year % 100:02d}")
        elif code == "m":
            parts.append(str(dt.month))
        elif code == "mm":
            parts.append(f"{dt.month:02d}")
        elif code == "mmm":
            parts.append(_MONTH_NAMES[dt.month - 1][:3])
        elif code == "mmmm":
            parts.append(_MONTH_NAMES[dt.month - 1])
        elif code == "mmmmm":
            parts.append(_MONTH_NAMES[dt.month - 1][0])
        elif code == "d":
            parts.append(str(dt.day))
        elif code == "dd":
            parts.append(f"{dt.day:02d}")
        elif code == "ddd":
            parts.append(_DAY_NAMES[(dt.weekday() + 1) % 7][:3])
        elif code == "dddd":
            parts.append(_DAY_NAMES[(dt.weekday() + 1) % 7])
        elif code == "h":
            parts.append(str(hour_out))
        elif code == "hh":
            parts.append(f"{hour_out:02d}")
        elif code == "mi":
            parts.append(str(minute))
        elif code == "mmi":
            parts.append(f"{minute:02d}")
        elif code == "s":
            parts.append(str(second))
        elif code == "ss":
            parts.append(f"{second:02d}")
        elif code.startswith(".0"):
            digits = len(code) - 1
            parts.append("." + f"{frac_second:.{digits}f}"[2:])
        elif code == "AM/PM":
            parts.append("AM" if h24 < 12 else "PM")
        elif code == "A/P":
            parts.append("A" if h24 < 12 else "P")
        else:
            parts.append("")
    return "".join(parts)


# ─────────────────────────────────────────────────────────────
# General
# ─────────────────────────────────────────────────────────────


def _format_general(value: float) -> str:
    """仿 Excel 的 General：11 位有效数字；大数/极小数走科学计数。"""
    if value == 0:
        return "0"
    if float(value).is_integer() and abs(value) < 1e11:
        return str(int(value))  # 11 位以内的整数原样显示；12 位起 Excel 转科学计数
    a = abs(value)
    if a >= 1e11 or a < 1e-5:
        s = f"{value:.5E}"
        mant, exp = s.split("E")
        mant = mant.rstrip("0").rstrip(".") if "." in mant else mant
        exp_n = int(exp)
        # ⚠️ Excel 的指数至少 2 位（0.0000001 → 1E-07，不是 1E-7）
        exp_str = f"{abs(exp_n):02d}"
        sign = "+" if exp_n >= 0 else "-"
        return f"{mant}E{sign}{exp_str}"
    s = f"{value:.11g}"
    # Python 小写 e → Excel 风格大写 E（如 1e-05 → 1E-05）
    s = re.sub(r"e([+-])(\d+)", lambda m: f"E{m.group(1)}{m.group(2).zfill(2)}", s)
    return s


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────


def format_value(value: Union[None, str, bool, int, float], code: str, date1904: bool = False) -> FormatValueResult:
    """按格式码把值渲染为显示文本。

    @param value   原始值（number / string / bool / None）
    @param code    格式码，如 '#,##0.00'；'General' 表示常规
    @param date1904 是否 1904 日期系统
    """
    if value is None:
        return FormatValueResult("")
    if isinstance(value, bool):
        return FormatValueResult("TRUE" if value else "FALSE")
    if isinstance(value, str):
        return FormatValueResult(value)

    fmt = (code or "General").strip() or "General"

    try:
        serial = to_serial_1900(float(value), date1904)
        if not math.isfinite(serial):
            return FormatValueResult(str(value), degraded=True)
        return FormatValueResult(_format_serial(serial, fmt))
    except Exception:
        return FormatValueResult(str(value), degraded=True)


def _format_serial(serial: float, fmt: str) -> str:
    if fmt == "General":
        return _format_general(serial)

    sections = _split_sections(fmt)
    if not sections:
        return _format_general(serial)
    # 首段就是裸 '@'（格式 id 49）→ 数字按 General 文本显示
    if len(sections) >= 1 and sections[0].raw.strip() == "@":
        return _format_general(serial)
    if len(sections) == 1 and not sections[0].raw.strip():
        return _format_general(serial)

    sec, val = _pick_section(sections, serial)
    if sec is None:
        return ""
    raw = sec.raw
    if not raw.strip():
        return ""

    if is_date_format(raw):
        return _format_date(val, raw)

    shape = _scan_numeric(raw)
    if shape.exp_part:
        return _format_scientific(val, shape)
    if re.search(r"[0#?]\s*/\s*[0#?]", raw):
        return _format_fraction(val, raw, shape)
    if shape.int_part or shape.frac_part or shape.has_dot:
        return _format_fixed(val, shape)
    # 没有任何数字占位符：原样输出字面量
    return shape.prefix + shape.suffix


def build_num_fmt_map(custom: Optional[dict[int, str]] = None) -> dict[int, str]:
    """合并内置表与自定义表，得到完整的 numFmtId → code 映射。"""
    out = dict(BUILTIN_NUMFMT)
    if custom:
        out.update({int(k): v for k, v in custom.items()})
    return out


def num_fmt_code_of(fmt_id: Optional[int], num_fmt_map: dict[int, str]) -> str:
    """由 id 取格式码；未知 id 返回 'General'。"""
    if fmt_id is None:
        return "General"
    return num_fmt_map.get(int(fmt_id), "General")
