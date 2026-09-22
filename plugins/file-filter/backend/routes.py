"""文件过滤器 —— JZToolsHub 后端插件路由。

功能：对上传表格（xlsx/xls/csv，≤20MB）做字段过滤（脱敏）与文本后处理（合规检查）：
- 预处理：**删除背景图片**（识别文档里嵌入的工作表背景图片并摘除，见 bg_image.py）；
- 上传预览：识别文档里的**列名**并给出硬过滤预判（哪些列会被删）与后处理预判
  （哪些列名会被改、预计替换多少处），用户按列开关、决定是否启用后处理，再提交过滤；
- 硬过滤：按固定字段名单保留列（规整后精确匹配），其余删除；
- 大模型过滤：提取全部表头交大模型判断与保留名单的语义关联（「时间」↔「开始时间」），
  匹配保留、其余删除；名单内同名字段直接保留（用户点名要的列不交给模型判断）；
- 后处理：按管理员配置的文本/正则规则对表头与单元格做替换（如「开始时间」-「开始」→「时间」），
  用户可在预览里**整体关闭**（默认开启）。

接口前缀：/api/file-filter（B-2）。
程序化调用（供其他插件）：POST /api/file-filter/apply —— JSON in / JSON out，同步，不落盘。

并发设计：大模型过滤走后台线程池 + task_id 轮询（8.5 异步任务模式）；
硬过滤同步完成（毫秒级），也统一走任务表返回 task_id，前端只实现一种轮询。
上传预览（/preview）**同步**返回：只读表 + 逐列预演，毫秒~秒级，故不必占用轮询通道
（与轨迹速写 /upload 同口径）；它把上传件暂存下来并返回 staged_id，/filter 可复用它，
用户在预览里反复调整字段时不必重复上传文件。
"""

# 会话工具经主体模块 jz_api 取用（依赖倒置：插件不再 import admin 插件的内部模块，
# 插件之间零依赖；admin 未加载时自动降级为「未登录 / 空操作」）。
# 见 docs/design/主体与插件解耦-设计文档.md §5.1 FC-3。
import jz_api
import jz_deps

_get_session_user = jz_api.get_session_user
_set_operation = jz_api.set_operation

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import jsonify, request, send_file

from . import bg_image, core, llm_client



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
# 上传暂存表 {staged_id: {...}}：/preview 落盘的上传件，供 /filter 复用（用户调整字段后
# 不必重新上传）。与 TASKS 同锁同 TTL、同归属校验（非创建者且非超管一律 404）。
STAGES = {}
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
        for table in (TASKS, STAGES):
            expired = [tid for tid, t in table.items()
                       if t.get("created_at", 0) < now - TASK_TTL_SECONDS]
            for tid in expired:
                table.pop(tid, None)


def create_task(user):
    tid = uuid.uuid4().hex[:12]
    set_task(tid, status="pending", created_at=time.time(),
             created_by=(user or {}).get("username") or "")
    return tid


def save_stage(blob, ext, original_name, user, sanitize):
    """落盘上传暂存件并登记（文件名用服务端 ID，不沿用原始文件名 —— SEC-3）。"""
    os.makedirs(TASK_DIR, exist_ok=True)
    sid = uuid.uuid4().hex[:12]
    path = os.path.join(TASK_DIR, f"{sid}_upload.{ext}")
    with open(path, "wb") as fp:
        fp.write(blob)
    stage = {"staged_id": sid, "path": path, "ext": ext, "size": len(blob),
             "original_name": original_name, "sanitize": sanitize,
             "created_at": time.time(),
             "created_by": (user or {}).get("username") or ""}
    with TASKS_LOCK:
        STAGES[sid] = stage
    return stage


def get_stage(staged_id):
    with TASKS_LOCK:
        return STAGES.get(staged_id)


def drop_stage(staged_id):
    """丢弃暂存（预览失败时立即回收，不必等 TTL）。"""
    with TASKS_LOCK:
        stage = STAGES.pop(staged_id, None)
    if not stage:
        return
    try:
        os.remove(stage["path"])
    except Exception:
        pass


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

