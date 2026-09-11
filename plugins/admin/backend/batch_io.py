"""管理后台 — 单位 / 部门 / 人员 批量导入导出（admin 插件同包模块）

设计要点（与主插件的 routes.py 解耦，不反向 import，避免循环依赖）：
- 由 routes.py 在 register(app) 时调用 batch_io.register(app, deps) 注入 admin 内部依赖
  （配置读写、层级查找、加密、会话判定、权限模块约束、操作标签）。
- 三个模块共用一套「模板下载 / 导出 / 导入」流程，差异集中在 MODULE_SPECS：
  - unit       ：单位名称（唯一键）+ 描述
  - department ：所属单位（外键）+ 部门名称（单位内唯一）+ 描述
  - user       ：登录名（唯一键）、姓名、密码、所属单位/部门（外键）、角色、身份证、
                 大模型配置、权限点
- 导入采用「先预览后执行」两步：
  - dry_run=1 仅校验并返回逐行计划（新增/更新/跳过/错误），不落盘；
  - dry_run=0 才写入 config/admin.json，且与预览走完全相同的代码路径
    （都在 cfg 深拷贝上推演），保证「预览所见 = 执行所得」。
- 支持 .xlsx（openpyxl，缺失时降级为仅 CSV）与 .csv（UTF-8-SIG / GBK 自适应）。
- 单元格语义：留空 = 不修改（更新场景）；"-"（或 无/空/清空/none/clear）= 清空该字段。

接口（均需登录，且分别要求 unit / department / user 模块权限）：
- GET  /api/admin/batch/<module>/template?format=xlsx|csv   下载导入模板
- GET  /api/admin/batch/<module>/export?format=xlsx|csv      导出当前数据
- POST /api/admin/batch/<module>/import                     导入（multipart）
       表单字段：file（必填）、mode=upsert|insert|update（默认 upsert）、
                 dry_run=1|0（默认 1）、auto_create_parent=1|0（默认 0）、
                 on_error=abort|skip（默认 abort）
"""

import copy
import csv
import io
import re
import uuid
from datetime import date as _date, datetime, time as _time

from flask import jsonify, request, send_file
from werkzeug.security import generate_password_hash

try:  # openpyxl 为 xlsx 支持的可选依赖（PyInstaller 已随包收集）
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    OPENPYXL_AVAILABLE = True
except Exception:  # pragma: no cover - 缺库时仅支持 CSV
    openpyxl = None
    OPENPYXL_AVAILABLE = False

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

MAX_IMPORT_BYTES = 10 * 1024 * 1024   # 单次导入文件大小上限
MAX_IMPORT_ROWS = 3000                # 单次导入数据行上限

IMPORT_MODES = ("upsert", "insert", "update")
IMPORT_MODE_NAMES = {
    "upsert": "存在则更新，不存在则新增",
    "insert": "仅新增（已存在则跳过）",
    "update": "仅更新（不存在则跳过）",
}

# 清空该字段的哨兵值（留空表示「不修改」，需要清空时显式填这些值之一）
CLEAR_TOKENS = {"-", "--", "—", "无", "空", "清空", "不分配", "none", "null", "clear"}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# 同一单元格内的多值分隔符（权限点）
MULTI_SPLIT_RE = re.compile(r"[,，;；|、\s]+")
# 不可见字符（BOM / 零宽空格等）：从网页、聊天工具复制来的表头与数值常带
INVISIBLE_RE = re.compile("[\ufeff\u200b\u200c\u200d\u200e\u200f\u2060]")
# XML 非法控制字符（\x00-\x08 \x0b \x0c \x0e-\x1f \x7f）—— CSV 里可能有，留着会污染入库数据
CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 表头行最多向下探测多少行（容忍文件顶部有标题行、说明行、空行）
HEADER_SCAN_ROWS = 8
# 读取阶段的行数保护：超出即停止读取（正常上限 + 表头探测余量），避免超大表吃内存
_GRID_ROW_LIMIT = MAX_IMPORT_ROWS + HEADER_SCAN_ROWS + 100
# 表头列数匹配的容差：定位表头时要求「匹配到的列数 ≥ 该工作表前 8 行中的最大匹配数」
CSV_DELIMITERS = (",", "\t", ";", "|")
CSV_DELIM_NAMES = {",": "逗号", "\t": "制表符", ";": "分号", "|": "竖线"}



# ===================== 模块规格 =====================

