"""知识库 —— JZToolsHub 后端插件路由。

功能：
- 管理员角色（role-admin）/超级管理员可上传 PDF / OFD / Word / Excel / Markdown /
  纯文本等文档，落盘到数据根目录 plugins/knowledge-base/data/files/
  （服务端生成 ID 重命名，SEC-3），并支持多级分类树管理（新建 / 改名 / 移动 / 删除）；
- **旧版格式自动转换**：上传 `.doc` / `.xls` 时先由服务端转换为 `.docx` / `.xlsx`
  再落盘（见 doc_convert.py），原格式记于 `original_ext` 字段，
  前端据此在页面提示"已自动转换"。转换依赖缺失或文件损坏时返回 4xx 明确提示，不 500；
- **双份保存（《插件库优化方案》阶段 1）**：上传时先保存**原件** `<id>.<original_ext>`
  （下载端点专用，权威原件），再保存**渲染件** `<id>.docx/.xlsx`（旧版格式转换产物，
  供预览与前端降级渲染）；
- **下载**：`GET /files/<id>/download` 以 attachment 附件形式返回**原件**
  （Word/Excel 下载给原始文档）；全体登录用户可用；
- **Office 预览（阶段 8，替代原 PDF/手绘两套管线）**：docx/doc/xlsx/xls 由
  vendor 的 xhr（Excel）/dhr（Word）双引擎**按需同步渲染**为 HTML——样式还原、
  连续单页不分页、亚秒级完成，结果缓存 `<id>.preview.json`；
  `GET /files/<id>/preview` 返回 `{kind, html, warnings}`；渲染失败/引擎缺失
  返回 404，前端自动回退 mammoth / SheetJS 降级渲染，不阻断任何功能；
  旧版的 pdf_status/html_status 异步状态机、/pdf、/pdf-retry、/preview-retry
  端点全部移除（引擎亚秒级，无需轮询）；
- 全体登录用户可浏览分类树与文件列表，并经 /files/<id>/raw 内联读取文件内容
  供前端纯 JS 渲染（pdf.js / ofd.js / mammoth / SheetJS / marked，无浏览器控件）。

数据持久化（规范 9.1/9.2）：
- data/categories.json  分类树（单库文件）
- data/files.json       文件元数据索引（单库文件）
- data/files/<id>.<original_ext>  上传的**原件**（下载用；<id> 匹配 ^[A-Za-z0-9_-]+$，SEC-1）
- data/files/<id>.<ext>           **渲染件**（doc/xls 的转换产物 docx/xlsx；
                                  原生 docx/xlsx/pdf/… 与原件同名，不重复落盘）
- data/files/<id>.preview.json    Office 预览缓存（阶段 8 起，按需生成）
- 分类与文件记录均含 created_by / created_by_name / unit_id / department_id
  四个归属字段，一律取自服务端会话、禁止从请求体接收（铁律一）；
  阅读为全站公共资源，列表不做单位/部门过滤（《知识库插件-设计文档》§3.3）。

接口前缀：/api/knowledge-base
"""

import json
import os
import re
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import jsonify, request, send_file

try:
    from jztools_admin.routes import get_session_user as _get_session_user
except Exception:  # admin 插件缺失时兜底（理论上不会发生）
    _get_session_user = None

try:
    from jztools_admin.routes import set_operation as _set_operation
except Exception:  # 主应用未提供日志辅助时兜底（理论上不会发生）
    def _set_operation(op):
        pass

import jztools_data

try:
    from . import doc_convert as _doc_convert
except ImportError:  # 插件以脚本方式加载时的兜底（无包上下文）
    import doc_convert as _doc_convert

# Office 预览渲染引擎（阶段 8 引入 office_render.py，vendor xhr/dhr 双引擎；
# 模块缺失 = 引擎不可用，走降级链。两段式兜底口径同 doc_convert：
# 包上下文（主应用加载）→ 裸 import（脚本方式加载，如测试场景））。
try:
    from . import office_render as _office_render
except ImportError:
    try:
        import office_render as _office_render
    except ImportError:
        _office_render = None

DATA_DIR = jztools_data.get_data_root_dir("plugins", "knowledge-base", "data")
FILES_DIR = jztools_data.get_data_root_dir("plugins", "knowledge-base", "data", "files")
API_PREFIX = "/api/knowledge-base"