def run_filter(headers, rows, mode, keep_columns, post_rules, llm_cfg, exclude=None):
    """执行过滤 + 后处理，返回 (headers2, rows2, kept, removed, replace_count, llm_used)。

    mode: hard / llm；llm 模式失败抛 llm_client.LLMError。
    exclude: 用户在上传预览里**手动关闭**的列名——两种模式下一律删除（用户决定优先于
        模式自身的判定：大模型认为该保留、但用户点了删除，就删除）。
    """
    excluded = {core.clean_text(x).casefold() for x in (exclude or []) if core.clean_text(x)}
    kept, removed = [], []
    llm_used = False
    if mode == "llm":
        mappings = llm_client.match_columns(
            headers, keep_columns,
            llm_cfg.get("base_url"), llm_cfg.get("api_key"), llm_cfg.get("model"),
        )
        llm_used = True
        keep_idx = []
        keep_fold = {core.clean_text(k).casefold(): core.clean_text(k)
                     for k in (keep_columns or []) if core.clean_text(k)}
        for i, h in enumerate(headers):
            hf = core.clean_text(h).casefold()
            if not hf or hf in excluded:
                removed.append(h)
                continue
            # 名单内同名字段直接保留：那是用户点名要的列，不把判断权交给模型
            # （模型偶发返回空值时会让用户"明明开着却被删"，与硬过滤口径也不一致）；
            # 其余交给大模型做语义匹配（「时间」↔「开始时间」）。
            matched = mappings.get(h) or keep_fold.get(hf) or ""
            if matched:
                keep_idx.append(i)
                kept.append((h, matched))
            else:
                removed.append(h)
        headers2 = [headers[i] for i in keep_idx]
        rows2 = [[(r[i] if i < len(r) else None) for i in keep_idx] for r in rows]
    else:
        keep_eff = [k for k in (keep_columns or [])
                    if core.clean_text(k) and core.clean_text(k).casefold() not in excluded]
        headers2, rows2, kept, removed = core.filter_columns(headers, rows, keep_eff)
    headers2, rows2, count = core.post_process(headers2, rows2, post_rules)
    return headers2, rows2, kept, removed, count, llm_used


def build_preview(headers, rows, keep_columns, post_rules):
    """上传预览：识别到的列名 + 硬过滤预判 + 后处理预判。

    返回 ``{"columns": [...], "summary": {...}}``，每列：

    - ``name`` 列名（规整后；空表头为空串）；``index`` 原列序号（0 起）；
    - ``keep`` / ``matched``：**硬过滤（字段名精确匹配）预判**——该列是否会被保留、
      匹配到名单里的哪个字段。大模型过滤的语义匹配发生在提交之后，故这里是预判；
    - ``post_name`` / ``replace_count``：按当前后处理规则预演的表头新名与该列替换处数
      （仅字符串单元格计数，与 :func:`core.post_process` 同口径）；
    - ``locked``：空表头列，无法被"按名字保留"，前端不给切换；
    - ``dup``：同名重复列（同名必然同判，前端点击时一起切换，避免"看着删了其实还在"）。
    """
    keep_fold = {}
    for k in (keep_columns or []):
        kc = core.clean_text(k)
        if kc:
            keep_fold.setdefault(kc.casefold(), kc)
    dup_counter = {}
    for h in headers:
        hf = core.clean_text(h).casefold()
        dup_counter[hf] = dup_counter.get(hf, 0) + 1
    post = core.post_process_columns(headers, rows, post_rules)

    columns, n_keep, n_drop, n_replace = [], 0, 0, 0
    for i, h in enumerate(headers):
        name = core.clean_text(h)
        matched = keep_fold.get(name.casefold(), "")
        keep = bool(matched)
        new_header, count = post[i]
        if keep:
            n_keep += 1
            n_replace += count          # 只有保留的列会进后处理，口径与运行结果一致
        else:
            n_drop += 1
        columns.append({
            "index": i,
            "name": name,
            "keep": keep,
            "matched": matched,
            "post_name": new_header,
            "replace_count": count,
            "locked": not name,
            "dup": dup_counter.get(name.casefold(), 0) > 1,
        })
    return {"columns": columns,
            "summary": {"total": len(headers), "keep": n_keep, "drop": n_drop,
                        "replace": n_replace}}


def _resolve_params(mode, columns_raw):
    """规整模式与保留名单：columns 参数缺省（None/空）时用管理员配置。

    注意「传了空数组」与「没传」在这里都走配置回退（/apply 的既有契约：columns 缺省用
    管理员配置）；/filter 另有一道显式校验——用户把字段全关时直接报错，见 _form_list。
    """
    cfg = load_config()
    if columns_raw:
        keep = columns_raw
    else:
        keep = cfg["keep_columns"]
    if mode not in ("hard", "llm"):
        mode = "hard"
    return cfg, mode, keep


