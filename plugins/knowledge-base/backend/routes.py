"""知识库 —— JZToolsHub 后端插件路由。

功能：
- 管理员角色（role-admin）/超级管理员可上传 PDF / OFD / Word / Excel / Markdown /
  纯文本等文档，落盘到数据根目录 plugins/knowledge-base/data/files/
  （服务端生成 ID 重命名，SEC-3），并支持多级分类树管理（新建 / 改名 / 移动 / 删除）；
- **旧版格式自动转换**：上传 `.doc` / `.xls` 时先由服务端转换为 `.docx` / `.xlsx`
  再落盘（见 doc_convert.py），库内只保留现代格式；原格式记于 `original_ext` 字段，
  前端据此在页面提示"已自动转换"。转换依赖缺失或文件损坏时返回 4xx 明确提示，不 500；
- 全体登录用户可浏览分类树与文件列表，并经 /files/<id>/raw 内联读取文件内容
  供前端纯 JS 渲染（pdf.js / ofd.js / mammoth / SheetJS / marked，无浏览器控件）；
- 本插件定位为"只读知识库"：不提供编辑接口、不提供附件下载（attachment）端点，
  阅读与复制均在前端只读渲染层完成。

数据持久化（规范 9.1/9.2）：
- data/categories.json  分类树（单库文件）
- data/files.json       文件元数据索引（单库文件）
- data/files/<id>.<ext> 上传的文件（<id> 匹配 ^[A-Za-z0-9_-]+$，SEC-1；
                        旧版格式保存的是**转换后**的 docx/xlsx）
- 分类与文件记录均含 created_by / created_by_name / unit_id / department_id
  四个归属字段，一律取自服务端会话、禁止从请求体接收（铁律一）；
  阅读为全站公共资源，列表不做单位/部门过滤（设计文档 §3.3）。

接口前缀：/api/knowledge-base
"""

import json
import os
import re
import threading
import uuid
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
    return {
        "id": rec.get("id"),
        "name": rec.get("name", ""),
        "summary": rec.get("summary", ""),
        "original_name": rec.get("original_name", ""),
        "ext": rec.get("ext", ""),
        "size": rec.get("size", 0),
        "category_id": rec.get("category_id"),
        # original_ext：原始上传格式（转换过的旧格式才有值，如 doc / xls）
        # converted：是否由服务端自动转换而来（前端据此显示"已转换"提示）
        "original_ext": rec.get("original_ext"),
        "converted": bool(rec.get("original_ext")),
        "created_by": rec.get("created_by", ""),
        "created_by_name": rec.get("created_by_name", ""),
        "created_at": rec.get("created_at", ""),
        "updated_at": rec.get("updated_at") or "",
    }


def register(app) -> None:
    """插件入口：由 JZToolsHub 主应用在启动时调用。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FILES_DIR, exist_ok=True)

    @app.get(f"{API_PREFIX}/status")
    def kb_status():
        """依赖自检（B-4）。

        - 核心功能（浏览/渲染现代格式）纯标准库实现，恒可用；
        - 旧版格式转换（.doc→.docx / .xls→.xlsx）依赖 openpyxl/xlrd/python-docx/olefile，
          缺失时仅"自动转换"能力不可用，插件其余功能不受影响（优雅降级）。
        """
        deps = _doc_convert.availability()
        return jsonify({
            "ok": True,
            "dependencies": deps,
            "convert_legacy": all(deps.values()),
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

        # 旧版格式（.doc / .xls）→ 服务端转换为 docx / xlsx 后再落盘。
        # 转换失败一律 4xx 明确提示（规范 B-4：缺依赖/坏文件都不得 500）。
        original_ext = None
        if ext in LEGACY_CONVERT:
            try:
                converted = _doc_convert.convert_legacy(ext, blob)
            except _doc_convert.ConvertError as e:
                return jsonify({"ok": False, "error": str(e)}), 422
            except Exception:
                return jsonify({"ok": False, "error": f".{ext} 转换失败，请尝试另存为 "
                                                     f".{LEGACY_CONVERT[ext]} 后上传"}), 500
            if not converted:
                return jsonify({"ok": False, "error": f".{ext} 转换失败（未产生有效文件）"}), 500
            new_blob, new_ext = converted
            if len(new_blob) > MAX_UPLOAD_BYTES:
                return jsonify({"ok": False, "error": "文件转换后超过 20MB 上限，无法保存"}), 413
            original_ext, blob, ext = ext, new_blob, new_ext

        with _LOCK:
            fstore = _load_store(_files_file())
            cstore = _load_store(_categories_file())
            if category_id:
                if not ID_RE.match(category_id) or not _find_cat(cstore["categories"], category_id):
                    return jsonify({"ok": False, "error": "目标分类不存在"}), 404
            if len(fstore["files"]) >= MAX_FILES:
                return jsonify({"ok": False, "error": "文件数量已达上限"}), 409
            fid = _gen_id("f-", {f.get("id") for f in fstore["files"]})
            path = _file_path(fid, ext)
            if path is None:
                return jsonify({"ok": False, "error": "文件标识不合法"}), 400
            with open(path, "wb") as f:
                f.write(blob)
            rec = {
                "id": fid,
                "name": name,
                "summary": summary,
                "original_name": os.path.basename(upload.filename),
                "ext": ext,
                "size": len(blob),
                # 服务端自动转换来源（None 表示原生格式，未做转换）
                "original_ext": original_ext,
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
        if original_ext:
            resp["converted_from"] = original_ext   # 前端据此提示"已自动转换"
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
            path = _file_path(fid, rec.get("ext", ""))
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
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
