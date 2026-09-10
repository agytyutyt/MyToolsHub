"""文件过滤器 —— JZToolsHub 后端插件路由。

功能：对上传表格（xlsx/xls/csv，≤20MB）做字段过滤（脱敏）与文本后处理（合规检查）：
- 硬过滤：按固定字段名单保留列（规整后精确匹配），其余删除；
- 大模型过滤：提取全部表头交大模型判断与保留名单的语义关联（「时间」↔「开始时间」），
  匹配保留、其余删除；
- 后处理：按管理员配置的文本/正则规则对表头与单元格做替换（如「开始时间」-「开始」→「时间」）。

接口前缀：/api/file-filter（B-2）。
程序化调用（供其他插件）：POST /api/file-filter/apply —— JSON in / JSON out，同步，不落盘。

并发设计：大模型过滤走后台线程池 + task_id 轮询（8.5 异步任务模式）；
硬过滤同步完成（毫秒级），也统一走任务表返回 task_id，前端只实现一种轮询。
"""

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import jsonify, request, send_file

from . import core, llm_client

try:
    from jztools_admin.routes import get_session_user as _get_session_user
except Exception:  # admin 插件缺失时兜底
    _get_session_user = None

try:
    from jztools_admin.routes import set_operation as _set_operation
except Exception:  # 主应用未提供日志辅助时兜底
    def _set_operation(op):
        pass

try:
    import requests  # noqa: F401  LLM HTTP 调用依赖
    REQUESTS_AVAILABLE = True
except Exception:
    requests = None
    REQUESTS_AVAILABLE = False

import jztools_data

CONFIG_FILE = jztools_data.get_data_root_file("plugins", "file-filter", "config.json")
TASK_DIR = jztools_data.get_data_root_dir("plugins", "file-filter", ".task_cache")
API_PREFIX = "/api/file-filter"

FILTER_WORKERS = 2
_executor = ThreadPoolExecutor(max_workers=FILTER_WORKERS)
TASKS = {}
TASKS_LOCK = threading.Lock()
TASK_TTL_SECONDS = 30 * 60

TASK_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB（SEC-3）

DEFAULT_CONFIG = {
    "llm": {"base_url": "", "api_key": "", "model": ""},
    "keep_columns": [],
    "post_rules": [],
}

# 管理权限口径与 knowledge-base / notice-board 一致：超管或管理员角色
MANAGE_ROLE_IDS = {"role-admin"}
MANAGE_ROLE_NAMES = {"管理员"}


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


# ===================== 配置读写 =====================

def load_config():
    data = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            data = {}
    llm = data.get("llm") or {}
    keep = data.get("keep_columns")
    rules = data.get("post_rules")
    return {
        "llm": {
            "base_url": (llm.get("base_url") or "").strip(),
            "api_key": (llm.get("api_key") or "").strip(),
            "model": (llm.get("model") or "").strip(),
        },
        "keep_columns": [str(k).strip() for k in keep if str(k).strip()] if isinstance(keep, list) else [],
        "post_rules": _sanitize_rules(rules),
    }


def _sanitize_rules(rules):
    """规整后处理规则列表（防脏数据）。"""
    out = []
    if isinstance(rules, list):
        for r in rules:
            if not isinstance(r, dict):
                continue
            pattern = str(r.get("pattern") or "").strip()
            if not pattern:
                continue
            out.append({
                "pattern": pattern[:200],
                "replacement": "" if r.get("replacement") is None else str(r.get("replacement"))[:200],
                "is_regex": bool(r.get("is_regex")),
                "enabled": r.get("enabled") is not False,
            })
    return out


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ===================== 任务表 =====================

def set_task(task_id, **kwargs):
    with TASKS_LOCK:
        TASKS[task_id] = {**TASKS.get(task_id, {}), **kwargs}


def get_task(task_id):
    with TASKS_LOCK:
        return TASKS.get(task_id)


def cleanup_tasks():
    now = time.time()
    with TASKS_LOCK:
        expired = [tid for tid, t in TASKS.items()
                   if t.get("created_at", 0) < now - TASK_TTL_SECONDS]
        for tid in expired:
            TASKS.pop(tid, None)


def create_task(user):
    tid = uuid.uuid4().hex[:12]
    set_task(tid, status="pending", created_at=time.time(),
             created_by=(user or {}).get("username") or "")
    return tid


