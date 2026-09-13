"""极简表达式求值器 —— 对应 TS 版 ``packages/style/src/expr.ts``。

用途：条件格式里的 expression / cellIs 规则需要判断布尔表达式。
不做完整公式引擎，只支持最常见子集；解析不了一律返回 None（上层跳过规则）。

支持：
  比较      >  <  >=  <=  =  <>（含 Excel 习惯的单 = 表示相等）
  逻辑      AND(…) OR(…) NOT(…)
  算术      + - * / ^ 与文本连接 &
  括号、数字、字符串、TRUE/FALSE
  单元格引用 A1 / $A$1 / Sheet1!A1（取 A1 部分）；区域 A1:B10 → 取左上角

⚠️ 安全（TS 版 14.8 的教训）：
  TS 版拼接字符串进 ``new Function``，必须靠「字符黑名单 + 严格分词」双防线防注入
  （``alert(1)`` 全由合法字符组成，单靠字符白/黑名单挡不住）。
  Python 版**不用 eval/exec**，改为递归下降解析成 AST 后自行求值 ——
  任何不认识的 token 在解析阶段直接拒绝，注入面从语言层面消除，
  同时黑名单仍然保留（把 `;` `{}` `[]` `` ` `` `\\` 等挡在门外，语义与 TS 一致）。

⚠️ Excel 公式里字符串只用双引号；单引号是工作表名定界符（'我的 表'!A1）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Union

from ..core import col_from_name, col_name

#: 表达式长度上限：超过即拒绝（正常 CF 公式都在 200 字符内）
MAX_EXPR_LEN = 500

#: 危险字符黑名单。用黑名单而非白名单的原因：白名单会把中文一并挡掉，
#: 而条件格式里 A1="已完成" 这类中文比较极其常见。
_CHAR_BLACKLIST = re.compile(r"[\x00-\x1f;\[\]{}`\\]")

#: Excel 逻辑函数（大小写不敏感）
FUNCS = {"AND", "OR", "NOT"}

RE_NUM = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
RE_REF = re.compile(
    r"(?:(?:'[^']*'|[A-Za-z0-9_]+)!)?(\$?[A-Za-z]{1,3}\$?[0-9]{1,7})"
    r"(?:\s*:\s*(?:(?:'[^']*'|[A-Za-z0-9_]+)!)?\$?[A-Za-z]{1,3}\$?[0-9]{1,7})?"
)
RE_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


class _ExprError(Exception):
    """解析/求值失败 —— 内部信号，对外统一折叠成 None。"""


# ─────────────────────────────────────────────────────────────
# AST
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Str:
    value: str


@dataclass(frozen=True)
class Bool:
    value: bool


@dataclass(frozen=True)
class Ref:
    a1: str


@dataclass(frozen=True)
class Bin:
    op: str
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class Unary:
    op: str
    operand: "Node"


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple  # tuple[Node, ...]


Node = Union[Num, Str, Bool, Ref, Bin, Unary, Call]

# ─────────────────────────────────────────────────────────────
# 分词 + 递归下降解析
# ─────────────────────────────────────────────────────────────


class _Parser:
    def __init__(self, src: str) -> None:
        self.src = src
        self.i = 0
        self.n = len(src)

    def parse(self) -> Node:
        node = self.parse_cmp()
        if self.i < self.n:
            raise _ExprError(f"trailing input at {self.i}")
        return node

    def _skip_ws(self) -> None:
        while self.i < self.n and self.src[self.i].isspace():
            self.i += 1

    def parse_cmp(self) -> Node:
        left = self.parse_concat()
        self._skip_ws()
        while self.i < self.n:
            two = self.src[self.i : self.i + 2]
            if two in (">=", "<=", "<>"):
                op = two
                self.i += 2
            elif self.src[self.i] in "<>=":
                op = self.src[self.i]
                self.i += 1
            else:
                break
            right = self.parse_concat()
            left = Bin(op, left, right)
            self._skip_ws()
        return left

    def parse_concat(self) -> Node:
        left = self.parse_add()
        self._skip_ws()
        while self.i < self.n and self.src[self.i] == "&":
            self.i += 1
            left = Bin("&", left, self.parse_add())
            self._skip_ws()
        return left

    def parse_add(self) -> Node:
        left = self.parse_mul()
        self._skip_ws()
        while self.i < self.n and self.src[self.i] in "+-":
            op = self.src[self.i]
            self.i += 1
            left = Bin(op, left, self.parse_mul())
            self._skip_ws()
        return left

    def parse_mul(self) -> Node:
        left = self.parse_pow()
        self._skip_ws()
        while self.i < self.n and self.src[self.i] in "*/":
            op = self.src[self.i]
            self.i += 1
            left = Bin(op, left, self.parse_pow())
            self._skip_ws()
        return left

    def parse_pow(self) -> Node:
        left = self.parse_unary()
        self._skip_ws()
        if self.i < self.n and self.src[self.i] == "^":
            self.i += 1
            return Bin("^", left, self.parse_pow())  # 右结合
        return left

    def parse_unary(self) -> Node:
        self._skip_ws()
        if self.i < self.n and self.src[self.i] in "+-":
            op = self.src[self.i]
            self.i += 1
            return Unary(op, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> Node:
        self._skip_ws()
        if self.i >= self.n:
            raise _ExprError("unexpected end")
        src, i = self.src, self.i
        ch = src[i]

        # 字符串字面量（只认双引号；"" 转义）
        if ch == '"':
            j = i + 1
            text: List[str] = []
            while j < self.n:
                if src[j] == '"':
                    if j + 1 < self.n and src[j + 1] == '"':
                        text.append('"')
                        j += 2
                        continue
                    break
                text.append(src[j])
                j += 1
            else:
                raise _ExprError("unterminated string")
            if j >= self.n:
                raise _ExprError("unterminated string")
            self.i = j + 1
            return Str("".join(text))

        # 括号
        if ch == "(":
            self.i += 1
            node = self.parse_cmp()
            self._skip_ws()
            if self.i >= self.n or self.src[self.i] != ")":
                raise _ExprError("expected )")
            self.i += 1
            return node

        # 数字
        m = RE_NUM.match(src, i)
        if m:
            self.i = m.end()
            return Num(float(m.group(0)))

        # 单元格引用／区域（要先于标识符：A1 既像引用也像标识符）
        m = RE_REF.match(src, i)
        if m:
            self.i = m.end()
            return Ref(m.group(1).replace("$", "").upper())

        # 标识符：函数 / 布尔 / 非法
        m = RE_IDENT.match(src, i)
        if m:
            name = m.group(0).upper()
            self.i = m.end()
            if name in FUNCS:
                self._skip_ws()
                if self.i >= self.n or self.src[self.i] != "(":
                    raise _ExprError(f"expected ( after {name}")
                self.i += 1
                args: List[Node] = []
                self._skip_ws()
                if self.i < self.n and self.src[self.i] == ")":
                    self.i += 1
                else:
                    while True:
                        args.append(self.parse_cmp())
                        self._skip_ws()
                        if self.i < self.n and self.src[self.i] == ",":
                            self.i += 1
                            continue
                        if self.i < self.n and self.src[self.i] == ")":
                            self.i += 1
                            break
                        raise _ExprError("expected , or ) in args")
                return Call(name, tuple(args))
            if name in ("TRUE", "FALSE"):
                return Bool(name == "TRUE")
            # ⚠️ 未知标识符：SUM / alert / Foo 等一律拒绝（防注入的关键一环）
            raise _ExprError(f"unknown identifier {name}")

        raise _ExprError(f"unexpected char {ch!r}")


def compile_expr(src: str) -> Optional[Node]:
    """Excel 风格表达式 → AST。遇到任何不认识的 token 返回 None。"""
    if not src or len(src) > MAX_EXPR_LEN or _CHAR_BLACKLIST.search(src):
        return None
    try:
        return _Parser(src.strip()).parse()
    except _ExprError:
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 求值（Excel 语义的类型协同）
# ─────────────────────────────────────────────────────────────

CellGetter = Callable[[str], object]


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _truthy(v: object) -> bool:
    """Excel 的布尔协同：数字非零为真；字符串不能转布尔（#VALUE!）。"""
    if isinstance(v, bool):
        return v
    if _is_number(v):
        return v != 0
    if v is None:
        return False
    raise _ExprError("not a boolean")