# 可管理（上传/分类维护）角色：超级管理员 + 管理员角色（与公告板口径一致）
MANAGE_ROLE_IDS = {"role-admin"}
MANAGE_ROLE_NAMES = {"管理员"}

ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_NAME_LEN = 80                      # 展示名/分类名长度上限
MAX_SUMMARY_LEN = 200                  # 文件简介长度上限
MAX_UPLOAD_BYTES = 20 * 1024 * 1024    # 20MB（SEC-3 大小上限）
MAX_CATEGORIES = 200                   # 分类总量上限（防滥用）
MAX_FILES = 2000                       # 文件总量上限（防滥用）

# 上传扩展名白名单（SEC-3）
# 其中 doc / xls 为旧版二进制格式，服务端会先转为 docx / xlsx 再落盘（见 doc_convert.py），
# 库内只保留现代格式，前端渲染链路无需为老格式引入额外渲染库。
ALLOWED_EXTS = {"pdf", "ofd", "docx", "xlsx", "xls", "doc", "md", "markdown", "txt", "csv"}

# 需要服务端转换的旧版格式 → 目标格式
LEGACY_CONVERT = {"doc": "docx", "xls": "xlsx"}

# Word / Excel 类：预览走 vendor 双引擎按需渲染（阶段 8 起；原 PDF_SOURCE_EXTS /
# XLSX_SOURCE_EXTS 异步管线废弃）
OFFICE_SOURCE_EXTS = {"doc", "docx", "xls", "xlsx"}

# raw 端点 mimetype（前端一律 fetch 字节流交渲染器，mimetype 仅供调试）
EXT_MIME = {
    "pdf": "application/pdf",
    "ofd": "application/ofd",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "doc": "application/msword",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
}

# 全局文件写锁（categories.json / files.json 读写共用）
_LOCK = threading.RLock()


# ===================== 会话与权限 =====================

def _viewer():
    """当前登录用户上下文；未登录返回 None。"""
    if _get_session_user is None:
        return None
    try:
        return _get_session_user()
    except Exception:
        return None


def _can_manage(user):
    """管理权限：超级管理员或管理员角色。"""
    if not user:
        return False
    if user.get("super_admin"):
        return True
    return (user.get("role_id") in MANAGE_ROLE_IDS
            or user.get("role") in MANAGE_ROLE_NAMES)


# ===================== 存储（单库 JSON + 原子写） =====================

def _categories_file():
    return os.path.join(DATA_DIR, "categories.json")


def _files_file():
    return os.path.join(DATA_DIR, "files.json")