def _task_owned_by(task, user):
    """任务归属校验：非创建者且非超管视为不存在（404，纵深防御）。"""
    owner = task.get("created_by")
    if not owner:
        return bool(user and user.get("super_admin"))
    if user is None:
        return False
    if user.get("super_admin"):
        return True
    return owner == user.get("username")


def _clean_task_files():
    os.makedirs(TASK_DIR, exist_ok=True)
    now = time.time()
    for name in os.listdir(TASK_DIR):
        path = os.path.join(TASK_DIR, name)
        try:
            if now - os.path.getmtime(path) > TASK_TTL_SECONDS:
                os.remove(path)
        except Exception:
            pass


# ===================== 过滤执行 =====================

def run_filter(headers, rows, mode, keep_columns, post_rules, llm_cfg):
    """执行过滤 + 后处理，返回 (headers2, rows2, kept, removed, replace_count, llm_used)。

    mode: hard / llm；llm 模式失败抛 llm_client.LLMError。
    """
    kept, removed = [], []
    llm_used = False
    if mode == "llm":
        mappings = llm_client.match_columns(
            headers, keep_columns,
            llm_cfg.get("base_url"), llm_cfg.get("api_key"), llm_cfg.get("model"),
        )
        llm_used = True
        keep_idx = []
        keep_fold = {k.casefold(): k for k in keep_columns}
        for i, h in enumerate(headers):
            if mappings.get(h):
                keep_idx.append(i)
                kept.append((h, keep_fold.get(mappings[h].casefold(), mappings[h])))
            else:
                removed.append(h)
        headers2 = [headers[i] for i in keep_idx]
        rows2 = [[(r[i] if i < len(r) else None) for i in keep_idx] for r in rows]
    else:
        headers2, rows2, kept, removed = core.filter_columns(headers, rows, keep_columns)
    headers2, rows2, count = core.post_process(headers2, rows2, post_rules)
    return headers2, rows2, kept, removed, count, llm_used


def _resolve_params(mode, columns_raw):
    """规整模式与保留名单：columns 参数缺省时用管理员配置。"""
    cfg = load_config()
    if columns_raw:
        keep = columns_raw
    else:
        keep = cfg["keep_columns"]
    if mode not in ("hard", "llm"):
        mode = "hard"
    return cfg, mode, keep


def _run_filter_task(task_id, in_path, in_ext, out_path, out_ext, mode, keep, post_rules, llm_cfg):
    """后台线程执行：读表 → 过滤 → 后处理 → 写出 → 任务置 done。"""
    try:
        set_task(task_id, status="running")
        headers, rows = core.read_table(in_path, f"input.{in_ext}")
        headers2, rows2, kept, removed, count, llm_used = run_filter(
            headers, rows, mode, keep, post_rules, llm_cfg)
        core.write_table(out_path, out_ext, headers2, rows2)
        set_task(task_id, status="done",
                 kept=[{"column": c, "matched": m} for c, m in kept],
                 removed=removed,
                 replace_count=count,
                 llm_used=llm_used,
                 rows=len(rows2),
                 filename=os.path.basename(out_path),
                 download=f"{API_PREFIX}/download/{task_id}",
                 output_ext=out_ext)
    except llm_client.LLMError as e:
        set_task(task_id, status="error", detail=f"大模型过滤失败：{e}")
    except core.TableError as e:
        set_task(task_id, status="error", detail=str(e))
    except Exception as e:  # SEC-5：不透出堆栈与路径
        set_task(task_id, status="error", detail=f"过滤失败（{type(e).__name__}）")


# ===================== 路由注册 =====================