def _compare(op: str, a: object, b: object) -> bool:
    """Excel 比较语义：数字 < 文本 < FALSE < TRUE；字符串比较不区分大小写。"""
    # None（空格）协同为对侧的零值
    if a is None and b is None:
        a = b = 0
    elif a is None:
        a = "" if isinstance(b, str) else (False if isinstance(b, bool) else 0)
    elif b is None:
        b = "" if isinstance(a, str) else (False if isinstance(a, bool) else 0)

    a_bool, b_bool = isinstance(a, bool), isinstance(b, bool)
    if a_bool or b_bool:
        # 布尔只与布尔比较；与数字/文本比较时布尔更大
        if a_bool and b_bool:
            cmp = (a > b) - (a < b)
        else:
            cmp = 1 if a_bool else -1
    elif _is_number(a) and _is_number(b):
        cmp = (a > b) - (a < b)
    elif isinstance(a, str) and isinstance(b, str):
        fa, fb = a.casefold(), b.casefold()
        cmp = (fa > fb) - (fa < fb)
    else:
        # 数字 < 文本
        cmp = -1 if _is_number(a) else 1
    if op == "=":
        return cmp == 0
    if op == "<>":
        return cmp != 0
    if op == ">":
        return cmp > 0
    if op == "<":
        return cmp < 0
    if op == ">=":
        return cmp >= 0
    if op == "<=":
        return cmp <= 0
    raise _ExprError(f"bad op {op}")