def _form_list(raw):
    """解析表单/请求体里的 JSON 数组字段，返回 (列表, 是否显式提供)。

    区分「没传」（用管理员配置）与「传了空数组」（用户把字段全关了）：混为一谈会让
    "我明明全删了"静默变成"怎么全保留了"。
    """
    if raw is None or raw == "":
        return None, False
    try:
        parsed = json.loads(raw)
    except Exception:
        return None, False
    if not isinstance(parsed, list):
        return None, False
    return [str(v) for v in parsed], True


def _flag(raw, default):
    """解析布尔型表单字段：缺省取 default；"0"/"false"/"no"/"off" 为假。"""
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _run_filter_task(task_id, in_path, in_ext, out_path, out_ext, mode, keep, post_rules,
                     llm_cfg, exclude=None):
    """后台线程执行：读表 → 过滤 → 后处理 → 写出 → 任务置 done。"""
    try:
        set_task(task_id, status="running")
        headers, rows = core.read_table(in_path, f"input.{in_ext}")
        headers2, rows2, kept, removed, count, llm_used = run_filter(
            headers, rows, mode, keep, post_rules, llm_cfg, exclude)
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
    # 依赖可用性按请求实时刷新：服务运行中装「依赖组件包」后无需重启即可生效
    # （标记在模块导入时算好，不刷新会让插件页面一直显示"未安装"——jz_deps.refresh_flags）
    jz_deps.install_refresher(app, globals(), core)
    # ★ 路由函数命名纪律：一律带插件前缀 ff_*。
    # Flask 以 view_func.__name__ 作为 endpoint，若本插件定义 def status() 而其他插件
    # 也定义同名函数，会在启动阶段抛 AssertionError 导致注册失败（历史缺陷：本插件
    # 曾是全项目唯一不带前缀的路由集合）。详见 docs/archive/P0问题修复方案.md FIX-4。
    @app.get(f"{API_PREFIX}/status")
    def ff_status():
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
    def ff_config_get():
        cfg = load_config()
        cfg = json.loads(json.dumps(cfg))  # 深拷贝
        llm_key = cfg["llm"]["api_key"]
        cfg["llm"]["api_key"] = _mask(llm_key)
        cfg["llm_configured"] = bool(llm_key)
        cfg["can_manage"] = _can_manage(_viewer())
        return jsonify(cfg)

    @app.post(f"{API_PREFIX}/config")
    def ff_config_post():
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
    def ff_config_test():
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

    @app.post(f"{API_PREFIX}/preview")
    def ff_preview():
        """上传表格 → 识别列名 → 返回硬过滤与后处理预判（**同步**）。

        只读表 + 逐列预演，毫秒~秒级，故不进任务队列（前端不需要第二种轮询）。
        返回的 ``staged_id`` 可交给 /filter 复用：用户在预览里反复调整字段时
        **不必重复上传文件**（上传件已落盘暂存，TTL 30 分钟）。
        """
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

        cfg = load_config()
        cleanup_tasks()
        _clean_task_files()
        # 预处理：删除背景图片（未发现背景图片时原字节返回，不做任何改写）。
        # 必须在落盘暂存之前做：预览与随后的过滤读到的必须是同一份"干净"字节。
        blob, sanitize = bg_image.strip_background_images(blob, ext)
        stage = save_stage(blob, ext, orig, user, sanitize)
        try:
            headers, rows = core.read_table(stage["path"], f"upload.{ext}")
        except core.TableError as e:
            drop_stage(stage["staged_id"])   # 预览失败即回收，不占 TTL
            return jsonify({"error": str(e)}), 400

        preview = build_preview(headers, rows, cfg["keep_columns"], cfg["post_rules"])
        _set_operation("预览表格字段与过滤预判")
        return jsonify({
            "ok": True,
            "staged_id": stage["staged_id"],
            "filename": orig,
            "size": len(blob),
            "ext": ext,
            "rows": len(rows),
            "columns": preview["columns"],
            "summary": preview["summary"],
            "post_rules": cfg["post_rules"],
            "keep_columns": cfg["keep_columns"],
            "sanitize": sanitize,
            "llm_configured": bool(cfg["llm"].get("api_key")),
        })

    @app.post(f"{API_PREFIX}/filter")
    def ff_filter():
        user = _viewer()
        f = request.files.get("file")
        staged_id = (request.form.get("staged_id") or "").strip()
        stage = None
        if f is not None and f.filename:
            orig = f.filename or ""
            ext = orig.lower().rsplit(".", 1)[-1] if "." in orig else ""
            if ext not in core.ALLOWED_EXTS:
                return jsonify({"error": "仅支持 xlsx / xls / csv 文件"}), 400
            blob = f.read()
            if len(blob) > MAX_UPLOAD_BYTES:
                return jsonify({"error": "文件超过 20MB 上限"}), 413
            if not blob:
                return jsonify({"error": "文件为空"}), 400
        else:
            # 复用 /preview 暂存的上传件（用户在预览里调整字段后不必重新上传）
            if not TASK_ID_RE.match(staged_id or ""):
                return jsonify({"error": "未选择文件"}), 400
            stage = get_stage(staged_id)
            if not stage or not _task_owned_by(stage, user):
                return jsonify({"error": "上传内容不存在或已过期，请重新选择文件",
                                "code": "staged_expired"}), 404
            orig, ext = stage["original_name"], stage["ext"]

        mode = (request.form.get("mode") or "hard").strip()
        columns_raw, columns_given = _form_list(request.form.get("columns"))
        exclude_raw, _ = _form_list(request.form.get("exclude"))
        if columns_given and not columns_raw:
            # 显式传空数组 = 用户把字段全关了：报错而不是静默回退管理员名单
            # （静默回退会让"我明明全删了"变成"怎么全保留了"）
            return jsonify({"error": "未选择任何保留字段：请在上方「识别与调整」中至少保留一列"}), 400
        cfg, mode, keep = _resolve_params(mode, columns_raw)
        if not keep:
            return jsonify({"error": "保留字段名单为空：请勾选保留字段或联系管理员配置"}), 400
        if mode == "llm" and not cfg["llm"].get("api_key"):
            return jsonify({"error": "大模型未配置：请管理员在「管理配置」中填写 API 地址、API Key 与模型名称"}), 400

        task_id = create_task(user)
        cleanup_tasks()
        _clean_task_files()
        if stage is None:
            # ① 预处理：删除背景图片（未发现背景图片时原字节返回，不做任何改写）
            blob, sanitize = bg_image.strip_background_images(blob, ext)
            # SEC-3：落盘文件名用 task_id 重命名，不沿用用户原始文件名
            in_path = os.path.join(TASK_DIR, f"{task_id}_input.{ext}")
            with open(in_path, "wb") as fp:
                fp.write(blob)
        else:
            in_path = stage["path"]
            sanitize = stage.get("sanitize") or {}
        out_ext = "csv" if ext == "csv" else "xlsx"  # .xls 只读，输出转 .xlsx
        out_path = os.path.join(TASK_DIR, f"{task_id}_output.{out_ext}")
        # 后处理默认开启（用户的显式关闭才不跑规则）
        post_enabled = _flag(request.form.get("post_process"), True)
        post_rules = cfg["post_rules"] if post_enabled else []
        set_task(task_id, output_ext=out_ext, original_name=orig, sanitize=sanitize,
                 post_enabled=post_enabled)
        _executor.submit(_run_filter_task, task_id, in_path, ext, out_path, out_ext,
                         mode, keep, post_rules, cfg["llm"], exclude_raw)
        _set_operation("提交表格过滤任务")
        return jsonify({"task_id": task_id, "mode": mode, "output_ext": out_ext,
                        "sanitize": sanitize, "post_enabled": post_enabled})

    @app.get(f"{API_PREFIX}/result/<task_id>")
    def ff_result(task_id):
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
                "post_enabled": task.get("post_enabled") is not False,
                "sanitize": task.get("sanitize") or {},
                "download": task.get("download"),
                "filename": task.get("original_name") or "",
                "output_ext": task.get("output_ext") or "",
            })
        elif task.get("status") == "error":
            payload["detail"] = task.get("detail") or "过滤失败"
        return jsonify(payload)

    @app.get(f"{API_PREFIX}/download/<task_id>")
    def ff_download(task_id):
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
    def ff_apply():
        """程序化调用接口（供其他插件调用）：JSON in / JSON out，同步，不落盘。

        请求体：{
          "rows": [[表头...], [数据行]...],   # 必填，首行为表头
          "mode": "hard" | "llm",            # 缺省 hard
          "columns": ["保留字段", ...],       # 缺省用管理员配置
          "exclude": ["强制删除的字段", ...],  # 可选：覆盖模式自身的判定
          "post_rules": [{"pattern","replacement","is_regex","enabled"}],  # 缺省用管理员配置
          "post_process": false              # 可选：显式关闭后处理（缺省 true）
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
        if data.get("post_process") is False:
            post_rules = []
        exclude = data.get("exclude") if isinstance(data.get("exclude"), list) else None
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
                headers, rows, mode, keep, post_rules, cfg["llm"], exclude)
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