def register(app):
    @app.get(f"{API_PREFIX}/status")
    def status():
        cfg = load_config()
        return jsonify({
            "ok": True,
            "dependencies": {
                "openpyxl": core.OPENPYXL_AVAILABLE,
                "xlrd": core.XLRD_AVAILABLE,
                "requests": REQUESTS_AVAILABLE,
            },
            "llm_configured": bool(cfg["llm"].get("api_key")),
        })

    @app.get(f"{API_PREFIX}/config")
    def get_config():
        cfg = load_config()
        cfg = json.loads(json.dumps(cfg))  # 深拷贝
        llm_key = cfg["llm"]["api_key"]
        cfg["llm"]["api_key"] = _mask(llm_key)
        cfg["llm_configured"] = bool(llm_key)
        cfg["can_manage"] = _can_manage(_viewer())
        return jsonify(cfg)

    @app.post(f"{API_PREFIX}/config")
    def post_config():
        user = _viewer()
        if not _can_manage(user):
            return jsonify({"error": "仅管理员可修改过滤配置"}), 403
        data = request.get_json(silent=True) or {}
        cfg = load_config()
        llm = data.get("llm")
        if isinstance(llm, dict):
            key = str(llm.get("api_key") or "").strip()
            if not key or key == "••••••••":
                key = cfg["llm"]["api_key"]  # 掩码/留空表示不修改
            cfg["llm"] = {
                "base_url": str(llm.get("base_url") or "").strip(),
                "api_key": key,
                "model": str(llm.get("model") or "").strip(),
            }
        if "keep_columns" in data:
            keep = data.get("keep_columns")
            cfg["keep_columns"] = [str(k).strip()[:100] for k in keep if str(k).strip()] \
                if isinstance(keep, list) else []
        if "post_rules" in data:
            cfg["post_rules"] = _sanitize_rules(data.get("post_rules"))
        save_config(cfg)
        _set_operation("保存过滤器配置")
        return jsonify({"ok": True})

    @app.post(f"{API_PREFIX}/config/test")
    def test_config():
        if not _can_manage(_viewer()):
            return jsonify({"error": "仅管理员可测试大模型配置"}), 403
        data = request.get_json(silent=True) or {}
        cfg = load_config()
        base_url = str(data.get("base_url") or cfg["llm"]["base_url"]).strip()
        api_key = str(data.get("api_key") or "").strip()
        if not api_key or api_key == "••••••••":
            api_key = cfg["llm"]["api_key"]
        model = str(data.get("model") or cfg["llm"]["model"]).strip()
        ok, detail = llm_client.test_connection(base_url, api_key, model)
        return jsonify({"ok": ok, "detail": detail})

    @app.post(f"{API_PREFIX}/filter")
    def filter_upload():
        user = _viewer()
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "未选择文件"}), 400
        orig = f.filename or ""
        ext = orig.lower().rsplit(".", 1)[-1] if "." in orig else ""
        if ext not in core.ALLOWED_EXTS:
            return jsonify({"error": "仅支持 xlsx / xls / csv 文件"}), 400
        blob = f.read()
        if len(blob) > MAX_UPLOAD_BYTES:
            return jsonify({"error": "文件超过 20MB 上限"}), 413
        if not blob:
            return jsonify({"error": "文件为空"}), 400
        mode = (request.form.get("mode") or "hard").strip()
        columns_raw = None
        try:
            columns_raw = json.loads(request.form.get("columns") or "null")
        except Exception:
            columns_raw = None
        cfg, mode, keep = _resolve_params(mode, columns_raw if isinstance(columns_raw, list) else None)
        if not keep:
            return jsonify({"error": "保留字段名单为空：请勾选保留字段或联系管理员配置"}), 400
        if mode == "llm" and not cfg["llm"].get("api_key"):
            return jsonify({"error": "大模型未配置：请管理员在「管理配置」中填写 API 地址、API Key 与模型名称"}), 400

        task_id = create_task(user)
        cleanup_tasks()
        _clean_task_files()
        # SEC-3：落盘文件名用 task_id 重命名，不沿用用户原始文件名
        in_path = os.path.join(TASK_DIR, f"{task_id}_input.{ext}")
        out_ext = "csv" if ext == "csv" else "xlsx"  # .xls 只读，输出转 .xlsx
        out_path = os.path.join(TASK_DIR, f"{task_id}_output.{out_ext}")
        with open(in_path, "wb") as fp:
            fp.write(blob)
        post_rules = cfg["post_rules"]
        set_task(task_id, output_ext=out_ext, original_name=orig)
        _executor.submit(_run_filter_task, task_id, in_path, ext, out_path, out_ext,
                         mode, keep, post_rules, cfg["llm"])
        _set_operation("提交表格过滤任务")
        return jsonify({"task_id": task_id, "mode": mode, "output_ext": out_ext})

    @app.get(f"{API_PREFIX}/result/<task_id>")
    def filter_result(task_id):
        if not TASK_ID_RE.match(task_id or ""):
            return jsonify({"error": "非法任务 ID"}), 400
        task = get_task(task_id)
        if not task or not _task_owned_by(task, _viewer()):
            return jsonify({"error": "任务不存在或已过期"}), 404
        payload = {
            "task_id": task_id,
            "status": task.get("status"),
        }
        if task.get("status") == "done":
            payload.update({
                "kept": task.get("kept") or [],
                "removed": task.get("removed") or [],
                "replace_count": task.get("replace_count") or 0,
                "rows": task.get("rows") or 0,
                "llm_used": bool(task.get("llm_used")),
                "download": task.get("download"),
                "filename": task.get("original_name") or "",
                "output_ext": task.get("output_ext") or "",
            })
        elif task.get("status") == "error":
            payload["detail"] = task.get("detail") or "过滤失败"
        return jsonify(payload)

    @app.get(f"{API_PREFIX}/download/<task_id>")
    def filter_download(task_id):
        if not TASK_ID_RE.match(task_id or ""):
            return jsonify({"error": "非法任务 ID"}), 400
        task = get_task(task_id)
        if not task or not _task_owned_by(task, _viewer()):
            return jsonify({"error": "任务不存在或已过期"}), 404
        if task.get("status") != "done":
            return jsonify({"error": "任务尚未完成"}), 409
        out_ext = task.get("output_ext") or "xlsx"
        path = os.path.join(TASK_DIR, f"{task_id}_output.{out_ext}")
        if not os.path.isfile(path):
            return jsonify({"error": "结果文件已过期，请重新过滤"}), 404
        orig = task.get("original_name") or ""
        base = os.path.splitext(os.path.basename(orig))[0] if orig else "filtered"
        download_name = f"{base}_已过滤.{out_ext}"
        _set_operation("下载过滤后文件")
        return send_file(path, as_attachment=True, download_name=download_name)

    @app.post(f"{API_PREFIX}/apply")
    def apply_filter():
        """程序化调用接口（供其他插件调用）：JSON in / JSON out，同步，不落盘。

        请求体：{
          "rows": [[表头...], [数据行]...],   # 必填，首行为表头
          "mode": "hard" | "llm",            # 缺省 hard
          "columns": ["保留字段", ...],       # 缺省用管理员配置
          "post_rules": [{"pattern","replacement","is_regex","enabled"}]  # 缺省用管理员配置
        }
        响应：{"rows": [...], "kept": [{"column","matched"}], "removed": [...],
               "replace_count": N, "mode": "..."}
        """
        user = _viewer()
        if user is None:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        raw_rows = data.get("rows")
        if not isinstance(raw_rows, list) or not raw_rows or not isinstance(raw_rows[0], list):
            return jsonify({"error": "rows 必须为非空的二维数组（首行为表头）"}), 400
        if len(raw_rows) > core.MAX_ROWS + 1:
            return jsonify({"error": f"行数超过上限（≤{core.MAX_ROWS}）"}), 400
        cfg, mode, keep = _resolve_params(
            (data.get("mode") or "hard").strip(),
            data.get("columns") if isinstance(data.get("columns"), list) else None)
        if not keep:
            return jsonify({"error": "保留字段名单为空"}), 400
        post_rules = data.get("post_rules") if isinstance(data.get("post_rules"), list) \
            else cfg["post_rules"]
        headers = [core.clean_text(h) for h in raw_rows[0]]
        rows = []
        for r in raw_rows[1:]:
            if not isinstance(r, list):
                continue
            row = list(r[:len(headers)])
            if len(row) < len(headers):
                row.extend([None] * (len(headers) - len(row)))
            if all(core.clean_text(v) == "" for v in row):
                continue
            rows.append(row)
        try:
            headers2, rows2, kept, removed, count, _ = run_filter(
                headers, rows, mode, keep, post_rules, cfg["llm"])
        except llm_client.LLMError as e:
            return jsonify({"error": f"大模型过滤失败：{e}"}), 502
        except core.TableError as e:
            return jsonify({"error": str(e)}), 400
        _set_operation("程序化表格过滤")
        return jsonify({
            "rows": [headers2] + rows2,
            "kept": [{"column": c, "matched": m} for c, m in kept],
            "removed": removed,
            "replace_count": count,
            "mode": mode,
        })


def _mask(key):
    """API Key 掩码回传。"""
    if not key:
        return ""
    return "••••••••"