def _to_number(v: object) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if _is_number(v):
        return float(v)
    if v is None:
        return 0.0
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            raise _ExprError("not a number") from None
    raise _ExprError("not a number")


def _eval(node: Node, get_cell: CellGetter) -> object:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Bool):
        return node.value
    if isinstance(node, Ref):
        try:
            return get_cell(node.a1)
        except Exception:
            return None
    if isinstance(node, Unary):
        v = _to_number(_eval(node.operand, get_cell))
        return -v if node.op == "-" else v
    if isinstance(node, Call):
        name = node.name
        if name == "NOT":
            if len(node.args) != 1:
                raise _ExprError("NOT arity")
            return not _truthy(_eval(node.args[0], get_cell))
        vals = [_truthy(_eval(a, get_cell)) for a in node.args]
        if not vals:
            raise _ExprError("empty args")
        if name == "AND":
            return all(vals)
        return any(vals)
    if isinstance(node, Bin):
        op = node.op
        if op in (">", "<", ">=", "<=", "=", "<>"):
            return _compare(op, _eval(node.left, get_cell), _eval(node.right, get_cell))
        if op == "&":
            l = _eval(node.left, get_cell)
            r = _eval(node.right, get_cell)
            ls = "" if l is None else (str(l) if not isinstance(l, float) else _num_text(l))
            rs = "" if r is None else (str(r) if not isinstance(r, float) else _num_text(r))
            return ls + rs
        a = _to_number(_eval(node.left, get_cell))
        b = _to_number(_eval(node.right, get_cell))
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                raise _ExprError("div by zero")
            return a / b
        if op == "^":
            return a ** b
    raise _ExprError("bad node")


def _num_text(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(v)


def eval_bool(expr: str, get_cell: CellGetter) -> Optional[bool]:
    """求值一个布尔表达式。

    @returns True / False；无法解析或求值出错时返回 None（调用方跳过该规则）。
    """
    ast = compile_expr(expr or "")
    if ast is None:
        return None
    try:
        r = _eval(ast, get_cell)
    except _ExprError:
        return None
    except Exception:
        return None
    return r if isinstance(r, bool) else None


def is_supported_expr(expr: str) -> bool:
    """判断一个表达式是否可以被本求值器处理（提前判定，避免无谓异常路径）。"""
    return compile_expr(expr or "") is not None


# ─────────────────────────────────────────────────────────────
# 相对引用平移 —— 条件格式在多格范围上求值的关键
# ─────────────────────────────────────────────────────────────

RE_REF_SHIFT = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]{0,6})")


def shift_formula(formula: str, d_row: int, d_col: int) -> str:
    """把公式里的**相对引用**按 (dRow, dCol) 平移。

    ⚠️ 条件格式的公式以规则范围的左上角为原点存储：规则作用在 B2:B4、
       公式写作 I2>100 时，对 B3 求值实际用 I3>100。不平移整段范围会
       全部按第一行的值判断（要么全亮要么全不亮）。
       带 $ 的绝对引用不平移；字符串字面量内部不参与平移。
    """
    if not formula or (not d_row and not d_col):
        return formula

    out: List[str] = []
    i = 0
    n = len(formula)
    while i < n:
        ch = formula[i]
        # 字符串字面量：整段复制，内部不做任何替换
        if ch == '"':
            j = i + 1
            while j < n and formula[j] != '"':
                j += 1
            out.append(formula[i : min(j + 1, n)])
            i = j + 1
            continue

        m = RE_REF_SHIFT.match(formula, i)
        if m:
            abs_col, letters, abs_row, digits = m.group(1), m.group(2), m.group(3), m.group(4)
            row = int(digits) - 1
            col = col_from_name(letters)
            if col < 0:
                out.append(m.group(0))
                i = m.end()
                continue
            new_row = row if abs_row else row + d_row
            new_col = col if abs_col else col + d_col
            # 越界保持原样（Excel 会返回 #REF!，这里保守处理）
            if new_row < 0 or new_row > 1_048_575 or new_col < 0 or new_col > 16_383:
                out.append(m.group(0))
                i = m.end()
                continue
            out.append(f"{abs_col}{col_name(new_col)}{abs_row}{new_row + 1}")
            i = m.end()
            continue

        out.append(ch)
        i += 1
    return "".join(out)