def _load_store(path):
    """读取单库 JSON；文件缺失/损坏时返回空库（幂等重建）。"""
    if not os.path.isfile(path):
        return {"version": 1, "categories": []} if path == _categories_file() \
            else {"version": 1, "files": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return {"version": 1, "categories": []} if path == _categories_file() \
            else {"version": 1, "files": []}
    if not isinstance(rec, dict):
        rec = {}
    if not isinstance(rec.get("categories"), list) and "categories" in path:
        rec["categories"] = []
    if not isinstance(rec.get("files"), list) and "files" in path:
        rec["files"] = []
    rec.setdefault("version", 1)
    return rec


def _save_store(path, rec):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _gen_id(prefix, existing_ids):
    """服务端生成 ID（8 位 hex），带查重（SEC-3：禁止沿用用户输入作为主键）。"""
    while True:
        rid = f"{prefix}{uuid.uuid4().hex[:8]}"
        if rid not in existing_ids:
            return rid


def _file_path(fid, ext):
    """落盘路径：id 必须匹配白名单正则（SEC-1 目录穿越防线）。"""
    if not ID_RE.match(fid or ""):
        return None
    if ext not in ALLOWED_EXTS:
        return None
    return os.path.join(FILES_DIR, f"{fid}.{ext}")


def _office_fallback_possible(ext):
    """旧版格式（doc/xls）上传转换失败后，预览是否仍有兜底途径（决定 4xx 是否放行）：

    - `.xls`：xhr 引擎有 xlrd 兜底通道，可直接读 .xls（内容 100%、样式降级）→ 恒放行；
    - `.doc`：dhr 引擎需 LibreOffice 归一化 → 仅在 soffice 可探测到时放行；
    - 其余格式不适用（False）。
    """
    if ext == "xls":
        return True
    if ext == "doc":
        try:
            return bool(_office_render is not None and _office_render.soffice_available())
        except Exception:
            return False
    return False


def _migrate_files():
    """files.json 字段补齐（幂等，启动时执行一次）。

    历史记录的原件已被转换件覆盖（"替换存储"时代的行为），无法回溯，
    因此按「现有落盘件即原件」回填，下载端点据此仍可提供文件。
    阶段 8 起预览改为按需渲染，pdf_status/html_status 等状态字段不再使用：
    历史记录里的旧字段原样保留（无消费方，无害），新记录不再写入。
    """
    with _LOCK:
        store = _load_store(_files_file())
        changed = False
        for rec in store.get("files", []):
            # 一律用「键是否存在」判定，不能用取值判定：
            # original_size 可能为 0，用 rec.get(...) 判空会导致每次启动都误判需迁移。
            if "original_ext" not in rec or not rec.get("original_ext"):
                rec["original_ext"] = rec.get("ext", "")   # 旧记录：以现有落盘件为原件
                changed = True
            if "original_size" not in rec:
                rec["original_size"] = rec.get("size", 0)
                changed = True
        if changed:
            _save_store(_files_file(), store)
        return changed


# ===================== Office 预览（阶段 8：vendor 双引擎按需渲染 + 磁盘缓存） =====================
# 原 pdf_status / html_status 异步状态机（线程池/轮询/重试端点）废弃：
# xhr/dhr 引擎亚秒级完成渲染（50 页 Word 实测约 0.25s），首次请求 /preview 时
# 同步渲染并落盘缓存 <id>.preview.json，后续请求直接读缓存，无需任何状态轮询。

# 预览缓存格式版本：**引擎渲染行为发生变化时递增**（vendor 补丁/升级），
# 读取时版本不符即视为无缓存重新渲染 —— 否则升级引擎后旧缓存会让修复"不生效"。
PREVIEW_CACHE_VERSION = 3   # v3：.doc 归一化警告文案按 flow 模式分流（旧缓存警告横幅需重新生成）


def _preview_cache_path(fid):
    """预览缓存落盘路径（id 白名单校验同 SEC-1）。"""
    if not ID_RE.match(fid or ""):
        return None
    return os.path.join(FILES_DIR, f"{fid}.preview.json")


def _needs_office_preview(rec):
    """该记录是否属于 Office 预览范围（Word/Excel 类渲染件）。"""
    return (rec.get("ext") or "") in OFFICE_SOURCE_EXTS


def _render_office_record(rec):
    """渲染件字节 → 预览 payload dict；失败抛 OfficeRenderError（用户可读文案）。"""
    ext = rec.get("ext") or ""
    path = _file_path(rec.get("id") or "", ext)
    if not path or not os.path.isfile(path):
        raise _office_render.OfficeRenderError("渲染件缺失，无法生成预览")
    with open(path, "rb") as f:
        data = f.read()
    if ext in ("doc", "docx"):
        return _office_render.render_word(data)
    return _office_render.render_sheet(data)


# ===================== 分类树 =====================

def _find_cat(cats, cid):
    for c in cats:
        if c.get("id") == cid:
            return c
    return None


def _descendant_ids(cats, cid):
    """收集某分类的全部子孙分类 id（移动防环 / 列表递归用）。"""
    children = {}
    for c in cats:
        children.setdefault(c.get("parent_id"), []).append(c.get("id"))
    out, stack = set(), [cid]
    while stack:
        cur = stack.pop()
        for ch in children.get(cur, []):
            if ch not in out:
                out.add(ch)
                stack.append(ch)
    return out


def _sibling_name_taken(cats, name, parent_id, exclude_id=None):
    n = (name or "").strip()
    for c in cats:
        if c.get("id") == exclude_id:
            continue
        if (c.get("name") or "").strip() == n and c.get("parent_id") == parent_id:
            return True
    return False


# ===================== 文件元数据 =====================

def _file_brief(rec):
    ext = rec.get("ext", "")
    # original_ext：上传时的扩展名（迁移后所有记录都有值；缺失时回落 ext 兼容旧数据）
    original_ext = rec.get("original_ext") or ext
    return {
        "id": rec.get("id"),
        "name": rec.get("name", ""),
        "summary": rec.get("summary", ""),
        "original_name": rec.get("original_name", ""),
        "ext": ext,
        "size": rec.get("size", 0),
        # 原件（下载端点的返回对象）：大小与扩展名
        "original_ext": original_ext,
        "original_size": rec.get("original_size", rec.get("size", 0)),
        "category_id": rec.get("category_id"),
        # converted：是否由服务端做过旧版格式转换（原格式 ≠ 落盘主格式）
        "converted": bool(original_ext and original_ext != ext),
        "created_by": rec.get("created_by", ""),
        "created_by_name": rec.get("created_by_name", ""),
        "created_at": rec.get("created_at", ""),
        "updated_at": rec.get("updated_at") or "",
    }


def register(app) -> None:
    """插件入口：由 JZToolsHub 主应用在启动时调用。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FILES_DIR, exist_ok=True)
    try:
        _migrate_files()   # 幂等补齐元数据字段（旧数据升级）
    except Exception:
        pass               # 迁移失败不影响插件主体可用（下一轮启动继续尝试）

    @app.get(f"{API_PREFIX}/status")
    def kb_status():
        """依赖自检（B-4）。

        - 核心功能（浏览/渲染现代格式）纯标准库实现，恒可用；
        - 旧版格式转换（.doc→.docx / .xls→.xlsx）依赖 openpyxl/xlrd/python-docx/olefile，
          缺失时仅"自动转换"能力不可用，插件其余功能不受影响（优雅降级）；
        - Office 预览（阶段 8）依赖 vendor 的 xhr/dhr 引擎（纯标准库，随包分发恒可用）；
          其中 .doc 归一化 / .xls 高保真通道**可选**依赖外部 LibreOffice，
          缺失时 `soffice=null`，仅影响这两条窄路径（阅读回退降级渲染）。
        """
        deps = _doc_convert.availability()
        if _office_render is None:
            engine = {"available": False, "soffice": None, "error": "渲染模块未加载"}
        else:
            try:
                engine = _office_render.availability()
            except Exception:
                engine = {"available": False, "soffice": None, "error": "渲染引擎自检失败"}
        return jsonify({
            "ok": True,
            "dependencies": deps,
            "convert_legacy": all(deps.values()),
            "office_render": engine,
            "office_preview": bool(engine.get("available")),
        })

    @app.get(f"{API_PREFIX}/config")
    def kb_config():
        """前端渲染开关：当前用户是否可管理（上传/分类维护）。"""
        user = _viewer()
        return jsonify({"ok": True, "can_manage": _can_manage(user)})

    # ---------------- 分类 ----------------

    @app.get(f"{API_PREFIX}/categories")
    def kb_categories():
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            store = _load_store(_categories_file())
        cats = sorted(store["categories"], key=lambda c: (c.get("order", 0), c.get("created_at", "")))
        return jsonify({"ok": True, "count": len(cats), "items": cats})

    @app.post(f"{API_PREFIX}/categories")
    def kb_category_create():
        _set_operation("新建知识库分类")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库分类"}), 403
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        parent_id = str(body.get("parent_id") or "").strip() or None
        if not name:
            return jsonify({"ok": False, "error": "请输入分类名称"}), 400
        if len(name) > 40:
            return jsonify({"ok": False, "error": "分类名称不能超过 40 字"}), 400
        with _LOCK:
            store = _load_store(_categories_file())
            cats = store["categories"]
            if len(cats) >= MAX_CATEGORIES:
                return jsonify({"ok": False, "error": "分类数量已达上限"}), 409
            if parent_id and not _find_cat(cats, parent_id):
                return jsonify({"ok": False, "error": "上级分类不存在"}), 404
            if _sibling_name_taken(cats, name, parent_id):
                return jsonify({"ok": False, "error": "同级下已存在同名分类"}), 409
            order = 1
            siblings = [c for c in cats if c.get("parent_id") == parent_id]
            if siblings:
                order = max(int(c.get("order", 0) or 0) for c in siblings) + 1
            cat = {
                "id": _gen_id("cat-", {c.get("id") for c in cats}),
                "name": name,
                "parent_id": parent_id,
                "order": order,
                # 归属四字段：来源为会话（规范 9.2 铁律一）
                "created_by": user.get("username", ""),
                "created_by_name": user.get("name", ""),
                "unit_id": user.get("unit_id", ""),
                "department_id": user.get("department_id", ""),
                "created_at": _now(),
            }
            cats.append(cat)
            _save_store(_categories_file(), store)
        return jsonify({"ok": True, "id": cat["id"]})

    @app.put(f"{API_PREFIX}/categories/<cid>")
    def kb_category_update(cid):
        _set_operation("修改知识库分类")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库分类"}), 403
        body = request.get_json(silent=True) or {}
        name = body.get("name")
        parent_id = body.get("parent_id")
        order = body.get("order")
        with _LOCK:
            store = _load_store(_categories_file())
            cats = store["categories"]
            cat = _find_cat(cats, cid)
            if not cat or not ID_RE.match(cid or ""):
                return jsonify({"ok": False, "error": "分类不存在或已被删除"}), 404
            if name is not None:
                name = str(name).strip()
                if not name:
                    return jsonify({"ok": False, "error": "请输入分类名称"}), 400
                if len(name) > 40:
                    return jsonify({"ok": False, "error": "分类名称不能超过 40 字"}), 400
                if _sibling_name_taken(cats, name, cat.get("parent_id"), exclude_id=cid):
                    return jsonify({"ok": False, "error": "同级下已存在同名分类"}), 409
                cat["name"] = name
            if parent_id is not None:
                parent_id = str(parent_id).strip() or None
                if parent_id == cid:
                    return jsonify({"ok": False, "error": "上级分类不能是自身"}), 400
                if parent_id and not _find_cat(cats, parent_id):
                    return jsonify({"ok": False, "error": "上级分类不存在"}), 404
                if parent_id and parent_id in _descendant_ids(cats, cid):
                    return jsonify({"ok": False, "error": "上级分类不能是自身的子分类"}), 400
                cat["parent_id"] = parent_id
            if order is not None:
                try:
                    cat["order"] = int(order)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "排序值不合法"}), 400
            _save_store(_categories_file(), store)
        return jsonify({"ok": True, "id": cid})

    @app.delete(f"{API_PREFIX}/categories/<cid>")
    def kb_category_delete(cid):
        _set_operation("删除知识库分类")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库分类"}), 403
        with _LOCK:
            store = _load_store(_categories_file())
            cats = store["categories"]
            if not ID_RE.match(cid or "") or not _find_cat(cats, cid):
                return jsonify({"ok": False, "error": "分类不存在或已被删除"}), 404
            if any(c.get("parent_id") == cid for c in cats):
                return jsonify({"ok": False, "error": "该分类下存在子分类，请先删除子分类"}), 409
            fstore = _load_store(_files_file())
            if any(f.get("category_id") == cid for f in fstore["files"]):
                return jsonify({"ok": False, "error": "该分类下仍有文件，请先移出或删除文件"}), 409
            store["categories"] = [c for c in cats if c.get("id") != cid]
            _save_store(_categories_file(), store)
        return jsonify({"ok": True})

    # ---------------- 文件 ----------------

    @app.get(f"{API_PREFIX}/files")
    def kb_files():
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        category = (request.args.get("category") or "").strip()
        q = (request.args.get("q") or "").strip().lower()
        with _LOCK:
            fstore = _load_store(_files_file())
            cstore = _load_store(_categories_file())
        files = fstore["files"]
        if category and category != "all":
            # 选中分类 = 该分类及其全部子孙分类（浏览子分类时同步呈现父级归档内容）
            keep = _descendant_ids(cstore["categories"], category)
            keep.add(category)
            files = [f for f in files if f.get("category_id") in keep]
        if q:
            files = [f for f in files
                     if q in (f.get("name") or "").lower()
                     or q in (f.get("original_name") or "").lower()]
        items = sorted(files, key=lambda f: f.get("created_at", ""), reverse=True)
        return jsonify({"ok": True, "count": len(items),
                        "items": [_file_brief(f) for f in items]})

    @app.post(f"{API_PREFIX}/files")
    def kb_file_upload():
        _set_operation("上传知识库文件")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可上传知识库文件"}), 403

        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"ok": False, "error": "请选择要上传的文件"}), 400
        name = str(request.form.get("name") or "").strip()
        category_id = str(request.form.get("category_id") or "").strip()
        if not name:
            name = os.path.splitext(upload.filename)[0] or "未命名"
        if len(name) > MAX_NAME_LEN:
            return jsonify({"ok": False, "error": f"文件名不能超过 {MAX_NAME_LEN} 字"}), 400
        summary = str(request.form.get("summary") or "").strip()
        if len(summary) > MAX_SUMMARY_LEN:
            return jsonify({"ok": False, "error": f"简介不能超过 {MAX_SUMMARY_LEN} 字"}), 400

        ext = os.path.splitext(upload.filename)[1].lstrip(".").lower()
        if not ext:
            return jsonify({"ok": False, "error": "无法识别文件类型（缺少扩展名）"}), 415
        if ext not in ALLOWED_EXTS:
            return jsonify({"ok": False, "error": f"暂不支持 .{ext} 格式（支持：PDF/OFD/Word/Excel/Markdown/文本）"}), 415

        # 大小校验（SEC-3）：先查 Content-Length，再流式读取硬上限+1 字节防虚报
        if request.content_length and request.content_length > MAX_UPLOAD_BYTES + 1024 * 1024:
            return jsonify({"ok": False, "error": "文件超过 20MB 上限"}), 413
        blob = upload.stream.read(MAX_UPLOAD_BYTES + 1)
        if len(blob) > MAX_UPLOAD_BYTES:
            return jsonify({"ok": False, "error": "文件超过 20MB 上限"}), 413
        if not blob:
            return jsonify({"ok": False, "error": "文件内容为空"}), 400

        # 旧版格式（.doc / .xls）→ 服务端转换为 docx / xlsx 作为**渲染件**；
        # 原件始终单独保存（下载端点专用），不再被转换产物替换。
        # 转换失败处理（B-4）：预览引擎对旧格式仍有兜底途径（.xls 走 xlrd 兜底通道、
        # .doc 走 LibreOffice 归一化，见 _office_fallback_possible）时不阻断；
        # 无兜底途径时渲染件是唯一预览途径 → 维持 4xx 明确提示。
        upload_ext = ext                    # 原始上传扩展名 = 原件扩展名（所有记录都记）
        converted_from = None               # 旧版转换来源（仅 doc/xls 有值，前端"已转换"提示用）
        render_blob, render_ext = blob, ext # 主落盘件（渲染件）
        if ext in LEGACY_CONVERT:
            converted = None
            try:
                converted = _doc_convert.convert_legacy(ext, blob)
            except _doc_convert.ConvertError as e:
                if not _office_fallback_possible(ext):
                    return jsonify({"ok": False, "error": str(e)}), 422
            except Exception:
                if not _office_fallback_possible(ext):
                    return jsonify({"ok": False, "error": f".{ext} 转换失败，请尝试另存为 "
                                                         f".{LEGACY_CONVERT[ext]} 后上传"}), 500
            if converted:
                new_blob, new_ext = converted
                if len(new_blob) > MAX_UPLOAD_BYTES:
                    return jsonify({"ok": False, "error": "文件转换后超过 20MB 上限，无法保存"}), 413
                render_blob, render_ext = new_blob, new_ext
                converted_from = ext

        with _LOCK:
            fstore = _load_store(_files_file())
            cstore = _load_store(_categories_file())
            if category_id:
                if not ID_RE.match(category_id) or not _find_cat(cstore["categories"], category_id):
                    return jsonify({"ok": False, "error": "目标分类不存在"}), 404
            if len(fstore["files"]) >= MAX_FILES:
                return jsonify({"ok": False, "error": "文件数量已达上限"}), 409
            fid = _gen_id("f-", {f.get("id") for f in fstore["files"]})
            # 1) 原件落盘（所有类型；下载端点返回它）
            orig_path = _file_path(fid, upload_ext)
            if orig_path is None:
                return jsonify({"ok": False, "error": "文件标识不合法"}), 400
            with open(orig_path, "wb") as f:
                f.write(blob)
            # 2) 渲染件落盘（与原件不同名时才额外写一份：doc/xls 的 docx/xlsx 转换产物）
            path = _file_path(fid, render_ext)
            if path is None:
                return jsonify({"ok": False, "error": "文件标识不合法"}), 400
            if os.path.abspath(path) != os.path.abspath(orig_path):
                with open(path, "wb") as f:
                    f.write(render_blob)
            # 3) 元数据
            rec = {
                "id": fid,
                "name": name,
                "summary": summary,
                "original_name": os.path.basename(upload.filename),
                "ext": render_ext,
                "size": len(render_blob),
                # 原件（下载用）：扩展名与字节数
                "original_ext": upload_ext,
                "original_size": len(blob),
                # 预览状态字段（pdf_status/html_status）自阶段 8 起废弃：
                # 预览按需渲染 + 磁盘缓存，不再有任何后台状态机
                "category_id": category_id or None,
                # 归属四字段：来源为会话（规范 9.2 铁律一）
                "created_by": user.get("username", ""),
                "created_by_name": user.get("name", ""),
                "unit_id": user.get("unit_id", ""),
                "department_id": user.get("department_id", ""),
                "created_at": _now(),
                "updated_at": None,
            }
            fstore["files"].append(rec)
            _save_store(_files_file(), fstore)
        resp = {"ok": True, "id": fid, "item": _file_brief(rec)}
        if converted_from:
            resp["converted_from"] = converted_from   # 前端据此提示"已自动转换"
        return jsonify(resp)

    @app.put(f"{API_PREFIX}/files/<fid>")
    def kb_file_update(fid):
        _set_operation("修改知识库文件信息")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库文件"}), 403
        body = request.get_json(silent=True) or {}
        name = body.get("name")
        summary = body.get("summary")
        category_id = body.get("category_id")
        with _LOCK:
            fstore = _load_store(_files_file())
            cstore = _load_store(_categories_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
            if not rec:
                return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
            if name is not None:
                name = str(name).strip()
                if not name:
                    return jsonify({"ok": False, "error": "请输入文件名"}), 400
                if len(name) > MAX_NAME_LEN:
                    return jsonify({"ok": False, "error": f"文件名不能超过 {MAX_NAME_LEN} 字"}), 400
                rec["name"] = name
            if summary is not None:
                summary = str(summary).strip()
                if len(summary) > MAX_SUMMARY_LEN:
                    return jsonify({"ok": False, "error": f"简介不能超过 {MAX_SUMMARY_LEN} 字"}), 400
                rec["summary"] = summary
            if category_id is not None:
                category_id = str(category_id).strip()
                if category_id and (not ID_RE.match(category_id)
                                    or not _find_cat(cstore["categories"], category_id)):
                    return jsonify({"ok": False, "error": "目标分类不存在"}), 404
                rec["category_id"] = category_id or None
            rec["updated_at"] = _now()
            _save_store(_files_file(), fstore)
        return jsonify({"ok": True, "item": _file_brief(rec)})

    @app.delete(f"{API_PREFIX}/files/<fid>")
    def kb_file_delete(fid):
        _set_operation("删除知识库文件")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        if not _can_manage(user):
            return jsonify({"ok": False, "error": "仅管理员可管理知识库文件"}), 403
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
            if not rec:
                return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
            # 清理：原件 + 渲染件 + 预览缓存 + 历史 PDF 残留（旧版本产物，按绝对路径去重）
            seen, paths = set(), []
            for e in (rec.get("original_ext") or "", rec.get("ext") or "", "pdf"):
                p = _file_path(fid, e)
                if not p:
                    continue
                ap = os.path.abspath(p)
                if ap in seen:
                    continue
                seen.add(ap)
                paths.append(p)
            cp = _preview_cache_path(fid)
            if cp and os.path.abspath(cp) not in seen:
                paths.append(cp)
            for p in paths:
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        return jsonify({"ok": False, "error": "删除失败，请稍后重试"}), 500
            fstore["files"] = [f for f in fstore["files"] if f.get("id") != fid]
            _save_store(_files_file(), fstore)
        return jsonify({"ok": True})

    @app.get(f"{API_PREFIX}/files/<fid>/raw")
    def kb_file_raw(fid):
        """内联返回文件内容（供前端渲染器 fetch 字节流）。

        - 不设置 attachment 头 → 浏览器不触发"另存为"，仅内联读取（只读定位）；
        - conditional=True → 支持 Range / ETag / 304（pdf.js 断点续读）；
        - ID 白名单正则 + 扩展名二次校验（SEC-1），不可读一律 404（不暴露存在性）。
        """
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
        if not rec:
            return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
        path = _file_path(fid, rec.get("ext", ""))
        if not path or not os.path.isfile(path):
            return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
        resp = send_file(path, mimetype=EXT_MIME.get(rec.get("ext", ""), "application/octet-stream"),
                         conditional=True)
        resp.headers["Content-Disposition"] = f'inline; filename="{fid}.{rec.get("ext", "")}"'
        return resp

    @app.get(f"{API_PREFIX}/files/<fid>/download")
    def kb_file_download(fid):
        """附件形式下载**原件**（全体登录用户可用）。

        - Word/Excel 下载的是用户上传的原始文档（doc/docx/xls/xlsx），不是 PDF 版；
        - 其余类型（pdf/ofd/md/txt/csv）原件即唯一文件，同一端点天然适用；
        - `download_name` 交给 Flask 处理 RFC 5987 编码（中文文件名老浏览器也可用）；
        - ID 白名单正则 + 扩展名二次校验（SEC-1），缺失/不可读一律 404（SEC-5）。
        """
        _set_operation("下载知识库文件")
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
        if not rec:
            return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
        ext = rec.get("original_ext") or rec.get("ext") or ""
        path = _file_path(fid, ext)
        if not path or not os.path.isfile(path):
            return jsonify({"ok": False, "error": "文件不存在或已被删除"}), 404
        name = rec.get("original_name") or f"{fid}.{ext}"
        return send_file(path, mimetype=EXT_MIME.get(ext, "application/octet-stream"),
                         as_attachment=True, download_name=name, conditional=True)

    @app.get(f"{API_PREFIX}/files/<fid>/preview")
    def kb_file_preview(fid):
        """返回 Office（Word/Excel）预览 JSON：{kind, html, warnings, truncated}。

        阶段 8 起 docx/doc/xlsx/xls 由 vendor 双引擎（dhr/xhr）**按需同步渲染**：
        - 首次请求渲染并原子落盘缓存 `<id>.preview.json`（文件按 id 不可变，缓存无失效问题）；
          后续请求直接读缓存；
        - 渲染失败 / 引擎未加载 / 非 Office 类 / 不存在 → 一律 404 + 用户可读 error
          （前端收到 404 自动回退 mammoth / SheetJS 降级渲染，SEC-5 不暴露内部差异）；
        - html 由引擎生成（全部文本转义 + URL 协议/字体白名单），前端注入前仍过
          DOMPurify 纵深防御；只读预览，不提供下载（原件走 /download）。
        """
        user = _viewer()
        if user is None:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        with _LOCK:
            fstore = _load_store(_files_file())
            rec = next((f for f in fstore["files"] if f.get("id") == fid and ID_RE.match(fid or "")), None)
        if not rec or not _needs_office_preview(rec):
            return jsonify({"ok": False, "error": "文件不存在或不支持预览"}), 404
        cache = _preview_cache_path(fid)
        if cache and os.path.isfile(cache):
            try:
                with open(cache, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if (isinstance(payload, dict) and payload.get("html")
                        and payload.get("version") == PREVIEW_CACHE_VERSION):
                    payload["cached"] = True
                    return jsonify(payload)
            except Exception:
                pass  # 缓存损坏/版本过期 → 走重新渲染（下次覆盖）
        if _office_render is None:
            return jsonify({"ok": False, "error": "服务器渲染引擎未加载"}), 404
        try:
            result = _render_office_record(rec)
        except _office_render.OfficeRenderError as e:
            return jsonify({"ok": False, "error": str(e)}), 404
        except Exception:
            return jsonify({"ok": False, "error": "预览生成失败（内部错误）"}), 404
        payload = {
            "ok": True,
            "version": PREVIEW_CACHE_VERSION,
            "kind": result["kind"],
            "html": result["html"],
            "warnings": result.get("warnings") or [],
            "truncated": bool(result.get("truncated")),
            "generated_at": _now(),
        }
        if cache:
            try:
                dst_dir = os.path.dirname(cache)
                fd, tmp = tempfile.mkstemp(prefix=".preview-", dir=dst_dir)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp, cache)   # 同分区原子替换
            except OSError:
                pass  # 缓存写失败不影响本次响应（下次重新渲染）
        return jsonify(payload)