MODULE_SPECS = {
    "unit": {
        "label": "单位",
        "perm": "unit",
        "sheet": "单位",
        "columns": [
            {"key": "name", "header": "单位名称", "required": True, "width": 26,
             "alias": ["单位名称", "单位", "名称", "unit", "unit name"],
             "hint": "必填，系统内唯一；已存在同名单位时按导入方式处理"},
            {"key": "description", "header": "描述", "width": 40,
             "alias": ["描述", "单位描述", "备注", "说明", "description"],
             "hint": "可选，单位职责说明"},
        ],
        "sample": ["一大队", "示例行：可删除后再填写"],
        "key_desc": "单位名称",
    },
    "department": {
        "label": "部门",
        "perm": "department",
        "sheet": "部门",
        "columns": [
            {"key": "unit", "header": "所属单位", "required": True, "width": 24,
             "alias": ["所属单位", "单位名称", "单位", "unit", "unit name"],
             "hint": "必填，须为已存在的单位名称或单位 ID；勾选「自动创建上级组织」时不存在则自动创建"},
            {"key": "name", "header": "部门名称", "required": True, "width": 24,
             "alias": ["部门名称", "部门", "名称", "department"],
             "hint": "必填，同一单位内唯一"},
            {"key": "description", "header": "描述", "width": 40,
             "alias": ["描述", "部门描述", "备注", "说明", "description"],
             "hint": "可选，部门职责说明"},
        ],
        "sample": ["管理单位", "技术科", "示例行：可删除后再填写"],
        "key_desc": "所属单位 + 部门名称",
    },
    "user": {
        "label": "人员",
        "perm": "user",
        "sheet": "人员",
        "columns": [
            {"key": "username", "header": "登录名", "required": True, "width": 20, "norm": "ident",
             "alias": ["登录名", "用户名", "账号", "登录账号", "username", "user"],
             "hint": "必填，系统内唯一；只能包含字母、数字、_、.、-；创建后不可修改"},
            {"key": "name", "header": "姓名", "required": True, "width": 16,
             "alias": ["姓名", "真实姓名", "名字", "name"],
             "hint": "必填"},
            {"key": "password", "header": "密码", "width": 18,
             "alias": ["密码", "初始密码", "登录密码", "password"],
             "hint": "新增人员必填且不少于 6 位；更新已有人员时留空表示不改密码"},
            {"key": "unit", "header": "所属单位", "required": True, "width": 24,
             "alias": ["所属单位", "单位名称", "单位", "unit"],
             "hint": "必填，须为已存在的单位名称或单位 ID"},
            {"key": "dept", "header": "所属部门", "required": True, "width": 24,
             "alias": ["所属部门", "部门名称", "部门", "department", "dept"],
             "hint": "必填，须为该单位下已存在的部门名称或部门 ID"},
            {"key": "role", "header": "角色", "width": 16,
             "alias": ["角色", "角色名称", "role"],
             "hint": "可选，填角色名称或角色 ID（如「办案员」）；留空不修改，填 \"-\" 表示不分配"},
            {"key": "idcard", "header": "身份证号码", "width": 22, "norm": "ident",
             "alias": ["身份证号码", "身份证号", "身份证", "idcard"],
             "hint": "可选，加密存储；留空不修改，填 \"-\" 表示清空。"
                     "请把该列单元格格式设为「文本」：按数字存储时 18 位号码末尾会丢精度（系统会检测并提示）"},
            {"key": "llm_base_url", "header": "大模型BaseURL", "width": 30, "norm": "halfwidth",
             "alias": ["大模型baseurl", "大模型地址", "baseurl", "api地址", "llm base url"],
             "hint": "可选，如 https://api.deepseek.com/v1；留空不修改，填 \"-\" 表示清空"},
            {"key": "llm_api_key", "header": "大模型APIKey", "width": 26,
             "alias": ["大模型apikey", "apikey", "大模型密钥", "api密钥", "llm api key"],
             "hint": "可选，加密存储；留空不修改（导出时默认留空），填 \"-\" 表示清空"},
            {"key": "llm_model", "header": "模型名称", "width": 18,
             "alias": ["模型名称", "模型", "model"],
             "hint": "可选，如 deepseek-chat；留空不修改，填 \"-\" 表示清空"},
            {"key": "permissions", "header": "权限点", "width": 40, "norm": "halfwidth",
             "alias": ["权限点", "功能权限", "权限", "可访问工具", "permissions"],
             "hint": "可选，工具 ID 或名称（如 case-report）；多个用顿号「、」或分号分隔"
                     "（CSV 中用逗号需加双引号）。新增人员留空时默认授予全部工具（不含管理后台）；"
                     "更新时留空不修改，填 \"-\" 表示清空"},
        ],
        "sample": ["zhangsan", "张三", "jz@123456", "管理单位", "管理部门", "办案员",
                   "", "", "", "", "case-report"],
        "key_desc": "登录名",
    },
}


# ===================== 依赖注入 =====================

_deps = {}


def register(app, deps):
    """挂载批量导入导出路由。

    deps 由 routes.py 注入（避免本模块反向 import routes 造成循环依赖）：
      load_admin_config / save_admin_config / load_registry /
      find_unit / find_dept / find_user / iter_users /
      encrypt_field / decrypt_field /
      role_super_admin / registered_tool_ids /
      get_session_user / set_operation
    """
    global _deps
    _deps = deps

    @app.get("/api/admin/batch/<module>/template")
    def admin_batch_template(module):
        """下载导入模板（xlsx / csv）。"""
        spec = MODULE_SPECS.get(module)
        if spec is None:
            return jsonify({"error": "不支持的导入模块"}), 404
        denied = _check_access(spec["perm"])
        if denied:
            return denied
        _deps["set_operation"](f"下载{spec['label']}导入模板")
        fmt = _want_format()
        try:
            payload, fname, mime = _build_template(spec, fmt)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 400
        return send_file(io.BytesIO(payload), as_attachment=True,
                         download_name=fname, mimetype=mime)

    @app.get("/api/admin/batch/<module>/export")
    def admin_batch_export(module):
        """导出当前数据（xlsx / csv），列结构与导入模板一致，可直接改后回导。"""
        spec = MODULE_SPECS.get(module)
        if spec is None:
            return jsonify({"error": "不支持的导出模块"}), 404
        denied = _check_access(spec["perm"])
        if denied:
            return denied
        _deps["set_operation"](f"导出{spec['label']}清单")
        fmt = _want_format()
        # 默认不导出大模型 API Key 明文，需要时显式 ?sensitive=1（谨慎使用）
        with_sensitive = (request.args.get("sensitive") or "").strip() in ("1", "true", "yes")
        cfg = _deps["load_admin_config"]()
        rows = _export_rows(module, cfg, with_sensitive)
        headers = [c["header"] for c in spec["columns"]]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            payload, _fname, mime = _build_table_file(
                spec, headers, rows, f"{spec['label']}清单-{stamp}", fmt)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 400
        ext = "csv" if fmt == "csv" else "xlsx"
        return send_file(io.BytesIO(payload), as_attachment=True,
                         download_name=f"{spec['label']}清单-{stamp}.{ext}", mimetype=mime)

    @app.post("/api/admin/batch/<module>/import")
    def admin_batch_import(module):
        """导入（预览 / 执行）：dry_run=1 只校验，dry_run=0 落盘。"""
        spec = MODULE_SPECS.get(module)
        if spec is None:
            return jsonify({"error": "不支持的导入模块"}), 404
        denied = _check_access(spec["perm"])
        if denied:
            return denied

        upload = request.files.get("file")
        if upload is None or not (upload.filename or "").strip():
            return jsonify({"error": "请选择要导入的文件"}), 400
        mode = (request.form.get("mode") or "upsert").strip()
        if mode not in IMPORT_MODES:
            return jsonify({"error": "导入方式不合法"}), 400
        dry_run = _truthy(request.form.get("dry_run"), default=True)
        auto_parent = _truthy(request.form.get("auto_create_parent"), default=False)
        on_error = (request.form.get("on_error") or "abort").strip()
        if on_error not in ("abort", "skip"):
            on_error = "abort"

        data = upload.read()
        if not data:
            return jsonify({"error": "文件内容为空"}), 400
        if len(data) > MAX_IMPORT_BYTES:
            return jsonify({"error": f"文件过大（上限 {MAX_IMPORT_BYTES // 1024 // 1024} MB）"}), 400

        _deps["set_operation"](f"批量导入{spec['label']}")

        try:
            table = _parse_table(upload.filename, data, spec)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:  # openpyxl 解析异常等
            return jsonify({"error": f"文件解析失败：{e}"}), 400

        if not table["items"]:
            return jsonify({"error": "未解析到任何数据行（请确认表头行与数据格式）"}), 400
        if len(table["items"]) > MAX_IMPORT_ROWS:
            return jsonify({"error": f"数据行数超过上限（{MAX_IMPORT_ROWS} 行）"}), 400

        cfg = _deps["load_admin_config"]()
        work, rows = _run_import(module, cfg, table["items"], mode, auto_parent)

        summary = {"total": len(rows), "create": 0, "update": 0, "skip": 0, "error": 0}
        for r in rows:
            summary[r["action"]] = summary.get(r["action"], 0) + 1

        blocked = summary["error"] > 0 and on_error == "abort"
        applied = False
        if not dry_run and not blocked:
            if summary["create"] or summary["update"]:
                _deps["save_admin_config"](work)
                applied = True

        return jsonify({
            "ok": True,
            "module": module,
            "label": spec["label"],
            "mode": mode,
            "mode_name": IMPORT_MODE_NAMES.get(mode, mode),
            "dry_run": dry_run,
            "applied": applied,
            "blocked": blocked,
            "summary": summary,
            "rows": rows,
            "ignored_columns": table["ignored"],
            "missing_columns": table["missing"],
            "total_rows": table["total_rows"],
            "sheet": table.get("sheet", ""),
            "header_row": table.get("header_row", 1),
            "notes": table.get("notes", []),
        })


# ===================== 通用小工具 =====================

def _check_access(perm_module):
    """登录 + 模块权限校验：通过返回 None，否则返回 (响应, 状态码)。"""
    info = _deps["get_session_user"]()
    if info is None:
        return jsonify({"error": "未登录或登录已过期"}), 401
    if perm_module not in (info.get("modules") or []):
        return jsonify({"error": "无此模块的操作权限"}), 403
    return None


def _want_format():
    """读取 format 参数：xlsx / csv。"""
    fmt = (request.args.get("format") or "xlsx").strip().lower()
    return "csv" if fmt == "csv" else "xlsx"


def _truthy(value, default=False):
    """解析表单布尔值。"""
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _norm_header(text):
    """表头归一化：去不可见字符与常见分隔符号、转小写，便于宽松匹配。"""
    s = _clean_invisible(text).lower()
    return re.sub(r"[\s\u3000_\-\.\(\)（）\[\]【】:：/\\*]+", "", s)


def _clean_invisible(text):
    """去掉不可见字符（BOM / 零宽空格等）与 XML 非法控制字符，再 trim。

    前者常见于从网页/系统复制的表头，后者常见于 CSV；留着都会污染入库数据。
    """
    s = INVISIBLE_RE.sub("", str(text if text is not None else ""))
    return CONTROL_RE.sub("", s).strip()


def _cell(value):
    """单元格取值归一化为字符串（取**底层值**，与显示格式无关）。

    - 空 → ""；布尔 → 是/否；日期/时间 → 可读文本；数字 → 去掉无意义小数位；
    - 百分比 / 货币 / 千分位等只是「显示格式」，取到的是底层数值（如 0.5 而非 50%）。
    """
    if value is None:
        return ""
    if isinstance(value, bytes):  # 极少数损坏文件会给出字节串
        value = value.decode("utf-8", "replace")
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        return (value.strftime("%Y-%m-%d") if value.time() == _time(0, 0, 0)
                else value.strftime("%Y-%m-%d %H:%M:%S"))
    if isinstance(value, _date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _clean_invisible(value)


def _is_precision_risk(raw):
    """判断原始单元格是否为「会丢精度的长数字」。

    Excel 数字只保留 15 位有效数字：身份证（18 位）等长数字若被按「数字」存储，
    末尾几位会被静默改写（例：110101199001011234 → …011200）。
    """
    if isinstance(raw, bool):
        return False
    if isinstance(raw, float):
        return abs(raw) >= 10 ** 15
    return isinstance(raw, int) and abs(raw) >= 10 ** 15


def _to_halfwidth(text):
    """全角 → 半角（含全角空格），用于登录名/身份证/权限点等标识类字段。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _normalize_ident(value):
    """标识类字段（登录名 / 身份证等）：取底层值 → 全角转半角 → 去掉全部空白。

    Excel 里粘贴来的身份证常带空格或全角数字，直接入库会变成无法匹配的脏数据。
    """
    return re.sub(r"\s+", "", _to_halfwidth(_cell(value)))


def _normalize_halfwidth(value):
    """半角化（保留空白，供权限点这类以空白/符号分隔的多值字段使用）。"""
    return _to_halfwidth(_cell(value))


def _normalize_value(col_spec, raw):
    """按列规格归一化单元格值。"""
    mode = col_spec.get("norm")
    if mode == "ident":
        return _normalize_ident(raw)
    if mode == "halfwidth":
        return _normalize_halfwidth(raw)
    return _cell(raw)



def _is_clear(value):
    """判断是否为「清空该字段」哨兵值。"""
    return str(value or "").strip().lower() in CLEAR_TOKENS


def _split_multi(value):
    """拆分多值单元格（权限点）：逗号 / 分号 / 竖线 / 顿号 / 空白。"""
    return [p for p in MULTI_SPLIT_RE.split(str(value or "")) if p]


# ===================== 数据读取（xlsx / csv，含单元格格式健壮性处理） =====================
#
# 用户交上来的表格什么格式都有，解析层统一做了这些兜底（逐项都有回归用例）：
#   1. 合并单元格     —— openpyxl 只在左上角给值，纵向合并会导致下方行集体「缺值」，
#                        故按合并区域把左上角值填充到区域内（仅只读模式读不到 merged_cells，
#                        因此用普通模式加载；文件已限 10MB）；
#   2. 多工作表/多分隔符 —— xlsx 的每个工作表、csv 的每种候选分隔符各作为一个「候选表」，
#                        取表头匹配度最高的那个（数据在第二张表、CSV 用分号/制表符分隔都能识别）；
#   3. 表头不在第一行 —— 向下探测前 HEADER_SCAN_ROWS 行，按「必填列齐全 > 精确匹配列数 >
#                        匹配列数」择优，容忍顶部有标题行/说明行/空行；
#   4. 公式无缓存值   —— 脚本生成或未保存计算的 xlsx，公式单元格 data_only 读出来是空，
#                        用第二遍 data_only=False 加载比对，明确指出是哪一列、该怎么做；
#   5. 数字型长数字   —— Excel 数字仅 15 位有效数字，18 位身份证按数字存会末尾改写，
#                        检测到即告警（数据已损坏，必须让用户改成文本格式重填）；
#   6. 全角 / 空白 / 不可见字符 —— 登录名、身份证等标识字段做全角转半角 + 去空白，
#                        表头去 BOM/零宽字符；
#   7. 重复表头行     —— 用户复制表头造成的重复行自动忽略并提示。

def _decode_csv_text(data):
    """CSV 解码（UTF-8-SIG / UTF-8 / GBK / GB18030 自适应）。"""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别 CSV 文件编码，请另存为 UTF-8 或 GBK 编码")


def _load_candidates(filename, data):
    """把上传文件解析为「候选表」列表：[{title, grid, formulas, merged}]。

    grid 为原始值二维数组（未做列映射），由 _parse_table 按表头匹配度择优。
    """
    lower = (filename or "").lower()
    if lower.endswith((".csv", ".txt", ".tsv")):
        return _csv_candidates(data, tab_first=lower.endswith(".tsv"))
    if lower.endswith((".xlsx", ".xlsm")):
        if not OPENPYXL_AVAILABLE:
            raise ValueError("服务器缺少 openpyxl，无法读取 .xlsx，请改用 CSV 模板")
        return _xlsx_candidates(data)
    if lower.endswith(".xls"):
        raise ValueError("暂不支持旧版 .xls，请在 Excel 中另存为 .xlsx 或 .csv 后重试")
    raise ValueError("仅支持 .xlsx / .csv 文件")


def _csv_candidates(data, tab_first=False):
    """按候选分隔符各解析一份（逗号 / 制表符 / 分号 / 竖线），交由表头匹配度决定用哪个。"""
    text = _decode_csv_text(data)
    delims = list(CSV_DELIMITERS)
    if tab_first:
        delims.remove("\t")
        delims.insert(0, "\t")
    out = []
    for delim in delims:
        grid = []
        for row in csv.reader(io.StringIO(text), delimiter=delim):
            # 行数保护：超限即停止读取（_parse_table 会给出明确报错），避免超大 CSV 吃内存
            if len(grid) > _GRID_ROW_LIMIT:
                break
            grid.append([_cell(c) for c in row])
        out.append({"title": f"CSV（{CSV_DELIM_NAMES[delim]}分隔）",
                    "grid": grid, "formulas": set(), "merged": 0})
    return out


def _xlsx_candidates(data):
    """读取 xlsx 全部工作表：填充合并单元格、标记「公式但无缓存值」的单元格。"""
    values_wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    formula_wb = None
    try:
        formula_wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    except Exception:  # 拿不到公式源码不影响取值
        formula_wb = None
    try:
        out = []
        for ws in values_wb.worksheets:
            grid = []
            for row in ws.iter_rows(values_only=True):
                if len(grid) > _GRID_ROW_LIMIT:
                    break
                grid.append(list(row))
            formulas = _uncached_formulas(ws.title, grid, formula_wb)
            merged = 0
            for rng in list(ws.merged_cells.ranges):
                merged += _fill_merged(grid, rng)
            out.append({"title": ws.title, "grid": grid, "formulas": formulas,
                        "merged": merged})
        return out
    finally:
        for wb in (values_wb, formula_wb):
            if wb is not None:
                try:
                    wb.close()
                except Exception:
                    pass


def _uncached_formulas(sheet_title, grid, formula_wb):
    """返回「是公式且没有缓存计算结果」的单元格坐标集合 {(行,列)}（1 基）。"""
    if formula_wb is None or sheet_title not in formula_wb.sheetnames:
        return set()
    hits = set()
    try:
        rows = formula_wb[sheet_title].iter_rows(values_only=True)
    except Exception:
        return set()
    for r, row in enumerate(rows, start=1):
        if r > len(grid) + 1:  # 只比对已读取的范围，避免重复扫全表
            break
        for c, val in enumerate(row, start=1):
            if not (isinstance(val, str) and val.startswith("=")):
                continue
            cached = None
            if r - 1 < len(grid) and c - 1 < len(grid[r - 1]):
                cached = grid[r - 1][c - 1]
            if cached in (None, ""):
                hits.add((r, c))
    return hits


def _fill_merged(grid, rng):
    """把合并区域内除左上角以外的空单元格填成左上角的值；返回填充的单元格数。"""
    r0, c0 = rng.min_row, rng.min_col
    if r0 - 1 >= len(grid) or c0 - 1 >= len(grid[r0 - 1]):
        return 0
    value = grid[r0 - 1][c0 - 1]
    if value in (None, ""):
        return 0
    filled = 0
    for r in range(rng.min_row, rng.max_row + 1):
        if r - 1 >= len(grid):
            break
        for c in range(rng.min_col, rng.max_col + 1):
            if r - 1 == r0 - 1 and c - 1 == c0 - 1:
                continue
            while len(grid[r - 1]) < c:
                grid[r - 1].append(None)
            if grid[r - 1][c - 1] in (None, ""):
                grid[r - 1][c - 1] = value
                filled += 1
    return filled


def _locate_header(grid, spec):
    """在表格前 HEADER_SCAN_ROWS 行内定位表头行。

    择优顺序：必填列齐全 > 精确匹配的列数多 > 匹配到的列数多。
    （用「精确匹配数」做次级排序，可避免把「单位名称与描述说明」这类标题行误判为表头）
    """
    best, best_key = None, None
    for i in range(min(HEADER_SCAN_ROWS, len(grid))):
        if not any(_cell(c) for c in grid[i]):
            continue
        col_index, ignored, exact = _match_columns(grid[i], spec)
        missing = [c["header"] for c in spec["columns"]
                   if c.get("required") and c["key"] not in col_index]
        key = (0 if missing else 1, exact, len(col_index))
        if best_key is None or key > best_key:
            best_key = key
            best = {"row": i, "col_index": col_index, "ignored": ignored, "missing": missing}
    return best


def _parse_table(filename, data, spec):
    """解析为 {headers, items:[{row,data,notes}], ignored, missing, total_rows, sheet, header_row, notes}。

    - 候选表择优（多工作表 / 多分隔符）→ 表头行定位 → 逐行取值与归一化；
    - 整行空白忽略；与表头重复的行忽略；公式无缓存值 / 数字精度风险写入该行 notes；
    - 必填列缺失直接报错（ValueError），未知列记入 ignored 供前端提示。
    """
    candidates = _load_candidates(filename, data)
    best, partial = None, None
    for cand in candidates:
        found = _locate_header(cand["grid"], spec)
        if found is None:
            continue
        # 记录「匹配得最多」的候选，用于必填列缺失时给出更精确的报错
        if partial is None or len(found["col_index"]) > len(partial["col_index"]):
            partial = found
        if found["missing"]:
            continue
        key = len(found["col_index"])
        if best is None or key > best[0]:
            best = (key, cand, found)

    if best is None:
        if partial is not None and partial["missing"]:
            raise ValueError("缺少必需列：" + "、".join(partial["missing"])
                             + "（请使用下载的模板填写）")
        required = "、".join(c["header"] for c in spec["columns"] if c.get("required"))
        raise ValueError(f"未找到可识别的表头行（表头行需包含：{required}），请使用下载的模板填写")

    _key, cand, found = best
    grid = cand["grid"]
    header_idx = found["row"]
    headers = grid[header_idx]
    col_index = found["col_index"]

    spec_by_key = {c["key"]: c for c in spec["columns"]}
    items, total, dup_rows = [], 0, 0
    for offset, row in enumerate(grid[header_idx + 1:], start=header_idx + 2):
        if not any(_cell(c) for c in row):
            continue
        data_row, raw_row = {}, {}
        for key, idx in col_index.items():
            raw = row[idx] if idx < len(row) else None
            raw_row[key] = raw
            data_row[key] = _normalize_value(spec_by_key[key], raw)
        if _is_header_repeat(data_row, spec_by_key):
            dup_rows += 1
            continue
        total += 1
        notes, errors = _row_format_notes(offset, col_index, raw_row, data_row,
                                          spec_by_key, cand["formulas"])
        items.append({"row": offset, "data": data_row, "notes": notes, "errors": errors})

    if total > MAX_IMPORT_ROWS:
        raise ValueError(f"数据行数超过单次上限（{MAX_IMPORT_ROWS} 行），请拆分后分批导入")

    notes = []
    if header_idx > 0:
        notes.append(f"表头前有 {header_idx} 行（标题/说明/空行）已跳过")
    if cand.get("merged"):
        notes.append(f"已自动补全 {cand['merged']} 个合并单元格的值")
    if dup_rows:
        notes.append(f"已忽略 {dup_rows} 行与表头重复的行")
    return {"headers": headers, "items": items, "ignored": found["ignored"],
            "missing": [], "total_rows": total, "sheet": cand["title"],
            "header_row": header_idx + 1, "notes": notes}


def _row_format_notes(row_no, col_index, raw_row, data_row, spec_by_key, formulas):
    """按行检查「单元格格式」造成的取值问题。

    返回 (notes, errors)：
    - notes  ：仅提示，该行照常导入；
    - errors ：数据已不可信（长数字丢精度 / 必填列是未计算的公式），必须阻断该行。
    """
    notes, errors = [], []
    for key, idx in col_index.items():
        col = spec_by_key[key]
        if (row_no, idx + 1) in formulas and not data_row.get(key):
            text = (f"「{col['header']}」是公式且没有保存计算结果，读出来是空的"
                    "（请先用 Excel/WPS 打开并另存，或直接换成文本）")
            (errors if col.get("required") else notes).append(text)
    for key in col_index:
        col = spec_by_key[key]
        if not data_row.get(key) or col.get("norm") != "ident":
            continue
        if _is_precision_risk(raw_row.get(key)):
            errors.append(
                f"「{col['header']}」在 Excel 中按数字存储，末尾几位已被丢精度改写为"
                f"{data_row[key]}（不可信）。请把该列单元格格式设为「文本」后重新填写，"
                "或先填 \"-\" 占位")
    return notes, errors


def _is_header_repeat(data_row, spec_by_key):
    """判断整行是否就是表头文本（用户复制表头造成的重复行）→ 忽略。"""
    non_empty = [k for k, v in data_row.items() if v]
    if len(non_empty) < 2:
        return False
    hits = sum(1 for k in non_empty
               if _norm_header(data_row[k]) == _norm_header(spec_by_key[k]["header"]))
    return hits == len(non_empty)


def _match_columns(headers, spec):
    """把表头映射为 列key → 下标；返回 (映射, 未识别表头列表, 精确匹配数)。"""
    norm = [_norm_header(h) for h in headers]
    col_index = {}
    used = set()
    exact_hits = 0
    # 第一轮：精确匹配
    for col in spec["columns"]:
        aliases = {_norm_header(a) for a in [col["header"]] + list(col.get("alias") or [])}
        aliases.discard("")
        for i, h in enumerate(norm):
            if i in used or not h:
                continue
            if h in aliases:
                col_index[col["key"]] = i
                used.add(i)
                exact_hits += 1
                break
    # 第二轮：包含匹配（长别名优先，避免「单位」抢走「所属单位」）
    for col in spec["columns"]:
        if col["key"] in col_index:
            continue
        aliases = sorted({_norm_header(a) for a in [col["header"]] + list(col.get("alias") or [])}
                         - {""}, key=len, reverse=True)
        for alias in aliases:
            hit = None
            for i, h in enumerate(norm):
                if i in used or not h:
                    continue
                if alias and alias in h:
                    hit = i
                    break
            if hit is not None:
                col_index[col["key"]] = hit
                used.add(hit)
                break
    ignored = [headers[i] for i, h in enumerate(norm) if i not in used and h]
    return col_index, ignored, exact_hits



# ===================== 导入推演（预览 / 执行共用） =====================

def _run_import(module, cfg, items, mode, auto_parent):
    """在 cfg 深拷贝上推演导入过程，返回 (推演后的配置, 逐行计划)。

    预览与真正落盘走同一函数，保证预览结果与执行结果一致。
    """
    work = copy.deepcopy(cfg)
    ctx = {
        "seen": set(),
        "auto_parent": bool(auto_parent),
        "notes": [],
    }
    planner = _PLANNERS[module]
    rows = []
    for item in items:
        # 解析阶段发现的「单元格格式问题」与推演结果一起回传，让用户看得见
        # 「哪些值其实已经不可靠」；其中 error 级问题（长数字丢精度等）直接阻断该行，
        # 在推演之前拦下，保证工作副本不会被写入不可信数据。
        ctx["notes"] = list(item.get("notes") or [])
        blocking = list(item.get("errors") or [])
        if blocking:
            action, message = "error", "；".join(blocking)
        else:
            try:
                action, message = planner(work, item["data"], item["row"], mode, ctx)
            except Exception as e:  # 单行异常不阻断整批推演
                action, message = "error", f"处理失败：{e}"
        if ctx["notes"]:
            message = message + "（" + "；".join(ctx["notes"]) + "）"
        rows.append({
            "row": item["row"],
            "action": action,
            "label": _row_label(module, item["data"]),
            "message": message,
        })
    return work, rows


def _row_label(module, d):
    """行标识（列表展示用）。"""
    if module == "unit":
        return d.get("name") or "（未填写单位名称）"
    if module == "department":
        return f"{d.get('unit') or '?'} / {d.get('name') or '（未填写部门名称）'}"
    name = d.get("name") or ""
    return f"{d.get('username') or '（未填写登录名）'}{(' · ' + name) if name else ''}"


def _find_unit_ref(work, ref):
    """按单位名称或单位 ID 查找单位。"""
    ref = (ref or "").strip()
    if not ref:
        return None
    return _deps["find_unit"](work, ref) or next(
        (u for u in work.get("units", []) if (u.get("name") or "").strip() == ref), None)


def _find_dept_ref(work, unit, ref):
    """在指定单位内按部门名称或部门 ID 查找部门。"""
    ref = (ref or "").strip()
    if not ref or unit is None:
        return None
    for dept in unit.get("departments", []):
        if dept.get("id") == ref or (dept.get("name") or "").strip() == ref:
            return dept
    return None


def _ensure_unit(work, ref, mode, ctx):
    """取单位；不存在时按需自动创建。返回 (unit, None) 或 (None, 错误信息)。"""
    unit = _find_unit_ref(work, ref)
    if unit is not None:
        return unit, None
    if not ctx["auto_parent"]:
        return None, f"所属单位「{ref}」不存在（可勾选「自动创建上级组织」）"
    if mode == "update":
        return None, f"所属单位「{ref}」不存在，无法执行「仅更新」"
    unit = {"id": "unit-" + uuid.uuid4().hex[:8], "name": ref.strip(),
            "description": "由批量导入自动创建", "departments": []}
    work.setdefault("units", []).append(unit)
    ctx["notes"].append(f"已自动创建所属单位「{unit['name']}」")
    return unit, None


def _ensure_dept(work, unit, ref, mode, ctx):
    """取部门；不存在时按需自动创建。返回 (dept, None) 或 (None, 错误信息)。"""
    dept = _find_dept_ref(work, unit, ref)
    if dept is not None:
        return dept, None
    if not ctx["auto_parent"]:
        return None, f"单位「{unit['name']}」下不存在部门「{ref}」（可勾选「自动创建上级组织」）"
    if mode == "update":
        return None, f"单位「{unit['name']}」下不存在部门「{ref}」，无法执行「仅更新」"
    dept = {"id": "dept-" + uuid.uuid4().hex[:8], "name": ref.strip(),
            "description": "由批量导入自动创建", "users": []}
    unit.setdefault("departments", []).append(dept)
    ctx["notes"].append(f"已自动创建部门「{dept['name']}」")
    return dept, None


def _role_index(work):
    """角色索引：ID 与名称 → 角色 ID。"""
    idx = {}
    for r in work.get("permissions", []):
        rid = r.get("id") or ""
        nm = (r.get("name") or "").strip()
        if rid:
            idx[rid] = rid
        if nm and rid:
            idx.setdefault(nm, rid)
    return idx


def _tool_index():
    """工具索引：插件 ID 与工具名称 → 插件 ID（权限点校验用）。"""
    idx = {}
    try:
        tools = _deps["load_registry"]().get("tools", [])
    except Exception:
        tools = []
    for t in tools:
        tid = (t.get("id") or "").strip()
        if not tid:
            continue
        idx[tid] = tid
        nm = (t.get("name") or "").strip()
        if nm:
            idx.setdefault(nm, tid)
    return idx


def _plan_unit(work, d, rowno, mode, ctx):
    """单位行推演。"""
    name = (d.get("name") or "").strip()
    desc = (d.get("description") or "").strip()
    if not name:
        return "error", "单位名称不能为空"
    key = ("unit", name)
    if key in ctx["seen"]:
        return "error", "同一文件内出现重复的单位名称"
    ctx["seen"].add(key)
    existing = next((u for u in work.get("units", []) if (u.get("name") or "").strip() == name), None)
    if existing is not None:
        if mode == "insert":
            return "skip", "已存在同名单位，按「仅新增」跳过"
        if (existing.get("description") or "") == desc:
            return "skip", "内容与现状一致，无需更新"
        existing["description"] = desc
        return "update", "已更新单位描述"
    if mode == "update":
        return "skip", "单位不存在，按「仅更新」跳过"
    work.setdefault("units", []).append({
        "id": "unit-" + uuid.uuid4().hex[:8],
        "name": name,
        "description": desc,
        "departments": [],
    })
    return "create", "新增单位"


def _plan_department(work, d, rowno, mode, ctx):
    """部门行推演。"""
    name = (d.get("name") or "").strip()
    desc = (d.get("description") or "").strip()
    unit_ref = (d.get("unit") or "").strip()
    if not unit_ref:
        return "error", "所属单位不能为空"
    if not name:
        return "error", "部门名称不能为空"
    unit, err = _ensure_unit(work, unit_ref, mode, ctx)
    if err:
        return "error", err
    key = ("dept", unit.get("id"), name)
    if key in ctx["seen"]:
        return "error", "同一文件内出现重复的部门（同单位同名）"
    ctx["seen"].add(key)
    existing = _find_dept_ref(work, unit, name)
    if existing is not None:
        if mode == "insert":
            return "skip", "该单位下已存在同名部门，按「仅新增」跳过"
        if (existing.get("description") or "") == desc:
            return "skip", "内容与现状一致，无需更新"
        existing["description"] = desc
        return "update", "已更新部门描述"
    if mode == "update":
        return "skip", "该单位下不存在该部门，按「仅更新」跳过"
    unit.setdefault("departments", []).append({
        "id": "dept-" + uuid.uuid4().hex[:8],
        "name": name,
        "description": desc,
        "users": [],
    })
    return "create", "新增部门"


def _plan_user(work, d, rowno, mode, ctx):
    """人员行推演：新增需密码，更新按「留空不修改 / \"-\" 清空」处理。"""
    username = (d.get("username") or "").strip()
    name = (d.get("name") or "").strip()
    password = d.get("password") or ""
    unit_ref = (d.get("unit") or "").strip()
    dept_ref = (d.get("dept") or "").strip()
    if not username:
        return "error", "登录名不能为空"
    if not USERNAME_RE.match(username):
        return "error", "登录名只能包含字母、数字、_、.、-"
    if not name:
        return "error", "姓名不能为空"
    if not unit_ref or not dept_ref:
        return "error", "所属单位与所属部门均不能为空"
    if username in ctx["seen"]:
        return "error", "同一文件内出现重复的登录名"
    ctx["seen"].add(username)

    unit, err = _ensure_unit(work, unit_ref, mode, ctx)
    if err:
        return "error", err
    dept, err = _ensure_dept(work, unit, dept_ref, mode, ctx)
    if err:
        return "error", err

    found = _deps["find_user"](work, username)
    is_new = found is None
    if is_new:
        if mode == "update":
            return "skip", "人员不存在，按「仅更新」跳过"
        if not password or len(password) < 6:
            return "error", "新增人员必须填写密码且不少于 6 位"
    else:
        if mode == "insert":
            return "skip", "该登录名已存在，按「仅新增」跳过"

    role_ref = (d.get("role") or "").strip()
    role_id = None
    if role_ref:
        if _is_clear(role_ref):
            role_id = ""
        else:
            role_id = _role_index(work).get(role_ref)
            if role_id is None:
                return "error", f"角色「{role_ref}」不存在"

    perm_raw = (d.get("permissions") or "").strip()
    permissions = None
    if perm_raw:
        if _is_clear(perm_raw):
            permissions = []
        else:
            idx = _tool_index()
            tokens = _split_multi(perm_raw)
            bad = [t for t in tokens if t not in idx]
            if bad:
                return "error", f"权限点不是有效的工具 ID/名称：{'、'.join(bad[:5])}"
            permissions = sorted({idx[t] for t in tokens})

    idcard_raw = d.get("idcard")
    llm_fields = {
        "base_url": d.get("llm_base_url"),
        "api_key": d.get("llm_api_key"),
        "model": d.get("llm_model"),
    }

    if is_new:
        if role_id is None:
            role_id = _role_index(work).get("办案员", "")
        if permissions is None:
            # 与 POST /api/admin/users 保持一致：超管不依赖权限点，其余默认全部工具（不含管理后台）
            permissions = ([] if _deps["role_super_admin"](work, role_id)
                           else sorted(_deps["registered_tool_ids"]() - {"admin"}))
        user = {
            "username": username,
            "password": _deps["encrypt_field"](generate_password_hash(password)),
            "name": name,
            "idcard": _deps["encrypt_field"](
                "" if _is_clear(idcard_raw) else (idcard_raw or "")),
            "role": role_id,
            "permissions": permissions,
            "llm": {
                "base_url": _llm_value(llm_fields["base_url"]),
                "api_key": _deps["encrypt_field"](_llm_value(llm_fields["api_key"])),
                "model": _llm_value(llm_fields["model"]),
            },
        }
        dept.setdefault("users", []).append(user)
        return "create", f"新增人员（所属：{unit['name']} / {dept['name']}）"

    # ---- 更新已有人员 ----
    _old_unit, old_dept, user = found
    if _deps["role_super_admin"](work, user.get("role")):
        # 超级管理员保护：允许同步姓名/密码/身份证/大模型/归属，
        # 但请求的角色或权限点与现状不一致时拒绝（避免批量导入误降权导致无法登录后台）。
        if role_id is not None and role_id != user.get("role"):
            return "error", "该账号为超级管理员，不支持通过批量导入修改角色（请在人员管理中单独调整）"
        if permissions is not None and permissions != (user.get("permissions") or []):
            return "error", "该账号为超级管理员，不支持通过批量导入修改权限点（请在人员管理中单独调整）"
        role_id = None
        permissions = None

    changed = []
    if user.get("name") != name:
        user["name"] = name
        changed.append("姓名")
    if password:
        if len(password) < 6:
            return "error", "密码长度不能少于 6 位"
        user["password"] = _deps["encrypt_field"](generate_password_hash(password))
        changed.append("密码")
    if role_id is not None and user.get("role") != role_id:
        user["role"] = role_id
        changed.append("角色")
    if permissions is not None and user.get("permissions") != permissions:
        user["permissions"] = permissions
        changed.append("权限点")
    if idcard_raw is not None and (idcard_raw or "") != "":
        new_idcard = "" if _is_clear(idcard_raw) else str(idcard_raw).strip()
        if _deps["decrypt_field"](user.get("idcard")) != new_idcard:
            user["idcard"] = _deps["encrypt_field"](new_idcard)
            changed.append("身份证")
    u_llm = user.setdefault("llm", {"base_url": "", "api_key": "", "model": ""})
    for field, raw in llm_fields.items():
        if raw is None or (raw or "") == "":
            continue
        if field == "api_key":
            new_val = "" if _is_clear(raw) else str(raw).strip()
            if _deps["decrypt_field"](u_llm.get(field, "")) != new_val:
                u_llm[field] = _deps["encrypt_field"](new_val)
                changed.append("大模型APIKey")
        else:
            new_val = _llm_value(raw)
            if (u_llm.get(field) or "") != new_val:
                u_llm[field] = new_val
                changed.append("大模型BaseURL" if field == "base_url" else "模型名称")
    moved = ""
    if old_dept.get("id") != dept.get("id"):
        old_dept["users"] = [x for x in old_dept.get("users", [])
                             if x.get("username") != username]
        dept.setdefault("users", []).append(user)
        moved = f"，已调整归属到 {unit['name']} / {dept['name']}"
    if not changed and not moved:
        return "skip", "内容与现状一致，无需更新"
    return "update", "已更新：" + ("、".join(changed) if changed else "所属部门") + moved


def _llm_value(raw):
    """大模型字段取值：清空哨兵 → 空串，否则原值。"""
    if raw is None:
        return ""
    return "" if _is_clear(raw) else str(raw).strip()


_PLANNERS = {
    "unit": _plan_unit,
    "department": _plan_department,
    "user": _plan_user,
}


# ===================== 导出 / 模板 =====================

def _export_rows(module, cfg, with_sensitive=False):
    """按模块输出二维数据行（列顺序与 MODULE_SPECS 一致）。"""
    if module == "unit":
        return [[u.get("name", ""), u.get("description", "")]
                for u in cfg.get("units", [])]
    if module == "department":
        rows = []
        for unit in cfg.get("units", []):
            for dept in unit.get("departments", []):
                rows.append([unit.get("name", ""), dept.get("name", ""),
                             dept.get("description", "")])
        return rows
    role_map = {r.get("id"): r.get("name", "") for r in cfg.get("permissions", [])}
    rows = []
    for unit, dept, user in _deps["iter_users"](cfg):
        llm = user.get("llm") or {}
        rows.append([
            user.get("username", ""),
            user.get("name", ""),
            "",  # 密码永不导出
            unit.get("name", ""),
            dept.get("name", ""),
            role_map.get(user.get("role", ""), user.get("role", "") or ""),
            _deps["decrypt_field"](user.get("idcard")),
            llm.get("base_url", ""),
            _deps["decrypt_field"](llm.get("api_key", "")) if with_sensitive else "",
            llm.get("model", ""),
            ",".join(user.get("permissions", []) or []),
        ])
    return rows


def _build_template(spec, fmt):
    """构建导入模板（表头 + 示例行；xlsx 附「填写说明」工作表）。"""
    headers = [c["header"] for c in spec["columns"]]
    rows = [list(spec["sample"])]
    return _build_table_file(spec, headers, rows, f"{spec['label']}导入模板", fmt,
                             template=True)


def _build_table_file(spec, headers, rows, title, fmt, template=False):
    """生成 xlsx / csv 文件字节，返回 (payload, default_filename, mime)。"""
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(headers)
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8-sig"), f"{title}.csv", "text/csv; charset=utf-8"
    if not OPENPYXL_AVAILABLE:
        raise RuntimeError("服务器缺少 openpyxl，无法导出 Excel，请改用 CSV（?format=csv）")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = spec["sheet"]
    thin = Side(style="thin", color="BBBBBB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4A90D9")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
    for row in rows:
        ws.append(["" if v is None else v for v in row])
    for i, col in enumerate(spec["columns"], start=1):
        ws.column_dimensions[get_column_letter(i)].width = col.get("width", 20)
    for r in ws.iter_rows(min_row=2):
        for cell in r:
            cell.alignment = Alignment(vertical="top", wrap_text=False)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"

    if template:
        info = wb.create_sheet("填写说明")
        info.append(["列名", "是否必填", "填写说明"])
        for c in range(1, 4):
            cell = info.cell(row=1, column=c)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="4A90D9")
        for col in spec["columns"]:
            info.append([col["header"], "是" if col.get("required") else "否",
                         col.get("hint", "")])
        info.append(["—— 通用说明 ——", "", ""])
        for line in [
            f"1. 唯一键：{spec['key_desc']}。重复行会按「导入方式」处理。",
            "2. 表头行请勿删除或改名，数据从表头下一行开始；表头上方可以有标题行或空行（会自动跳过）。",
            "3. 留空表示「不修改」（仅更新场景）；需要清空某字段时填半角「-」。",
            "4. 导入前建议先「预览」，确认每行结果为「新增/更新」后再执行。",
            "5. 支持 .xlsx 与 .csv（UTF-8 / GBK 均可识别；CSV 的逗号/分号/制表符/竖线分隔都能识别）。",
            "6. 登录名、身份证号等标识请把单元格格式设为「文本」：按数字存储时前导零会丢失，"
            "18 位身份证末尾还会被改成 0（系统会检测并提示）。",
            "7. 合并单元格支持（自动把左上角的值补到同组各行）；数据放在哪个工作表都可以，会自动找表头。",
            "8. 单元格的百分比/货币/日期等「显示格式」不影响取值，系统取底层数值；"
            "公式单元格请先用 Excel/WPS 打开保存过（否则读不到计算结果）。",
            "9. CSV 单元格内含逗号时请用双引号包裹整个单元格（如权限点 \"a,b\"）。",
        ]:
            info.append([line, "", ""])
        info.column_dimensions["A"].width = 30
        info.column_dimensions["B"].width = 12
        info.column_dimensions["C"].width = 70

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"{title}.xlsx", XLSX_MIME
